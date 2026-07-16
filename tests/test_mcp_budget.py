"""§3.9 budget regression (BUD-1…5, D3, AM-4/AM-11, BUD-3).

Fail-closed pre-flight; a projected breach pauses the lane, writes a budget event,
and enqueues exactly one Paperclip outbox row per window (dedupe = AM-4); a cap-0
provider is rejected (AM-11); local Ollama calls still meter units (BUD-3); and
owner cap/resume changes leave an audit-ref trail (BUD-4).
"""

from __future__ import annotations

import pytest
from conftest import fake_adapter, seed_local_routing

from mcp_service import budget as budget_mod
from mcp_service import routing
from mcp_service.errors import MCPDenied
from mcp_service.providers import base as pbase
from mcp_service.providers.ollama import OllamaAdapter


def test_preflight_no_budget_is_denied_bud5(mcp_conn):
    with pytest.raises(MCPDenied) as exc:
        budget_mod.preflight(mcp_conn, None, estimated_units=1)
    assert exc.value.code == "denied:budget"


def test_preflight_cap_zero_is_denied_am11(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="b0", provider_id="fake", cap_units=0)
    with pytest.raises(MCPDenied) as exc:
        budget_mod.preflight(mcp_conn, b, estimated_units=0)
    assert exc.value.code == "denied:budget"  # cap 0 never "succeeds"


def test_preflight_ok_under_cap_returns_spend(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="b1", provider_id="fake", cap_units=100)
    assert budget_mod.preflight(mcp_conn, b, estimated_units=10) == 0


def test_projected_breach_pauses_writes_event_and_outbox(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="bb", provider_id="fake",
                                 cap_units=5, area_id="alpine")
    with pytest.raises(MCPDenied) as exc:
        budget_mod.preflight(mcp_conn, b, estimated_units=10)  # 0 + 10 > 5
    assert exc.value.code == "denied:budget"
    reloaded = budget_mod.load_budget(mcp_conn, "bb")
    assert reloaded.paused_at is not None  # lane paused (D3)
    events = mcp_conn.execute(
        "SELECT event_kind FROM mcp_budget_events WHERE budget_id='bb'").fetchall()
    assert [e["event_kind"] for e in events] == ["breach"]
    outbox = mcp_conn.execute(
        "SELECT dedupe_key FROM paperclip_outbox WHERE dedupe_key LIKE 'mcp-budget-breach:%'"
    ).fetchall()
    assert outbox and outbox[0]["dedupe_key"] == "mcp-budget-breach:bb:total"


def test_breach_outbox_is_deduped_per_window_am4(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="bb", provider_id="fake", cap_units=5)
    # Two breaches in the same (total) window collapse into one outbox row.
    budget_mod.record_breach(mcp_conn, b, spent=99)
    budget_mod.record_breach(mcp_conn, b, spent=150)
    n = mcp_conn.execute(
        "SELECT COUNT(*) FROM paperclip_outbox WHERE dedupe_key='mcp-budget-breach:bb:total'"
    ).fetchone()[0]
    assert n == 1  # UNIQUE dedupe prevents an issue flood


def test_paused_budget_is_denied_before_spend_check(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="bp", provider_id="fake", cap_units=1000)
    budget_mod.record_breach(mcp_conn, b, spent=0)  # pauses it
    paused = budget_mod.load_budget(mcp_conn, "bp")
    with pytest.raises(MCPDenied):
        budget_mod.preflight(mcp_conn, paused, estimated_units=0)


def test_resume_and_set_cap_record_audit_ref_bud4(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="bp", provider_id="fake", cap_units=5)
    budget_mod.record_breach(mcp_conn, b, spent=99)
    budget_mod.resume(mcp_conn, "bp", audit_ref="mcp-abc123", note="owner ok")
    budget_mod.set_cap(mcp_conn, "bp", new_cap=500, audit_ref="mcp-def456")
    rows = mcp_conn.execute(
        "SELECT event_kind, audit_ref FROM mcp_budget_events "
        "WHERE budget_id='bp' AND event_kind IN ('resume','owner-change') ORDER BY event_kind"
    ).fetchall()
    kinds = {(r["event_kind"], r["audit_ref"]) for r in rows}
    assert ("resume", "mcp-abc123") in kinds
    assert ("owner-change", "mcp-def456") in kinds
    assert budget_mod.load_budget(mcp_conn, "bp").paused_at is None
    assert budget_mod.load_budget(mcp_conn, "bp").cap_units == 500


def test_window_spend_sums_allowed_audit_units(mcp_conn):
    b = budget_mod.create_budget(mcp_conn, budget_id="bw", provider_id="fake", cap_units=1000)
    from mcp_service import audit
    audit.record(mcp_conn, kind="provider", name="lens", outcome="allow",
                 provider="fake", input_units=7, output_units=3)
    audit.record(mcp_conn, kind="provider", name="lens", outcome="deny",
                 provider="fake", input_units=100, output_units=100)  # deny not counted
    assert budget_mod.window_spend(mcp_conn, b) == 10


def test_ollama_units_metered_through_route_bud3(mcp_conn):
    # A local Ollama call (stub transport, zero network) meters real units so a
    # free local provider still has enforceable, comparable spend.
    seed_local_routing(mcp_conn, provider_id="ollama", kind="ollama", model="llama3.2")
    stub = OllamaAdapter(
        model="llama3.2",
        transport=lambda url, payload: {"response": "a neutral reading",
                                        "prompt_eval_count": 11, "eval_count": 4,
                                        "done": True})
    res = routing.route_and_generate(
        mcp_conn, job_kind="lens_analysis", context_class="local_only",
        adapters={"ollama": stub}, context_parts=["ctx"], job_id="job1",
        area_id="alpine", lens_version="1.0.0")
    assert (res.result.input_units, res.result.output_units) == (11, 4)
    row = mcp_conn.execute(
        "SELECT input_units, output_units, direct_cost_units FROM mcp_audit_events "
        "WHERE provider='ollama' AND outcome='allow' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert (row["input_units"], row["output_units"]) == (11, 4)
    assert row["direct_cost_units"] == 0  # local = zero direct cost, units still metered
    b = budget_mod.budget_for_provider(mcp_conn, "ollama")
    assert budget_mod.window_spend(mcp_conn, b) == 15
