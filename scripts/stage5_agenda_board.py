"""GOV-605 agenda-board projection over reviewed Alpine data (GOV-601 §2 contract).

The additive board-projection read surface GOV-599's shipped agenda-Kanban shell
(PR #27) consumes so it renders **real reviewed Alpine data** instead of fixtures
(Isaac June-6 directive: "see real Alpine progress in the browser"). It closes
exactly the gap the GOV-601 backend contract named — projection/plumbing, **no new
tables** — by keying cards on ``agenda_item + meeting + thread`` (not just
``statementId``) and composing the two named PROJECTION GAPs onto each card:

* ``videoRef`` — ``transcripts.video_url`` + the segment ``timestamp_seconds`` (a
  deep-link that ``read_api`` deliberately keeps out of the web-safe record because
  ``segment_id`` is web-UNSAFE; it is composed here from a raw join and emitted as a
  public YouTube URL + integer timestamp only); and
* typed ``lineage`` — the typed agenda lifecycle edges
  (``agenda_item_supersedes`` / ``_amends`` / ``_revisits``) plus the
  ``updates_statement_id`` correction lineage — never an untyped "related".

Boundary rules (GOV-601 §0, restated as invariants):

* **Reviewer-internal gating is preserved and single-sourced.** Every statement a
  card is built from is a row :func:`read_api.reviewer_internal_records` returned —
  the one eight-clause fail-closed gate — so the board can never serve an uncleared
  row and the gate is never re-implemented here. The only raw touch is a
  ``segment_id`` column fetch for an *already-cleared* statement id (``segment_id``
  is web-UNSAFE, so it is absent from the web-safe record but needed to compose
  ``videoRef``); it is a lookup, not a gate.
* **Additive only (I4).** This module is a leaf consumer: it never mutates or
  re-derives ``read_api`` / ``publication`` / ``stage5_watchdog_signals`` /
  ``stage5_frontend_surface`` — it consumes their already-web-safe output and the
  frozen display vocabulary (lane order, status/gap badges, anchor disclosures),
  adding labelling and grouping, never a new claim (Isaac concept-map directive).
  The frozen ``read_api`` module is left byte-for-byte unchanged (the "extend not
  fork the SSOT" invariant the zero-diff guards enforce).
* **Fail-closed labels.** A card is never labelled ``Verified`` unless *every*
  reviewed statement under it composes to ``verified``; mixed confidence floors to
  the conservative label; latent fields (``decisions`` / ``categoryAnchor``) are
  emitted **empty + disclosed**, never fabricated (GOV-601 §3 — landing real
  vote/decision rows or a topic layer is Isaac-scoped, explicitly OUT of scope).
* **Web-safe boundary.** Every leaf value is a web-safe projection or a derived
  public locator; the whole assembled board is transport-swept by
  :func:`read_api.assert_no_raw_paths` before return, so a leaked path fails LOUDLY.
* **Empty-state honesty (GOV-601 §5).** No reviewed Alpine agenda records yet ->
  a well-formed empty board (all six lanes shown, empty) with disclosure, never an
  error.

Pure function of the registry: same DB -> byte-identical board. No mutation, no AI,
no network. ``access: reviewer_internal`` / ``scope: alpine``; public launch stays
Isaac-gated (GOV-420 — untouched).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402  (consumed read-only: single gate + transport sweep)
import stage3_card_feed as card_feed  # noqa: E402  (status vocabulary, by reference)
import stage5_frontend_surface as surface  # noqa: E402  (frozen display vocabulary)
import stage5_watchdog_signals as watchdog  # noqa: E402  (per-statement lane/gaps)

SCOPE = "alpine"
ACCESS = "reviewer_internal"  # never "public" — GOV-420 gate untouched


class AgendaBoardError(AssertionError):
    """Raised when a projected board node violates a GOV-605 contract invariant."""


# ---------------------------------------------------------------------------
# Fail-closed aggregation vocab (frozen — consumed from the modules by reference).
# ---------------------------------------------------------------------------

# Card status is aggregated most-conservative-wins: a card wears "Verified" ONLY when
# every reviewed statement under it composes to `verified`, because any lesser status
# sits earlier in this order and is therefore chosen. Fail-closed default: unverified.
_STATUS_CONSERVATISM: tuple[str, ...] = (
    card_feed.STATUS_SOURCE_MISSING,
    card_feed.STATUS_UNVERIFIED,
    card_feed.STATUS_AI_PRESENTED,
    card_feed.STATUS_CORRECTED,
    card_feed.STATUS_VERIFIED,
)

# Card lane is aggregated most-specific-signal-wins, mirroring watchdog.derive_lane's
# precedence: a correction anywhere under the card dominates, an upcoming floor loses.
_LANE_PRECEDENCE: tuple[str, ...] = (
    watchdog.LANE_CORRECTION,
    watchdog.LANE_FOLLOW_UP,
    watchdog.LANE_DECIDED,
    watchdog.LANE_PENDING_DECISION,
    watchdog.LANE_ACTIVE,
    watchdog.LANE_UPCOMING,
)

# Typed lineage relations (never an untyped "related" — BEH-AGENDA-2 / GOV-601 §2).
_LIFECYCLE_EDGE_TYPES: tuple[str, ...] = (
    "agenda_item_supersedes",
    "agenda_item_amends",
    "agenda_item_revisits",
)
_LINEAGE_UPDATES_STATEMENT = "updates_statement"

# Board-level gap codes (never hidden). Rendered through the frozen surface badge map
# extended with these two board-specific codes; an unknown code passes through verbatim.
GAP_THREAD_UNLINKED = "agenda_thread_unlinked"
GAP_VIDEO_UNAVAILABLE = "video_ref_unavailable"
_BOARD_GAP_BADGES: dict[str, str] = {
    GAP_THREAD_UNLINKED: (
        "Agenda thread not yet linked — card anchored to its agenda item only"
    ),
    GAP_VIDEO_UNAVAILABLE: (
        "Video deep-link unavailable — no timestamped transcript segment for this item"
    ),
}
_ALL_GAP_BADGES: dict[str, str] = {**surface.GAP_BADGES, **_BOARD_GAP_BADGES}

# Category anchor: `topic_id` is structurally latent (VSR GOV-521); the agenda thread
# is the honest anchor. Disclosure text reused from the frozen surface vocabulary.
_CATEGORY_ANCHOR = {
    "kind": surface.ANCHOR_AGENDA_THREAD,
    "disclosure": surface.ANCHOR_DISCLOSURES[surface.ANCHOR_AGENDA_THREAD],
}

_DISCLOSURES = {
    "decisions": (
        "No vote/decision rows have landed yet; decisions:[] is disclosed-empty, "
        "never fabricated. Landing real vote/decision rows is Isaac-scoped (GOV-601 §3)."
    ),
    "categories": (
        "No explicit topic edge exists; categoryAnchor is the agenda thread the card "
        "sits in (GOV-601 §3 — topic layer is Isaac-scoped, out of scope here)."
    ),
    "scope": "Reviewed Alpine records only, reviewer-internal; public launch stays Isaac-gated (GOV-420).",
}


# ---------------------------------------------------------------------------
# Aggregation helpers (deterministic, fail-closed).
# ---------------------------------------------------------------------------


def _rank(order: tuple[str, ...], value: str | None) -> int:
    """Index of ``value`` in ``order``; unknown/missing -> 0 (most conservative)."""
    try:
        return order.index(value)  # type: ignore[arg-type]
    except ValueError:
        return 0


def _card_status(statuses: list[str]) -> str:
    """Most-conservative composed status across a card's statements (fail-closed)."""
    present = [s for s in statuses if s]
    if not present:
        return card_feed.STATUS_UNVERIFIED
    return min(present, key=lambda s: _rank(_STATUS_CONSERVATISM, s))


