"""Stage 3.05 card feed (GOV-347) — web-safe, stable-id timeline cards.

The reviewer-internal Alpine timeline's **sole data source**: a deterministic JSON
card feed projected ENTIRELY on top of the already-web-safe Stage 2 read surface —
:func:`read_api.reviewer_internal_records` (served statements) +
:func:`read_api.completeness_gap_cards` (the ~90 ``no_primary_source`` gaps). It
implements the GOV-346 contract (``Docs/stage3-05-card-feed-contract.md``):

* **§1 handle** — a stable, opaque, derived card identity
  (``c1_`` + truncated SHA-256 over an already-web-safe natural key), never a raw
  DB id (:func:`card_handle`).
* **§2 status** — a single status string composed first-match/top-down from the
  read keys ``ui_status`` + ``provenance_status`` + ``produced_by`` + the gap lane,
  fail-closed to ``unverified``; no fabricated ``disputed``/``source_changed`` edge
  (:func:`_compose_record_status`).
* **§3 feed shape** — the ``{scope, access, cards[]}`` envelope
  (:func:`build_card_feed`).

Boundary rules (GOV-336 §2.1, restated as feed invariants):

* the card layer **never** calls ``to_web_safe`` / ``publication.py`` (the read
  surface already crossed both web-safe layers);
* it **never** issues a new raw query and **never** re-derives a field the read
  surface dropped — every field rides straight from a named read-surface envelope
  key, or is derived **here** from already-web-safe keys (§1/§2/§3.2);
* it runs entirely at ``access: reviewer_internal`` (the whole MVP is behind the
  gated beta — GOV-336 §2.3); ``provenance_status`` rides the reviewer-internal
  lane only (``reviewer_internal_records`` already sets
  ``include_provenance_status=True``).

Fail-closed / honesty posture (the GOV-337 ACs are the contract):

* AC-1 — ``cards`` carries ≥5 *sourced* cards (non-empty ``evidence``) when the
  data allows; an evidence-less served record is still emitted (never silently
  dropped — back-gap guard), it simply does not count toward the sourced tally,
  and gaps are shown, never padded.
* AC-3 — the assembled feed is transport-swept by
  :func:`read_api.assert_no_raw_paths`: a ``file://`` / FS path / ``.sha256`` /
  raw-id leak fails LOUDLY at the boundary, not silently downstream.
* AC-4 — unknown provenance ⇒ gated ``unverified``; no fabricated correction edge.
* back-gap (3.13 / GOV-322 pattern) — :func:`assert_feed_covers_surface` proves the
  feed never silently drops a record/gap the read surface emits (RED on a planted
  drop).

Pure function of the read surface: same DB -> byte-identical feed (idempotent
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
import read_api  # noqa: E402

# ---------------------------------------------------------------------------
# §1 — stable web-safe card handle
# ---------------------------------------------------------------------------

# Scheme-version prefix: ``c`` = card, ``1`` = v1. Lets the handle scheme evolve
# without ambiguity (GOV-346 §1.3).
HANDLE_SCHEME_PREFIX = "c1_"
# ASCII Unit Separator: an unambiguous, type-namespacing delimiter that cannot
# appear inside a slug id, so ``(meeting, "a-b")`` and ``(meetin, "ga-b")`` can
# never alias to the same preimage (GOV-346 §1.3).
_HANDLE_DELIM = "\x1f"
# 160 bits of SHA-256 -> 40 lowercase-hex chars: URL-safe, fixed-length, no
# padding; birthday bound ~2^80 cards (Alpine scale is 10^2-10^4) so no
# application-level collision is reachable (GOV-346 §1.4).
_HANDLE_HEX_LEN = 40


def card_handle(card_type: str, natural_key: str) -> str:
    """Derive the stable, opaque card handle (GOV-346 §1.3).

    ``handle = "c1_" + lowerhex(SHA256(card_type + "\\x1f" + natural_key))[:40]``.

    Deterministic by construction: a pure function of two already-web-safe inputs
    — no timestamp, no randomness, no DB rowid, no ordering dependence — so the
    same record always yields the same handle on every feed build (AC evidence
    stability + frontend list-key stability).

    NC-2 (no raw-id / internal-key leak): the inputs are only fields already past
    both web-safe layers, and SHA-256 is preimage-resistant, so even the
    (already web-safe) natural key cannot be recovered from the digest — hashing it
    rather than passing it verbatim is defense in depth. A 40-char hex digest
    contains no ``/`` / ``file://`` / ``.sha256`` / path shape by construction.
    """
    if not card_type or not natural_key:
        # An empty input would weaken uniqueness; this never happens for a served
        # card (orphans are never served, so the natural key is always present).
        # Fail loudly rather than emit a degenerate handle.
        raise ValueError(
            f"card_handle requires non-empty card_type and natural_key; "
            f"got {card_type!r} / {natural_key!r}"
        )
    preimage = f"{card_type}{_HANDLE_DELIM}{natural_key}".encode("utf-8")
    digest = hashlib.sha256(preimage).hexdigest()[:_HANDLE_HEX_LEN]
    return HANDLE_SCHEME_PREFIX + digest


# ---------------------------------------------------------------------------
# §2 — derived status vocabulary (fail-closed, no invented status)
# ---------------------------------------------------------------------------

# The master-plan card status vocab. Only the values the read surface can actually
# produce are reachable from this module (GOV-346 §2.1): a served record carries
# ``ui_status in {source-backed, archived-source-backed, corrected}`` only, so
# ``disputed`` / ``source_changed`` are not surfaceable today (bounded gaps -> 3.07),
# and ``source_missing`` is reachable ONLY via the gap lane.
STATUS_SOURCE_MISSING = "source_missing"
STATUS_CORRECTED = "corrected"
STATUS_AI_PRESENTED = "ai_presented"
STATUS_VERIFIED = "verified"
STATUS_UNVERIFIED = "unverified"  # the global fail-closed default

# ``ui_status`` values that may underwrite a ``verified`` card (GOV-346 §2.2 row 4).
# A subset of ``publication.PUBLICATION_ELIGIBLE_UI_STATUSES`` — ``corrected`` is
# handled by its own higher-precedence row, never as ``verified``.
_VERIFIED_UI_STATUSES = frozenset({"source-backed", "archived-source-backed"})


def _compose_record_status(record: dict[str, Any]) -> str:
    """Compose a single status for a served record (GOV-346 §2.2, first-match top-down).

    Precedence (gap lane / row 1 ``source_missing`` is handled in :func:`_gap_card`,
    never here):

    2. ``ui_status == "corrected"`` -> ``corrected`` (the reviewed correction render
       state; no fabricated edge).
    3. ``produced_by == "ai"`` -> ``ai_presented`` (the AI *flag* dominates the
       single status; trust *within* the AI card stays the ``provenance_status``
       envelope key, which rides alongside).
    4. ``provenance_status == "grounded"`` AND ``ui_status`` in the verified set
       -> ``verified`` (requires an explicit ``grounded``).
    5. otherwise -> ``unverified`` (DEFAULT: any record that cannot be resolved to a
       higher row collapses here, never to a reassuring state).
    """
    if record.get("ui_status") == "corrected":
        return STATUS_CORRECTED
    if record.get("produced_by") == "ai":
        return STATUS_AI_PRESENTED
    if (
        record.get("provenance_status") == read_api.PROVENANCE_GROUNDED
        and record.get("ui_status") in _VERIFIED_UI_STATUSES
    ):
        return STATUS_VERIFIED
    return STATUS_UNVERIFIED


# ---------------------------------------------------------------------------
# §3.2 — card type resolution (bounded to the master concept set, fail-closed)
# ---------------------------------------------------------------------------

TYPE_MEETING = "meeting"
TYPE_INFO = "info"
TYPE_DECISION = "decision"
TYPE_STATEMENT = "statement"
TYPE_SOURCE = "source"
TYPE_CORRECTION = "correction"
TYPE_AI_PRESENTED = "ai_presented"
TYPE_SOURCE_MISSING = "source_missing"


def _resolve_record_type(record: dict[str, Any]) -> str:
    """Resolve a record card's ``type`` (GOV-346 §3.2), fail-closed.

    Note the precedence here differs from :func:`_compose_record_status` *by design*
    (GOV-346 §3.2 vs §2.2): for the *type label* the AI flag dominates the kind,
    while for the *status* ``corrected`` dominates trust. So an AI-produced corrected
    record reads ``type=ai_presented`` / ``status=corrected`` — both grounded, no
    fabrication.

    1. ``produced_by == "ai"`` -> ``ai_presented`` (the AI flag dominates the kind).
    2. ``ui_status == "corrected"`` -> ``correction``.
    3. otherwise the structural kind from already-allowlisted fields: a statement
       row (``statement_id`` + ``statement_text``) -> ``statement``.
    4. unknown/unresolved -> ``info`` (neutral, non-asserting) — never a stronger
       type than the evidence supports.

    ``meeting`` / ``source`` / ``decision`` are in the master set but are NOT
    producible from this surface (``reviewer_internal_records`` serves statement
    rows); their resolution lives in §3.2 for when a future surface emits them.
    """
    if record.get("produced_by") == "ai":
        return TYPE_AI_PRESENTED
    if record.get("ui_status") == "corrected":
        return TYPE_CORRECTION
    if record.get("statement_id") and record.get("statement_text"):
        return TYPE_STATEMENT
    return TYPE_INFO


# ---------------------------------------------------------------------------
# §3.3 — per-card field maps
# ---------------------------------------------------------------------------

# Record-level allowlisted timing fields, in preference order. A statement row
# typically carries none (timing rides the evidence drawer), so the date falls back
# to the earliest evidence ``scan_date``. The feed NEVER invents a date.
_RECORD_DATE_FIELDS = ("scan_date", "first_seen_date", "last_validated_utc")
# Per-evidence allowlisted date field used as the fallback timeline date.
_EVIDENCE_DATE_FIELD = "scan_date"

JURISDICTION = "alpine"  # envelope scope (fixed; broader = planned — GOV-336 §2.3)


def _card_date(record: dict[str, Any]) -> str | None:
    """Resolve a card's timeline ``date`` from already-web-safe fields, never invented.

    Prefer a record-level allowlisted timing field; else the earliest evidence
    ``scan_date`` (the drawer is already web-safe). Returns ``None`` when no grounded
    date exists — the feed shows no date rather than fabricating one.
    """
    for field in _RECORD_DATE_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    evidence_dates = [
        link.get(_EVIDENCE_DATE_FIELD)
        for link in record.get("evidence", [])
        if isinstance(link.get(_EVIDENCE_DATE_FIELD), str) and link.get(_EVIDENCE_DATE_FIELD)
    ]
    return min(evidence_dates) if evidence_dates else None


def _record_card(record: dict[str, Any]) -> dict[str, Any]:
    """Build a normal (record-backed) card from a served reviewer-internal record.

    Every field rides straight from a named read-surface envelope key, or is derived
    here (§1 handle / §2 status / §3.2 type / :func:`_card_date`) from already-web-safe
    keys. The natural key is ``statement_id`` — the record's primary slug, present on
    every served (non-orphan) record and unique within the surface — matching
    GOV-346 §1.2 for every producible card kind.
    """
    statement_id = record["statement_id"]  # KeyError = orphan served upstream (a bug)
    card_type = _resolve_record_type(record)
    card: dict[str, Any] = {
        "handle": card_handle(card_type, statement_id),
        "type": card_type,
        "jurisdiction": JURISDICTION,
        "status": _compose_record_status(record),
        "evidence": list(record.get("evidence", [])),
    }
    # Optional presentation fields — included only when the read surface carries
    # them (never invented, never re-derived).
    title = record.get("title")
    if isinstance(title, str) and title:
        card["title"] = title
    date = _card_date(record)
    if date is not None:
        card["date"] = date
    summary = record.get("statement_text")
    if isinstance(summary, str) and summary:
        card["reviewed_summary"] = summary  # REVIEWER-INTERNAL free text (§2.3)
    for envelope_key in ("confidence_label", "speaker_label", "provenance_status"):
        if envelope_key in record:
            card[envelope_key] = record[envelope_key]
    return card


def _gap_card(gap: dict[str, Any]) -> dict[str, Any]:
    """Build a ``source_missing`` gap card from a ``completeness_gap_cards`` row.

    Reduced shape (no statement fields). The gap card is built from the already
    fail-closed, web-safe gap projection (GOV-298): ``gap_type`` / ``severity`` /
    ``resolved_status`` are SSOT-validated there, and a leak-prone ``detail`` is
    already omitted. ``status`` is ``source_missing`` — the only gap status the
    master vocab and the timeline surface today (§2.2 row 1); the true ``gap_type``
    is retained in the card for the drawer, so nothing is fabricated and the gap is
    never hidden.
    """
    gap_id = gap["gap_id"]  # gap-card primary slug
    card: dict[str, Any] = {
        "handle": card_handle(TYPE_SOURCE_MISSING, gap_id),
        "type": TYPE_SOURCE_MISSING,
        "jurisdiction": JURISDICTION,
        "status": STATUS_SOURCE_MISSING,
        "gap_type": gap.get("gap_type"),
        "severity": gap.get("severity"),
        "resolved_status": gap.get("resolved_status"),
    }
    if "detail" in gap:  # present ONLY when it cleared the read-time leak guards
        card["detail"] = gap["detail"]
    return card


# ---------------------------------------------------------------------------
# §3 — the card feed envelope
# ---------------------------------------------------------------------------


def build_card_feed(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the ``{scope, access, cards[]}`` card feed and transport-sweep it.

    Record cards (one per served reviewer-internal record) come first, then gap
    cards (one per completeness gap) — a 1:1 projection of the read surface with NO
    silent drop (the back-gap invariant; :func:`assert_feed_covers_surface` proves
    it). The whole body is then swept by :func:`read_api.assert_no_raw_paths`, so a
    leak fails LOUDLY at the boundary (AC-3) — the same backstop every read surface
    uses.
    """
    cards: list[dict[str, Any]] = [
        _record_card(record) for record in read_api.reviewer_internal_records(conn)
    ]
    cards.extend(_gap_card(gap) for gap in read_api.completeness_gap_cards(conn))
    feed: dict[str, Any] = {
        "scope": JURISDICTION,
        "access": "reviewer_internal",
        "cards": cards,
    }
    return read_api.assert_no_raw_paths(feed)


