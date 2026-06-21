"""Stage 3.12 read-surface traceability + audit trail (GOV-406, Stage 3.12).

Owner: BackendCrawlerEngineer. Parent: GOV-405 (CEO Stage-3 sequencing mandate).
Read-only, deterministic, Alpine-only, reviewer-internal. This is the GOV-306 /
Stage 2.12 traceability win — :mod:`stage2_traceability` — **one layer up**, over the
live **Stage-3** read surface (exactly as GOV-393 / Stage 3.10 was the GOV-318
analogue one layer up). It **mirrors** the Stage-2 backbone and **reuses every
existing invariant by reference** — it never forks them.

The Stage-3 surface the reviewable Alpine MVP renders is two pure projections layered
on top of the already-web-safe Stage-2 read surface (``scripts/read_api.py``):

* :func:`stage3_card_feed.build_card_feed` (3.05, GOV-347) — the timeline card feed;
* :func:`stage3_verify_at_source.build_verify_at_source` (3.07, GOV-376) — the
  per-card verify-at-source drill-down (resolvability + drill locators).

For a watchdog product the trust backbone is **provenance**: every value the Stage-3
surface would surface — a timeline card, a verify-at-source resolvability status, a
drill-down locator, a correction render state, a completeness-gap card — must trace
back to a canonical source with **no orphan, no drift, no leak, no dangling
correction**. This module proves that end-to-end Stage-3 traceability invariant. Each
check **independently recomputes** the expected value from the canonical columns the
served body itself no longer carries (``to_web_safe`` strips ``segment_id`` /
``transcript_class`` / ``speaker_attribution_id`` / the raw ``to_source_id``) and
compares it to the live Stage-3 projection. A divergence flips ``clean=False``.

The five first-class checks (each a report key) — the Stage-2.12 backbone
(grounding / gap parity / transport) plus the Stage-3-specific deepeners:

1. **card_grounding** — every served Stage-3 *record card*
   (:func:`stage3_card_feed.build_card_feed`) resolves through the FULL canonical
   chain to an existing source/segment (reusing
   :func:`stage2_traceability.statement_grounded` verbatim). A served-but-ungrounded
   card is an orphan. Gap cards are exempt by construction — they ARE the
   honestly-labeled ``source_missing`` surface, never a statement claim.
2. **verify_at_source_parity** — every served ``resolvability_status`` / drill-down
   locator (the Stage 3.07 verify-at-source projection) re-derives as the OR of the
   same real GOV-306 resolvability predicates (NOT a stored optimistic value), and the
   drill-down is a 1:1 bijection with the live feed. Reuses
   :func:`stage3_verify_at_source.resolvability_status` /
   :func:`stage3_verify_at_source.assert_covers_surface` by reference, recomputing
   independently from the canonical evidence-link rows and comparing to the projection.
3. **correction_audit_trail** — the goal's core contract: every served record card
   rendered as a **correction** (``ui_status == 'corrected'``, i.e.
   ``publication.compute_ui_status`` rule #5) links back BOTH to its source (grounded)
   AND to its review evidence — a ``reviewer_decisions`` row with
   ``decision = 'corrected'`` (migration 0011, the append-only Lane-5 audit ledger
   :func:`ai_risk_gate.promote_statement` writes BEFORE it flips the claim). A served
   correction with no such audit row is a **dangling correction** — a render state that
   bypassed the sanctioned who/when/reason path. No new schema: the audit table already
   exists (GOV-406 §pass-up: pass-up is for *no* existing audit table; this is not it).
4. **completeness_gap_parity** — the projected gap-card set matches canonical
   ``completeness_gaps`` (migration 0015) 1:1 by ``gap_id`` — carried forward verbatim
   from Stage 2.12 (:func:`stage2_traceability.gap_parity`) over the SAME intermediate
   :func:`read_api.completeness_gap_cards`, and then proven to ride through the Stage-3
   feed's gap cards 1:1 by the derived handle (the Stage-3 layer drops none).
5. **transport** — the assembled Stage-3 surfaced body (feed + verify-at-source) has
   zero raw FS paths / structured PII (reuses :func:`read_api.assert_no_raw_paths` +
   the :func:`concept_map.assert_no_pii` guard verbatim).

Hard constraints (GOV-406): NO migration, NO schema change, NO mutation, NO AI, NO
network. Does NOT touch ``read_api.py`` / ``publication.py`` (``to_web_safe`` /
``WEB_SAFE_FIELD_ALLOWLIST``) or any serving behavior — additive only (new script +
new test file), reviewer-internal lane only, no public projection, no new
served/premium-gated field. Reuses existing invariants — mirrors, never forks.

No ``--apply`` → inherently dry-run; exit 1 on any break so it doubles as a CI gate.
If a real traceability/audit-trail break is found that implies a defect in shipped code
(a drifting status on ``main``, or a correction with no audit row), STOP and escalate
the failing row — the fix is a separate scoped CTO-routed issue, never a silent
self-heal (GOV-230 ABSOLUTE drift rule).

Usage:
    python scripts/stage3_traceability.py [--db PATH] [--json]
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

import concept_map as cm  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402  (read-only: lane + predicates + transport guard, no mutation)
import stage2_traceability as trace  # noqa: E402  (GOV-306 backbone, reused verbatim)
import stage3_card_feed as feed  # noqa: E402  (3.05 projection, described not forked)
import stage3_verify_at_source as vas  # noqa: E402  (3.07 projection, described not forked)

# The Lane-5 decision (migration 0011) that records a reviewed correction. The
# ai_risk_gate.promote_statement gate writes exactly this row BEFORE it sets
# statements.correction_status='corrected', so a sanctioned correction always has it.
_CORRECTION_DECISION = "corrected"
# The rendered ui_status a correction collapses to (publication.compute_ui_status
# rule #5). Imported intent — feed.STATUS_CORRECTED is the card-layer mirror.
_CORRECTED_UI_STATUS = "corrected"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Served set — the Stage-3 surface the audit runs over. The card feed is built
# ENTIRELY from reviewer_internal_records (record cards) + completeness_gap_cards
# (gap cards), so the served record set IS the reviewer-internal lane; the public
# lane carries no Stage-3 card by construction (B-5).
# ---------------------------------------------------------------------------


def _served_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every reviewer-internal served record the Stage-3 card surface projects.

    The Stage-3 card feed and verify-at-source drill-down are both 1:1 covers of
    :func:`read_api.reviewer_internal_records` (record cards) plus the gap lane; this
    is that record set — the exact set every Stage-3 record card derives from.
    """
    if not _table_exists(conn, "statements"):
        return []
    return list(read_api.reviewer_internal_records(conn))


