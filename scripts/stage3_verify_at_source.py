"""Stage 3.07 verify-at-source drill-down projection (GOV-376) — reviewer-internal.

Implements the GOV-375 contract (``Docs/stage3-07-verify-at-source-contract.md``)
over the EXISTING Alpine card surface: for every served card it exposes the
already-present three-node drill path (card -> statement -> ordered evidence links
-> original-source locator), attaches a per-link **resolvability** status (§2) and a
per-card **verify-at-source** status (§3), and proves a back-gap bijection with the
live feed (§4). It is a **separate additive module** (the GOV-347 / GOV-364 /
GOV-367 precedent): it *calls* ``read_api`` / ``stage2_traceability`` /
``stage3_card_feed``, never edits them — ``read_api.py`` / ``publication.py`` stay
**0-diff**.

Boundary / no-leak invariants the impl satisfies by construction (contract §5):

* **B-1 no new web-safe field** — resolvability / verify-at-source are
  reviewer-internal **envelope keys** attached AFTER the web-safe projection (exactly
  like ``provenance_status``), never added to ``WEB_SAFE_FIELD_ALLOWLIST``.
* **B-2 ``read_api`` / ``publication`` 0-diff** — this module imports them read-only.
* **B-3 raw join keys never cross** — ``segment_id`` / ``to_source_id`` are read as
  CANONICAL inputs to the resolvability derivation (re-fetched from the DB row, NOT
  the stripped web-safe drawer); they are never placed in the projected body.
* **B-4 transport backstop** — the assembled body is swept by
  :func:`read_api.assert_no_raw_paths` (a ``file://`` / FS path / ``.sha256`` / raw
  marker fails LOUDLY at the boundary).
* **B-5 reviewer-internal lane only** — the whole projection runs at
  ``access: reviewer_internal``; the public lane stays byte-identical.
* **B-6 pure** — a pure function of stored fields: same DB -> byte-identical
  projection. No mutation, no AI, no network.

Resolvability is **derived from real predicates, never stored / fabricated**
(contract §2 / VS-3): it reuses the GOV-306 predicates verbatim
(:func:`read_api._segment_resolves`, :func:`stage2_traceability.raw_linked`) plus the
per-link ``to_source_id -> sources`` branch of
:func:`stage2_traceability.statement_grounded`. Fail-closed: ANY break collapses to
``unresolved`` / ``unverified`` (GOV-230 §default).
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
import read_api  # noqa: E402  (read-only: lane + predicates + transport guard, no mutation)
import stage2_traceability as trace  # noqa: E402  (read-only: GOV-306 predicates, verbatim)
import stage3_card_feed as feed  # noqa: E402  (read-only: handle + type projection, extend not fork)

JURISDICTION = "alpine"  # envelope scope (fixed; broader = planned — contract §0)

# ---------------------------------------------------------------------------
# §2 — per-link resolvability status (frozen, fail-closed SSOT)
# ---------------------------------------------------------------------------

# Mirror of read_api.PROVENANCE_STATUS_VALUES: a binary, frozen vocabulary whose
# DEFAULT is the conservative "unresolved" — optimistic "resolved" is NEVER the
# default (GOV-230 §default). The derivation always returns a member of this set.
RESOLVABILITY_RESOLVED = "resolved"
RESOLVABILITY_UNRESOLVED = "unresolved"  # the global fail-closed DEFAULT
RESOLVABILITY_VALUES: frozenset[str] = frozenset(
    {RESOLVABILITY_RESOLVED, RESOLVABILITY_UNRESOLVED}
)

# ---------------------------------------------------------------------------
# §3 — per-card verify-at-source status (frozen, fail-closed SSOT)
# ---------------------------------------------------------------------------

VERIFY_AT_SOURCE_VERIFIABLE = "verifiable"
VERIFY_AT_SOURCE_UNVERIFIED = "unverified"  # the fail-closed DEFAULT (record cards)
VERIFY_AT_SOURCE_VALUES: frozenset[str] = frozenset(
    {VERIFY_AT_SOURCE_VERIFIABLE, VERIFY_AT_SOURCE_UNVERIFIED}
)

# A gap card has no statement and no evidence, so verify-at-source is N/A by
# construction — it is labeled with the feed's own ``source_missing`` status, NEVER
# ``verifiable`` (contract §4). This is deliberately OUTSIDE the binary record-card
# SSOT above; the auditor (VS-5) asserts record cards stay binary and gap cards stay
# exactly this label.
VERIFY_AT_SOURCE_SOURCE_MISSING = feed.STATUS_SOURCE_MISSING  # "source_missing"


# ---------------------------------------------------------------------------
# Canonical-column reads (the resolvability inputs; NOT from the web-safe drawer)
# ---------------------------------------------------------------------------


def _canonical_segment_id(conn: sqlite3.Connection, statement_id: str) -> str | None:
    """The statement's CANONICAL ``segment_id`` (stripped from the served body, §2).

    Re-read read-only from ``statements`` — mirrors how
    :func:`stage2_traceability._canonical_statement` re-fetches the canonical row, so
    the resolvability derivation never trusts the already-web-safe drawer (where
    ``segment_id`` is stripped). Only the safe edge key is selected.
    """
    row = conn.execute(
        "SELECT segment_id FROM statements WHERE statement_id = ?", (statement_id,)
    ).fetchone()
    return row["segment_id"] if row is not None else None


def _link_source_resolves(conn: sqlite3.Connection, to_source_id: str | None) -> bool:
    """§2 leg 2 (per-link): the link's ``to_source_id`` resolves to a real ``sources`` row.

    The ``statement_grounded`` evidence-link branch (:func:`stage2_traceability`
    ``:164-166``) applied to ONE link's canonical ``to_source_id`` — so a dangling /
    orphan link (no backing ``sources`` row) does not resolve. Fail-closed on a
    missing id.
    """
    if not to_source_id:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM sources WHERE source_id = ?", (to_source_id,)
        ).fetchone()
        is not None
    )


def resolvability_status(
    conn: sqlite3.Connection,
    statement_id: str,
    canonical_link: dict[str, Any],
    *,
    segment_resolves: bool | None = None,
    raw_ok: bool | None = None,
) -> str:
    """Derive one link's resolvability (§2), fail-closed, from real predicates.

    ``resolved`` iff the grounding unit resolves through the canonical chain — REUSING
    the GOV-306 predicates verbatim and the live serving primitive:

    1. the statement's ``segment_id`` resolves to a ``transcript_segments`` row
       (:func:`read_api._segment_resolves`), **OR**
    2. THIS link's ``to_source_id`` resolves to a ``sources`` row
       (:func:`_link_source_resolves`), **OR**
    3. a preserved raw predecessor exists for the grounding unit
       (:func:`stage2_traceability.raw_linked`, GOV-262).

    Otherwise ``unresolved`` (DEFAULT). ``segment_resolves`` / ``raw_ok`` are
    statement-level legs (same for every link of the statement) and may be passed in
    precomputed; if omitted they are derived here. The returned value is always a
    member of :data:`RESOLVABILITY_VALUES`.
    """
    if segment_resolves is None:
        segment_resolves = read_api._segment_resolves(
            conn, _canonical_segment_id(conn, statement_id)
        )
    if raw_ok is None:
        raw_ok = trace.raw_linked(conn, statement_id)
    resolved = (
        segment_resolves  # leg 1
        or _link_source_resolves(conn, canonical_link.get("to_source_id"))  # leg 2
        or raw_ok  # leg 3
    )
    return RESOLVABILITY_RESOLVED if resolved else RESOLVABILITY_UNRESOLVED


def verify_at_source_status(provenance_grounded: bool, has_resolved_link: bool) -> str:
    """Derive a record card's verify-at-source status (§3), fail-closed.

    ``verifiable`` iff BOTH legs hold: (1) ≥1 evidence link is ``resolved`` (§2) AND
    (2) the card's existing ``provenance_status`` is ``grounded`` (GOV-311). Otherwise
    ``unverified`` (DEFAULT) — so no card claims verify-at-source on a dangling locator
    (leg 1) nor on ungrounded provenance (leg 2). Always a member of
    :data:`VERIFY_AT_SOURCE_VALUES`.
    """
    if has_resolved_link and provenance_grounded:
        return VERIFY_AT_SOURCE_VERIFIABLE
    return VERIFY_AT_SOURCE_UNVERIFIED


# ---------------------------------------------------------------------------
# Per-card drill-down projection
# ---------------------------------------------------------------------------


def _record_drilldown(conn: sqlite3.Connection, record: dict[str, Any]) -> dict[str, Any]:
    """Build the verify-at-source drill-down for one served reviewer-internal record.

    The ``handle`` / ``type`` are reused VERBATIM from :mod:`stage3_card_feed`
    (:func:`feed.card_handle` / :func:`feed._resolve_record_type`) so this projection
    is a 1:1 cover of the live feed (the §4 bijection is genuine, not a re-derivation).
    Each link's displayable ``locator`` is the already-web-safe drawer entry; its
    ``resolvability_status`` is derived from the CANONICAL evidence-link row (aligned
    by ``ORDER BY evidence_link_id`` — the same order both the drawer and
    :func:`read_api._evidence_links_for` use), never from the web-safe body.
    """
    statement_id = record["statement_id"]
    card_type = feed._resolve_record_type(record)
    # Statement-level legs — computed once, shared across this statement's links.
    segment_resolves = read_api._segment_resolves(
        conn, _canonical_segment_id(conn, statement_id)
    )
    raw_ok = trace.raw_linked(conn, statement_id)
    canonical_links = read_api._evidence_links_for(conn, statement_id)
    web_drawer = record.get("evidence", [])

    links_out: list[dict[str, Any]] = []
    has_resolved_link = False
    for idx, canonical_link in enumerate(canonical_links):
        status = resolvability_status(
            conn,
            statement_id,
            canonical_link,
            segment_resolves=segment_resolves,
            raw_ok=raw_ok,
        )
        if status == RESOLVABILITY_RESOLVED:
            has_resolved_link = True
        # The web-safe drawer entry is the displayable original-source locator
        # (allowlisted keys only; already past to_web_safe + non-web-URL strip). It
        # aligns positionally with the canonical link (shared evidence_link_id order).
        locator = dict(web_drawer[idx]) if idx < len(web_drawer) else {}
        links_out.append({"locator": locator, "resolvability_status": status})

    provenance_grounded = (
        record.get("provenance_status") == read_api.PROVENANCE_GROUNDED
    )
    return {
        "handle": feed.card_handle(card_type, statement_id),
        "type": card_type,
        "jurisdiction": JURISDICTION,
        # reviewer-internal envelope keys (attached AFTER the web-safe projection):
        "provenance_status": record.get(
            "provenance_status", read_api.PROVENANCE_UNVERIFIED
        ),
        "verify_at_source_status": verify_at_source_status(
            provenance_grounded, has_resolved_link
        ),
        "links": links_out,
    }


def _gap_drilldown(gap: dict[str, Any]) -> dict[str, Any]:
    """Build the verify-at-source entry for a ``source_missing`` gap card (§4).

    A gap has no statement and no evidence, so verify-at-source is N/A by
    construction: it is labeled :data:`VERIFY_AT_SOURCE_SOURCE_MISSING`, NEVER
    ``verifiable``, and carries an empty ``links`` drawer. The ``handle`` is reused
    verbatim from :func:`feed._gap_card` so it covers the feed's gap card exactly.
    """
    return {
        "handle": feed.card_handle(feed.TYPE_SOURCE_MISSING, gap["gap_id"]),
        "type": feed.TYPE_SOURCE_MISSING,
        "jurisdiction": JURISDICTION,
        "verify_at_source_status": VERIFY_AT_SOURCE_SOURCE_MISSING,
        "links": [],
    }


def verify_at_source_cards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One drill-down per served card: record cards first, then gap cards (§4).

    A 1:1 cover of the live feed surface with NO silent drop — record cards from
    :func:`read_api.reviewer_internal_records`, then gap cards from
    :func:`read_api.completeness_gap_cards`, in the same order
    :func:`stage3_card_feed.build_card_feed` emits them.
    """
    cards: list[dict[str, Any]] = [
        _record_drilldown(conn, record)
        for record in read_api.reviewer_internal_records(conn)
    ]
    cards.extend(_gap_drilldown(gap) for gap in read_api.completeness_gap_cards(conn))
    return cards