def _card_lane(lanes: list[str]) -> str:
    """Most-specific lane across a card's statements; floor to ``upcoming``."""
    present = [lane for lane in lanes if lane]
    if not present:
        return watchdog.LANE_UPCOMING
    return min(present, key=lambda lane: _rank(_LANE_PRECEDENCE, lane))


def _card_confidence(labels: list[str | None]) -> str | None:
    """Card confidence badge: the shared label, else the conservative floor when mixed."""
    present = {label for label in labels if label}
    if not present:
        return None
    if len(present) == 1:
        return next(iter(present))
    # Mixed confidence must not over-claim — floor to the conservative label.
    return read_api._CONSERVATIVE_CONFIDENCE_LABEL


def _render_gap_badges(codes: set[str]) -> list[str]:
    """Render fail-closed gap codes as visible badges (sorted, never hidden)."""
    return [_ALL_GAP_BADGES.get(code, code) for code in sorted(codes)]


# ---------------------------------------------------------------------------
# Per-card projection helpers (web-safe by construction; raw joins for the two gaps).
# ---------------------------------------------------------------------------


def _agenda_item_row(conn: sqlite3.Connection, agenda_item_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT agenda_item_id, meeting_id, item_order, title "
        "FROM agenda_items WHERE agenda_item_id = ?",
        (agenda_item_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _meeting_fields(conn: sqlite3.Connection, meeting_id: Any) -> dict[str, Any]:
    """Web-safe meeting identity for the card (source_url only when a public web URL)."""
    fields: dict[str, Any] = {"meetingId": meeting_id}
    if meeting_id is None:
        return fields
    row = conn.execute(
        "SELECT meeting_date, body, title, source_url FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return fields
    fields["meetingDate"] = row["meeting_date"]
    fields["meetingBody"] = row["body"]
    fields["meetingTitle"] = row["title"]
    source_url = row["source_url"]
    if isinstance(source_url, str) and read_api._is_web_url(source_url):
        fields["meetingSourceUrl"] = source_url
    return fields


def _thread_for_item(conn: sqlite3.Connection, agenda_item_id: str) -> dict[str, Any] | None:
    """The agenda thread this item is in (via the typed ``agenda_item_in_thread`` edge)."""
    edge = conn.execute(
        "SELECT to_node_id FROM concept_edges "
        "WHERE edge_type = 'agenda_item_in_thread' AND from_node_id = ? "
        "ORDER BY to_node_id LIMIT 1",
        (agenda_item_id,),
    ).fetchone()
    if edge is None:
        return None
    thread_id = edge["to_node_id"]
    row = conn.execute(
        "SELECT agenda_thread_id, status, canonical_human_label "
        "FROM agenda_threads WHERE agenda_thread_id = ?",
        (thread_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "agendaThreadId": row["agenda_thread_id"],
        "threadStatus": row["status"],
        "threadLabel": row["canonical_human_label"],
    }


def _segment_ids_for(
    conn: sqlite3.Connection, statement_ids: list[str]
) -> dict[str, str | None]:
    """Map cleared ``statement_id`` -> its raw ``segment_id`` (for videoRef composition).

    A column lookup for statement ids the reviewer serve ALREADY cleared — it re-derives
    no gate and touches no other row. ``segment_id`` is web-UNSAFE (never crosses the
    boundary); only the derived, public ``videoRef`` does.
    """
    segments: dict[str, str | None] = {}
    for statement_id in statement_ids:
        row = conn.execute(
            "SELECT segment_id FROM statements WHERE statement_id = ?", (statement_id,)
        ).fetchone()
        segments[statement_id] = row["segment_id"] if row is not None else None
    return segments


def _video_ref(conn: sqlite3.Connection, segment_id: str | None) -> dict[str, Any] | None:
    """Compose the ``videoRef`` deep-link from a segment id (PROJECTION GAP #1).

    Joins ``statements.segment_id`` -> ``transcript_segments`` -> ``transcripts`` and
    emits ONLY the public YouTube ``video_url`` + integer ``timestamp_seconds``. The
    ``segment_id`` itself is web-UNSAFE and never crosses; a non-web ``video_url`` (a
    malformed/local value) fails closed to ``None`` (no partial ref).
    """
    if not segment_id:
        return None
    row = conn.execute(
        "SELECT seg.timestamp_seconds AS ts, t.video_url AS url "
        "FROM transcript_segments seg "
        "JOIN transcripts t ON t.id = seg.transcript_id "
        "WHERE seg.segment_id = ?",
        (segment_id,),
    ).fetchone()
    if row is None:
        return None
    url = row["url"]
    ts = row["ts"]
    if not (isinstance(url, str) and read_api._is_web_url(url)) or ts is None:
        return None
    return {"url": url, "timestampSeconds": ts}


def _lifecycle_lineage(conn: sqlite3.Connection, agenda_item_id: str) -> list[dict[str, Any]]:
    """Typed agenda lifecycle edges out of this item (PROJECTION GAP #2, part a)."""
    lineage: list[dict[str, Any]] = []
    placeholders = ",".join("?" for _ in _LIFECYCLE_EDGE_TYPES)
    for row in conn.execute(
        f"SELECT edge_type, to_node_id FROM concept_edges "
        f"WHERE from_node_id = ? AND edge_type IN ({placeholders}) "
        f"ORDER BY edge_type, to_node_id",
        (agenda_item_id, *_LIFECYCLE_EDGE_TYPES),
    ):
        lineage.append({"relation": row["edge_type"], "ref": row["to_node_id"]})
    return lineage


def _source_refs(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a served statement's web-safe evidence drawer onto the §2 sourceRef shape."""
    refs: list[dict[str, Any]] = []
    for link in record.get("evidence", []):
        ref: dict[str, Any] = {"sourceId": link.get("to_source_id")}
        original = link.get("original_url") or link.get("final_url")
        if original:
            ref["originalUrl"] = original
        if link.get("archive_url"):
            ref["archiveUrl"] = link["archive_url"]
        locator: dict[str, Any] = {}
        for src_key, out_key in (
            ("timestamp_human", "timestampHuman"),
            ("timestamp_seconds", "timestampSeconds"),
            ("page", "page"),
            ("section", "section"),
            ("paragraph", "paragraph"),
        ):
            value = link.get(src_key)
            if value not in (None, ""):
                locator[out_key] = value
        if locator:
            ref["locator"] = locator
        refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# Board assembly.
# ---------------------------------------------------------------------------


def _empty_lanes() -> list[dict[str, Any]]:
    """The six frozen lane columns, all empty (a board never hides an empty lane)."""
    return [
        {
            "lane": lane,
            "laneLabel": surface.lane_label(lane),
            "cardCount": 0,
            "cards": [],
        }
        for lane in surface.LANE_ORDER
    ]


def build_cards(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], int]:
    """Project reviewer-cleared statements into agenda-item cards.

    Returns ``(cards, unanchored_statement_count)``. Cards are keyed on
    ``agenda_item_id`` (the natural card key: agenda item + meeting + thread). A
    cleared statement with no ``agenda_item_id`` cannot be an agenda card — it is
    counted into ``unanchored_statement_count`` and disclosed at board level rather
    than being silently dropped.
    """
    # THE single fail-closed gate — the already-web-safe reviewer-cleared serve
    # (evidence drawer, labels, agenda_item_id, updates ref). Never re-implemented here.
    safe_by_id = {r["statement_id"]: r for r in read_api.reviewer_internal_records(conn)}
    # segment_id is web-UNSAFE (absent from the web-safe record) but needed to compose
    # videoRef — fetch it ONLY for these already-cleared ids (a lookup, not a gate).
    segment_by_id = _segment_ids_for(conn, list(safe_by_id))
    # Per-statement lane / status / gaps from the watchdog view (statements with sources).
    view_by_id = {e["statementId"]: e for e in watchdog.build_watchdog_view(conn)}

    # Group cleared statements under their agenda item.
    groups: dict[str, list[str]] = {}
    unanchored = 0
    for statement_id in sorted(safe_by_id):
        agenda_item_id = safe_by_id[statement_id].get("agenda_item_id")
        if not agenda_item_id:
            unanchored += 1
            continue
        groups.setdefault(agenda_item_id, []).append(statement_id)

    cards: list[dict[str, Any]] = []
    for agenda_item_id in sorted(groups):
        statement_ids = sorted(groups[agenda_item_id])
        item = _agenda_item_row(conn, agenda_item_id)
        # An agenda_item row must back the card; if the FK is dangling, disclose a gap
        # rather than fabricate identity.
        meeting_id = item["meeting_id"] if item else None

        card: dict[str, Any] = {"cardId": agenda_item_id, "agendaItemId": agenda_item_id}
        card.update(_meeting_fields(conn, meeting_id))
        if item is not None:
            card["itemOrder"] = item["item_order"]
            card["agendaItemTitle"] = item["title"]

        gap_codes: set[str] = set()

        thread = _thread_for_item(conn, agenda_item_id)
        if thread is not None:
            card.update(thread)
        else:
            gap_codes.add(GAP_THREAD_UNLINKED)

        # Aggregate lane / status / confidence / gaps over the card's statements.
        lanes: list[str] = []
        statuses: list[str] = []
        confidences: list[str | None] = []
        for statement_id in statement_ids:
            view = view_by_id.get(statement_id)
            if view is not None:
                lanes.append(view.get("lane"))
                statuses.append(view.get("status"))
                confidences.append(view.get("sourceConfidence"))
                gap_codes.update(view.get("gaps", []))
            else:
                # Cleared but not in the watchdog view (no evidence source) — compose
                # its status directly so it is never silently given a reassuring badge.
                safe = safe_by_id.get(statement_id)
                if safe is not None:
                    statuses.append(card_feed._compose_record_status(safe))
                    confidences.append(safe.get("confidence_label"))

        lane = _card_lane(lanes)
        card["lane"] = lane
        card["laneLabel"] = surface.lane_label(lane)
        status = _card_status(statuses)
        card["statusBadge"] = surface.status_badge(status)
        card["confidenceBadge"] = _card_confidence(confidences)

        # videoRef: earliest resolvable segment among the card's statements (start of
        # the item's discussion); deterministic tiebreak by statement id.
        video_ref: dict[str, Any] | None = None
        for statement_id in statement_ids:
            candidate = _video_ref(conn, segment_by_id.get(statement_id))
            if candidate is None:
                continue
            if video_ref is None or candidate["timestampSeconds"] < video_ref["timestampSeconds"]:
                video_ref = candidate
        if video_ref is not None:
            card["videoRef"] = video_ref
        else:
            gap_codes.add(GAP_VIDEO_UNAVAILABLE)

        # Typed lineage: agenda lifecycle edges + updates_statement correction refs.
        lineage = _lifecycle_lineage(conn, agenda_item_id)
        for statement_id in statement_ids:
            updates = safe_by_id[statement_id].get("updates_statement_id")
            if updates:
                lineage.append({"relation": _LINEAGE_UPDATES_STATEMENT, "ref": updates})
        lineage.sort(key=lambda e: (e["relation"], e["ref"]))
        card["lineage"] = lineage

        # sourceRefs: union of the card's statements' web-safe evidence drawers.
        source_refs: list[dict[str, Any]] = []
        seen_refs: set[str] = set()
        for statement_id in statement_ids:
            safe = safe_by_id.get(statement_id)
            if safe is None:
                continue
            for ref in _source_refs(safe):
                key = json.dumps(ref, sort_keys=True)
                if key not in seen_refs:
                    seen_refs.add(key)
                    source_refs.append(ref)
        source_refs.sort(key=lambda r: json.dumps(r, sort_keys=True))
        card["sourceRefs"] = source_refs

        # Latent-by-data fields — emitted empty + disclosed, never fabricated (§3).
        card["decisions"] = []
        card["categoryAnchor"] = dict(_CATEGORY_ANCHOR)

        card["gapBadges"] = _render_gap_badges(gap_codes)
        card["statementIds"] = statement_ids
        card["recordCount"] = len(statement_ids)
        cards.append(card)
    return cards, unanchored


def agenda_board(conn: sqlite3.Connection) -> dict[str, Any]:
    """The GOV-605 agenda-board projection over reviewed Alpine data (GOV-601 §2).

    Groups reviewer-cleared statements into agenda-item cards, composes ``videoRef``
    and typed ``lineage``, and lays the cards out across the six frozen lifecycle
    lanes (all shown, empties included — a Kanban never hides an empty lane). Latent
    ``decisions`` / categories are emitted empty + disclosed. If no reviewed Alpine
    agenda records exist, returns a well-formed empty board with disclosure (not an
    error). The whole body is transport-swept before return.
    """
    cards, unanchored = build_cards(conn)

    by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in surface.LANE_ORDER}
    for card in cards:
        # build_cards guarantees a frozen-vocab lane; group defensively so a surprise
        # lane surfaces under its own column rather than vanishing.
        by_lane.setdefault(card["lane"], []).append(card)

    lanes: list[dict[str, Any]] = []
    for lane in surface.LANE_ORDER:
        lane_cards = sorted(by_lane[lane], key=lambda c: c["agendaItemId"])
        lanes.append(
            {
                "lane": lane,
                "laneLabel": surface.lane_label(lane),
                "cardCount": len(lane_cards),
                "cards": lane_cards,
            }
        )

    disclosures = dict(_DISCLOSURES)
    disclosures["emptyState"] = not cards
    disclosures["unanchoredStatementCount"] = unanchored

    board: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,
        "generatedFrom": "read_api.reviewer_internal_records",
        "lanes": lanes,
        "cardCount": len(cards),
        "unanchoredStatementCount": unanchored,
        "disclosures": disclosures,
    }
    # Transport backstop (I1): any raw/vault/FS path that slipped a column fails LOUDLY.
    return read_api.assert_no_raw_paths(board)


# ---------------------------------------------------------------------------
# CLI — reviewer-internal inspection only (never a public server).
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GOV-605 agenda-board projection over reviewed Alpine data."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    conn = db.open_db(args.db)
    try:
        board = agenda_board(conn)
    finally:
        conn.close()
    print(json.dumps(board, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
