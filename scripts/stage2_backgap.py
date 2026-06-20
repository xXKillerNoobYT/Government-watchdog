"""Stage 2 read-surface back-gap / coverage-regression auditor (GOV-322, Stage 2.13).

Owner: BackendCrawlerEngineer. Parent: GOV-321 (CTO sequencing). Read-only,
deterministic, Alpine-only. Mirrors the proven auditor pattern of
:mod:`stage2_traceability` (GOV-306): open the DB, run SELECTs, never write —
there is no ``--apply``, so it is inherently dry-run and doubles as a CI gate
(exit 1 on any back-gap).

Two directions of read-surface trust are already proven on ``main``:

* **Composition** (GOV-318) — the five Stage-2 overlays are co-present,
  fail-closed, no cross-lane leak, deterministic.
* **Forward traceability** (GOV-306, :mod:`stage2_traceability`) — every value the
  read surface *serves* traces back to a canonical source with no orphan / drift /
  leak.

The **unproven inverse** this module closes is *coverage completeness*: does every
canonical record that **should** reach a reviewer/public lane **still** get served?
A forward auditor passes even if an overlay's fail-closed default silently dropped a
whole class of records — every *remaining* served row is still grounded. That
silent-shrinkage blind spot ("back-gap") is the highest-severity trust failure for a
first external reviewer: the surface looks complete while showing *less* than reality.
This auditor proves no earlier-stage Alpine coverage silently regressed under the five
Stage-2 overlays.

The six first-class checks (each a report key; CLI exit 1 if any non-clean):

1. **reviewer_lane_no_backgap** — INDEPENDENTLY recompute the set of canonical
   statements eligible for the reviewer-internal lane (from the canonical columns +
   the SSOT ledger predicates, NOT by calling
   :func:`read_api.reviewer_internal_records`'s own assembly gate) and assert the
   served set covers it: ``eligible - served`` is empty. A served set smaller than
   the eligible set is a back-gap. Recomputing independently is essential — calling
   the same assembly would compare it to itself and let a regression that drops rows
   shrink both sides equally and pass silently.
2. **public_lane_no_backgap** — the same reconciliation for
   :func:`read_api.published_records` vs an independently-recomputed publish-eligible
   set (``ui_status`` eligible AND ``publication_state='publishable'`` AND not orphan).
3. **completeness_gap_coverage_parity** — every canonical ``completeness_gaps``
   (migration 0015) row still surfaces 1:1 by ``gap_id`` via
   :func:`read_api.completeness_gap_cards`; no gap silently dropped (the inverse
   complement of GOV-306's drift check, whose ``missing`` direction this hardens).
4. **overlay_presence_no_regression** — for every served reviewer-internal record,
   all five Stage-2 overlays are present/non-missing: the four per-record envelope
   keys (``ui_status``, ``confidence_label``, ``speaker_label``, ``provenance_status``)
   each present + SSOT-bounded, plus the per-record evidence/source linkage drawer
   (``evidence``); and the body-level completeness-gap surface composes. An overlay
   must not silently stop attaching to a class of records.
5. **stage1_field_floor** — the Stage-1 base fields each served record carried before
   Stage-2 layering (the 10-value ``ui_status`` base + source/transcript linkage)
   remain present — no Stage-2 overlay silently removed a Stage-1 field.
6. **determinism + read-only** — two independent snapshot passes are byte-identical,
   and the module performs zero writes (no ``--apply``; SELECT-only by construction).

Hard constraints (GOV-322): NO migration, NO schema change, NO mutation, NO AI, NO
network, NO new envelope key, NO public-projection change. **0 production diff** to
``read_api.py`` / ``publication.py`` — this is an additive module + test only.
**Imports and reuses :mod:`read_api` functions verbatim** — it never reimplements the
serving/serialization gates; it only recomputes the *eligible membership set*
independently and reconciles it against what ``read_api`` actually serves.

If a real back-gap is found that implies a defect in shipped code (e.g. an overlay's
fail-closed default dropping a record class on ``main``), STOP and escalate the
failing rows — the fix is a SEPARATE scoped issue, never a silent self-heal or a
patch to ``read_api``/``publication`` from this ticket (GOV-322 pass-up trigger;
GOV-230 ABSOLUTE drift rule). This ticket ships the net, not the patch.

Usage:
    python scripts/stage2_backgap.py [--db PATH] [--json]
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

import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import transcript_class as tc  # noqa: E402

# Imported from the SSOT — never re-declared, so the auditor cannot drift from the
# values read_api actually projects. The 10-value ui_status base (the Stage-1 floor),
# the publish allowlist, the confidence-label range, and the provenance vocabulary all
# come straight from their owning modules.
_UI_STATUS_BASE = pub.ALLOWED_UI_STATUSES
_ELIGIBLE_UI_STATUSES = pub.PUBLICATION_ELIGIBLE_UI_STATUSES
_VALID_CONFIDENCE_LABELS = frozenset(tc.CONFIDENCE_LABEL_BY_CLASS.values())
_PROVENANCE_VALUES = read_api.PROVENANCE_STATUS_VALUES

# The four per-record envelope overlays that MUST attach to every served
# reviewer-internal record, plus the evidence/source linkage drawer. Together with
# the body-level completeness-gap surface these are the five Stage-2 overlays GOV-318
# proved co-present; this auditor proves none silently stops attaching.
_REQUIRED_OVERLAY_KEYS = ("ui_status", "confidence_label", "speaker_label", "provenance_status")
# The Stage-1 base fields a served record carried before any Stage-2 layering. A
# Stage-2 overlay that silently dropped one of these is a back-gap of its own.
_STAGE1_FLOOR_KEYS = ("statement_id", "ui_status", "evidence")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Independent eligibility recompute — the canonical "should be served" set,
# rebuilt WITHOUT calling read_api's assembly functions. Reuses only the stable
# leaf predicates (ledger queries, ui_status derivation, SSOT constants), never
# the membership loop being audited.
# ---------------------------------------------------------------------------


def reviewer_eligible_ids(conn: sqlite3.Connection) -> set[str]:
    """The canonical statement ids that SHOULD reach the reviewer-internal lane.

    Recomputed independently of :func:`read_api.reviewer_internal_records`: its own
    ``SELECT … ORDER BY`` loop is mirrored here over the canonical ``statements`` rows,
    reusing the stable SSOT leaf predicates (the Lane-5 ledger queries
    :func:`ai_risk_gate.latest_decision` / :func:`ai_risk_gate.open_risk_flags`, the
    reviewed-status frozenset, the publish-eligible ui_status set, the producing-run
    health check, and the orphan check) — but assembling the membership SET here, not
    by calling the audited function. A row is eligible iff EVERY clause holds:

    * ``publication_state`` is NOT ``publishable`` (a publishable row is the public
      lane's, never the reviewer view's);
    * ``verification_status`` is a reviewed value;
    * a promoting reviewer decision exists in the Lane-5 ledger;
    * no unresolved no-go risk flag remains;
    * the producing gateway run (if any) is healthy;
    * the re-derived ``ui_status`` is publication-eligible (source-backed); and
    * the row is not orphaned (a segment edge OR >=1 evidence pointer).

    Returning a SET (not the serialized records) keeps this a pure membership oracle:
    a divergence from what ``read_api`` actually serves is a coverage regression, not a
    serialization artifact.
    """
    if not _table_exists(conn, "statements"):
        return set()
    eligible: set[str] = set()
    for row in conn.execute("SELECT * FROM statements ORDER BY statement_id"):
        record = dict(row)
        statement_id = record["statement_id"]
        if record.get("publication_state") == "publishable":
            continue
        if record.get("verification_status") not in pub.REVIEWED_VERIFICATION_STATUSES:
            continue
        decision = gate.latest_decision(conn, statement_id)
        if not decision or not decision.get("promoted"):
            continue
        if gate.open_risk_flags(conn, statement_id):
            continue
        if not read_api._producing_run_ok(conn, record):
            continue
        links = read_api._evidence_links_for(conn, statement_id)
        ui_status = read_api._eligible_ui_status(record, links)
        if ui_status not in _ELIGIBLE_UI_STATUSES:
            continue
        if not (read_api._segment_resolves(conn, record.get("segment_id")) or links):
            continue
        eligible.add(statement_id)
    return eligible


def publish_eligible_ids(conn: sqlite3.Connection) -> set[str]:
    """The canonical statement ids that SHOULD reach the public (published) lane.

    Recomputed independently of :func:`read_api.published_records`: both gates must
    agree (re-derived ``ui_status`` publication-eligible AND ``publication_state`` is
    ``publishable``) and the row must not be orphaned. On a pre-publish Alpine DB this
    is empty (nothing is owner-published), so served and eligible are both 0 — but the
    reconciliation still catches a future regression that drops a published row.
    """
    if not _table_exists(conn, "statements"):
        return set()
    eligible: set[str] = set()
    for row in conn.execute("SELECT * FROM statements ORDER BY statement_id"):
        record = dict(row)
        statement_id = record["statement_id"]
        links = read_api._evidence_links_for(conn, statement_id)
        ui_status = read_api._eligible_ui_status(record, links)
        if ui_status not in _ELIGIBLE_UI_STATUSES:
            continue
        if record.get("publication_state") != "publishable":
            continue
        if not (read_api._segment_resolves(conn, record.get("segment_id")) or links):
            continue
        eligible.add(statement_id)
    return eligible


# ---------------------------------------------------------------------------
# Check 1 / 2 — lane back-gap reconciliation (independent eligible vs served).
# ---------------------------------------------------------------------------


def _lane_backgap(eligible: set[str], served: set[str]) -> dict[str, Any]:
    """Reconcile an independently-recomputed eligible set against the served set.

    ``backgap`` = eligible-but-not-served (the silent-shrinkage failure this auditor
    exists to catch). ``over_served`` = served-but-not-eligible (the forward-direction
    concern GOV-306 owns) — surfaced for visibility but it does NOT, by itself, flip
    this auditor's verdict; ``clean`` is governed by the back-gap direction alone.
    """
    backgap = sorted(eligible - served)
    over_served = sorted(served - eligible)
    return {
        "eligible_count": len(eligible),
        "served_count": len(served),
        "backgap": backgap,
        "over_served": over_served,
        "clean": not backgap,
    }


def reviewer_lane_no_backgap(conn: sqlite3.Connection) -> dict[str, Any]:
    eligible = reviewer_eligible_ids(conn)
    served = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    return _lane_backgap(eligible, served)


def public_lane_no_backgap(conn: sqlite3.Connection) -> dict[str, Any]:
    eligible = publish_eligible_ids(conn)
    served = {r["statement_id"] for r in read_api.published_records(conn)}
    return _lane_backgap(eligible, served)


# ---------------------------------------------------------------------------
# Check 3 — completeness-gap coverage parity (no gap silently dropped).
# ---------------------------------------------------------------------------


def completeness_gap_coverage_parity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assert every canonical ``completeness_gaps`` row still surfaces 1:1 by ``gap_id``.

    The inverse complement of GOV-306's gap-drift check: GOV-298's
    :func:`read_api.completeness_gap_cards` emits EVERY row (fail-closed but never
    hidden — GOV-125's "never silently dropped" rule), so a canonical gap absent from
    the projection is a coverage back-gap. The ``no_primary_source`` headline count is
    reconciled too so the surfaced "~90 meetings" figure can never silently shrink.
    """
    if not _table_exists(conn, "completeness_gaps"):
        return {
            "canonical_count": 0, "projected_count": 0,
            "no_primary_source_canonical": 0, "no_primary_source_projected": 0,
            "dropped": [], "clean": True,
        }
    canonical_ids = {r[0] for r in conn.execute("SELECT gap_id FROM completeness_gaps")}
    canonical_nps = conn.execute(
        "SELECT count(*) FROM completeness_gaps WHERE gap_type = 'no_primary_source'"
    ).fetchone()[0]
    cards = read_api.completeness_gap_cards(conn)
    projected_ids = {c["gap_id"] for c in cards}
    projected_nps = sum(1 for c in cards if c.get("gap_type") == "no_primary_source")
    dropped = sorted(canonical_ids - projected_ids)
    return {
        "canonical_count": len(canonical_ids),
        "projected_count": len(projected_ids),
        "no_primary_source_canonical": canonical_nps,
        "no_primary_source_projected": projected_nps,
        "dropped": dropped,
        "clean": not dropped and canonical_nps == projected_nps,
    }