def build_verify_at_source(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the ``{scope, access, cards[]}`` drill-down and transport-sweep it.

    Reviewer-internal lane only (B-5). The whole body is swept by
    :func:`read_api.assert_no_raw_paths` (B-4 / GOV-34 backstop), so a leak that
    slipped past the web-safe drawer fails LOUDLY at the boundary.
    """
    body: dict[str, Any] = {
        "scope": JURISDICTION,
        "access": "reviewer_internal",  # never "public" — B-5
        "cards": verify_at_source_cards(conn),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# §4 — back-gap coverage guard (extends, not forks, the feed's own guard)
# ---------------------------------------------------------------------------


class VerifyCoverageError(AssertionError):
    """Raised when the drill-down drifts from the feed's mandated card set (§4)."""


def assert_covers_surface(
    conn: sqlite3.Connection, body: dict[str, Any] | None = None
) -> bool:
    """RED unless the drill-down is a 1:1 bijection with the live feed's cards (§4).

    Independently recomputes the feed's mandated handle set via the live guard
    :func:`stage3_card_feed.expected_handles` (reused, not forked) and asserts the
    drill-down covers exactly it — a feed card missing a drill-down -> ``missing``; a
    drill-down for a card the surface does not emit -> ``extra``. Either way the guard
    goes RED, mirroring the GOV-322 / GOV-347 back-gap rule "never silently drop, never
    fabricate".
    """
    if body is None:
        body = build_verify_at_source(conn)
    projected = {card.get("handle") for card in body.get("cards", [])}
    expected = feed.expected_handles(conn)
    missing = expected - projected
    extra = projected - expected
    if missing or extra:
        raise VerifyCoverageError(
            f"verify-at-source drift vs feed: missing={sorted(missing)} "
            f"extra={sorted(extra)}"
        )
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3.07 reviewer-internal Alpine verify-at-source drill-down (GOV-376)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check-coverage",
        action="store_true",
        help="assert the drill-down covers every feed card 1:1 (back-gap guard)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        body = build_verify_at_source(conn)
        if args.check_coverage:
            assert_covers_surface(conn, body)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