def sourced_cards(feed: dict[str, Any]) -> list[dict[str, Any]]:
    """The AC-1 *sourced* subset: cards with a non-empty ``evidence`` drawer.

    Gap cards and any evidence-less served record are emitted in the feed (never
    dropped) but do not count toward the AC-1 "≥5 sourced cards" tally.
    """
    return [card for card in feed.get("cards", []) if card.get("evidence")]


# ---------------------------------------------------------------------------
# Back-gap coverage guard (3.13 / GOV-322 pattern) — feed never silently drops.
# ---------------------------------------------------------------------------


class FeedCoverageError(AssertionError):
    """Raised when the feed drops a record/gap the read surface still emits."""


def expected_handles(conn: sqlite3.Connection) -> set[str]:
    """The handle set the read surface mandates — recomputed independently.

    A separate derivation from :func:`build_card_feed` so the guard is a genuine
    cross-check, not a tautology: it re-projects each ``reviewer_internal_records``
    and ``completeness_gap_cards`` row to its handle directly.
    """
    handles: set[str] = set()
    for record in read_api.reviewer_internal_records(conn):
        handles.add(_record_card(record)["handle"])
    for gap in read_api.completeness_gap_cards(conn):
        handles.add(_gap_card(gap)["handle"])
    return handles


def assert_feed_covers_surface(
    conn: sqlite3.Connection, feed: dict[str, Any] | None = None
) -> bool:
    """RED if any read-surface record/gap is missing from ``feed`` (back-gap guard).

    Independently recomputes the mandated handle set (:func:`expected_handles`) and
    asserts every one appears in the feed. A silently dropped card -> ``missing`` is
    non-empty -> :class:`FeedCoverageError` (the guard goes RED), mirroring the
    GOV-322 back-gap auditor's "never silently drop a record the read surface emits".
    """
    if feed is None:
        feed = build_card_feed(conn)
    feed_handles = {card.get("handle") for card in feed.get("cards", [])}
    expected = expected_handles(conn)
    missing = expected - feed_handles
    if missing:
        raise FeedCoverageError(
            f"feed dropped {len(missing)} read-surface card(s): {sorted(missing)}"
        )
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3.05 reviewer-internal Alpine card feed (GOV-347)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check-coverage",
        action="store_true",
        help="assert the feed covers every read-surface record/gap (back-gap guard)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        feed = build_card_feed(conn)
        if args.check_coverage:
            assert_feed_covers_surface(conn, feed)
    print(json.dumps(feed, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
