"""Stage 3.07 verify-at-source read-time auditor (GOV-376) — reviewer-internal.

Proves the GOV-375 contract (``Docs/stage3-07-verify-at-source-contract.md``) over
the live Alpine surface, mirroring :mod:`stage3_preservation_audit` /
:mod:`stage3_source_inventory`. It *reads* the :mod:`stage3_verify_at_source`
projection and asserts the five verify-at-source invariants — it re-implements none
of the projection (extend-not-fork):

* **VS-1 completeness / back-gap** — every served card is verify-at-source-capable
  (``verifiable``) OR honestly labeled (``unverified`` / ``source_missing``), and the
  drill-down is a 1:1 bijection with the live feed (never silently dropped). Delegated
  to :func:`stage3_verify_at_source.assert_covers_surface`.
* **VS-2 no dangling claim** — no card claims ``verifiable`` without ≥1 ``resolved``
  link (no verify-at-source on a dangling/unresolved locator).
* **VS-3 derived, never stored / fabricated** — resolvability is recomputed
  independently from the real GOV-306 predicates and matches the projection; the
  module binds the real ``read_api`` / ``stage2_traceability`` (not a stand-in).
* **VS-4 web-safe 0-diff / lane separation** — the body carries no raw path
  (``assert_no_raw_paths``), and the public lane emits NO verify-at-source key
  (reviewer-internal only).
* **VS-5 frozen fail-closed SSOT vocab** — every link status ∈
  ``RESOLVABILITY_VALUES``, every record-card status ∈ ``VERIFY_AT_SOURCE_VALUES``,
  every gap card is exactly ``source_missing``.

Separate ADDITIVE module: ``read_api.py`` / ``publication.py`` stay 0-diff. CLI exits
0 (all invariants hold) / 1 (any breach) — the read-time proof, mirroring the
GOV-332 escalation auditor.
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
import read_api  # noqa: E402  (read-only: lane + transport guard, no mutation)
import stage2_traceability as trace  # noqa: E402  (read-only: GOV-306 predicates)
import stage3_verify_at_source as vas  # noqa: E402  (the projection — described, never forked)


# ---------------------------------------------------------------------------
# Per-invariant checks (each returns a structured verdict; never raises on a
# data breach — the auditor SURFACES defects, it does not abort the read).
# ---------------------------------------------------------------------------


def check_vs1_completeness(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    """VS-1: every card honestly labeled + 1:1 bijection with the live feed."""
    try:
        vas.assert_covers_surface(conn, body)
        bijection_ok = True
        detail = ""
    except vas.VerifyCoverageError as exc:
        bijection_ok = False
        detail = str(exc)

    honest = {
        vas.VERIFY_AT_SOURCE_VERIFIABLE,
        vas.VERIFY_AT_SOURCE_UNVERIFIED,
        vas.VERIFY_AT_SOURCE_SOURCE_MISSING,
    }
    unlabeled = [
        card.get("handle")
        for card in body.get("cards", [])
        if card.get("verify_at_source_status") not in honest
    ]
    ok = bijection_ok and not unlabeled
    return {"ok": ok, "bijection_ok": bijection_ok, "unlabeled": unlabeled, "detail": detail}


def check_vs2_no_dangling_claim(body: dict[str, Any]) -> dict[str, Any]:
    """VS-2: no ``verifiable`` card without ≥1 ``resolved`` link."""
    offenders: list[str] = []
    for card in body.get("cards", []):
        if card.get("verify_at_source_status") != vas.VERIFY_AT_SOURCE_VERIFIABLE:
            continue
        has_resolved = any(
            link.get("resolvability_status") == vas.RESOLVABILITY_RESOLVED
            for link in card.get("links", [])
        )
        if not has_resolved:
            offenders.append(card.get("handle"))
    return {"ok": not offenders, "offenders": offenders}


def check_vs3_derived_not_fabricated(
    conn: sqlite3.Connection, body: dict[str, Any]
) -> dict[str, Any]:
    """VS-3: resolvability recomputed independently from real predicates matches.

    Re-derives every record card's per-link resolvability straight from the canonical
    GOV-306 predicates (not from the projected body) and asserts it equals what the
    projection emitted — a stored/fabricated flag would diverge. Also asserts the
    projection binds the REAL predicate modules (not a monkeypatched stand-in).
    """
    bound_ok = vas.read_api is read_api and vas.trace is trace
    mismatches: list[dict[str, Any]] = []

    drill_by_handle = {card.get("handle"): card for card in body.get("cards", [])}
    for record in read_api.reviewer_internal_records(conn):
        statement_id = record["statement_id"]
        card_type = vas.feed._resolve_record_type(record)
        handle = vas.feed.card_handle(card_type, statement_id)
        projected = drill_by_handle.get(handle)
        if projected is None:
            mismatches.append({"handle": handle, "reason": "missing from drill-down"})
            continue
        # Independent recompute from canonical columns (no shared state with the
        # projection beyond the predicate functions themselves).
        seg = read_api._segment_resolves(
            conn, vas._canonical_segment_id(conn, statement_id)
        )
        raw_ok = trace.raw_linked(conn, statement_id)
        canonical_links = read_api._evidence_links_for(conn, statement_id)
        expected = [
            vas.resolvability_status(
                conn, statement_id, link, segment_resolves=seg, raw_ok=raw_ok
            )
            for link in canonical_links
        ]
        got = [link.get("resolvability_status") for link in projected.get("links", [])]
        if expected != got:
            mismatches.append({"handle": handle, "expected": expected, "got": got})
    return {"ok": bound_ok and not mismatches, "bound_ok": bound_ok, "mismatches": mismatches}


def check_vs4_lane_and_no_leak(conn: sqlite3.Connection, body: dict[str, Any]) -> dict[str, Any]:
    """VS-4: reviewer-internal lane only + no raw path crosses (web-safe 0-diff)."""
    # Transport backstop over the assembled body (raises LOUDLY on a leak).
    try:
        read_api.assert_no_raw_paths(body)
        no_raw_path = True
        detail = ""
    except read_api.RawPathLeak as exc:
        no_raw_path = False
        detail = str(exc)

    lane_ok = body.get("access") == "reviewer_internal"

    # The public lane must carry NO verify-at-source key — by construction the
    # projection lives in this separate reviewer-internal module, never on
    # build_response's public default.
    public = read_api.build_response(conn)
    public_blob = json.dumps(public)
    public_clean = (
        "verify_at_source_status" not in public_blob
        and "resolvability_status" not in public_blob
    )
    return {
        "ok": no_raw_path and lane_ok and public_clean,
        "no_raw_path": no_raw_path,
        "lane_ok": lane_ok,
        "public_clean": public_clean,
        "detail": detail,
    }


def check_vs5_frozen_vocab(body: dict[str, Any]) -> dict[str, Any]:
    """VS-5: every status is a member of the frozen, fail-closed SSOT vocabulary."""
    bad_links: list[str] = []
    bad_cards: list[str] = []
    for card in body.get("cards", []):
        status = card.get("verify_at_source_status")
        if card.get("type") == vas.feed.TYPE_SOURCE_MISSING:
            if status != vas.VERIFY_AT_SOURCE_SOURCE_MISSING:
                bad_cards.append(card.get("handle"))
        elif status not in vas.VERIFY_AT_SOURCE_VALUES:
            bad_cards.append(card.get("handle"))
        for link in card.get("links", []):
            if link.get("resolvability_status") not in vas.RESOLVABILITY_VALUES:
                bad_links.append(card.get("handle"))
    return {"ok": not bad_links and not bad_cards, "bad_links": bad_links, "bad_cards": bad_cards}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run VS-1..5 over the live drill-down; return a structured verdict."""
    body = vas.build_verify_at_source(conn)
    checks = {
        "VS-1": check_vs1_completeness(conn, body),
        "VS-2": check_vs2_no_dangling_claim(body),
        "VS-3": check_vs3_derived_not_fabricated(conn, body),
        "VS-4": check_vs4_lane_and_no_leak(conn, body),
        "VS-5": check_vs5_frozen_vocab(body),
    }
    return {
        "scope": vas.JURISDICTION,
        "access": "reviewer_internal",
        "card_count": len(body.get("cards", [])),
        "clean": all(c["ok"] for c in checks.values()),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3.07 reviewer-internal verify-at-source auditor (GOV-376)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        result = run_audit(conn)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(_main())
