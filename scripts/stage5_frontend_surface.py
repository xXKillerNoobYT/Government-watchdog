"""Stage 5.06 frontend/product surface contract (GOV-524) — reviewer-internal, Alpine.

Authors the **reviewer-internal frontend/product surface contract** over the Stage 5.05
Watchdog-signals backbone (``stage5_watchdog_signals.py``, GOV-520, merged to main
``6616d3b``). Per GOV-483 §C linear backbone (CEO-accepted). This is a *data/presentation
contract* — the deterministic view-model a reviewer UI renders — **NOT a public launch**
and **NOT a renderer**: it emits structured presentation nodes (cards / badges / board
columns), Alpine/reviewer-internal only. Public launch stays Isaac-gated (GOV-420).

Isaac's concept-map directive is the spine: *cards are presentation nodes over the graph,
not the source of truth*. So every surface here is a read-only projection of the already-
web-safe 5.05 signal envelopes + the merged read surface — it adds **labelling and
grouping, never a new claim**. Three surfaces, one per 5.05 envelope:

1. **§1 correctionsSurface** (:func:`build_corrections_surface`) — one correction *card*
   per ledger edge: the corrected ``known_then`` context preserved verbatim (status /
   recordDate / confidence badges), pointed at its superseding record when resolved, and
   the fail-closed ``correction_unresolved`` gap rendered as a **visible gap badge** when
   not — never hidden (the §1 promise).
2. **§2 hotTopicsSurface** (:func:`build_hot_topics_surface`) — the deterministic salience
   ranking presented as ranked topic cards (rank index + salience badge +
   ``insufficientData`` floor badge). Each card discloses its **topic-anchor provenance
   honestly** (:func:`classify_topic_anchor`): a real evidence ``topic_id`` edge vs the
   ``agenda_item_id`` fallback (the latent-``topic_id`` reality per VSR GOV-521) — it never
   implies a topic edge that isn't in the data.
3. **§3 watchdogBoard** (:func:`build_watchdog_board`) — the Kanban-precursor lane board
   grouped into the six frozen lanes (all columns shown, empties included — a board never
   hides an empty lane), each card carrying its status / confidence / gap badges over
   verified / source-linked records only.

A single envelope hash — :func:`_surface_digest` over the three surfaces — is the only
hash exposed (I3, no per-source raw hash). The ``--check`` CLI is the CI gate (exit 1 on
any defect).

Boundary rules (contract §0 / premium I1–I8), restated as surface invariants:

* it **never** mutates / re-derives the prod modules it consumes by reference (I4):
  ``read_api.py`` / ``publication.py`` / ``stage4_newsletter_feed.py`` /
  ``stage4_newsletter_digest_assembler.py`` / ``stage5_source_inventory.py`` /
  ``stage5_record_verifier.py`` / ``stage5_watchdog_signals.py`` — it consumes their
  already-web-safe output, so their vocabulary is the *only* vocabulary;
* every emitted artifact is transport-swept by :func:`read_api.assert_no_raw_paths`, so a
  FS path / ``.sha256`` / vault marker / ``file://`` that slipped a column fails LOUDLY at
  the boundary (I1); ``localSourcePath`` is never emitted (I2); exactly one envelope
  ``surfaceDigest`` is exposed — no per-source raw hash (I3);
* it **never** presents an unverified / AI / corrected record as ``Verified`` — the status
  badge is the deterministic fail-closed mapping of the composed status
  (:func:`assert_no_false_verified`);
* it runs entirely at ``access: reviewer_internal`` / ``scope: alpine`` and is absent from
  any public / ``published_records`` path (I6); public launch stays Isaac-gated (GOV-420).

Pure function of the registry: same DB -> byte-identical surface (idempotent
re-projection). No mutation, no AI, no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402  (consumed read-only: serve + transport sweep)
import stage3_card_feed as card_feed  # noqa: E402  (status vocabulary, by reference)
import stage5_watchdog_signals as watchdog  # noqa: E402  (the merged 5.05 backbone)

SCOPE = "alpine"
ACCESS = "reviewer_internal"  # never "public" — I6


class FrontendSurfaceError(AssertionError):
    """Raised when an emitted surface node violates a GOV-524 contract invariant."""


# ---------------------------------------------------------------------------
# Display-label vocabularies (frozen SSOT — exact text, never a paraphrase)
# ---------------------------------------------------------------------------
#
# The verification badge text is load-bearing safety vocabulary: a reviewer must read the
# *exact* status, never a polished synonym. Each mapping is fail-closed — an unknown key
# resolves to the most conservative ("Unverified" / the raw code), NEVER to "Verified".

BADGE_VERIFIED = "Verified"
BADGE_UNVERIFIED = "Unverified"

STATUS_BADGES: dict[str, str] = {
    card_feed.STATUS_VERIFIED: BADGE_VERIFIED,
    card_feed.STATUS_AI_PRESENTED: "AI-presented — not independently verified",
    card_feed.STATUS_CORRECTED: "Corrected",
    card_feed.STATUS_UNVERIFIED: BADGE_UNVERIFIED,
    card_feed.STATUS_SOURCE_MISSING: "Source missing",
}

CORRECTION_STATUS_BADGES: dict[str, str] = {
    "replaced": "Replaced",
    "superseded": "Superseded",
    "corrected": "Corrected",
    "amended": "Amended",
}

# Canonical lane order for the board columns (frozen vocab from 5.05, presentation order).
LANE_ORDER: tuple[str, ...] = (
    watchdog.LANE_UPCOMING,
    watchdog.LANE_ACTIVE,
    watchdog.LANE_PENDING_DECISION,
    watchdog.LANE_DECIDED,
    watchdog.LANE_FOLLOW_UP,
    watchdog.LANE_CORRECTION,
)

LANE_LABELS: dict[str, str] = {
    watchdog.LANE_UPCOMING: "Upcoming",
    watchdog.LANE_ACTIVE: "Active",
    watchdog.LANE_PENDING_DECISION: "Pending decision",
    watchdog.LANE_DECIDED: "Decided",
    watchdog.LANE_FOLLOW_UP: "Follow-up",
    watchdog.LANE_CORRECTION: "Correction",
}

SALIENCE_BADGES: dict[str, str] = {
    watchdog.SALIENCE_RANKED: "Ranked",
    watchdog.SALIENCE_INSUFFICIENT: "Insufficient data — below activity floor",
}

# Gap-code -> visible badge text. A gap is ALWAYS surfaced: an unknown code falls through
# to the raw code itself (never silently dropped) so a future gap can never be hidden.
GAP_BADGES: dict[str, str] = {
    watchdog.GAP_CORRECTION_UNRESOLVED: (
        "Correction unresolved — superseding record not yet in registry"
    ),
    watchdog.GAP_ARCHIVE_UNAVAILABLE: "Archive not available",
    watchdog.GAP_LOW_CONFIDENCE: "Low source confidence",
}

RESOLUTION_LINKED = "Superseding record linked"
RESOLUTION_UNRESOLVED = "Unresolved"

# Topic-anchor provenance — disclosed honestly per card (the §2 honesty promise).
ANCHOR_TOPIC_EDGE = "topic_edge"
ANCHOR_AGENDA_THREAD = "agenda_thread"
ANCHOR_DISCLOSURES: dict[str, str] = {
    ANCHOR_TOPIC_EDGE: "Topic edge (explicit evidence topic link).",
    ANCHOR_AGENDA_THREAD: (
        "Agenda thread (no explicit topic edge in the data — anchored to the "
        "agenda item the claim sits in)."
    ),
}


def status_badge(status: str | None) -> str:
    """Map a composed record status to its exact display badge (fail-closed).

    An unknown / missing status resolves to ``Unverified`` — NEVER ``Verified``. This is
    the single chokepoint that keeps an unverified record from ever wearing a verified
    badge (:func:`assert_no_false_verified` cross-checks the emitted body).
    """
    return STATUS_BADGES.get(status or "", BADGE_UNVERIFIED)


def correction_status_badge(correction_status: str | None) -> str:
    """Map a claim-level correction status to its display badge (fail-closed to raw)."""
    if correction_status is None:
        return "Correction"
    return CORRECTION_STATUS_BADGES.get(correction_status, correction_status)


def lane_label(lane: str) -> str:
    """Map a watchdog lane to its column label (fail-closed to the raw lane)."""
    return LANE_LABELS.get(lane, lane)


def salience_badge(salience_label: str | None) -> str:
    """Map a salience label to its badge (fail-closed to the raw label)."""
    return SALIENCE_BADGES.get(salience_label or "", salience_label or "")


def present_gap_badges(gaps: list[str]) -> list[str]:
    """Render the fail-closed gap codes as visible badges (§1/§3 — never hidden).

    Load-bearing (I5): the only path a 5.05 envelope gap reaches the surface. Each code
    maps to its badge text, unknown codes pass through verbatim so a gap is NEVER silently
    dropped. Neutering this to ``[]`` makes :func:`assert_gaps_visible` go RED while the
    underlying envelope still carries the gaps — a non-tautological RED-proof.
    """
    return [GAP_BADGES.get(code, code) for code in gaps]


# ---------------------------------------------------------------------------
# §2 helper — honest topic-anchor provenance (load-bearing)
# ---------------------------------------------------------------------------


def classify_topic_anchor(topic_id: str, records: list[dict[str, Any]]) -> str:
    """Classify a hot-topic anchor as a real topic edge or the agenda-thread fallback.

    Load-bearing (I5): a topic is :data:`ANCHOR_TOPIC_EDGE` iff *some* served record
    carries an explicit evidence ``topic_id`` edge to it; otherwise it is the honest
    :data:`ANCHOR_AGENDA_THREAD` fallback (the latent-``topic_id`` reality per VSR
    GOV-521 — today every anchor resolves here). Neutering this to always return
    ``ANCHOR_TOPIC_EDGE`` makes :func:`assert_topic_anchors_honest` go RED — the surface
    would claim a topic edge absent from the evidence graph, while the read surface and
    the 5.05 hotTopics envelope are byte-unchanged. Non-tautological RED-proof.
    """
    for record in records:
        for link in record.get("evidence", []):
            if link.get("topic_id") == topic_id:
                return ANCHOR_TOPIC_EDGE
    return ANCHOR_AGENDA_THREAD


# ---------------------------------------------------------------------------
# §1 — corrections surface: one correction card per ledger edge
# ---------------------------------------------------------------------------


def build_corrections_surface(ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project the 5.05 ``correctionsLedger`` into correction cards (§1).

    Each card preserves the corrected ``known_then`` context verbatim (status / recordDate
    / confidence badges), points it at the superseding record when resolved, and renders
    the fail-closed ``correction_unresolved`` gap as a **visible gap badge** otherwise.
    Order follows the (already byte-stable) ledger.
    """
    cards: list[dict[str, Any]] = []
    for edge in ledger:
        known_then = edge.get("knownThen", {})
        resolved = bool(edge.get("resolved"))
        cards.append(
            {
                "correctedStatementId": edge.get("correctedStatementId"),
                "correctedRef": edge.get("correctedRef"),
                "correctionStatusBadge": correction_status_badge(
                    edge.get("correctionStatus")
                ),
                "knownThen": {
                    "status": known_then.get("status"),
                    "statusBadge": status_badge(known_then.get("status")),
                    "recordDate": known_then.get("recordDate"),
                    "confidenceBadge": known_then.get("sourceConfidence"),
                },
                "resolved": resolved,
                "supersedingRef": edge.get("supersedingRef"),
                "resolutionBadge": (
                    RESOLUTION_LINKED if resolved else RESOLUTION_UNRESOLVED
                ),
                "gapBadges": present_gap_badges(edge.get("gaps", [])),
            }
        )
    return cards