# ---------------------------------------------------------------------------
# Check 4 — overlay presence (no overlay silently stops attaching).
# ---------------------------------------------------------------------------


def _overlay_bounded(record: dict[str, Any]) -> list[str]:
    """Return the list of overlay-presence violations for one served record (empty == OK).

    Each of the four per-record envelope overlays must be present AND inside its
    SSOT-bounded range (a present-but-``None`` value is a silent regression, caught by
    the bounds check), and the evidence/source linkage drawer must be a present list.
    """
    out: list[str] = []
    for key in _REQUIRED_OVERLAY_KEYS:
        if key not in record:
            out.append(f"overlay {key!r} absent")
    if record.get("ui_status") not in _UI_STATUS_BASE:
        out.append(f"ui_status off-SSOT: {record.get('ui_status')!r}")
    if record.get("confidence_label") not in _VALID_CONFIDENCE_LABELS:
        out.append(f"confidence_label off-SSOT: {record.get('confidence_label')!r}")
    label = record.get("speaker_label")
    if not isinstance(label, str) or not label.strip():
        out.append(f"speaker_label missing/empty: {label!r}")
    if record.get("provenance_status") not in _PROVENANCE_VALUES:
        out.append(f"provenance_status off-SSOT: {record.get('provenance_status')!r}")
    if not isinstance(record.get("evidence"), list):
        out.append("evidence drawer missing")
    return out


