"""Stage 5 back-gap / regression auditor (GOV-574, Stage 5.13).

Owner: BackendCrawlerEngineer. Parent: GOV-573 (CTO sequencing). Contract:
``Docs/stage5-13-backgap-regression-analysis-contract.md`` (commit ``cd6dc02``, PR #93;
the prior phantom ``a1fe3d8`` / JS spec was repaired under GOV-578). Read-only,
deterministic, Alpine-only, reviewer-internal. There is no ``--apply`` — it is inherently
dry-run and doubles as a CI gate (exit 1 on any finding).

This is the twice-shipped Python back-gap pattern (:mod:`stage2_backgap`, GOV-322 /
:mod:`stage3_backgap`, GOV-411) raised **one layer up** over the merged Stage-5 trust
substrate. The forward-traceability auditors already prove that every value the surface
*serves* traces back to a canonical source. This module closes the **inverse** direction
over the Stage-5 surface along two axes:

* **Axis A — point-in-time back-gap (completeness).** Independently recompute the
  "should-be-served" membership (reusing the GOV-322 oracle
  :func:`stage2_backgap.reviewer_eligible_ids`, which mirrors the read_api gate over
  canonical columns + SSOT leaf predicates, NOT any assembly loop) and reconcile it
  against what the OUTERMOST Stage-5 projection
  (:func:`stage5_frontend_surface.build_surface`) actually serves. A served set smaller
  than the eligible set is a silent-shrinkage back-gap — the highest-severity trust
  failure for a first external reviewer (the surface looks complete while showing *less*
  than reality). Recomputing independently is essential: calling the same assembly would
  compare it to itself and let a regression that drops a record class shrink both sides
  equally and pass silently.
* **Axis B — regression / monotonicity (vs a pinned baseline).** Compare the current
  surface against a committed golden baseline JSON (``tests/fixtures/``) loaded via
  ``--baseline PATH``. Trust state and coverage must not silently shrink between runs.
  Absence of a baseline is **never** silently reported as "no regressions" — it fails
  closed (``baseline_absent``, exit 1).

Each finding: ``{axis, type, severity, subjectId, detail}``. Stable sort
``(axis, severity desc, type, subjectId)``. The run is idempotent — two passes over the
same ``(conn, baseline)`` are byte-identical. Exactly one envelope hash is exposed
(``backgapDigest``, I3); the whole body is swept by
:func:`read_api.assert_no_raw_paths` (I1). The substrate is **reused by reference, never
forked** — this module recomputes only the *eligible membership set* and reconciles it
against what the Stage-5 modules actually serve. Every pre-existing serving module stays
byte-0-diff (I4).

Honest-gap consolidations (contract — surface, never fabricate; 5.05/5.07 latent-anchor
precedent): the Stage-5 surfaces do not re-project meeting-level
:func:`read_api.completeness_gap_cards`, so ``coverage_hole`` is grounded by the auditor
*carrying those canonical gaps up* itself (so a reviewer still sees them); and the
substrate fail-closes to valid enums upstream, so ``coverage_unknown`` is a *defensive*
fail-closed detector for an unresolvable envelope (normally empty — it still runs so a
future substrate regression that emits an unresolvable envelope is caught).

If a real back-gap/regression is found that implies a defect in shipped code, STOP and
escalate the failing subjects — the fix is a SEPARATE scoped issue, never a silent
self-heal or a patch to a serving module from this ticket. This ticket ships the net.

Usage:
    python scripts/stage5_backgap.py --db PATH [--baseline PATH] [--json] [--check]
"""

from __future__ import annotations

import argparse
import json
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402  (transport guard + served lanes; read-only)
import stage2_backgap as oracle  # noqa: E402  (membership oracle; reused by reference)
import stage5_source_inventory as inv  # noqa: E402  (5.03 lifecycle + archive)
import stage5_record_verifier as rec  # noqa: E402  (5.04 verification resolution)
import stage5_trust_model as tm  # noqa: E402  (5.07 corrections + archive binding)
import stage5_frontend_surface as surface  # noqa: E402  (5.06 outermost served surface)