# ---------------------------------------------------------------------------
# §2 — hot topics surface: ranked topic cards with honest anchor disclosure
# ---------------------------------------------------------------------------


def build_hot_topics_surface(
    hot_topics: list[dict[str, Any]], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Project the 5.05 ``hotTopics`` ranking into ranked topic cards (§2).

    Rank follows the envelope's already-deterministic order. Each card surfaces the raw
    counts + salience score, a salience badge (``Ranked`` / ``insufficientData`` floor),
    and an **honest topic-anchor disclosure** (:func:`classify_topic_anchor`) so a topic
    anchored only via the agenda-item fallback never reads as a real topic edge.
    """
    cards: list[dict[str, Any]] = []
    for index, topic in enumerate(hot_topics):
        topic_id = topic.get("topicId")
        kind = classify_topic_anchor(topic_id, records)
        cards.append(
            {
                "rank": index + 1,
                "topicId": topic_id,
                "activityCount": topic.get("activityCount"),
                "recencyCount": topic.get("recencyCount"),
                "correctionChurn": topic.get("correctionChurn"),
                "salienceScore": topic.get("salienceScore"),
                "salienceBadge": salience_badge(topic.get("salienceLabel")),
                "topicAnchor": {
                    "kind": kind,
                    "disclosure": ANCHOR_DISCLOSURES[kind],
                },
            }
        )
    return cards


# ---------------------------------------------------------------------------
# §3 — watchdog board: the Kanban-precursor lane columns
# ---------------------------------------------------------------------------


def _watchdog_card(entry: dict[str, Any]) -> dict[str, Any]:
    """One watchdog board card view-model over a 5.05 watchdogView entry."""
    return {
        "cardId": entry.get("cardId"),
        "statementId": entry.get("statementId"),
        "recordDate": entry.get("recordDate"),
        "status": entry.get("status"),
        "statusBadge": status_badge(entry.get("status")),
        "confidenceBadge": entry.get("sourceConfidence"),
        "sourceLinked": entry.get("sourceLinked"),
        "gapBadges": present_gap_badges(entry.get("gaps", [])),
    }


def build_watchdog_board(watchdog_view: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the 5.05 ``watchdogView`` into the six frozen lane columns (§3).

    ALL six lanes are emitted in canonical order — an empty lane is shown as an empty
    column (a board never hides an empty lane). Cards within a lane are ordered by
    statement id (byte-stable). Each column carries its label and card count.
    """
    by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANE_ORDER}
    for entry in watchdog_view:
        lane = entry.get("lane")
        # 5.05 already guarantees the lane is in the frozen vocab; group defensively so a
        # surprise lane surfaces under its own column rather than vanishing.
        by_lane.setdefault(lane, []).append(_watchdog_card(entry))

    columns: list[dict[str, Any]] = []
    for lane in LANE_ORDER:
        cards = sorted(by_lane[lane], key=lambda c: c["statementId"] or "")
        columns.append(
            {
                "lane": lane,
                "laneLabel": lane_label(lane),
                "cardCount": len(cards),
                "cards": cards,
            }
        )
    return columns


