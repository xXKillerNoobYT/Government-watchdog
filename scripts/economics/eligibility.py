"""AREA-6 F-ELIG + GATE-P entitlement readiness — RECOMMEND ONLY (LEDGER-2026 §2).

Evaluates whether an area *would* be eligible for the ``funded`` state
(F-ELIG) and whether a paid entitlement is designed and ready — and returns a
**recommendation**. It NEVER writes ``area_state``: only ``areas.transition``
(owner-gated) can move an area. This is the operational half of
"define, not activate".

F-ELIG (AREA-6): ``monthly_measured_cost <= monthly_funding_balance x
safety_factor``. ``monthly_measured_cost`` is the MEASURED variable cost (F1);
``monthly_funding_balance`` is the OWNER-SET running sum of funding entries;
``safety_factor`` is OWNER-SET (default 1.0 if unset).
"""

from __future__ import annotations

import sqlite3

from . import basis as _basis
from . import formulas as _f
from . import ledger as _ledger


def funding_balance(conn: sqlite3.Connection, area_id: str, period: str) -> int:
    """OWNER-SET running funding balance for an area through ``period`` (inclusive)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_units), 0) FROM area_funding_entries"
        " WHERE area_id = ? AND period <= ?",
        (area_id, period),
    ).fetchone()
    return int(row[0])


def safety_factor(conn: sqlite3.Connection, area_id: str) -> tuple[float, str]:
    """Return ``(safety_factor, basis)``: OWNER-SET value or the default 1.0 (unset)."""
    row = conn.execute(
        "SELECT safety_factor FROM area_funding_policy WHERE area_id = ?",
        (area_id,),
    ).fetchone()
    if row is None:
        return 1.0, _basis.OWNER_SET_UNSET
    return float(row["safety_factor"]), _basis.OWNER_SET


def entitlement_readiness(conn: sqlite3.Connection, area_id: str) -> dict:
    """GATE-P readiness: does the area have a designed entitlement? (recommend-only)."""
    rows = conn.execute(
        "SELECT tier, state, owner_decision_ref FROM area_entitlements"
        " WHERE area_id = ? ORDER BY entitlement_id",
        (area_id,),
    ).fetchall()
    designed = [dict(r) for r in rows]
    return {
        "has_designed_entitlement": any(r["state"] == "designed" for r in rows),
        "entitlements": designed,
        # A paid activation is GATE-P downstream; this build only ever recommends.
        "activation_gate": "GATE-P (owner decision, downstream)",
    }


def evaluate(conn: sqlite3.Connection, area_id: str, period: str) -> dict:
    """F-ELIG + entitlement recommendation for one (area, period). Never writes state."""
    measured_cost = _f.f1_area_variable_cost(
        _ledger.job_cost_units(conn, area_id, period)
    )
    balance = funding_balance(conn, area_id, period)
    factor, factor_basis = safety_factor(conn, area_id)

    eligible = _f.f_elig(measured_cost.value, balance, factor)
    current = None
    row = conn.execute(
        "SELECT state FROM area_state WHERE area_id = ?", (area_id,)
    ).fetchone()
    if row:
        current = row[0]

    return {
        "area_id": area_id,
        "period": period,
        "monthly_measured_cost": _basis.cell(
            measured_cost.value, measured_cost.basis, unit="units",
            formula_id=measured_cost.formula_id,
        ),
        "monthly_funding_balance": _basis.cell(balance, _basis.OWNER_SET, unit="units"),
        "safety_factor": _basis.cell(factor, factor_basis),
        "f_elig": eligible,
        "current_state": current,
        "recommended_state": "funded" if eligible else "locked",
        "entitlement": entitlement_readiness(conn, area_id),
        # Loud reminder to any caller: this is advisory, not a state change.
        "note": "recommendation only; use `transition` with an owner_decision_ref to act",
    }
