"""PILOT-2026 §5.3 tests 1 + 4: workload determinism + zero-credit (GOV-781)."""

from __future__ import annotations

import pytest

from pilot import workload


# --- §5.3 test 1: workload dry-run determinism --------------------------------

def test_manifest_is_deterministic():
    """Same seed + bounds => byte-identical planned-job manifest."""
    b = {"WL-1": 7, "WL-2": 3, "WL-3": 9, "WL-4": 1, "WL-5": 4, "WL-6": 2}
    m1 = workload.manifest("seed-xyz", b)
    m2 = workload.manifest("seed-xyz", b)
    assert m1["manifest_sha256"] == m2["manifest_sha256"]
    assert m1["planned_jobs"] == m2["planned_jobs"]


def test_manifest_varies_with_seed():
    m1 = workload.manifest("seed-A")
    m2 = workload.manifest("seed-B")
    # WL-2 nonces are seed-derived, so the manifests differ.
    assert m1["manifest_sha256"] != m2["manifest_sha256"]


def test_plan_counts_fan_out_lenses_and_probes():
    b = {"WL-1": 10, "WL-2": 5, "WL-3": 10, "WL-4": 1, "WL-5": 4, "WL-6": 3}
    jobs = workload.plan("s", b)
    wl2 = [j for j in jobs if j["wl"] == "WL-2"]
    lens_packs = {j["detail"] for j in wl2}
    assert len(wl2) == 5 * len(lens_packs)  # one block per shipped pack
    wl5 = [j for j in jobs if j["wl"] == "WL-5"]
    assert sum(1 for j in wl5 if j["detail"] == "no_consent_probe") == 2


def test_plan_respects_total_cap():
    with pytest.raises(ValueError, match="MAX_TOTAL_CALLS"):
        workload.plan("s", {"WL-1": 999, "WL-2": 999, "WL-3": 999})


def test_dry_run_writes_nothing(pilot_conn):
    before = pilot_conn.execute("SELECT COUNT(*) FROM mcp_audit_events").fetchone()[0]
    rep = workload.run(pilot_conn, seed="s", apply=False)
    assert rep["applied"] is False
    after = pilot_conn.execute("SELECT COUNT(*) FROM mcp_audit_events").fetchone()[0]
    assert before == after == 0


# --- apply run: every WL path fires -------------------------------------------

def test_apply_runs_all_wl_types(pilot_applied):
    _, rep = pilot_applied
    c = rep["counts"]
    assert c["WL-1"] == 5
    assert c["WL-2"] == 2 * 3  # 2 per pack x 3 shipped packs
    assert c["WL-3"] == 4
    assert c["WL-6"] == 2
    assert c["WL-4"] == {"scope_deny": 1, "redaction_deny": 1,
                         "budget_deny": 1, "revocation_deny": 1}
    assert c["WL-5"] == {"sent": 3, "no_consent_refused": 2}


# --- §5.3 test 4: zero-credit assertion over a fake-adapter run ----------------

def test_zero_credit_assertion_passes(pilot_applied):
    _, rep = pilot_applied
    zc = rep["zero_credit"]
    assert zc["ok"] is True
    assert zc["non_local_providers"] == []
    # The only breach is the declared WL-4(c) probe.
    assert zc["breach_budget_ids"] == ["budget-pilot-breach-probe"]


def test_zero_credit_flags_a_nonlocal_provider(pilot_applied):
    conn, rep = pilot_applied
    # Inject a rogue non-local provider audit row into the run window.
    conn.execute(
        "INSERT INTO mcp_audit_events (audit_id, kind, name, outcome, provider,"
        " input_units, output_units, direct_cost_units, created_at)"
        " VALUES ('rogue', 'provider', 'x', 'allow', 'openai-gpt', 1, 1, 100, ?)",
        (rep["period"] + "-15T00:00:00.000+00:00",))
    conn.commit()
    with pytest.raises(workload.ZeroCreditViolation, match="non-local provider"):
        workload.assert_zero_credit(conn, rep["period"],
                                    breach_budget_ids=["budget-pilot-breach-probe"])