def overlay_presence_no_regression(conn: sqlite3.Connection) -> dict[str, Any]:
    served = read_api.reviewer_internal_records(conn)
    missing: list[dict[str, Any]] = []
    for record in served:
        violations = _overlay_bounded(record)
        if violations:
            missing.append(
                {"statement_id": record.get("statement_id"), "violations": violations}
            )
    # The fifth GOV-318 overlay is the body-level completeness-gap surface; assert it
    # still composes (a list, never silently None) when the table is present.
    gap_surface_present = True
    if _table_exists(conn, "completeness_gaps"):
        gap_surface_present = isinstance(read_api.completeness_gap_cards(conn), list)
    return {
        "checked": len(served),
        "missing": missing,
        "gap_surface_present": gap_surface_present,
        "clean": not missing and gap_surface_present,
    }


# ---------------------------------------------------------------------------
# Check 5 — Stage-1 field floor (no Stage-2 overlay removed a Stage-1 field).
# ---------------------------------------------------------------------------


def stage1_field_floor(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assert every served record still carries its Stage-1 base fields.

    The Stage-2 overlays are additive envelope keys; none may strip the Stage-1 floor.
    The floor is the stable record identity (``statement_id``), the 10-value
    ``ui_status`` base (the Stage-1 trust vocabulary, bounded to
    :data:`_UI_STATUS_BASE`), and the source/transcript linkage drawer (``evidence``).
    A served record missing any of these has silently regressed below Stage-1.
    """
    served = read_api.reviewer_internal_records(conn)
    breaches: list[dict[str, Any]] = []
    for record in served:
        violations = [k for k in _STAGE1_FLOOR_KEYS if k not in record]
        if record.get("ui_status") not in _UI_STATUS_BASE:
            violations.append(f"ui_status off 10-value base: {record.get('ui_status')!r}")
        if violations:
            breaches.append(
                {"statement_id": record.get("statement_id"), "violations": violations}
            )
    return {"checked": len(served), "breaches": breaches, "clean": not breaches}


# ---------------------------------------------------------------------------
# Check 6 — determinism + read-only.
# ---------------------------------------------------------------------------


def _snapshot(conn: sqlite3.Connection) -> str:
    """A byte-stable JSON snapshot of every coverage set this auditor reconciles.

    Two snapshots over the same DB must be identical — the read surface is a pure
    function of stored fields. Sorting every set makes the comparison order-stable.
    """
    return json.dumps(
        {
            "reviewer_eligible": sorted(reviewer_eligible_ids(conn)),
            "reviewer_served": sorted(
                r["statement_id"] for r in read_api.reviewer_internal_records(conn)
            ),
            "publish_eligible": sorted(publish_eligible_ids(conn)),
            "publish_served": sorted(
                r["statement_id"] for r in read_api.published_records(conn)
            ),
            "gap_projected": sorted(
                c["gap_id"] for c in (
                    read_api.completeness_gap_cards(conn)
                    if _table_exists(conn, "completeness_gaps") else []
                )
            ),
        },
        sort_keys=True,
    )


def determinism_read_only(conn: sqlite3.Connection) -> dict[str, Any]:
    """Two snapshot passes byte-identical, and the DB row counts are unchanged.

    The read-only leg is structural (the module issues no INSERT/UPDATE/DELETE), but
    asserting the ``statements`` + ``completeness_gaps`` row counts are unchanged across
    the two passes is a cheap, positive proof that running the audit mutated nothing.
    """
    def _counts() -> tuple[int, int]:
        s = (
            conn.execute("SELECT count(*) FROM statements").fetchone()[0]
            if _table_exists(conn, "statements") else 0
        )
        g = (
            conn.execute("SELECT count(*) FROM completeness_gaps").fetchone()[0]
            if _table_exists(conn, "completeness_gaps") else 0
        )
        return s, g

    before = _counts()
    first = _snapshot(conn)
    second = _snapshot(conn)
    after = _counts()
    return {
        "byte_identical": first == second,
        "row_counts_stable": before == after,
        "clean": first == second and before == after,
    }


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def audit_backgap(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run all six read-surface back-gap / coverage checks. Returns a JSON-able report.

    ``clean`` is the conjunction of every check. Read-only: recomputes each eligible set
    independently from canonical columns, reconciles against what ``read_api`` serves,
    and reports any coverage regression — it never writes.
    """
    reviewer = reviewer_lane_no_backgap(conn)
    public = public_lane_no_backgap(conn)
    gaps = completeness_gap_coverage_parity(conn)
    overlays = overlay_presence_no_regression(conn)
    floor = stage1_field_floor(conn)
    determinism = determinism_read_only(conn)

    clean = all(
        c["clean"] for c in (reviewer, public, gaps, overlays, floor, determinism)
    )
    return {
        "reviewer_lane_no_backgap": reviewer,
        "public_lane_no_backgap": public,
        "completeness_gap_coverage_parity": gaps,
        "overlay_presence_no_regression": overlays,
        "stage1_field_floor": floor,
        "determinism_read_only": determinism,
        "clean": clean,
    }


def _format_report(report: dict[str, Any], db_path: Path) -> str:
    rv = report["reviewer_lane_no_backgap"]
    pu = report["public_lane_no_backgap"]
    gp = report["completeness_gap_coverage_parity"]
    ov = report["overlay_presence_no_regression"]
    fl = report["stage1_field_floor"]
    dt = report["determinism_read_only"]
    rv_n, pu_n, gp_n = len(rv["backgap"]), len(pu["backgap"]), len(gp["dropped"])
    ov_n, fl_n = len(ov["missing"]), len(fl["breaches"])
    lines = [
        f"Stage 2 read-surface back-gap audit (GOV-322) — {db_path}",
        f"  1 reviewer-lane back-gap          : {'OK' if rv['clean'] else f'BACK-GAP ({rv_n})'} "
        f"(eligible={rv['eligible_count']} served={rv['served_count']})",
        f"  2 public-lane back-gap            : {'OK' if pu['clean'] else f'BACK-GAP ({pu_n})'} "
        f"(eligible={pu['eligible_count']} served={pu['served_count']})",
        f"  3 completeness-gap coverage parity: {'OK' if gp['clean'] else f'DROPPED ({gp_n})'} "
        f"(canonical={gp['canonical_count']} projected={gp['projected_count']} "
        f"no_primary_source={gp['no_primary_source_canonical']})",
        f"  4 overlay presence no-regression  : {'OK' if ov['clean'] else f'MISSING ({ov_n})'} "
        f"(checked={ov['checked']} gap_surface={ov['gap_surface_present']})",
        f"  5 Stage-1 field floor             : {'OK' if fl['clean'] else f'BREACH ({fl_n})'} "
        f"(checked={fl['checked']})",
        f"  6 determinism / read-only         : {'OK' if dt['clean'] else 'UNSTABLE'} "
        f"(byte_identical={dt['byte_identical']} row_counts_stable={dt['row_counts_stable']})",
        f"  NO BACK-GAP / CLEAN               : {report['clean']}",
    ]
    for sid in rv["backgap"]:
        lines.append(f"    REVIEWER BACK-GAP (eligible, not served): {sid}")
    for sid in pu["backgap"]:
        lines.append(f"    PUBLIC BACK-GAP (eligible, not served): {sid}")
    for gid in gp["dropped"]:
        lines.append(f"    GAP DROPPED (canonical, not projected): {gid}")
    for m in ov["missing"]:
        lines.append(f"    OVERLAY MISSING: {m['statement_id']} -> {m['violations']}")
    for b in fl["breaches"]:
        lines.append(f"    FLOOR BREACH: {b['statement_id']} -> {b['violations']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 2 read-surface back-gap / coverage-regression audit "
        "(read-only). GOV-322 Stage 2.13."
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
        print(f"stage2 back-gap audit: DB not found at {args.db}", file=sys.stderr)
        return 2

    with db.open_db(args.db) as conn:
        report = audit_backgap(conn)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report, args.db))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