SCOPE = "alpine"  # envelope scope (fixed; broader = planned)
ACCESS = "reviewer_internal"  # never "public" — I6

BASELINE_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Finding vocabulary (frozen). Axis + severity SSOT.
# ---------------------------------------------------------------------------

AXIS_BACK_GAP = "back_gap"
AXIS_REGRESSION = "regression"

SEV_BLOCKING = "blocking"
SEV_WARN = "warn"
SEV_INFO = "info"
_SEVERITY_RANK = {SEV_BLOCKING: 3, SEV_WARN: 2, SEV_INFO: 1}

# Axis A — point-in-time back-gap
T_UNTRACED_STATEMENT = "untraced_statement"
T_ORPHAN_SOURCE = "orphan_source"
T_DANGLING_TRACE = "dangling_trace"
T_COVERAGE_HOLE = "coverage_hole"
T_COVERAGE_UNKNOWN = "coverage_unknown"
T_ARCHIVE_UNCHECKED = "archive_unchecked"
T_ARCHIVE_MISSING = "archive_missing"

# Axis B — regression vs baseline
T_VERIFICATION_REGRESSED = "verification_regressed"
T_PUBLISH_REGRESSED = "publish_regressed"
T_CAPTURE_LOST = "capture_lost"
T_DIGEST_ITEM_DROPPED = "digest_item_dropped"
T_CORRECTION_NOT_PROPAGATED = "correction_not_propagated"
T_BASELINE_ABSENT = "baseline_absent"

# Default severity per finding type (a coverage_hole carries the gap card's own severity).
_SEVERITY_BY_TYPE = {
    T_UNTRACED_STATEMENT: SEV_BLOCKING,
    T_ORPHAN_SOURCE: SEV_WARN,
    T_DANGLING_TRACE: SEV_BLOCKING,
    T_COVERAGE_HOLE: SEV_WARN,
    T_COVERAGE_UNKNOWN: SEV_WARN,
    T_ARCHIVE_UNCHECKED: SEV_WARN,
    T_ARCHIVE_MISSING: SEV_WARN,
    T_VERIFICATION_REGRESSED: SEV_BLOCKING,
    T_PUBLISH_REGRESSED: SEV_BLOCKING,
    T_CAPTURE_LOST: SEV_WARN,
    T_DIGEST_ITEM_DROPPED: SEV_WARN,
    T_CORRECTION_NOT_PROPAGATED: SEV_BLOCKING,
    T_BASELINE_ABSENT: SEV_BLOCKING,
}

BACK_GAP_TYPES = frozenset(
    {
        T_UNTRACED_STATEMENT, T_ORPHAN_SOURCE, T_DANGLING_TRACE, T_COVERAGE_HOLE,
        T_COVERAGE_UNKNOWN, T_ARCHIVE_UNCHECKED, T_ARCHIVE_MISSING,
    }
)
REGRESSION_TYPES = frozenset(
    {
        T_VERIFICATION_REGRESSED, T_PUBLISH_REGRESSED, T_CAPTURE_LOST,
        T_DIGEST_ITEM_DROPPED, T_CORRECTION_NOT_PROPAGATED, T_BASELINE_ABSENT,
    }
)

# Lifecycle states whose archive availability is a first-class concern (5.03/5.07).
_CHANGED_STATES = frozenset(
    {inv.LIFECYCLE_CHANGED, inv.LIFECYCLE_DISAPPEARED, inv.LIFECYCLE_REPLACED}
)


class BackgapContractError(AssertionError):
    """Raised when an emitted back-gap body violates a GOV-574 contract invariant."""