def _record_handle(record: dict[str, Any]) -> str:
    """The derived card handle for a served record — reused verbatim from the feed."""
    return feed.card_handle(feed._resolve_record_type(record), record["statement_id"])


# ---------------------------------------------------------------------------
# Check 1 — Stage-3 record-card grounding (no orphan card).
# ---------------------------------------------------------------------------


def card_grounding(conn: sqlite3.Connection, served: list[dict[str, Any]]) -> dict[str, Any]:
    """Every served record card resolves through the FULL canonical chain (no orphan).

    Reuses :func:`stage2_traceability.statement_grounded` verbatim (the GOV-306
    full-chain predicate — a strict superset of read_api's serving gate, so a served
    card whose transcript dangles or whose evidence source was deleted is caught here).
    A served record card whose statement is ungrounded is a Stage-3 orphan: the card
    asserts a claim the canonical chain no longer backs. Gap cards are exempt — they
    are the honestly-labeled ``source_missing`` surface, not a statement claim.
    """
    orphans = [
        {"statement_id": rec["statement_id"], "handle": _record_handle(rec)}
        for rec in served
        if not trace.statement_grounded(conn, rec["statement_id"])
    ]
    return {"checked": len(served), "orphans": orphans, "clean": not orphans}


# ---------------------------------------------------------------------------
# Check 2 — verify-at-source parity (resolvability derived, not stored; bijection).
# ---------------------------------------------------------------------------


