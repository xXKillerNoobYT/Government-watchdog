"""BUD-2 / AM-4 budget-breach linkage (LEDGER-2026 §2).

The mcp_service budget enforcer (0023 / GOV-736) already fails a call closed and
pauses the budget on a breach. This module is the economics-lane reconciler: for
every ``mcp_budget_events`` breach it (1) ASSERTS the budget is actually paused —
a breach that did NOT pause the lane is a silent overrun and a hard RED — and
(2) emits exactly one bounded Paperclip outbox row so the breach surfaces on the
board. The outbox ``dedupe_key`` is UNIQUE, so re-running the reconciler never
floods (AM-4).

It reuses GOV-733's ``paperclip_outbox.write_outbox_row`` verbatim (one writer,
no drift) and passes only allow-listed scalar summary fields — no raw context
can ride along.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# paperclip_outbox is a sibling leaf in scripts/ (economics/ is a subpackage of
# scripts/). Import it the same way mcp_service.budget does — one copy, no drift.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import paperclip_outbox as _outbox  # noqa: E402


class BudgetOverrun(RuntimeError):
    """RED: a budget breach that did not pause its budget/lane (silent overrun)."""


def is_paused(conn: sqlite3.Connection, budget_id: str) -> bool:
    """True iff the budget's ``paused_at`` is set (the lane is failed-closed)."""
    row = conn.execute(
        "SELECT paused_at FROM mcp_budgets WHERE budget_id = ?", (budget_id,)
    ).fetchone()
    return bool(row and row[0])


def reconcile(conn: sqlite3.Connection) -> list[int]:
    """Reconcile every breach event; return the outbox_ids newly written.

    Fail-closed: raises :class:`BudgetOverrun` on the FIRST breach whose budget
    is not paused (no silent overrun). Idempotent otherwise — already-written
    outbox rows are skipped via the UNIQUE dedupe key.
    """
    breaches = conn.execute(
        "SELECT e.event_id, e.budget_id, e.window_start, e.spent_units, e.cap_units,"
        "       b.area_id"
        " FROM mcp_budget_events e"
        " JOIN mcp_budgets b ON b.budget_id = e.budget_id"
        " WHERE e.event_kind = 'breach'"
        " ORDER BY e.event_id",
    ).fetchall()

    written: list[int] = []
    for e in breaches:
        budget_id = e["budget_id"]
        if not is_paused(conn, budget_id):
            raise BudgetOverrun(
                f"budget {budget_id!r} breached but is not paused — silent overrun (AM-4 RED)"
            )
        window = e["window_start"] or "all"
        outbox_id = _outbox.write_outbox_row(
            conn,
            kind="economics-budget-breach",
            dedupe_key=f"economics-budget-breach:{budget_id}:{window}",
            umbrella_key=f"umbrella:economics-budget-breach:{window}",
            summary={
                "kind": "economics-budget-breach",
                "area_id": e["area_id"],
                "state": "paused",
                "count": int(e["spent_units"] or 0),
            },
        )
        if outbox_id is not None:
            written.append(outbox_id)
    conn.commit()
    return written
