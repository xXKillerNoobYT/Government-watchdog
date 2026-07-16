"""Pure cost formulas F1-F7 + F-ELIG (LEDGER-2026 §2, REQ-2026-COMM §9).

Every function here is a pure function of its arguments — no DB, no I/O, no
clock — so the numbers are deterministic and unit-testable in isolation (see
``test_economics_formulas.py``). Each returns a :class:`FormulaResult`
``(value, basis, formula_id)`` so the caller can drop it straight into a report
cell without inventing a basis.

Basis assignment (LEDGER-2026 §2 provenance rules):
  * F1 area_variable_cost — a pure sum of MEASURED provider/compute units.
  * F2 area_allocated_fixed — OWNER-SET fixed total x an ASSUMED document-share
    weight, so the product is ASSUMED (the assumption dominates).
  * F3/F4/F5 — DERIVED (formula outputs); ``worst_input_basis`` carries taint.
  * F6 capacity_headroom — MEASURED off a deterministic synthetic load harness.
  * F7 weight — the ASSUMED allocation assumption, disclosed on every rollup.

Division-by-zero (F4/F5) returns ``value=None`` with the basis intact — a
labeled hole, never a fabricated number or a raised exception mid-report.
"""

from __future__ import annotations

from typing import NamedTuple

from . import basis as _basis


class FormulaResult(NamedTuple):
    """A formula output, ready to become a report cell."""

    value: float | int | None
    basis: str
    formula_id: str
    # For DERIVED outputs: the worst basis among the inputs (taint carry).
    worst_input_basis: str | None = None


def f1_area_variable_cost(job_cost_units) -> FormulaResult:
    """F1: ``area_variable_cost(a, m) = Sum job costs where area_id=a in month m``.

    ``job_cost_units`` is the already-filtered iterable of per-job measured cost
    units for one (area, period). Pure sum of MEASURED units -> MEASURED.
    """
    total = sum(int(u) for u in job_cost_units)
    return FormulaResult(total, _basis.MEASURED, "LED-F1")


def f7_weight(documents_area: int, documents_total: int) -> FormulaResult:
    """F7: the F2 allocation weight = share of documents processed (ASSUMED).

    ``weight(a, m) = documents_area / documents_total``. Total 0 -> weight 0.0
    (an area processed nothing gets no fixed-cost share; never a divide error).
    """
    if documents_total <= 0:
        return FormulaResult(0.0, _basis.ASSUMED, "LED-F7")
    return FormulaResult(documents_area / documents_total, _basis.ASSUMED, "LED-F7")


def f2_area_allocated_fixed(fixed_total_units, weight) -> FormulaResult:
    """F2: ``area_allocated_fixed(a, m) = fixed_total(m) x weight(a, m)``.

    ``fixed_total_units`` is OWNER-SET; ``weight`` is ASSUMED (F7). The product
    inherits the ASSUMED basis — the assumption is the dominant provenance.
    ``fixed_total_units`` None (owner has not set it) -> value None, still labeled.
    """
    if fixed_total_units is None:
        return FormulaResult(None, _basis.ASSUMED, "LED-F2")
    return FormulaResult(fixed_total_units * weight, _basis.ASSUMED, "LED-F2")


def f3_area_total_cost(variable, fixed, *, variable_basis=_basis.MEASURED,
                       fixed_basis=_basis.ASSUMED) -> FormulaResult:
    """F3: ``area_total_cost = F1 + F2``. DERIVED, carrying its inputs' worst basis."""
    if variable is None or fixed is None:
        total = None
    else:
        total = variable + fixed
    worst = _basis.worst_basis(variable_basis, fixed_basis)
    return FormulaResult(total, _basis.DERIVED, "LED-F3", worst)


def f4_cost_per_active_user(total_cost, active_users,
                            *, total_basis=_basis.DERIVED) -> FormulaResult:
    """F4: ``cost_per_active_user(a) = F3 / active_users(a)``. DERIVED.

    ``active_users`` 0 or None -> value None (labeled hole, no divide error).
    """
    if total_cost is None or not active_users:
        value = None
    else:
        value = total_cost / active_users
    return FormulaResult(value, _basis.DERIVED, "LED-F4",
                         _basis.worst_basis(total_basis))


def f5_cost_per_document(total_cost, documents_processed,
                         *, total_basis=_basis.DERIVED) -> FormulaResult:
    """F5: ``cost_per_document(a) = F3 / documents_processed(a)``. DERIVED.

    ``documents_processed`` 0 or None -> value None (labeled hole).
    """
    if total_cost is None or not documents_processed:
        value = None
    else:
        value = total_cost / documents_processed
    return FormulaResult(value, _basis.DERIVED, "LED-F5",
                         _basis.worst_basis(total_basis))


def f6_capacity_headroom(max_sustainable_throughput, current_load) -> FormulaResult:
    """F6: ``capacity_headroom = max_sustainable_throughput - current_load``.

    Both inputs come from the deterministic synthetic load harness
    (``capacity.py``), so the result is MEASURED (measured against a synthetic
    but reproducible load).
    """
    return FormulaResult(
        max_sustainable_throughput - current_load, _basis.MEASURED, "LED-F6"
    )


def f_elig(monthly_measured_cost, monthly_funding_balance, safety_factor) -> bool:
    """AREA-6 funded-eligibility rule.

    ``monthly_measured_cost(a) <= monthly_funding_balance(a) x safety_factor``.
    cost + balance are MEASURED/OWNER-SET real figures; ``safety_factor`` is
    OWNER-SET. No dollar threshold is asserted. Returns a plain bool — the
    recommendation, never a state write.
    """
    return monthly_measured_cost <= monthly_funding_balance * safety_factor