def _finding(axis: str, ftype: str, subject_id: Any, detail: str, *, severity: str | None = None) -> dict[str, Any]:
    """One finding record. ``severity`` defaults to the type's SSOT severity."""
    return {
        "axis": axis,
        "type": ftype,
        "severity": severity or _SEVERITY_BY_TYPE[ftype],
        "subjectId": "" if subject_id is None else str(subject_id),
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Independent membership recompute / served-surface read-back.
# ---------------------------------------------------------------------------


def served_statement_ids(conn: sqlite3.Connection) -> set[str]:
    """The statement ids the OUTERMOST Stage-5 surface actually serves.

    Read back from :func:`stage5_frontend_surface.build_surface` (the 5.06 board, layered
    on the 5.05 watchdog view) — so a regression that drops a record class from EITHER
    ``read_api`` OR any Stage-5 layer shrinks this set and is caught against the
    independently-recomputed eligible set. NOT the assembly being audited.
    """
    body = surface.build_surface(conn)
    served: set[str] = set()
    for column in body.get("watchdogBoard", []):
        for card in column.get("cards", []):
            sid = card.get("statementId")
            if sid:
                served.add(sid)
    return served


def referenced_source_ids(conn: sqlite3.Connection) -> set[str]:
    """Every ``to_source_id`` a served reviewer-internal record traces back to."""
    referenced: set[str] = set()
    for record in read_api.reviewer_internal_records(conn):
        for link in record.get("evidence", []):
            source_id = link.get("to_source_id")
            if source_id:
                referenced.add(source_id)
    return referenced


def _membership_backgap(eligible: set[str], served: set[str]) -> list[str]:
    """eligible − served, sorted — the core silent-shrinkage reconciliation (RED-proof seam).

    Neutering this resolver to a constant ``[]`` makes the auditor stop detecting a real
    back-gap while the read surface still serves the same records — the load-bearing,
    non-tautological RED-proof (I5).
    """
    return sorted(eligible - served)


# ---------------------------------------------------------------------------
# Axis A — point-in-time back-gap.
# ---------------------------------------------------------------------------


def build_backgap(
    conn: sqlite3.Connection, *, wayback_probe: Callable[[dict[str, Any]], str] | None = None
) -> list[dict[str, Any]]:
    """All Axis-A point-in-time back-gap findings (read-only, deterministic).

    ``wayback_probe`` is **default-closed** (``None``): the module makes NO network call.
    When an authorized caller injects a probe (a mock in tests), an ``not_checked``
    changed-source archive is resolved through it instead of being surfaced as
    ``archive_unchecked``. A live Wayback call requires CEO/CTO authorization.
    """
    findings: list[dict[str, Any]] = []

    eligible = oracle.reviewer_eligible_ids(conn)
    served = served_statement_ids(conn)

    # 1) untraced_statement — eligible but absent from the served surface.
    for sid in _membership_backgap(eligible, served):
        findings.append(
            _finding(AXIS_BACK_GAP, T_UNTRACED_STATEMENT, sid,
                     "reviewer-eligible statement absent from the Stage-5 served surface")
        )

    # Inventory (5.03) — the canonical registered-source set + its lifecycle/archive.
    inventory = inv.build_inventory(conn).get("sources", [])
    inventory_ids = {s.get("source_id") for s in inventory if s.get("source_id")}
    referenced = referenced_source_ids(conn)

    # 2) orphan_source — a registered source no served record traces back to.
    for source_id in sorted(inventory_ids - referenced):
        findings.append(
            _finding(AXIS_BACK_GAP, T_ORPHAN_SOURCE, source_id,
                     "registered source not referenced by any served record")
        )

    # 3) dangling_trace — a served record cites a source absent from the registry.
    for source_id in sorted(referenced - inventory_ids):
        findings.append(
            _finding(AXIS_BACK_GAP, T_DANGLING_TRACE, source_id,
                     "served record references a source absent from the canonical inventory")
        )

    # 4) coverage_hole — canonical completeness gaps the Stage-5 surfaces do not re-project;
    #    the auditor carries each up so a recorded gap is never lost (honest consolidation).
    for card in read_api.completeness_gap_cards(conn):
        findings.append(
            _finding(AXIS_BACK_GAP, T_COVERAGE_HOLE, card.get("gap_id"),
                     f"recorded completeness gap '{card.get('gap_type')}' not carried up by the Stage-5 surface",
                     severity=_clamp_severity(card.get("severity")))
        )

    # 5) coverage_unknown — defensive fail-closed: a source whose lifecycle/archive envelope
    #    cannot be resolved to a valid SSOT enum (substrate normally fail-closes upstream).
    for source in inventory:
        state = source.get("lifecycle", {}).get("state")
        availability = source.get("archiveAvailability", {}).get("snapshotAvailability")
        if state not in inv.SOURCE_LIFECYCLE_STATES or availability not in inv.ARCHIVE_AVAILABILITY_STATES:
            findings.append(
                _finding(AXIS_BACK_GAP, T_COVERAGE_UNKNOWN, source.get("source_id"),
                         "source lifecycle/archive state unresolvable — surfaced unknown, never assumed covered")
            )

    # 6/7) archive_unchecked / archive_missing — over the 5.07 source-change/archive binding.
    for entry in tm.build_source_change_archive(conn):
        if entry.get("lifecycleState") not in _CHANGED_STATES:
            continue
        source_id = entry.get("sourceId")
        if entry.get("archiveStatus") == inv.ARCHIVE_STATUS_NOT_CHECKED:
            probed = wayback_probe(entry) if wayback_probe is not None else None
            if probed == inv.SNAPSHOT_AVAILABLE:
                continue  # authorized probe resolved availability — no gap
            if probed == inv.SNAPSHOT_NOT_AVAILABLE:
                findings.append(
                    _finding(AXIS_BACK_GAP, T_ARCHIVE_MISSING, source_id,
                             "changed source archive determined absent (via authorized probe)")
                )
                continue
            findings.append(
                _finding(AXIS_BACK_GAP, T_ARCHIVE_UNCHECKED, source_id,
                         "changed/disappeared/replaced source has no archive determination near scan_date "
                         "(Wayback default-closed)")
            )
        elif entry.get("archiveBinding") == tm.ARCHIVE_BINDING_GAP:
            findings.append(
                _finding(AXIS_BACK_GAP, T_ARCHIVE_MISSING, source_id,
                         "changed source archive availability determined and absent (archive_gap)")
            )

    return findings


def _clamp_severity(value: Any) -> str:
    return value if value in _SEVERITY_RANK else SEV_WARN


# ---------------------------------------------------------------------------
# Axis B — regression vs a pinned baseline.
# ---------------------------------------------------------------------------


def _verified_ids(conn: sqlite3.Connection, served: Iterable[str]) -> set[str]:
    """Served statements whose 5.04 verification resolves to ``verified``."""
    return {sid for sid in served if rec.resolve_verification(conn, sid).get("verified")}


def _capture_source_ids(conn: sqlite3.Connection) -> set[str]:
    """Sources carrying an available-near-scan archive capture (5.07 binding)."""
    return {
        entry.get("sourceId")
        for entry in tm.build_source_change_archive(conn)
        if entry.get("snapshotAvailability") == inv.SNAPSHOT_AVAILABLE
    }


def _correction_edges(conn: sqlite3.Connection) -> set[str]:
    """Resolved forward-only correction edges (``corrected->superseding``), 5.07 spine."""
    edges: set[str] = set()
    for edge in tm.build_corrections(conn):
        if edge.get("resolved") and edge.get("supersedingStatementId"):
            edges.add(f"{edge.get('correctedStatementId')}->{edge.get('supersedingStatementId')}")
    return edges


def capture_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """The committed-baseline shape: the monotonicity-tracked sets, sorted (deterministic).

    A golden snapshot of the prior Stage-5 surface. Axis B reconciles a later run against
    it: any tracked id present in the baseline but absent now is a regression.
    """
    served = served_statement_ids(conn)
    inventory_ids = sorted(
        s.get("source_id") for s in inv.build_inventory(conn).get("sources", []) if s.get("source_id")
    )
    return {
        "schemaVersion": BASELINE_SCHEMA_VERSION,
        "servedStatementIds": sorted(served),
        "verifiedStatementIds": sorted(_verified_ids(conn, served)),
        "publishEligibleIds": sorted(oracle.publish_eligible_ids(conn)),
        "captureSourceIds": sorted(_capture_source_ids(conn)),
        "inventorySourceIds": inventory_ids,
        "correctionEdges": sorted(_correction_edges(conn)),
    }


def _baseline_set(baseline: dict[str, Any], key: str) -> set[str]:
    value = baseline.get(key)
    return set(value) if isinstance(value, list) else set()


def build_regression(conn: sqlite3.Connection, baseline: dict[str, Any] | None) -> list[dict[str, Any]]:
    """All Axis-B regression findings against ``baseline`` (fail-closed when absent).

    ``baseline`` is the parsed golden snapshot (:func:`capture_snapshot` shape) or
    ``None``. A ``None`` / unparseable / wrong-schema baseline emits ``baseline_absent``
    and the regression axis is reported unverifiable — never silently "no regressions".
    """
    if not isinstance(baseline, dict) or baseline.get("schemaVersion") != BASELINE_SCHEMA_VERSION:
        return [
            _finding(AXIS_REGRESSION, T_BASELINE_ABSENT, None,
                     "no parseable --baseline supplied: regression axis is unverifiable (fail-closed)")
        ]

    findings: list[dict[str, Any]] = []
    served = served_statement_ids(conn)

    comparisons = (
        (T_VERIFICATION_REGRESSED, "verifiedStatementIds", _verified_ids(conn, served),
         "statement verified in baseline is no longer verified"),
        (T_PUBLISH_REGRESSED, "publishEligibleIds", oracle.publish_eligible_ids(conn),
         "record publish-eligible in baseline dropped from the current surface"),
        (T_CAPTURE_LOST, "captureSourceIds", _capture_source_ids(conn),
         "archive capture present in baseline is now absent"),
        (T_DIGEST_ITEM_DROPPED, "inventorySourceIds",
         {s.get("source_id") for s in inv.build_inventory(conn).get("sources", []) if s.get("source_id")},
         "registered source present in baseline inventory digest is now missing"),
    )
    for ftype, key, current, detail in comparisons:
        for subject in sorted(_baseline_set(baseline, key) - current):
            findings.append(_finding(AXIS_REGRESSION, ftype, subject, detail))

    for edge in sorted(_baseline_set(baseline, "correctionEdges") - _correction_edges(conn)):
        findings.append(
            _finding(AXIS_REGRESSION, T_CORRECTION_NOT_PROPAGATED, edge,
                     "correction edge present in baseline is not reflected in the current surface")
        )
    return findings


# ---------------------------------------------------------------------------
# Ordering + single envelope digest.
# ---------------------------------------------------------------------------


def _sort_key(finding: dict[str, Any]) -> tuple[str, int, str, str]:
    # (axis, severity DESC, type, subjectId) — severity descending via negated rank.
    return (
        finding["axis"],
        -_SEVERITY_RANK.get(finding["severity"], 0),
        finding["type"],
        finding["subjectId"],
    )


def _backgap_digest(findings: list[dict[str, Any]]) -> str:
    """A single sha256 over the canonical sorted findings list (I3)."""
    payload = json.dumps(findings, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _corpus_anchor(conn: sqlite3.Connection) -> str | None:
    """The newest source ``scan_date`` — a data-derived recency anchor (never wall-clock).

    Honors the contract's "corpus-anchored recency, no ``Date.now()``" rule: any time
    horizon anchors to the data's own newest scan, so the same DB yields a byte-identical
    envelope regardless of when the audit runs.
    """
    anchors = [
        s.get("archiveAvailability", {}).get("scanDate")
        for s in inv.build_inventory(conn).get("sources", [])
    ]
    dated = sorted(a for a in anchors if isinstance(a, str) and a)
    return dated[-1] if dated else None


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def analyze_backgap(
    conn: sqlite3.Connection,
    baseline: dict[str, Any] | None = None,
    *,
    wayback_probe: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    """Run both axes and assemble the swept, single-digest report.

    ``clean`` is True iff zero findings (no back-gap, no regression, baseline parsed).
    Read-only: recomputes each set independently from canonical columns, reconciles
    against what the Stage-5 surface serves, and reports any gap/regression — never writes.
    """
    findings = build_backgap(conn, wayback_probe=wayback_probe) + build_regression(conn, baseline)
    findings.sort(key=_sort_key)

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["type"]] = counts.get(finding["type"], 0) + 1

    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public" — I6
        "corpusAnchor": _corpus_anchor(conn),
        "baselinePresent": isinstance(baseline, dict)
        and baseline.get("schemaVersion") == BASELINE_SCHEMA_VERSION,
        "findings": findings,
        "counts": counts,
        "clean": not findings,
        "backgapDigest": _backgap_digest(findings),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# Contract guards (load-bearing).
# ---------------------------------------------------------------------------

_HEX64 = frozenset("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX64 for c in value.lower())


def assert_single_envelope_digest(body: dict[str, Any]) -> bool:
    """RED if any 64-hex string appears outside the top-level ``backgapDigest`` (I3)."""
    if not _is_hex64(body.get("backgapDigest")):
        raise BackgapContractError("envelope backgapDigest is not a sha256")
    for finding in body.get("findings", []):
        for text in read_api._iter_strings(finding):
            if _is_hex64(text):
                raise BackgapContractError(f"unexpected 64-hex inside a finding: {text!r}")
    return True


def assert_findings_well_formed(body: dict[str, Any]) -> bool:
    """RED if any finding is off-vocab (axis/type/severity outside the frozen SSOT)."""
    for finding in body.get("findings", []):
        axis, ftype, severity = finding.get("axis"), finding.get("type"), finding.get("severity")
        if axis not in (AXIS_BACK_GAP, AXIS_REGRESSION):
            raise BackgapContractError(f"finding axis {axis!r} off-SSOT")
        valid_types = BACK_GAP_TYPES if axis == AXIS_BACK_GAP else REGRESSION_TYPES
        if ftype not in valid_types:
            raise BackgapContractError(f"finding type {ftype!r} not valid for axis {axis!r}")
        if severity not in _SEVERITY_RANK:
            raise BackgapContractError(f"finding severity {severity!r} off-SSOT")
    return True


# ---------------------------------------------------------------------------
# Baseline I/O + CLI.
# ---------------------------------------------------------------------------


def load_baseline(path: Path | None) -> dict[str, Any] | None:
    """Parse a committed golden baseline JSON, or ``None`` on absence/parse failure.

    Fail-closed: a missing path, unreadable file, or malformed JSON returns ``None`` so
    :func:`build_regression` surfaces ``baseline_absent`` rather than a false all-clear.
    """
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _format_report(report: dict[str, Any], db_path: Path, baseline_path: Path | None) -> str:
    lines = [
        f"Stage 5 back-gap / regression audit (GOV-574) — {db_path}",
        f"  baseline           : {baseline_path if baseline_path else '(none — regression axis fail-closed)'}",
        f"  corpus anchor      : {report.get('corpusAnchor')}",
        f"  baseline present   : {report.get('baselinePresent')}",
        f"  findings           : {len(report.get('findings', []))}",
        f"  CLEAN              : {report.get('clean')}",
    ]
    for ftype, count in sorted(report.get("counts", {}).items()):
        lines.append(f"    {ftype:<28}: {count}")
    for finding in report.get("findings", []):
        lines.append(
            f"    [{finding['axis']}/{finding['severity']}] {finding['type']}: "
            f"{finding['subjectId']} — {finding['detail']}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5 back-gap / regression audit (read-only). GOV-574 Stage 5.13."
    )
    parser.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help=f"path to the sqlite DB (default: {db.DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--baseline", type=Path, default=None,
        help="path to the committed golden baseline JSON (Axis B). Absent => fail-closed.",
    )
    parser.add_argument("--json", action="store_true", help="emit the machine-readable JSON report")
    parser.add_argument(
        "--check", action="store_true",
        help="CI gate mode: exit 1 on any finding, 0 only on a fully clean audit with a parsed baseline",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"stage5 back-gap audit: DB not found at {args.db}", file=sys.stderr)
        return 2

    baseline = load_baseline(args.baseline)
    with db.open_db(args.db) as conn:
        report = analyze_backgap(conn, baseline)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report, args.db, args.baseline))

    # Exit 1 on any finding (back-gap OR regression OR baseline_absent); 0 only on a fully
    # clean audit. --check is the same gate (kept explicit for CI symmetry with siblings).
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
