"""Per-area report assembly + reproducibility hash (LEDGER-2026 §2, LED-5, AM-7).

:func:`build_pack` assembles a per-area cost pack in which EVERY reported value
is a basis-labeled cell (so :func:`economics.basis.lint_report` passes — AM-7).
The pack is deterministic: it carries no timestamp or id, so hashing it yields a
stable ``content_sha256`` (reproducibility — ``verify-hash`` recomputes and
asserts equality). :func:`record_run` stamps that hash into
``ledger_report_runs`` (the only write in this module).

:func:`build_rollup` is pure aggregation over child areas (AREA-3): it sums the
member areas' MEASURED variable cost and allocated fixed cost — never a
re-collection, never a smear of the shared pool onto a named parent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import areas as _areas
from . import basis as _basis
from . import capacity as _capacity
from . import eligibility as _eligibility
from . import fixed_cost as _fixed
from . import formulas as _f
from . import ledger as _ledger
from . import reviewer_cost as _reviewer


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_pack(conn: sqlite3.Connection, area_id: str | None, period: str,
               *, active_users: int | None = None,
               include_capacity: bool = True) -> dict:
    """Assemble a fully basis-labeled per-area pack (no timestamp/id => hashable).

    ``area_id=None`` builds the disclosed shared-cost pool pack (AREA-2).
    """
    agg = _ledger.aggregate(conn, area_id, period)

    variable = _f.f1_area_variable_cost(agg["job_cost_units"])
    fixed = _fixed.allocate(conn, area_id, period)
    allocated_fixed_cell = fixed["allocated_fixed"]

    total = _f.f3_area_total_cost(
        variable.value, allocated_fixed_cell["value"],
        variable_basis=variable.basis, fixed_basis=allocated_fixed_cell["basis"],
    )
    docs = agg["documents_processed"]
    per_user = _f.f4_cost_per_active_user(total.value, active_users,
                                          total_basis=total.basis)
    per_doc = _f.f5_cost_per_document(total.value, docs, total_basis=total.basis)

    provider = agg["provider"]
    pack: dict = {
        "schema": "LEDGER-2026/report/v1",
        "area_id": area_id,
        "period": period,
        "scope": "area",
        "weight_basis": fixed["weight_basis"],
        "variable_cost": _basis.cell(
            variable.value, variable.basis, unit="units", formula_id=variable.formula_id
        ),
        "fixed_cost": {
            "documents_area": fixed["documents_area"],
            "documents_total": fixed["documents_total"],
            "weight": fixed["weight"],
            "fixed_total": fixed["fixed_total"],
            "allocated_fixed": allocated_fixed_cell,
        },
        "total_cost": _basis.cell(
            total.value, total.basis, unit="units", formula_id=total.formula_id
        ),
        "total_cost_worst_input_basis": total.worst_input_basis,
        "cost_per_active_user": _basis.cell(
            per_user.value, per_user.basis, unit="units_per_user",
            formula_id=per_user.formula_id,
        ),
        "cost_per_document": _basis.cell(
            per_doc.value, per_doc.basis, unit="units_per_doc",
            formula_id=per_doc.formula_id,
        ),
        "documents_processed": _basis.cell(docs, _basis.MEASURED, unit="documents"),
        "provider_units": {
            "call_count": _basis.cell(provider["call_count"], _basis.MEASURED),
            "input_units": _basis.cell(provider["input_units"], _basis.MEASURED, unit="units"),
            "output_units": _basis.cell(provider["output_units"], _basis.MEASURED, unit="units"),
            "direct_cost_units": _basis.cell(
                provider["direct_cost_units"], _basis.MEASURED, unit="units"
            ),
        },
        "reviewer_work": _reviewer.reviewer_work(conn, area_id, period),
        "slo": _ledger.slo_metrics(conn, area_id, period),
    }

    if area_id is None:
        # Shared pool discloses the un-attributable substrate (AREA-2).
        pack["shared_pool_extras"] = agg.get("shared_pool_extras")
    else:
        pack["eligibility"] = _eligibility.evaluate(conn, area_id, period)
        if include_capacity:
            pack["capacity"] = _capacity.forecast(area_id, conn=conn)

    return pack


def build_rollup(conn: sqlite3.Connection, scope: str, area_id: str,
                 period: str) -> dict:
    """Pure county/state rollup: aggregate self + descendant areas (AREA-3)."""
    if scope not in ("county_rollup", "state_rollup"):
        raise ValueError(f"unknown rollup scope {scope!r}")
    members = [area_id] + _areas.descendants(conn, area_id)

    variable_total = 0
    fixed_total = 0
    fixed_seen = False
    docs_total = 0
    member_packs = []
    for m in members:
        p = build_pack(conn, m, period, include_capacity=False)
        member_packs.append({"area_id": m, "total_cost": p["total_cost"]})
        variable_total += p["variable_cost"]["value"] or 0
        av = p["fixed_cost"]["allocated_fixed"]["value"]
        if av is not None:
            fixed_total += av
            fixed_seen = True
        docs_total += p["documents_processed"]["value"] or 0

    total = _f.f3_area_total_cost(
        variable_total, fixed_total if fixed_seen else None,
        variable_basis=_basis.MEASURED, fixed_basis=_basis.ASSUMED,
    )
    return {
        "schema": "LEDGER-2026/rollup/v1",
        "area_id": area_id,
        "period": period,
        "scope": scope,
        "member_areas": members,
        "variable_cost": _basis.cell(variable_total, _basis.MEASURED, unit="units",
                                     formula_id="LED-F1"),
        "allocated_fixed": _basis.cell(
            fixed_total if fixed_seen else None,
            _basis.ASSUMED if fixed_seen else _basis.OWNER_SET_UNSET,
            unit="units", formula_id="LED-F2",
        ),
        "total_cost": _basis.cell(total.value, total.basis, unit="units",
                                  formula_id=total.formula_id),
        "documents_processed": _basis.cell(docs_total, _basis.MEASURED, unit="documents"),
        "members": member_packs,
        "weight_basis": "document_share",
    }


def content_hash(pack: dict) -> str:
    """Canonical SHA-256 of a pack (sorted keys, UTF-8). Reproducibility anchor."""
    canonical = json.dumps(pack, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_run(conn: sqlite3.Connection, pack: dict) -> tuple[str, str]:
    """Lint, hash, and write a ``ledger_report_runs`` row. Returns (report_id, hash).

    Fails closed via :func:`basis.assert_labeled` — an unlabeled value never gets
    a report id. ``report_id`` is deterministic per (scope, area, period) so a
    re-generate updates the same row (INSERT OR REPLACE) rather than piling up.
    """
    _basis.assert_labeled(pack)
    digest = content_hash(pack)
    scope = pack["scope"]
    area_id = pack["area_id"]
    period = pack["period"]
    report_id = f"{scope}:{area_id}:{period}"
    conn.execute(
        "INSERT OR REPLACE INTO ledger_report_runs"
        " (report_id, area_id, period, scope, content_sha256, generated_utc)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (report_id, area_id, period, scope, digest, _utcnow()),
    )
    conn.commit()
    return report_id, digest


def verify_hash(conn: sqlite3.Connection, report_id: str) -> dict:
    """Recompute a stored report's pack and assert its hash is unchanged.

    Returns ``{report_id, stored, recomputed, match}``. This is the reproducibility
    proof: a byte-stable pack rebuilds to the same ``content_sha256``.
    """
    row = conn.execute(
        "SELECT area_id, period, scope, content_sha256 FROM ledger_report_runs"
        " WHERE report_id = ?",
        (report_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"no report run {report_id!r}")
    scope = row["scope"]
    if scope == "area":
        pack = build_pack(conn, row["area_id"], row["period"], include_capacity=True)
    else:
        pack = build_rollup(conn, scope, row["area_id"], row["period"])
    recomputed = content_hash(pack)
    return {
        "report_id": report_id,
        "stored": row["content_sha256"],
        "recomputed": recomputed,
        "match": recomputed == row["content_sha256"],
    }
