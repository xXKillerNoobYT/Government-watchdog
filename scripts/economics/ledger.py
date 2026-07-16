"""LED-1 per-job cost aggregation + SLO metrics (LEDGER-2026 §2, LED-1, AM-12).

Reads the cost substrate that GOV-717/718/719 already landed and rolls it up by
``(area_id, period, lane)``. This module NEVER writes — it is pure read
aggregation over:

* ``event_jobs``       (0021) — compute/queue half of LED-1, keyed by ``area_id``,
  bucketed by ``lane``.
* ``mcp_audit_events`` (0022) — provider/model half of LED-1, the MEASURED
  ``direct_cost_units`` that feed F1, keyed by ``area_id``.
* ``ai_extraction_runs`` (0019) + ``crawl_runs`` (0019) — have NO ``area_id``, so
  they are un-attributable and roll into the ``area_id IS NULL`` shared-cost pool
  (AREA-2), never smeared onto a named area.

``period`` is a ``'YYYY-MM'`` string; rows are matched by the first 7 chars of
their UTC timestamp column. Passing ``area_id=None`` selects the shared pool
(``area_id IS NULL``).
"""

from __future__ import annotations

import sqlite3

# SLO contract (REQ-2026-COMM §7). (slo_id, name, target_value, target_unit).
# Targets are ASSUMED (local-server class) except SLO-5 which has no target until
# a pilot. AM-12 requires every one of these emitted per-area.
SLO_DEFS = (
    ("SLO-1", "ingest_freshness", 72.0, "hours"),
    ("SLO-2", "processing_latency", 7.0, "days"),
    ("SLO-3", "read_latency_p95", 500.0, "ms"),
    ("SLO-4", "availability", 99.0, "percent"),
    ("SLO-5", "review_turnaround", None, "days"),
    ("SLO-6", "notification_outcome_rate", 95.0, "percent"),
)


def _area_pred(area_id: str | None) -> tuple[str, tuple]:
    """Return an SQL predicate + params for ``area_id = ?`` or ``area_id IS NULL``."""
    if area_id is None:
        return "area_id IS NULL", ()
    return "area_id = ?", (area_id,)


def job_cost_units(conn: sqlite3.Connection, area_id: str | None, period: str) -> list[int]:
    """Per-call MEASURED ``direct_cost_units`` for one (area, period) — F1 input."""
    pred, params = _area_pred(area_id)
    rows = conn.execute(
        f"SELECT direct_cost_units FROM mcp_audit_events"
        f" WHERE {pred} AND substr(created_at, 1, 7) = ?",
        (*params, period),
    ).fetchall()
    return [int(r[0]) for r in rows]


def documents_processed(conn: sqlite3.Connection, area_id: str | None, period: str) -> int:
    """Documents processed = count of ``event_jobs`` for the (area, period).

    Each micro-job processes one source/document, so the job count is the
    per-area document proxy that feeds F5 (cost_per_document) and F7 (weight).
    """
    pred, params = _area_pred(area_id)
    row = conn.execute(
        f"SELECT COUNT(*) FROM event_jobs"
        f" WHERE {pred} AND substr(enqueued_at, 1, 7) = ?",
        (*params, period),
    ).fetchone()
    return int(row[0])


