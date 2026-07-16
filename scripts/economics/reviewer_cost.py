"""LED-2 reviewer-work meter (LEDGER-2026 §2).

Reviewer cost is MEASURED when reviewer minutes are captured, otherwise a
declared proxy: ``decision_count x per_decision_units`` where the constant is
OWNER-SET and the resulting row's basis is DERIVED (LED-2). Correction /
rejection / source-coverage rates are MEASURED.

This reads ``ledger_reviewer_work`` (the meter rows) for an (area, period). No
row for the area -> a labeled hole, not a fabricated zero of work.
"""

from __future__ import annotations

import sqlite3

from . import basis as _basis


def reviewer_work(conn: sqlite3.Connection, area_id: str | None, period: str) -> dict:
    """Aggregate reviewer-work meter rows for an (area, period) into report cells."""
    if area_id is None:
        rows = conn.execute(
            "SELECT * FROM ledger_reviewer_work WHERE area_id IS NULL AND period = ?",
            (period,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ledger_reviewer_work WHERE area_id = ? AND period = ?",
            (area_id, period),
        ).fetchall()

    if not rows:
        return {
            "batches": 0,
            "reviewer_units": _basis.cell(None, _basis.NOT_INSTRUMENTED, unit="minutes"),
            "decision_count": _basis.cell(0, _basis.MEASURED),
            "correction_rate": _basis.cell(None, _basis.NOT_INSTRUMENTED),
            "rejection_rate": _basis.cell(None, _basis.NOT_INSTRUMENTED),
            "source_coverage_rate": _basis.cell(None, _basis.NOT_INSTRUMENTED),
        }

    total_units = 0.0
    measured_any = False
    decision_count = 0
    # Rate averages are weighted equally per batch (simple mean); MEASURED.
    corr = []
    rej = []
    cov = []
    for r in rows:
        decisions = int(r["decision_count"] or 0)
        decision_count += decisions
        if r["reviewer_minutes"] is not None:
            total_units += float(r["reviewer_minutes"])
            measured_any = True
        elif r["per_decision_units"] is not None:
            # DERIVED proxy: decision_count x OWNER-SET per_decision_units.
            total_units += decisions * float(r["per_decision_units"])
        if r["correction_rate"] is not None:
            corr.append(float(r["correction_rate"]))
        if r["rejection_rate"] is not None:
            rej.append(float(r["rejection_rate"]))
        if r["source_coverage_rate"] is not None:
            cov.append(float(r["source_coverage_rate"]))

    units_basis = _basis.MEASURED if measured_any else _basis.DERIVED

    def _rate_cell(vals):
        if not vals:
            return _basis.cell(None, _basis.NOT_INSTRUMENTED)
        return _basis.cell(sum(vals) / len(vals), _basis.MEASURED)

    return {
        "batches": len(rows),
        "reviewer_units": _basis.cell(total_units, units_basis, unit="minutes"),
        "decision_count": _basis.cell(decision_count, _basis.MEASURED),
        "correction_rate": _rate_cell(corr),
        "rejection_rate": _rate_cell(rej),
        "source_coverage_rate": _rate_cell(cov),
    }
