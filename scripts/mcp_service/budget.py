"""Fail-closed budget enforcement (PLAN-2026-AI §3.2, BUD-1…5, D3, AM-4/AM-11).

A budget caps metered spend for a provider over a window. The rules, all
fail-closed:

* **BUD-5 / AM-11 — no budget, no call.** A provider with no budget row, or a
  ``cap_units <= 0`` budget, is un-callable; :func:`preflight` refuses before any
  adapter is touched. There is no "succeed at cap 0".
* **BUD-1 — window spend is measured, not trusted.** Spend is summed live from
  ``mcp_audit_events`` (the LED-1 cost envelope every call already writes), never
  a running counter that could drift. Local calls meter units too (BUD-3), so a
  free local provider still has a comparable, enforceable cap.
* **D3 — breach fails closed via the existing outbox.** On a projected breach the
  call is refused, the budget is *paused* (``paused_at``), a ``mcp_budget_events``
  breach row is written, and a Paperclip issue is enqueued through GOV-733's
  ``paperclip_outbox.write_outbox_row`` with dedupe key
  ``mcp-budget-breach:<budget_id>:<window-start>`` — the UNIQUE dedupe prevents an
  issue flood (AM-4). No silent overrun, no auto-raise.
* **BUD-4 — owner changes leave a trail.** Raising a cap or resuming a paused
  budget writes an ``owner-change``/``resume`` event carrying the ``audit_ref``
  that authorized it. Un-pausing is never automatic.

This module writes only its own two tables (``mcp_budgets``,
``mcp_budget_events``) and the shared ``paperclip_outbox``. It never touches a
canonical table or the provider registry.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DENY_BUDGET, MCPDenied

# paperclip_outbox is a sibling leaf in scripts/ (GOV-733). Import it the same way
# redaction.py imports the frozen scanners — one copy, no drift, no re-implement.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import paperclip_outbox as _outbox  # noqa: E402


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Budget:
    """A BUD-1 budget object, loaded from ``mcp_budgets``."""

    budget_id: str
    provider_id: str
    area_id: str | None
    window_kind: str
    cap_units: int
    basis: str
    paused_at: str | None


def create_budget(
    conn: sqlite3.Connection,
    *,
    budget_id: str,
    provider_id: str,
    cap_units: int,
    area_id: str | None = None,
    window_kind: str = "total",
    basis: str = "OWNER-SET",
) -> Budget:
    """Create an owner-set budget (idempotent on ``budget_id``)."""
    conn.execute(
        "INSERT OR IGNORE INTO mcp_budgets "
        "(budget_id, provider_id, area_id, window_kind, cap_units, basis, "
        " paused_at, created_utc) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
        (budget_id, provider_id, area_id, window_kind, int(cap_units), basis, _utcnow()),
    )
    conn.commit()
    loaded = load_budget(conn, budget_id)
    assert loaded is not None  # just inserted
    return loaded


def load_budget(conn: sqlite3.Connection, budget_id: str) -> Budget | None:
    row = conn.execute(
        "SELECT * FROM mcp_budgets WHERE budget_id = ?", (budget_id,)
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    return Budget(
        budget_id=row["budget_id"],
        provider_id=row["provider_id"],
        area_id=row["area_id"],
        window_kind=row["window_kind"],
        cap_units=int(row["cap_units"]),
        basis=row["basis"],
        paused_at=row["paused_at"],
    )


def budget_for_provider(conn: sqlite3.Connection, provider_id: str) -> Budget | None:
    """The provider's budget, if an owner has created one (BUD-5)."""
    row = conn.execute(
        "SELECT budget_id FROM mcp_budgets WHERE provider_id = ? "
        "ORDER BY created_utc DESC, budget_id LIMIT 1",
        (provider_id,),
    ).fetchone()
    return load_budget(conn, row["budget_id"]) if row else None


def window_start(window_kind: str, now_utc: str | None = None) -> str:
    """The dedupe/label boundary for a window.

    ``total`` -> ``'total'``; ``day`` -> ``YYYY-MM-DD``; ``month`` -> ``YYYY-MM``.
    ``now_utc`` is injectable so tests are deterministic and hermetic.
    """
    if window_kind == "total":
        return "total"
    now = now_utc or _utcnow()
    if window_kind == "day":
        return now[:10]
    if window_kind == "month":
        return now[:7]
    raise ValueError(f"unknown window_kind {window_kind!r}")


def window_spend(
    conn: sqlite3.Connection, budget: Budget, now_utc: str | None = None
) -> int:
    """Live spend for ``budget``'s provider inside its window.

    Sums ``input_units + output_units`` over *allowed* audit rows for the
    provider — the same metered units the cap is expressed in. Denied attempts
    (no generation) do not count. Computed from ``mcp_audit_events`` so it can
    never diverge from what actually happened.
    """
    sql = (
        "SELECT COALESCE(SUM(input_units + output_units), 0) AS spent "
        "FROM mcp_audit_events WHERE provider = ? AND outcome = 'allow'"
    )
    params: list[Any] = [budget.provider_id]
    if budget.window_kind != "total":
        sql += " AND created_at LIKE ?"
        params.append(window_start(budget.window_kind, now_utc) + "%")
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row["spent"])