def document_counts(conn: sqlite3.Connection, period: str) -> dict[str | None, int]:
    """{area_id: document count} across ALL areas for a period (F7 denominator)."""
    rows = conn.execute(
        "SELECT area_id, COUNT(*) FROM event_jobs"
        " WHERE substr(enqueued_at, 1, 7) = ? GROUP BY area_id",
        (period,),
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def lane_rollup(conn: sqlite3.Connection, area_id: str | None, period: str) -> dict:
    """Per-lane compute/queue aggregation from ``event_jobs`` (LED-1)."""
    pred, params = _area_pred(area_id)
    rows = conn.execute(
        f"SELECT lane,"
        f"       COUNT(*)               AS job_count,"
        f"       COALESCE(SUM(cpu_s), 0)        AS cpu_s,"
        f"       COALESCE(SUM(queue_wait_s), 0) AS queue_wait_s,"
        f"       COALESCE(SUM(retry_count), 0)  AS retry_count,"
        f"       COALESCE(SUM(cache_hit), 0)    AS cache_hits"
        f" FROM event_jobs"
        f" WHERE {pred} AND substr(enqueued_at, 1, 7) = ?"
        f" GROUP BY lane ORDER BY lane",
        (*params, period),
    ).fetchall()
    return {
        r["lane"]: {
            "job_count": int(r["job_count"]),
            "cpu_s": float(r["cpu_s"]),
            "queue_wait_s": float(r["queue_wait_s"]),
            "retry_count": int(r["retry_count"]),
            "cache_hits": int(r["cache_hits"]),
        }
        for r in rows
    }


def provider_rollup(conn: sqlite3.Connection, area_id: str | None, period: str) -> dict:
    """Provider/model aggregation from ``mcp_audit_events`` (LED-1 MEASURED units)."""
    pred, params = _area_pred(area_id)
    row = conn.execute(
        f"SELECT COUNT(*)                       AS call_count,"
        f"       COALESCE(SUM(input_units), 0)       AS input_units,"
        f"       COALESCE(SUM(output_units), 0)      AS output_units,"
        f"       COALESCE(SUM(direct_cost_units), 0) AS direct_cost_units,"
        f"       COALESCE(SUM(cache_hit), 0)         AS cache_hits"
        f" FROM mcp_audit_events"
        f" WHERE {pred} AND substr(created_at, 1, 7) = ?",
        (*params, period),
    ).fetchone()
    return {
        "call_count": int(row["call_count"]),
        "input_units": int(row["input_units"]),
        "output_units": int(row["output_units"]),
        "direct_cost_units": int(row["direct_cost_units"]),
        "cache_hits": int(row["cache_hits"]),
    }


def shared_pool_extras(conn: sqlite3.Connection, period: str) -> dict:
    """Un-attributable substrate (no ``area_id``) — reported only in the shared pool.

    ``ai_extraction_runs`` (tokens + ASSUMED est. usd) and ``crawl_runs``
    (documents / skipped-by-hash) cannot be tied to an area, so per AREA-2 they
    are surfaced here and NEVER folded into a named area's totals.
    """
    ext = conn.execute(
        "SELECT COUNT(*)                          AS run_count,"
        "       COALESCE(SUM(tokens_input), 0)         AS tokens_input,"
        "       COALESCE(SUM(tokens_output), 0)        AS tokens_output,"
        "       COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd"
        " FROM ai_extraction_runs WHERE substr(started_utc, 1, 7) = ?",
        (period,),
    ).fetchone()
    crawl = conn.execute(
        "SELECT COUNT(*)                       AS run_count,"
        "       COALESCE(SUM(new_documents), 0)     AS new_documents,"
        "       COALESCE(SUM(skipped_hash), 0)      AS skipped_hash"
        " FROM crawl_runs WHERE substr(started_utc, 1, 7) = ?",
        (period,),
    ).fetchone()
    return {
        "extraction": {
            "run_count": int(ext["run_count"]),
            "tokens_input": int(ext["tokens_input"]),
            "tokens_output": int(ext["tokens_output"]),
            # estimated_cost_usd is an ASSUMED provider list-price ESTIMATE, not a
            # customer price or an asserted operating cost. It is surfaced as a
            # scalar annotation (NOT a value-cell), so it is disclosed here but
            # never enters the unit-only LED-6 export or the basis lint walk.
            "estimated_cost_usd_assumed": float(ext["estimated_cost_usd"]),
            "estimated_cost_usd_basis": "ASSUMED",
            "estimated_cost_usd_disclosure": (
                "provider list-price estimate; not a customer price, not an asserted cost"
            ),
        },
        "ingest": {
            "run_count": int(crawl["run_count"]),
            "new_documents": int(crawl["new_documents"]),
            "skipped_hash": int(crawl["skipped_hash"]),
        },
    }


def aggregate(conn: sqlite3.Connection, area_id: str | None, period: str) -> dict:
    """Full LED-1 aggregation for one (area, period)."""
    agg = {
        "area_id": area_id,
        "period": period,
        "lanes": lane_rollup(conn, area_id, period),
        "provider": provider_rollup(conn, area_id, period),
        "job_cost_units": job_cost_units(conn, area_id, period),
        "documents_processed": documents_processed(conn, area_id, period),
    }
    if area_id is None:
        agg["shared_pool_extras"] = shared_pool_extras(conn, period)
    return agg


def _read_latency_p95(conn: sqlite3.Connection, area_id: str | None, period: str):
    """SLO-3: p95 of ``mcp_audit_events.latency_ms`` for the area (MEASURED)."""
    pred, params = _area_pred(area_id)
    rows = conn.execute(
        f"SELECT latency_ms FROM mcp_audit_events"
        f" WHERE {pred} AND substr(created_at, 1, 7) = ? AND latency_ms IS NOT NULL"
        f" ORDER BY latency_ms",
        (*params, period),
    ).fetchall()
    vals = [int(r[0]) for r in rows]
    if not vals:
        return None
    # Nearest-rank p95 (deterministic, no interpolation).
    idx = max(0, (95 * len(vals) + 99) // 100 - 1)
    return float(vals[idx])


def _avg_processing_latency_s(conn: sqlite3.Connection, area_id: str | None, period: str):
    """SLO-2 proxy: mean (queue_wait_s + cpu_s) over the area's jobs (MEASURED)."""
    pred, params = _area_pred(area_id)
    row = conn.execute(
        f"SELECT AVG(COALESCE(queue_wait_s, 0) + COALESCE(cpu_s, 0))"
        f" FROM event_jobs WHERE {pred} AND substr(enqueued_at, 1, 7) = ?",
        (*params, period),
    ).fetchone()
    return None if row[0] is None else float(row[0])


def slo_metrics(conn: sqlite3.Connection, area_id: str | None, period: str) -> list[dict]:
    """AM-12 / SLO-7: emit ALL six SLO metrics per-area for the ledger surface.

    Each entry carries a ``measured`` value (MEASURED where derivable from the
    substrate, else a ``n/a (not yet instrumented)`` labeled hole) and a
    ``target`` (ASSUMED, or ``OWNER-SET (unset)`` where the contract sets no
    target). Presence of all six with valid bases is what AM-12 checks.
    """
    from . import basis as _basis

    measured_map = {
        "SLO-2": _avg_processing_latency_s(conn, area_id, period),
        "SLO-3": _read_latency_p95(conn, area_id, period),
    }
    out: list[dict] = []
    for slo_id, name, target_value, unit in SLO_DEFS:
        measured = measured_map.get(slo_id)
        measured_basis = _basis.MEASURED if measured is not None else _basis.NOT_INSTRUMENTED
        target_basis = _basis.ASSUMED if target_value is not None else _basis.OWNER_SET_UNSET
        out.append({
            "slo_id": slo_id,
            "name": name,
            "measured": _basis.cell(measured, measured_basis, unit=unit),
            "target": _basis.cell(target_value, target_basis, unit=unit),
        })
    return out