def verify_at_source_parity(
    conn: sqlite3.Connection, served: list[dict[str, Any]], verify_body: dict[str, Any]
) -> dict[str, Any]:
    """Every projected resolvability re-derives from real predicates + 1:1 with the feed.

    Two legs, both reusing the Stage 3.07 invariants by reference (never forked):

    * **bijection** — :func:`stage3_verify_at_source.assert_covers_surface` proves the
      drill-down covers exactly the live feed's card set (no silent drop, no fabricated
      card).
    * **resolvability derived, not stored** — for every served record card, recompute
      each link's resolvability INDEPENDENTLY from the canonical evidence-link rows via
      :func:`stage3_verify_at_source.resolvability_status` (which reuses the GOV-306
      predicates :func:`read_api._segment_resolves` / :func:`stage2_traceability.raw_linked`
      / the per-link ``to_source_id -> sources`` branch) and assert the recomputed list
      EQUALS the projected list — element by element, aligned by ``evidence_link_id``
      order. A stored/fabricated optimistic ``resolved`` would diverge; a locator with no
      canonical link (or vice-versa) makes the lists differ in length. This is the
      locator <-> canonical-id bijection per link.
    """
    try:
        vas.assert_covers_surface(conn, verify_body)
        bijection_ok = True
        bijection_detail = ""
    except vas.VerifyCoverageError as exc:
        bijection_ok = False
        bijection_detail = str(exc)

    drill_by_handle = {c.get("handle"): c for c in verify_body.get("cards", [])}
    mismatches: list[dict[str, Any]] = []
    for rec in served:
        statement_id = rec["statement_id"]
        handle = _record_handle(rec)
        projected = drill_by_handle.get(handle)
        if projected is None:
            mismatches.append({"handle": handle, "reason": "record card missing from drill-down"})
            continue
        # Independent recompute from canonical columns — shares only the predicate
        # functions with the projection, never its state.
        seg = read_api._segment_resolves(conn, vas._canonical_segment_id(conn, statement_id))
        raw_ok = trace.raw_linked(conn, statement_id)
        canonical_links = read_api._evidence_links_for(conn, statement_id)
        expected = [
            vas.resolvability_status(conn, statement_id, link, segment_resolves=seg, raw_ok=raw_ok)
            for link in canonical_links
        ]
        got = [link.get("resolvability_status") for link in projected.get("links", [])]
        if expected != got:
            mismatches.append({"handle": handle, "expected": expected, "got": got})
    return {
        "checked": len(served),
        "bijection_ok": bijection_ok,
        "bijection_detail": bijection_detail,
        "mismatches": mismatches,
        "clean": bijection_ok and not mismatches,
    }


# ---------------------------------------------------------------------------
# Check 3 — correction audit trail (no dangling correction).
# ---------------------------------------------------------------------------


def _has_correction_decision(conn: sqlite3.Connection, statement_id: str) -> bool:
    """True iff a ``reviewer_decisions`` row records a 'corrected' decision (audit row).

    The Lane-5 ledger (migration 0011) is append-only and written by the sanctioned
    :func:`ai_risk_gate.promote_statement` gate BEFORE the claim's correction_status is
    flipped — so a legitimately-corrected card always has this who/when/reason audit row.
    Fail-closed: a missing table or no matching row -> no audit trail.
    """
    if not _table_exists(conn, "reviewer_decisions"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM reviewer_decisions WHERE statement_id = ? AND decision = ? LIMIT 1",
            (statement_id, _CORRECTION_DECISION),
        ).fetchone()
        is not None
    )


