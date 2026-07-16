"""AM-4: a budget breach pauses the lane + emits exactly one Paperclip outbox row.

RED guard: a breach whose budget is NOT paused is a silent overrun and must
raise. Idempotency: re-running the reconciler never floods (UNIQUE dedupe key).
"""

from __future__ import annotations

import pytest
from conftest import ECON_PERIOD

from economics import budget_link


def _seed_budget(conn, *, budget_id, paused, spent=500, cap=100, window="2026-07"):
    conn.execute(
        "INSERT INTO mcp_budgets (budget_id, provider_id, area_id, window_kind,"
        " cap_units, basis, paused_at, created_utc) VALUES (?, 'fake', 'alpine',"
        " 'month', ?, 'OWNER-SET', ?, '2026-07-05T00:00:00.000+00:00')",
        (budget_id, cap, "2026-07-05T00:00:00.000+00:00" if paused else None),
    )
    conn.execute(
        "INSERT INTO mcp_budget_events (event_id, budget_id, event_kind, window_start,"
        " spent_units, cap_units, created_utc) VALUES (?, ?, 'breach', ?, ?, ?,"
        " '2026-07-05T00:00:00.000+00:00')",
        (f"bev-{budget_id}", budget_id, window, spent, cap),
    )
    conn.commit()


def _outbox_count(conn):
    return conn.execute("SELECT COUNT(*) FROM paperclip_outbox").fetchone()[0]


def test_breach_with_paused_budget_writes_one_outbox_row(econ_conn):
    _seed_budget(econ_conn, budget_id="b-paused", paused=True)
    assert _outbox_count(econ_conn) == 0
    written = budget_link.reconcile(econ_conn)
    assert len(written) == 1
    assert _outbox_count(econ_conn) == 1
    row = econ_conn.execute(
        "SELECT kind, umbrella_key, safe_summary, state FROM paperclip_outbox"
    ).fetchone()
    assert row["kind"] == "economics-budget-breach"
    assert row["state"] == "pending"
    # safe_summary carries only allow-listed scalars — no raw context.
    assert '"area_id": "alpine"' in row["safe_summary"]
    assert '"state": "paused"' in row["safe_summary"]


def test_reconcile_is_idempotent_no_flood(econ_conn):
    _seed_budget(econ_conn, budget_id="b-paused", paused=True)
    budget_link.reconcile(econ_conn)
    written2 = budget_link.reconcile(econ_conn)
    assert written2 == []  # dedupe key already present
    assert _outbox_count(econ_conn) == 1


def test_breach_without_pause_is_a_silent_overrun_red(econ_conn):
    _seed_budget(econ_conn, budget_id="b-open", paused=False)
    with pytest.raises(budget_link.BudgetOverrun):
        budget_link.reconcile(econ_conn)
    # Fail-closed: no outbox row is written when the invariant is violated.
    assert _outbox_count(econ_conn) == 0


def test_is_paused_reflects_budget_state(econ_conn):
    _seed_budget(econ_conn, budget_id="b-paused", paused=True)
    _seed_budget(econ_conn, budget_id="b-open", paused=False)
    assert budget_link.is_paused(econ_conn, "b-paused") is True
    assert budget_link.is_paused(econ_conn, "b-open") is False