def preflight(
    conn: sqlite3.Connection,
    budget: Budget | None,
    *,
    estimated_units: int = 0,
    now_utc: str | None = None,
) -> int:
    """Fail-closed budget gate run BEFORE any adapter call. Returns window spend.

    Raises :class:`MCPDenied` (``denied:budget``) when there is no budget, the cap
    is <= 0 (AM-11), the budget is paused, or the projected spend
    (``window_spend + estimated_units``) would exceed the cap (D3). A projected
    breach also pauses the budget and enqueues the throttled Paperclip issue.
    """
    if budget is None:
        raise MCPDenied(DENY_BUDGET, "no budget for provider (BUD-5)")
    if budget.cap_units <= 0:
        raise MCPDenied(DENY_BUDGET, f"budget {budget.budget_id} cap is {budget.cap_units} (AM-11)")
    if budget.paused_at:
        raise MCPDenied(DENY_BUDGET, f"budget {budget.budget_id} paused at {budget.paused_at}")
    spent = window_spend(conn, budget, now_utc)
    if spent + int(estimated_units) > budget.cap_units:
        record_breach(conn, budget, spent=spent, now_utc=now_utc)
        raise MCPDenied(
            DENY_BUDGET,
            f"budget {budget.budget_id} breach: {spent}+{estimated_units} > {budget.cap_units}",
        )
    return spent


def record_breach(
    conn: sqlite3.Connection,
    budget: Budget,
    *,
    spent: int,
    now_utc: str | None = None,
) -> None:
    """Pause the budget, write a breach event, and enqueue a throttled issue (D3).

    Idempotent within a window: the outbox dedupe key
    ``mcp-budget-breach:<budget_id>:<window-start>`` is UNIQUE, so repeated
    breaches in the same window collapse into one Paperclip issue (AM-4).
    """
    now = now_utc or _utcnow()
    wstart = window_start(budget.window_kind, now)
    conn.execute(
        "UPDATE mcp_budgets SET paused_at = ? WHERE budget_id = ? AND paused_at IS NULL",
        (now, budget.budget_id),
    )
    conn.execute(
        "INSERT INTO mcp_budget_events "
        "(event_id, budget_id, event_kind, window_start, spent_units, cap_units, "
        " audit_ref, note, created_utc) VALUES (?, ?, 'breach', ?, ?, ?, NULL, ?, ?)",
        (
            f"bev-{uuid.uuid4()}", budget.budget_id, wstart, int(spent),
            budget.cap_units, "budget cap exceeded; lane paused (D3)", now,
        ),
    )
    # Fail-closed Paperclip notification via GOV-733's outbox. safe_summary drops
    # anything not scalar/allow-listed, so no raw context can ride along.
    _outbox.write_outbox_row(
        conn,
        kind="mcp-budget-breach",
        dedupe_key=f"mcp-budget-breach:{budget.budget_id}:{wstart}",
        umbrella_key=f"umbrella:mcp-budget-breach:{wstart}",
        summary={
            "kind": "mcp-budget-breach",
            "area_id": budget.area_id,
            "state": "paused",
            "count": int(spent),
        },
        created_at=now,
    )
    conn.commit()


def set_cap(
    conn: sqlite3.Connection,
    budget_id: str,
    *,
    new_cap: int,
    audit_ref: str,
    note: str | None = None,
) -> Budget:
    """Owner-change the cap, recording the authorizing ``audit_ref`` (BUD-4)."""
    budget = load_budget(conn, budget_id)
    if budget is None:
        raise MCPDenied(DENY_BUDGET, f"budget {budget_id!r} not found")
    conn.execute(
        "UPDATE mcp_budgets SET cap_units = ? WHERE budget_id = ?",
        (int(new_cap), budget_id),
    )
    _record_owner_event(conn, budget_id, "owner-change", audit_ref=audit_ref,
                        cap_units=int(new_cap), note=note or f"cap -> {new_cap}")
    conn.commit()
    loaded = load_budget(conn, budget_id)
    assert loaded is not None
    return loaded


def resume(
    conn: sqlite3.Connection,
    budget_id: str,
    *,
    audit_ref: str,
    note: str | None = None,
) -> Budget:
    """Clear a pause. Un-pausing is an owner/CTO decision with an audit ref (BUD-4)."""
    budget = load_budget(conn, budget_id)
    if budget is None:
        raise MCPDenied(DENY_BUDGET, f"budget {budget_id!r} not found")
    conn.execute("UPDATE mcp_budgets SET paused_at = NULL WHERE budget_id = ?", (budget_id,))
    _record_owner_event(conn, budget_id, "resume", audit_ref=audit_ref,
                        cap_units=budget.cap_units, note=note or "owner resume")
    conn.commit()
    loaded = load_budget(conn, budget_id)
    assert loaded is not None
    return loaded


def _record_owner_event(
    conn: sqlite3.Connection,
    budget_id: str,
    event_kind: str,
    *,
    audit_ref: str,
    cap_units: int,
    note: str | None,
) -> None:
    conn.execute(
        "INSERT INTO mcp_budget_events "
        "(event_id, budget_id, event_kind, window_start, spent_units, cap_units, "
        " audit_ref, note, created_utc) VALUES (?, ?, ?, NULL, 0, ?, ?, ?, ?)",
        (f"bev-{uuid.uuid4()}", budget_id, event_kind, int(cap_units),
         audit_ref, note, _utcnow()),
    )