def correction_audit_trail(
    conn: sqlite3.Connection, served: list[dict[str, Any]]
) -> dict[str, Any]:
    """Every served correction links back to its source AND its review-evidence row.

    A served record card renders as a correction iff ``ui_status == 'corrected'``
    (``publication.compute_ui_status`` rule #5: reviewed AND
    ``correction_status == 'corrected'``). For each such card the goal's core contract
    requires BOTH:

    * **source link** — the statement is grounded (:func:`stage2_traceability.statement_grounded`),
      so the correction rests on a real source/segment, never an orphan; AND
    * **review evidence** — a ``reviewer_decisions`` row with ``decision = 'corrected'``
      (:func:`_has_correction_decision`), the reviewer-internal audit row recording who
      corrected it, when, and why.

    A served correction missing EITHER is a **dangling correction** — a correction render
    state that bypassed the sanctioned audit path. Listed with the specific breach so the
    failing row can be escalated, never silently healed (GOV-230).
    """
    dangling: list[dict[str, Any]] = []
    corrections = 0
    for rec in served:
        if rec.get("ui_status") != _CORRECTED_UI_STATUS:
            continue
        corrections += 1
        statement_id = rec["statement_id"]
        grounded = trace.statement_grounded(conn, statement_id)
        has_evidence = _has_correction_decision(conn, statement_id)
        if not (grounded and has_evidence):
            dangling.append(
                {
                    "statement_id": statement_id,
                    "handle": _record_handle(rec),
                    "grounded": grounded,
                    "has_review_evidence": has_evidence,
                }
            )
    return {
        "checked": len(served),
        "corrections": corrections,
        "dangling": dangling,
        "clean": not dangling,
    }


# ---------------------------------------------------------------------------
# Check 4 — completeness_gap parity (carry forward Stage 2.12 + Stage-3 cover).
# ---------------------------------------------------------------------------


def gap_parity_stage3(
    conn: sqlite3.Connection, feed_body: dict[str, Any]
) -> dict[str, Any]:
    """Canonical ``completeness_gaps`` 1:1 with the projection, through the Stage-3 feed.

    Two layers, no fork:

    * **Stage 2.12 verbatim** — :func:`stage2_traceability.gap_parity` over the SAME
      intermediate :func:`read_api.completeness_gap_cards` (canonical-table-vs-projection
      parity by ``gap_id`` + the ``no_primary_source`` headline-count parity).
    * **Stage-3 cover** — every intermediate gap card must ride through the Stage-3 feed
      as a gap card, matched 1:1 by the derived handle
      (``card_handle(TYPE_SOURCE_MISSING, gap_id)`` — an injective function of ``gap_id``).
      A gap silently dropped by the Stage-3 layer makes ``feed_missing`` non-empty.
    """
    intermediate = (
        read_api.completeness_gap_cards(conn)
        if _table_exists(conn, "completeness_gaps")
        else []
    )
    base = trace.gap_parity(conn, intermediate)

    expected_gap_handles = {
        feed.card_handle(feed.TYPE_SOURCE_MISSING, gap["gap_id"]) for gap in intermediate
    }
    feed_gap_handles = {
        c.get("handle")
        for c in feed_body.get("cards", [])
        if c.get("type") == feed.TYPE_SOURCE_MISSING
    }
    feed_missing = sorted(expected_gap_handles - feed_gap_handles)
    feed_phantom = sorted(feed_gap_handles - expected_gap_handles)

    return {
        **base,
        "feed_gap_count": len(feed_gap_handles),
        "feed_missing": feed_missing,
        "feed_phantom": feed_phantom,
        "clean": base["clean"] and not feed_missing and not feed_phantom,
    }


# ---------------------------------------------------------------------------
# Check 5 — transport guard over the assembled Stage-3 body (no raw path / PII).
# ---------------------------------------------------------------------------


