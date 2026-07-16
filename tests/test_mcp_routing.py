"""§3.9 routing regression (D1/D2/D7).

Policy-driven, deterministic provider selection; the local-only guard refuses a
non-local provider fail-closed; a degraded provider is skipped; and the single
chokepoint writes exactly one audit row + one health row per attempt.
"""

from __future__ import annotations

import json

import pytest
from conftest import fake_adapter, seed_local_routing

from mcp_service import health, routing
from mcp_service.errors import MCPDenied
from mcp_service.providers import base as pbase


def _add_provider(conn, provider_id, kind, *, enabled=1, cap=1000, budget=True):
    pbase.register_provider(conn, provider_id=provider_id, kind=kind, budget_cap_units=cap)
    conn.execute("UPDATE mcp_provider_registry SET enabled=?, budget_cap_units=? "
                 "WHERE provider_id=?", (enabled, cap, provider_id))
    if budget:
        from mcp_service import budget as budget_mod
        budget_mod.create_budget(conn, budget_id=f"b-{provider_id}",
                                 provider_id=provider_id, cap_units=cap)
    conn.commit()


def _policy(conn, preference, *, job_kind="lens_analysis", context="local_only",
            policy_id="pol", version="1.0.0", model="fake-1"):
    conn.execute(
        "INSERT INTO mcp_routing_policies(policy_id,version,job_kind,context_class,"
        "provider_preference,model,max_output_units,created_utc) "
        "VALUES(?,?,?,?,?,?,50,'2026-07-16T00:00:00.000+00:00')",
        (policy_id, version, job_kind, context, json.dumps(preference), model))
    conn.commit()


def test_policy_eval_fail_closed_when_no_policy(mcp_conn):
    with pytest.raises(MCPDenied) as exc:
        routing.evaluate_policy(mcp_conn, job_kind="nope", context_class="local_only")
    assert exc.value.code == "denied:unsupported"


def test_deterministic_ordered_preference(routed):
    # 'fake' is callable+local (from fixture); add a second local provider later in
    # the order — selection must always return the FIRST eligible, deterministically.
    _add_provider(routed, "fake2", "local")
    routed.execute("DELETE FROM mcp_routing_policies")
    _policy(routed, ["fake", "fake2"])
    policy = routing.evaluate_policy(routed, job_kind="lens_analysis",
                                     context_class="local_only")
    assert policy.provider_preference == ("fake", "fake2")
    for _ in range(5):
        assert routing.select_provider(routed, policy) == "fake"


def test_d7_local_only_skips_non_local_provider(routed):
    # A callable but NON-local provider must be skipped for a local_only policy.
    _add_provider(routed, "cloudco", "cloud")
    routed.execute("DELETE FROM mcp_routing_policies")
    _policy(routed, ["cloudco", "fake"])
    policy = routing.evaluate_policy(routed, job_kind="lens_analysis",
                                     context_class="local_only")
    assert routing.is_local_provider(routed, "cloudco") is False
    assert routing.select_provider(routed, policy) == "fake"  # cloud skipped


def test_d7_refuses_when_only_non_local_available(routed):
    _add_provider(routed, "cloudco", "cloud")
    routed.execute("DELETE FROM mcp_routing_policies")
    _policy(routed, ["cloudco"])  # no local option
    with pytest.raises(MCPDenied) as exc:
        routing.route_and_generate(
            routed, job_kind="lens_analysis", context_class="local_only",
            adapters={"cloudco": fake_adapter("cloudco")}, context_parts=["ctx"],
            job_id="job1")
    assert exc.value.code == "denied:capability"  # no eligible provider, no fallback


def test_degraded_provider_is_skipped(routed):
    for _ in range(health.DEFAULT_DEGRADE_THRESHOLD):
        health.record(routed, provider_id="fake", outcome="error", error_code="boom")
    assert health.is_degraded(routed, "fake") is True
    policy = routing.evaluate_policy(routed, job_kind="lens_analysis",
                                     context_class="local_only")
    assert routing.select_provider(routed, policy) is None
    with pytest.raises(MCPDenied) as exc:
        routing.route_and_generate(
            routed, job_kind="lens_analysis", context_class="local_only",
            adapters={"fake": fake_adapter()}, context_parts=["ctx"], job_id="job1")
    assert exc.value.code == "denied:capability"


def test_one_success_clears_degraded_streak(routed):
    for _ in range(health.DEFAULT_DEGRADE_THRESHOLD):
        health.record(routed, provider_id="fake", outcome="error")
    health.record(routed, provider_id="fake", outcome="ok")
    assert health.is_degraded(routed, "fake") is False


def test_route_writes_exactly_one_audit_and_health_per_attempt(routed):
    a0 = routed.execute("SELECT COUNT(*) FROM mcp_audit_events WHERE kind='provider'").fetchone()[0]
    h0 = routed.execute("SELECT COUNT(*) FROM mcp_provider_health").fetchone()[0]
    res = routing.route_and_generate(
        routed, job_kind="lens_analysis", context_class="local_only",
        adapters={"fake": fake_adapter()}, context_parts=["a", "b"], job_id="job1",
        area_id="alpine", lens_version="1.0.0")
    a1 = routed.execute("SELECT COUNT(*) FROM mcp_audit_events WHERE kind='provider'").fetchone()[0]
    h1 = routed.execute("SELECT COUNT(*) FROM mcp_provider_health").fetchone()[0]
    assert (a1 - a0, h1 - h0) == (1, 1)
    assert res.provider_id == "fake" and res.result.output_units > 0
    row = routed.execute(
        "SELECT provider, policy_version, lens_version, outcome FROM mcp_audit_events "
        "WHERE kind='provider' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["provider"] == "fake" and row["outcome"] == "allow"
    assert row["policy_version"] == "1.0.0" and row["lens_version"] == "1.0.0"


def test_adapter_exception_records_error_health_and_deny_audit(routed):
    class Boom:
        provider_id, kind = "fake", "fake"
        def capabilities(self):  # noqa: D401
            return {"local": True}
        def generate(self, request):
            raise RuntimeError("kaboom")

    with pytest.raises(MCPDenied):
        routing.route_and_generate(
            routed, job_kind="lens_analysis", context_class="local_only",
            adapters={"fake": Boom()}, context_parts=["x"], job_id="job1")
    last_health = routed.execute(
        "SELECT outcome FROM mcp_provider_health ORDER BY rowid DESC LIMIT 1").fetchone()
    last_audit = routed.execute(
        "SELECT outcome FROM mcp_audit_events WHERE kind='provider' "
        "ORDER BY rowid DESC LIMIT 1").fetchone()
    assert last_health["outcome"] == "error"
    assert last_audit["outcome"] == "deny"
