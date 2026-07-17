"""Metric-snapshot extractor (PILOT-2026 §2, GOV-781 leg 2).

Reads EXACTLY the §2 sources for one ``(area_id, period)`` and returns a single
basis-labeled dict. Every reported number is an ``economics.basis.cell`` so
``lint_report`` passes (AM-7); any metric with no substrate emits a
``NOT_INSTRUMENTED`` labeled hole, never a fabricated value (§5.3 test 2). The
extractor is a pure read — it writes nothing — and its key order is deterministic.

It reuses the merged readers (``economics.ledger`` / ``reviewer_cost`` /
``fixed_cost``, ``credit_metering``) rather than re-querying the ledger tables,
so the snapshot can never diverge from what the economics pack reports.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import credit_metering
from economics import basis as _basis
from economics import capacity as _capacity
from economics import fixed_cost as _fixed
from economics import ledger as _ledger
from economics import reviewer_cost as _reviewer

from . import DEFAULT_SEED

SCHEMA = "PILOT-2026/snapshot/v1"


def _period_pred(column: str) -> str:
    return f"substr({column}, 1, 7) = ?"


# ---------------------------------------------------------------------------
# §2.1 Cost
# ---------------------------------------------------------------------------

def _cost(conn, area_id, period) -> dict:
    variable = _ledger.job_cost_units(conn, area_id, period)
    lanes = _ledger.lane_rollup(conn, area_id, period)
    provider = _ledger.provider_rollup(conn, area_id, period)
    fixed = _fixed.allocate(conn, area_id, period)
    reviewer = _reviewer.reviewer_work(conn, area_id, period)
    # AI-lane trend meter (period-windowed; trend only, never billing truth).
    trend = credit_metering.meter(
        conn, since_utc=f"{period}-01T00:00:00.000+00:00",
        until_utc=f"{period}-31T23:59:59.999+00:00")
    cpu_s = sum(l["cpu_s"] for l in lanes.values())
    queue_wait_s = sum(l["queue_wait_s"] for l in lanes.values())
    return {
        "variable_cost_units": _basis.cell(sum(variable), _basis.MEASURED,
                                           unit="units", formula_id="LED-F1"),
        "compute_cpu_s": _basis.cell(round(cpu_s, 6), _basis.MEASURED, unit="seconds"),
        "compute_queue_wait_s": _basis.cell(round(queue_wait_s, 6), _basis.MEASURED,
                                            unit="seconds"),
        "provider_call_count": _basis.cell(provider["call_count"], _basis.MEASURED),
        "provider_input_units": _basis.cell(provider["input_units"], _basis.MEASURED,
                                            unit="units"),
        "provider_output_units": _basis.cell(provider["output_units"], _basis.MEASURED,
                                             unit="units"),
        "provider_direct_cost_units": _basis.cell(provider["direct_cost_units"],
                                                  _basis.MEASURED, unit="units"),
        "fixed_allocation": fixed,
        "reviewer_work": reviewer,
        # Trend meter est. cost is an ASSUMED provider list-price estimate; carried
        # as a scalar annotation (not a value-cell) so it never enters LED-6/lint.
        "ai_trend_tokens_input": _basis.cell(trend["gateway"]["tokens_input"],
                                             _basis.MEASURED, unit="tokens"),
        "ai_trend_tokens_output": _basis.cell(trend["gateway"]["tokens_output"],
                                              _basis.MEASURED, unit="tokens"),
        "ai_trend_estimated_cost_usd_assumed": trend["gateway"]["estimated_cost_usd"],
        "ai_trend_estimated_cost_usd_basis": "ASSUMED",
    }


# ---------------------------------------------------------------------------
# §2.2 Quality  /  §2.3 Latency
# ---------------------------------------------------------------------------

def _audit_outcomes(conn, area_id, period) -> dict:
    pred, params = _ledger._area_pred(area_id)
    rows = conn.execute(
        f"SELECT outcome, error_code, COUNT(*) AS n FROM mcp_audit_events"
        f" WHERE {pred} AND {_period_pred('created_at')}"
        f" GROUP BY outcome, error_code",
        (*params, period)).fetchall()
    allow = sum(r["n"] for r in rows if r["outcome"] == "allow")
    deny = sum(r["n"] for r in rows if r["outcome"] == "deny")
    by_error = {r["error_code"]: r["n"] for r in rows if r["error_code"]}
    return {"allow": allow, "deny": deny, "by_error_code": by_error}


def _quality(conn, area_id, period) -> dict:
    outcomes = _audit_outcomes(conn, area_id, period)
    reviewer = _reviewer.reviewer_work(conn, area_id, period)
    total = outcomes["allow"] + outcomes["deny"]
    pass_rate = (outcomes["allow"] / total) if total else None
    return {
        "mcp_calls_total": _basis.cell(total, _basis.MEASURED),
        "mcp_allow": _basis.cell(outcomes["allow"], _basis.MEASURED),
        "mcp_deny": _basis.cell(outcomes["deny"], _basis.MEASURED),
        "mcp_validation_pass_rate": _basis.cell(
            round(pass_rate, 6) if pass_rate is not None else None,
            _basis.MEASURED if pass_rate is not None else _basis.NOT_INSTRUMENTED),
        "deny_by_error_code": {
            code: _basis.cell(n, _basis.MEASURED)
            for code, n in sorted(outcomes["by_error_code"].items())
        },
        # Wave 0 has no human review: reviewer-quality cells stay as reviewer_cost
        # reports them (NOT_INSTRUMENTED when no batch), never fabricated (§2.2).
        "reviewer_correction_rate": reviewer["correction_rate"],
        "reviewer_rejection_rate": reviewer["rejection_rate"],
        "reviewer_source_coverage_rate": reviewer["source_coverage_rate"],
    }


def _latency(conn, area_id, period) -> dict:
    p95 = _ledger._read_latency_p95(conn, area_id, period)
    lanes = _ledger.lane_rollup(conn, area_id, period)
    queue_wait_s = sum(l["queue_wait_s"] for l in lanes.values())
    return {
        "read_latency_p95_ms": _basis.cell(
            p95, _basis.MEASURED if p95 is not None else _basis.NOT_INSTRUMENTED,
            unit="ms", formula_id="SLO-3"),
        "queue_wait_total_s": _basis.cell(round(queue_wait_s, 6), _basis.MEASURED,
                                          unit="seconds"),
        "slo": _ledger.slo_metrics(conn, area_id, period),
    }


# ---------------------------------------------------------------------------
# §2.4 Safety
# ---------------------------------------------------------------------------

def _safety(conn, area_id, period) -> dict:
    pred, params = _ledger._area_pred(area_id)
    redaction = conn.execute(
        f"SELECT COUNT(*) FROM mcp_audit_events WHERE {pred}"
        f" AND {_period_pred('created_at')} AND error_code = 'denied:redaction'",
        (*params, period)).fetchone()[0]
    # Risk-gate lane-4 runs + unresolved blocking flags are global (no area_id).
    risk_runs = conn.execute(
        f"SELECT COUNT(*) FROM ai_extraction_runs WHERE lane = '4_risk'"
        f" AND {_period_pred('started_utc')}", (period,)).fetchone()[0]
    unresolved_blocking = conn.execute(
        "SELECT COUNT(*) FROM ai_risk_flags WHERE resolved = 0"
        " AND blocks_downstream = 1").fetchone()[0]
    revoked_grants = conn.execute(
        "SELECT COUNT(*) FROM mcp_capability_grants WHERE revoked = 1").fetchone()[0]
    revocation_denies = conn.execute(
        f"SELECT COUNT(*) FROM mcp_audit_events WHERE {pred}"
        f" AND {_period_pred('created_at')} AND outcome = 'deny'"
        f" AND error_code = 'denied:capability'", (*params, period)).fetchone()[0]
    return {
        "redaction_events": _basis.cell(redaction, _basis.MEASURED),
        "risk_gate_lane4_runs": _basis.cell(risk_runs, _basis.MEASURED),
        "risk_unresolved_blocking_flags": _basis.cell(unresolved_blocking, _basis.MEASURED),
        "revoked_grants": _basis.cell(revoked_grants, _basis.MEASURED),
        "capability_denies": _basis.cell(revocation_denies, _basis.MEASURED),
    }


# ---------------------------------------------------------------------------
# §2.5 Support (append-only JSONL; NOT_INSTRUMENTED when absent)
# ---------------------------------------------------------------------------

def _support(support_log_path) -> dict:
    if not support_log_path:
        return {
            "tickets": _basis.cell(None, _basis.NOT_INSTRUMENTED),
            "total_minutes": _basis.cell(None, _basis.NOT_INSTRUMENTED, unit="minutes"),
            "owner_minutes": _basis.cell(None, _basis.NOT_INSTRUMENTED, unit="minutes"),
        }
    path = Path(support_log_path)
    if not path.exists():
        return {
            "tickets": _basis.cell(0, _basis.MEASURED),
            "total_minutes": _basis.cell(0, _basis.MEASURED, unit="minutes"),
            "owner_minutes": _basis.cell(0, _basis.MEASURED, unit="minutes"),
        }
    tickets = total = owner = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        tickets += 1
        total += int(rec.get("minutes_spent", 0) or 0)
        owner += int(rec.get("owner_minutes", 0) or 0)
    return {
        "tickets": _basis.cell(tickets, _basis.MEASURED),
        "total_minutes": _basis.cell(total, _basis.MEASURED, unit="minutes"),
        "owner_minutes": _basis.cell(owner, _basis.MEASURED, unit="minutes"),
    }


# ---------------------------------------------------------------------------
# §2.6 Notification
# ---------------------------------------------------------------------------

def _notification(conn, period) -> dict:
    # Outbox rows are not area-scoped; report the run-global view (NOTIF set).
    sent = conn.execute(
        f"SELECT COUNT(*) FROM email_outbox WHERE status = 'sent'"
        f" AND {_period_pred('queued_utc')}", (period,)).fetchone()[0]
    suppressed = conn.execute(
        f"SELECT COUNT(*) FROM email_outbox WHERE status = 'suppressed'"
        f" AND {_period_pred('queued_utc')}", (period,)).fetchone()[0]
    delivery = {
        r[0]: r[1] for r in conn.execute(
            f"SELECT event_kind, COUNT(*) FROM email_delivery_log"
            f" WHERE {_period_pred('recorded_utc')} GROUP BY event_kind", (period,))
    }
    notif_events = conn.execute(
        f"SELECT COUNT(*) FROM notification_events WHERE {_period_pred('created_utc')}",
        (period,)).fetchone()[0]
    total_delivery = sum(delivery.values())
    good = sum(delivery.get(k, 0) for k in ("sent", "delivered"))
    outcome_rate = (good / total_delivery) if total_delivery else None
    return {
        "consented_sends": _basis.cell(sent, _basis.MEASURED),
        "suppressed": _basis.cell(suppressed, _basis.MEASURED),
        "in_app_events": _basis.cell(notif_events, _basis.MEASURED),
        "delivery_outcomes": {
            k: _basis.cell(v, _basis.MEASURED) for k, v in sorted(delivery.items())
        },
        "outcome_rate": _basis.cell(
            round(outcome_rate, 6) if outcome_rate is not None else None,
            _basis.MEASURED if outcome_rate is not None else _basis.NOT_INSTRUMENTED,
            formula_id="SLO-6"),
        "disclosure": ("FE↔BE notification HTTP endpoint stays inert (feature-flag "
                       "fail-closed) per GOV-771; metrics captured at the backend "
                       "service layer only."),
    }


# ---------------------------------------------------------------------------
# §2.7 Capacity (synthetic baseline + observed — never in the same column)
# ---------------------------------------------------------------------------

def observed_rates(conn, area_id, period) -> dict:
    """Observed job rates from audit + queue timestamps within the window.

    Additive, optional (§2.7): the seed forecast path in ``economics.capacity``
    is untouched, so ``test_economics_capacity.py`` determinism holds.
    """
    pred, params = _ledger._area_pred(area_id)
    row = conn.execute(
        f"SELECT COUNT(*) AS n, MIN(created_at) AS lo, MAX(created_at) AS hi"
        f" FROM mcp_audit_events WHERE {pred} AND {_period_pred('created_at')}",
        (*params, period)).fetchone()
    n = int(row["n"])
    span_s = _span_seconds(row["lo"], row["hi"])
    jobs_per_min = (n / (span_s / 60.0)) if (n and span_s) else None
    return {
        "observed_calls": _basis.cell(n, _basis.MEASURED),
        "observed_window_seconds": _basis.cell(
            round(span_s, 3) if span_s is not None else None,
            _basis.MEASURED if span_s is not None else _basis.NOT_INSTRUMENTED,
            unit="seconds"),
        "observed_jobs_per_min": _basis.cell(
            round(jobs_per_min, 3) if jobs_per_min is not None else None,
            _basis.DERIVED if jobs_per_min is not None else _basis.NOT_INSTRUMENTED,
            unit="jobs_per_min"),
    }


def _span_seconds(lo: str | None, hi: str | None):
    if not lo or not hi:
        return None
    from datetime import datetime

    try:
        return (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).total_seconds()
    except ValueError:
        return None


def _capacity_block(conn, area_id, period, seed) -> dict:
    return {
        "synthetic_baseline": _capacity.forecast(area_id, seed=seed, conn=conn),
        "observed": observed_rates(conn, area_id, period),
    }


# ---------------------------------------------------------------------------
# Top-level extractor
# ---------------------------------------------------------------------------

def extract(conn: sqlite3.Connection, area_id: str | None, period: str, *,
            seed: str = DEFAULT_SEED, support_log_path=None) -> dict:
    """One basis-labeled §2 snapshot for ``(area_id, period)``. Pure read."""
    snap = {
        "schema": SCHEMA,
        "area_id": area_id,
        "period": period,
        "seed": seed,
        "cost": _cost(conn, area_id, period),
        "quality": _quality(conn, area_id, period),
        "latency": _latency(conn, area_id, period),
        "safety": _safety(conn, area_id, period),
        "support": _support(support_log_path),
        "notification": _notification(conn, period),
        "capacity": _capacity_block(conn, area_id, period, seed),
    }
    return snap


def lint(snapshot: dict) -> list[str]:
    """AM-7 basis lint over the whole snapshot (empty list => clean)."""
    return _basis.lint_report(snapshot)