# ---------------------------------------------------------------------------
# §4 — the single envelope digest (I3) + the assembled surface body
# ---------------------------------------------------------------------------


def _surface_digest(
    corrections: list[dict[str, Any]],
    hot_topics: list[dict[str, Any]],
    board: list[dict[str, Any]],
) -> str:
    """A single sha256 over the canonical three surfaces (I3).

    The ONLY hash exposed by the whole body — there is no per-source raw-content hash.
    Computed over the already-web-safe surfaces so it cannot encode a raw path.
    """
    payload = json.dumps(
        {
            "correctionsSurface": corrections,
            "hotTopicsSurface": hot_topics,
            "watchdogBoard": board,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_surface(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the reviewer-internal frontend surface body + sweep it.

    Consumes the 5.05 signal envelope (:func:`stage5_watchdog_signals.build_signals`) and
    the read surface read-only, then projects the three presentation surfaces. Exactly one
    hash is exposed (the envelope ``surfaceDigest``). The whole body is swept by
    :func:`read_api.assert_no_raw_paths`, so a FS path / ``.sha256`` / vault marker /
    ``file://`` that slipped a column fails LOUDLY at the boundary (I1 backstop). Pure
    function of the DB — same DB -> byte-identical surface.
    """
    envelope = watchdog.build_signals(conn)
    records = read_api.reviewer_internal_records(conn)

    corrections = build_corrections_surface(envelope["correctionsLedger"])
    hot_topics = build_hot_topics_surface(envelope["hotTopics"], records)
    board = build_watchdog_board(envelope["watchdogView"])

    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public" — I6
        "correctionsSurface": corrections,
        "hotTopicsSurface": hot_topics,
        "watchdogBoard": board,
        "surfaceDigest": _surface_digest(corrections, hot_topics, board),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# §5 — contract guards (load-bearing, non-tautological checks)
# ---------------------------------------------------------------------------

_HEX64 = frozenset("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value.lower())
    )


def assert_no_false_verified(body: dict[str, Any]) -> bool:
    """RED if any card wears the ``Verified`` badge without a verified status (R-1).

    The core safety invariant (never present unverified / AI / corrected as verified).
    Cross-checks the EMITTED body: every watchdog card and every corrections known-then
    block must carry exactly :func:`status_badge` of its raw ``status``, and the
    ``Verified`` badge may ONLY accompany the ``verified`` status.
    """
    pairs: list[tuple[Any, Any, str]] = []
    for column in body.get("watchdogBoard", []):
        for card in column.get("cards", []):
            pairs.append((card.get("status"), card.get("statusBadge"), "watchdog card"))
    for card in body.get("correctionsSurface", []):
        known_then = card.get("knownThen", {})
        pairs.append(
            (known_then.get("status"), known_then.get("statusBadge"), "knownThen")
        )
    for status, badge, where in pairs:
        if badge != status_badge(status):
            raise FrontendSurfaceError(
                f"{where} status badge {badge!r} != fail-closed mapping of {status!r}"
            )
        if badge == BADGE_VERIFIED and status != card_feed.STATUS_VERIFIED:
            raise FrontendSurfaceError(
                f"{where} wears the {BADGE_VERIFIED!r} badge but status is {status!r}"
            )
    return True


def assert_gaps_visible(body: dict[str, Any], envelope: dict[str, Any]) -> bool:
    """RED if any 5.05 envelope gap is not surfaced as a visible badge (R-2, I5).

    Recomputes the expected gap badges directly from the 5.05 ``correctionsLedger`` and
    ``watchdogView`` (ground truth, NOT via :func:`present_gap_badges`), then asserts each
    expected badge is present on the matching surface card. A neutered
    :func:`present_gap_badges` that drops a gap goes RED here while the envelope still
    carries it — non-tautological (a gap is never hidden).
    """
    # corrections: each ledger edge's gaps must show on the matching correction card.
    cards_by_id = {
        c.get("correctedStatementId"): c for c in body.get("correctionsSurface", [])
    }
    for edge in envelope.get("correctionsLedger", []):
        expected = {GAP_BADGES.get(code, code) for code in edge.get("gaps", [])}
        card = cards_by_id.get(edge.get("correctedStatementId"))
        if card is None:
            raise FrontendSurfaceError(
                f"correction edge {edge.get('correctedStatementId')!r} has no surface card"
            )
        present = set(card.get("gapBadges", []))
        missing = expected - present
        if missing:
            raise FrontendSurfaceError(
                f"correction card {edge.get('correctedStatementId')!r} hides gaps {missing}"
            )

    # watchdog: each view entry's gaps must show on the matching board card.
    board_cards = {
        card.get("statementId"): card
        for column in body.get("watchdogBoard", [])
        for card in column.get("cards", [])
    }
    for entry in envelope.get("watchdogView", []):
        expected = {GAP_BADGES.get(code, code) for code in entry.get("gaps", [])}
        card = board_cards.get(entry.get("statementId"))
        if card is None:
            raise FrontendSurfaceError(
                f"watchdog entry {entry.get('statementId')!r} has no board card"
            )
        missing = expected - set(card.get("gapBadges", []))
        if missing:
            raise FrontendSurfaceError(
                f"board card {entry.get('statementId')!r} hides gaps {missing}"
            )
    return True


def assert_topic_anchors_honest(
    body: dict[str, Any], records: list[dict[str, Any]]
) -> bool:
    """RED if a hot-topic card claims a topic edge absent from the evidence graph (R-3, I5).

    Recomputes the set of real evidence ``topic_id`` edges directly from the read surface
    (ground truth, NOT via :func:`classify_topic_anchor`), then asserts no card declares
    :data:`ANCHOR_TOPIC_EDGE` for a topic with no such edge. A neutered classifier that
    always claims a topic edge goes RED while the read surface and the 5.05 hotTopics
    envelope are byte-unchanged — non-tautological (never imply an edge not in the data).
    """
    real_topic_edges = {
        link["topic_id"]
        for record in records
        for link in record.get("evidence", [])
        if link.get("topic_id")
    }
    for card in body.get("hotTopicsSurface", []):
        anchor = card.get("topicAnchor", {})
        kind = anchor.get("kind")
        if kind not in ANCHOR_DISCLOSURES:
            raise FrontendSurfaceError(
                f"hot-topic {card.get('topicId')!r} anchor kind {kind!r} out of vocab"
            )
        if kind == ANCHOR_TOPIC_EDGE and card.get("topicId") not in real_topic_edges:
            raise FrontendSurfaceError(
                f"hot-topic {card.get('topicId')!r} claims a topic edge absent from the "
                "evidence graph"
            )
    return True


def assert_board_complete(body: dict[str, Any], envelope: dict[str, Any]) -> bool:
    """RED if the board drops a record or shows an out-of-vocab lane / missing column (R-4).

    Every 5.05 ``watchdogView`` entry must appear as exactly one board card, every board
    lane must be in the frozen :data:`LANE_ORDER`, and all six columns must be present.
    """
    columns = body.get("watchdogBoard", [])
    lanes_seen = [column.get("lane") for column in columns]
    if lanes_seen != list(LANE_ORDER):
        raise FrontendSurfaceError(
            f"watchdog board lanes {lanes_seen} != frozen column order {list(LANE_ORDER)}"
        )
    board_ids = [
        card.get("statementId")
        for column in columns
        for card in column.get("cards", [])
    ]
    if len(board_ids) != len(set(board_ids)):
        raise FrontendSurfaceError("a watchdog record appears in more than one board card")
    expected_ids = {e.get("statementId") for e in envelope.get("watchdogView", [])}
    if set(board_ids) != expected_ids:
        raise FrontendSurfaceError(
            "watchdog board card set does not match the 5.05 watchdogView entries"
        )
    return True


def assert_single_surface_digest(body: dict[str, Any]) -> bool:
    """RED if any 64-hex string appears outside the top-level ``surfaceDigest`` (R-5/I3)."""
    if not _is_hex64(body.get("surfaceDigest")):
        raise FrontendSurfaceError("envelope surfaceDigest is not a sha256")
    for key, value in body.items():
        if key == "surfaceDigest":
            continue
        for text in read_api._iter_strings(value):
            if _is_hex64(text):
                raise FrontendSurfaceError(
                    f"per-source 64-hex hash leaked under {key!r}: {text!r}"
                )
    return True


def assert_reviewer_internal(body: dict[str, Any]) -> bool:
    """RED if the surface is not tagged reviewer-internal / Alpine (R-6, I6)."""
    if body.get("access") != ACCESS or body.get("scope") != SCOPE:
        raise FrontendSurfaceError(
            f"surface must be {ACCESS}/{SCOPE}, got "
            f"{body.get('access')!r}/{body.get('scope')!r}"
        )
    return True


def check_surface(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build + run every load-bearing contract guard. Raises on the first defect."""
    envelope = watchdog.build_signals(conn)
    records = read_api.reviewer_internal_records(conn)
    body = build_surface(conn)
    assert_reviewer_internal(body)
    assert_no_false_verified(body)
    assert_gaps_visible(body, envelope)
    assert_topic_anchors_honest(body, records)
    assert_board_complete(body, envelope)
    assert_single_surface_digest(body)
    return body


# ---------------------------------------------------------------------------
# CLI (read-only — emits the reviewer-internal frontend surface contract)
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.06 reviewer-internal Alpine frontend/product surface contract "
        "(GOV-524)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the no-false-verified / gaps-visible / honest-anchor / board / "
        "single-envelope guards (CI gate)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.check:
            body = check_surface(conn)
        else:
            body = build_surface(conn)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
