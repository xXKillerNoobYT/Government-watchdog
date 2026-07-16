"""LED-4 / F2 / F7 fixed-cost allocation (LEDGER-2026 §2).

Monthly fixed infrastructure enters a per-area figure ONLY through the declared
F2 allocation, weighted by the F7 assumption (document-share by default). The
weight definition is the single most prominent assumption in every report
(LED-4), so :func:`allocate` always returns the ``weight_basis`` string for the
report to disclose.

``fixed_total_units`` is OWNER-SET. If no ``ledger_fixed_costs`` row exists for
the period, the allocation is a labeled hole (``OWNER-SET (unset)``, value None)
— never a fabricated zero-dollar allocation.
"""

from __future__ import annotations

import sqlite3

from . import basis as _basis
from . import formulas as _f
from . import ledger as _ledger


def fixed_total(conn: sqlite3.Connection, period: str):
    """Return ``(fixed_total_units, weight_basis)`` for a period, or ``(None, default)``."""
    row = conn.execute(
        "SELECT fixed_total_units, weight_basis FROM ledger_fixed_costs WHERE period = ?",
        (period,),
    ).fetchone()
    if row is None:
        return None, "document_share"
    return int(row["fixed_total_units"]), row["weight_basis"]


def allocate(conn: sqlite3.Connection, area_id: str | None, period: str) -> dict:
    """Allocate the period's fixed cost to one area via F2 (weighted by F7).

    Returns a dict of report cells: ``weight`` (F7, ASSUMED), ``allocated_fixed``
    (F2), the OWNER-SET ``fixed_total`` input, and the disclosed ``weight_basis``.
    """
    total_units, weight_basis = fixed_total(conn, period)

    counts = _ledger.document_counts(conn, period)
    docs_area = counts.get(area_id, 0)
    docs_total = sum(counts.values())
    weight = _f.f7_weight(docs_area, docs_total)

    if total_units is None:
        allocated = _f.FormulaResult(None, _basis.ASSUMED, "LED-F2")
        fixed_cell = _basis.cell(None, _basis.OWNER_SET_UNSET, unit="units")
    else:
        allocated = _f.f2_area_allocated_fixed(total_units, weight.value)
        fixed_cell = _basis.cell(total_units, _basis.OWNER_SET, unit="units")

    return {
        "weight_basis": weight_basis,
        "documents_area": docs_area,
        "documents_total": docs_total,
        "weight": _basis.cell(weight.value, weight.basis, formula_id=weight.formula_id),
        "fixed_total": fixed_cell,
        "allocated_fixed": _basis.cell(
            allocated.value, allocated.basis, unit="units",
            formula_id=allocated.formula_id,
        ),
    }