def transport_clean(
    conn: sqlite3.Connection, feed_body: dict[str, Any], verify_body: dict[str, Any]
) -> dict[str, Any]:
    """Sweep the assembled Stage-3 surfaced body for raw paths / structured PII.

    Runs the two independent guards over the composed Stage-3 body (feed +
    verify-at-source, the surfaces the reviewer actually sees): the GOV-34
    :func:`read_api.assert_no_raw_paths` transport sweep and the concept-map
    :func:`concept_map.assert_no_pii` structured-PII sweep over the serialized body.
    Reused verbatim from the Stage-2 transport check — mirrors, never forks.
    """
    body = {"feed": feed_body, "verify_at_source": verify_body}
    try:
        read_api.assert_no_raw_paths(body)
        cm.assert_no_pii(json.dumps(body, sort_keys=True), "stage3_traceability.transport")
    except (read_api.RawPathLeak, cm.PiiGuardError) as exc:
        return {"clean": False, "error": str(exc)}
    return {"clean": True, "error": None}


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def audit_stage3_traceability(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run all five Stage-3 read-surface traceability checks. Returns a JSON-able report.

    ``clean`` is the conjunction of every check. Read-only: builds the live Stage-3
    projections (feed + verify-at-source), recomputes each expected value from canonical
    columns, compares, and reports drift — it never writes.
    """
    served = _served_records(conn)
    feed_body = feed.build_card_feed(conn)
    verify_body = vas.build_verify_at_source(conn)

    grounding = card_grounding(conn, served)
    verify = verify_at_source_parity(conn, served, verify_body)
    corrections = correction_audit_trail(conn, served)
    gaps = gap_parity_stage3(conn, feed_body)
    transport = transport_clean(conn, feed_body, verify_body)

    clean = all(c["clean"] for c in (grounding, verify, corrections, gaps, transport))
    return {
        "served_count": len(served),
        "card_grounding": grounding,
        "verify_at_source_parity": verify,
        "correction_audit_trail": corrections,
        "completeness_gap_parity": gaps,
        "transport": transport,
        "clean": clean,
    }


def _format_report(report: dict[str, Any], db_path: Path) -> str:
    g = report["card_grounding"]
    v = report["verify_at_source_parity"]
    x = report["correction_audit_trail"]
    p = report["completeness_gap_parity"]
    t = report["transport"]
    lines = [
        f"Stage 3 read-surface traceability + audit trail (GOV-406) — {db_path}",
        f"  served record cards                  : {report['served_count']}",
        f"  1 card->source grounding             : {'OK' if g['clean'] else f'BREAK ({len(g['orphans'])} orphan)'}",
        f"  2 verify-at-source parity            : {'OK' if v['clean'] else f'DRIFT ({len(v['mismatches'])} mismatch, bijection={v['bijection_ok']})'}",
        f"  3 correction audit trail             : {'OK' if x['clean'] else f'DANGLING ({len(x['dangling'])})'} "
        f"(corrections={x['corrections']})",
        f"  4 completeness_gap parity            : {'OK' if p['clean'] else 'BREAK'} "
        f"(canonical={p['canonical_count']} projected={p['projected_count']} "
        f"feed_gaps={p['feed_gap_count']} no_primary_source={p['no_primary_source_count']})",
        f"  5 transport (no raw path / PII)      : {'OK' if t['clean'] else 'LEAK'}",
        f"  TRACEABLE / CLEAN                     : {report['clean']}",
    ]
    for o in g["orphans"]:
        lines.append(f"    ORPHAN card: {o['statement_id']} ({o['handle']})")
    if not v["bijection_ok"]:
        lines.append(f"    VERIFY BIJECTION BREAK: {v['bijection_detail']}")
    for m in v["mismatches"]:
        lines.append(f"    RESOLVABILITY DRIFT: {m.get('handle')} {m}")
    for d in x["dangling"]:
        lines.append(
            f"    DANGLING correction: {d['statement_id']} "
            f"(grounded={d['grounded']} review_evidence={d['has_review_evidence']})"
        )
    for gid in p["missing"]:
        lines.append(f"    GAP MISSING (not projected): {gid}")
    for gid in p["phantom"]:
        lines.append(f"    GAP PHANTOM (no canonical row): {gid}")
    for h in p["feed_missing"]:
        lines.append(f"    GAP DROPPED BY FEED: {h}")
    for h in p["feed_phantom"]:
        lines.append(f"    GAP PHANTOM ON FEED: {h}")
    if not t["clean"]:
        lines.append(f"    TRANSPORT LEAK: {t['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3 read-surface traceability + audit trail (read-only). GOV-406 Stage 3.12."
    )
    parser.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help=f"path to the sqlite DB (default: {db.DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable JSON report"
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"stage3 traceability audit: DB not found at {args.db}", file=sys.stderr)
        return 2

    with db.open_db(args.db) as conn:
        report = audit_stage3_traceability(conn)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report, args.db))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
