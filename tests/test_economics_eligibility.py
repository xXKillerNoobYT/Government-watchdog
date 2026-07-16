"""F-ELIG + entitlement readiness: RECOMMEND-ONLY ("define, not activate")."""

from __future__ import annotations

from conftest import ECON_PERIOD

from economics import basis, eligibility


def test_evaluate_is_recommendation_only_never_writes_state(econ_conn):
    before = econ_conn.execute("SELECT COUNT(*) FROM area_state").fetchone()[0]
    before_tx = econ_conn.execute("SELECT COUNT(*) FROM area_transitions").fetchone()[0]
    rec = eligibility.evaluate(econ_conn, "alpine", ECON_PERIOD)
    # No state row and no transition row was created by evaluating.
    assert econ_conn.execute("SELECT COUNT(*) FROM area_state").fetchone()[0] == before
    assert econ_conn.execute("SELECT COUNT(*) FROM area_transitions").fetchone()[0] == before_tx
    assert "recommendation only" in rec["note"]


def test_f_elig_true_when_cost_within_funded_headroom(econ_conn):
    # alpine measured cost = 250; funding 5000 * safety 1.5 = 7500 -> eligible.
    rec = eligibility.evaluate(econ_conn, "alpine", ECON_PERIOD)
    assert rec["monthly_measured_cost"]["value"] == 250
    assert rec["monthly_funding_balance"]["value"] == 5000
    assert rec["safety_factor"]["value"] == 1.5
    assert rec["f_elig"] is True
    assert rec["recommended_state"] == "funded"


def test_recommendation_cells_are_basis_labeled(econ_conn):
    rec = eligibility.evaluate(econ_conn, "alpine", ECON_PERIOD)
    for key in ("monthly_measured_cost", "monthly_funding_balance", "safety_factor"):
        assert rec[key]["basis"] in basis.ACCEPTED_BASIS_LABELS


def test_entitlement_readiness_reports_designed_but_inert(econ_conn):
    rec = eligibility.evaluate(econ_conn, "alpine", ECON_PERIOD)
    ent = rec["entitlement"]
    assert ent["has_designed_entitlement"] is True
    assert ent["activation_gate"].startswith("GATE-P")
    # every seeded entitlement is still 'designed' — nothing activated.
    assert all(e["state"] == "designed" for e in ent["entitlements"])


def test_unset_safety_factor_defaults_owner_set_unset(econ_conn):
    # etna has no funding_policy row -> default 1.0, labeled OWNER-SET (unset).
    rec = eligibility.evaluate(econ_conn, "etna", ECON_PERIOD)
    assert rec["safety_factor"]["value"] == 1.0
    assert rec["safety_factor"]["basis"] == basis.OWNER_SET_UNSET
