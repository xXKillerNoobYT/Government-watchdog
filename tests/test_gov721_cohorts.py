"""ACCT-2026 leg 2 (GOV-754): cohort machine — AC-2/AC-3, INV-3/INV-6, D4.

The INV-6 RED-proof lives in test_cap_rejects_with_current_size_desynced_low:
neuter the in-transaction recompute in ``accounts.cohorts.advance`` (e.g.
read ``cohort_state.current_size`` instead of ``_member_count``) and that
test goes RED while the happy-path tests stay green — proving the cache is
never the enforcement authority.
"""

from __future__ import annotations

import pytest

from accounts import cohorts, service


@pytest.fixture()
def conn(acct2_conn):
    return acct2_conn


def _users(conn, n):
    return [service.create_user(conn, email=f"u{i}@example.com") for i in range(n)]


def _transitions(conn):
    return conn.execute("SELECT COUNT(*) FROM cohort_transitions").fetchone()[0]


# --- AC-3 / INV-3: owner gate on every write --------------------------------------

@pytest.mark.parametrize("ref", [None, ""])
def test_open_and_advance_reject_missing_owner_ref(conn, ref):
    with pytest.raises(cohorts.OwnerlessCohortAction):
        cohorts.open_cohort(conn, "beta-2", owner_decision_ref=ref)
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)
    with pytest.raises(cohorts.OwnerlessCohortAction):
        cohorts.advance(conn, uid, to_cohort="beta-2", owner_decision_ref=ref)
    assert _transitions(conn) == 0


def test_advance_writes_audit_row_with_owner_ref(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)
    cohorts.advance(conn, uid, to_cohort="beta-2", owner_decision_ref="card-t1")
    row = conn.execute(
        "SELECT from_cohort, to_cohort, owner_decision_ref"
        " FROM cohort_transitions WHERE user_id = ?", (uid,)).fetchone()
    assert (row["from_cohort"], row["to_cohort"]) == (None, "beta-2")
    assert row["owner_decision_ref"] == "card-t1"


def test_unknown_and_unopened_cohorts_rejected(conn):
    (uid,) = _users(conn, 1)
    with pytest.raises(cohorts.UnknownCohort):
        cohorts.advance(conn, uid, to_cohort="beta-99", owner_decision_ref="c")
    with pytest.raises(cohorts.CohortNotOpen):
        cohorts.advance(conn, uid, to_cohort="beta-3", owner_decision_ref="c")
    assert _transitions(conn) == 0


# --- AC-2 / INV-6: cap enforcement by in-transaction recompute ----------------------

def test_cap_rejects_third_user_in_beta2_no_rows_written(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    u = _users(conn, 3)
    cohorts.advance(conn, u[0], to_cohort="beta-2", owner_decision_ref="c1")
    cohorts.advance(conn, u[1], to_cohort="beta-2", owner_decision_ref="c2")
    before = _transitions(conn)
    with pytest.raises(cohorts.CohortCapExceeded):
        cohorts.advance(conn, u[2], to_cohort="beta-2", owner_decision_ref="c3")
    assert _transitions(conn) == before, "rejection must leave zero rows behind"
    size, status = conn.execute(
        "SELECT current_size, status FROM cohort_state WHERE cohort_id='beta-2'"
    ).fetchone()
    assert size == 2 and status == "full"


def test_cap_rejects_with_current_size_desynced_low(conn):
    """INV-6 RED-proof: the cache lies LOW, the recompute must still reject."""
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    u = _users(conn, 3)
    for i in range(2):
        cohorts.advance(conn, u[i], to_cohort="beta-2", owner_decision_ref=f"c{i}")
    # Desync the cache to zero AND reopen so only the recompute can say no.
    conn.execute("UPDATE cohort_state SET current_size = 0, status = 'open'"
                 " WHERE cohort_id = 'beta-2'")
    conn.commit()
    with pytest.raises(cohorts.CohortCapExceeded):
        cohorts.advance(conn, u[2], to_cohort="beta-2", owner_decision_ref="c3")
    assert conn.execute(
        "SELECT COUNT(DISTINCT user_id) FROM cohort_transitions"
        " WHERE to_cohort = 'beta-2'").fetchone()[0] == 2


def test_current_size_never_exceeds_max_size(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    u = _users(conn, 4)
    for i in range(2):
        cohorts.advance(conn, u[i], to_cohort="beta-2", owner_decision_ref=f"c{i}")
    for i in (2, 3):
        with pytest.raises(cohorts.CohortCapExceeded):
            cohorts.advance(conn, u[i], to_cohort="beta-2", owner_decision_ref="cx")
    size, cap = conn.execute(
        "SELECT current_size, max_size FROM cohort_state WHERE cohort_id='beta-2'"
    ).fetchone()
    assert size <= cap


# --- D4: additive membership, cumulative caps ---------------------------------------

def test_beta2_members_carry_into_beta3_and_count_against_its_cap(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open-2")
    u = _users(conn, 5)
    cohorts.advance(conn, u[0], to_cohort="beta-2", owner_decision_ref="c0")
    cohorts.advance(conn, u[1], to_cohort="beta-2", owner_decision_ref="c1")
    cohorts.open_cohort(conn, "beta-3", owner_decision_ref="card-open-3")
    # carried members: beta-2's 2 already count toward beta-3's cap of 3
    assert cohorts.is_member(conn, u[0], "beta-3") is True
    cohorts.advance(conn, u[2], to_cohort="beta-3", owner_decision_ref="c2")
    with pytest.raises(cohorts.CohortCapExceeded):
        cohorts.advance(conn, u[3], to_cohort="beta-3", owner_decision_ref="c3")
    # advancing a carried beta-2 member into the now-full beta-3 is fine:
    # size does not grow, and 'full' status is cache, never authority (INV-6)
    cohorts.advance(conn, u[0], to_cohort="beta-3", owner_decision_ref="c0b")
    assert cohorts._member_count(conn, "beta-3") == 3


def test_open_cap_below_previous_step_rejected(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    with pytest.raises(ValueError):
        cohorts.open_cohort(conn, "beta-3", owner_decision_ref="card",
                            max_size=1)


def test_advance_emits_cohort_advanced_notification(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)
    cohorts.advance(conn, uid, to_cohort="beta-2", owner_decision_ref="c1")
    kinds = [r[0] for r in conn.execute(
        "SELECT kind FROM notification_events WHERE user_id = ?", (uid,))]
    assert "cohort_advanced" in kinds
