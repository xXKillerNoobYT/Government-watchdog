"""ACCT-2026 leg 2 (GOV-754): cohort machine — AC-2/AC-3, INV-3/INV-6, D4.

The INV-6 RED-proof lives in test_cap_rejects_with_current_size_desynced_low:
neuter the in-transaction recompute in ``accounts.cohorts.advance`` (e.g.
read ``cohort_state.current_size`` instead of ``_member_count``) and that
test goes RED while the happy-path tests stay green — proving the cache is
never the enforcement authority.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import db
from accounts import cohorts, service


@pytest.fixture()
def conn(acct2_conn):
    return acct2_conn


def _users(conn, n):
    return [service.create_user(conn, email=f"u{i}@example.com") for i in range(n)]


def _transitions(conn):
    return conn.execute("SELECT COUNT(*) FROM cohort_transitions").fetchone()[0]


# --- AC-3 / INV-3: owner gate on every write --------------------------------------

@pytest.mark.parametrize("ref", [None, "", "   "])
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


def test_persisted_closed_cohort_rejects_with_zero_writes(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)
    conn.execute(
        "UPDATE cohort_state SET status = 'closed'"
        " WHERE cohort_id = 'beta-2'"
    )
    conn.commit()

    with pytest.raises(cohorts.CohortNotOpen):
        cohorts.advance(
            conn,
            uid,
            to_cohort="beta-2",
            owner_decision_ref="card-transition",
        )

    assert conn.in_transaction is False
    assert _transitions(conn) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM notification_events"
    ).fetchone()[0] == 0
    state = conn.execute(
        "SELECT current_size, max_size, status, owner_decision_ref"
        " FROM cohort_state WHERE cohort_id = 'beta-2'"
    ).fetchone()
    assert tuple(state) == (0, 2, "closed", "card-open")


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


def test_cap_recompute_serializes_across_two_connections(tmp_path, monkeypatch):
    db_path = tmp_path / "cohort-race.db"
    db.apply_migrations(db_path)

    setup = db.open_db(db_path)
    try:
        cohorts.open_cohort(
            setup,
            "beta-2",
            owner_decision_ref="card-open",
            max_size=1,
        )
        first_user, second_user = _users(setup, 2)
    finally:
        setup.close()

    def open_concurrent_connection():
        connection = sqlite3.connect(
            db_path,
            timeout=5.0,
            check_same_thread=False,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.row_factory = sqlite3.Row
        return connection

    first = open_concurrent_connection()
    second = open_concurrent_connection()
    first_counted = threading.Event()
    release_first = threading.Event()
    second_begin_immediate = threading.Event()
    real_member_count = cohorts._member_count

    def hold_first_writer_after_count(connection, cohort_id):
        value = real_member_count(connection, cohort_id)
        if connection is first:
            first_counted.set()
            if not release_first.wait(5):
                raise AssertionError("test did not release the first writer")
        return value

    monkeypatch.setattr(cohorts, "_member_count", hold_first_writer_after_count)

    def trace_second(sql):
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            second_begin_immediate.set()

    second.set_trace_callback(trace_second)
    outcomes = {}
    outcomes_lock = threading.Lock()

    def run(label, connection, user_id):
        try:
            outcome = cohorts.advance(
                connection,
                user_id,
                to_cohort="beta-2",
                owner_decision_ref=f"card-{label}",
            )
        except Exception as exc:
            outcome = exc
        with outcomes_lock:
            outcomes[label] = outcome

    first_thread = threading.Thread(
        target=run,
        args=("first", first, first_user),
    )
    second_thread = threading.Thread(
        target=run,
        args=("second", second, second_user),
    )
    second_started = False
    try:
        first_thread.start()
        assert first_counted.wait(2), "first writer never reached the cap read"

        second_thread.start()
        second_started = True
        assert second_begin_immediate.wait(2), (
            "second connection did not request the writer lock before "
            "reading capacity"
        )

        release_first.set()
        first_thread.join(5)
        second_thread.join(5)

        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert isinstance(outcomes["first"], int)
        assert isinstance(outcomes["second"], cohorts.CohortCapExceeded)
        assert first.in_transaction is False
        assert second.in_transaction is False
    finally:
        release_first.set()
        if first_thread.is_alive():
            first_thread.join(5)
        if second_started and second_thread.is_alive():
            second_thread.join(5)
        first.close()
        second.close()

    verify = db.open_db(db_path)
    try:
        transitions = verify.execute(
            "SELECT user_id FROM cohort_transitions ORDER BY transition_id"
        ).fetchall()
        assert [row[0] for row in transitions] == [first_user]

        state = verify.execute(
            "SELECT current_size, max_size, status"
            " FROM cohort_state WHERE cohort_id = 'beta-2'"
        ).fetchone()
        assert tuple(state) == (1, 1, "full")

        notifications = verify.execute(
            "SELECT user_id FROM notification_events"
            " WHERE kind = 'cohort_advanced'"
        ).fetchall()
        assert [row[0] for row in notifications] == [first_user]
    finally:
        verify.close()


def test_advance_refuses_active_transaction_without_rolling_it_back(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)
    original_email = conn.execute(
        "SELECT email FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()[0]
    pending_email = "pending-transaction@example.com"
    conn.execute(
        "UPDATE users SET email = ? WHERE user_id = ?",
        (pending_email, uid),
    )
    assert conn.in_transaction is True

    with pytest.raises(cohorts.CohortTransactionActive):
        cohorts.advance(
            conn,
            uid,
            to_cohort="beta-2",
            owner_decision_ref="card-transition",
        )

    assert conn.in_transaction is True
    assert _transitions(conn) == 0
    assert conn.execute(
        "SELECT email FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()[0] == pending_email
    conn.rollback()
    assert conn.execute(
        "SELECT email FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()[0] == original_email


def test_open_refuses_active_transaction_without_committing_it(conn):
    (uid,) = _users(conn, 1)
    original_email = conn.execute(
        "SELECT email FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()[0]
    pending_email = "pending-open@example.com"
    conn.execute(
        "UPDATE users SET email = ? WHERE user_id = ?",
        (pending_email, uid),
    )

    with pytest.raises(cohorts.CohortTransactionActive):
        cohorts.open_cohort(
            conn,
            "beta-2",
            owner_decision_ref="card-open",
        )

    assert conn.in_transaction is True
    assert conn.execute(
        "SELECT email FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()[0] == pending_email
    assert conn.execute(
        "SELECT COUNT(*) FROM cohort_state WHERE cohort_id = 'beta-2'"
    ).fetchone()[0] == 0
    conn.rollback()
    assert conn.execute(
        "SELECT email FROM users WHERE user_id = ?",
        (uid,),
    ).fetchone()[0] == original_email


def test_busy_writer_fails_closed_as_retryable_domain_error(tmp_path):
    db_path = tmp_path / "cohort-busy.db"
    db.apply_migrations(db_path)
    setup = db.open_db(db_path)
    try:
        cohorts.open_cohort(setup, "beta-2", owner_decision_ref="card-open")
        (uid,) = _users(setup, 1)
    finally:
        setup.close()

    locker = sqlite3.connect(db_path, timeout=0.1)
    waiter = sqlite3.connect(db_path, timeout=0.01)
    locker.execute("PRAGMA foreign_keys = ON")
    waiter.execute("PRAGMA foreign_keys = ON")
    try:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(cohorts.CohortAdmissionBusy) as caught:
            cohorts.advance(
                waiter,
                uid,
                to_cohort="beta-2",
                owner_decision_ref="card-transition",
            )
        assert isinstance(caught.value.__cause__, sqlite3.OperationalError)
        assert waiter.in_transaction is False
    finally:
        if locker.in_transaction:
            locker.rollback()
        locker.close()
        waiter.close()

    verify = db.open_db(db_path)
    try:
        assert _transitions(verify) == 0
        assert verify.execute(
            "SELECT current_size FROM cohort_state WHERE cohort_id = 'beta-2'"
        ).fetchone()[0] == 0
        assert verify.execute(
            "SELECT COUNT(*) FROM notification_events"
        ).fetchone()[0] == 0
    finally:
        verify.close()


def test_notification_failure_rolls_back_transition_cache_and_event(
    conn,
    monkeypatch,
):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)
    real_notify = cohorts.notif.notify_cohort_advanced

    def insert_then_fail(*args, **kwargs):
        real_notify(*args, **kwargs)
        raise RuntimeError("simulated notification failure")

    monkeypatch.setattr(
        cohorts.notif,
        "notify_cohort_advanced",
        insert_then_fail,
    )

    with pytest.raises(RuntimeError, match="notification failure"):
        cohorts.advance(
            conn,
            uid,
            to_cohort="beta-2",
            owner_decision_ref="card-transition",
        )

    assert conn.in_transaction is False
    assert _transitions(conn) == 0
    state = conn.execute(
        "SELECT current_size, status FROM cohort_state"
        " WHERE cohort_id = 'beta-2'"
    ).fetchone()
    assert tuple(state) == (0, "open")
    assert conn.execute(
        "SELECT COUNT(*) FROM notification_events"
    ).fetchone()[0] == 0


def test_exact_target_retry_is_idempotent_without_duplicate_notification(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open")
    (uid,) = _users(conn, 1)

    first_id = cohorts.advance(
        conn,
        uid,
        to_cohort="beta-2",
        owner_decision_ref="card-first",
    )
    retry_id = cohorts.advance(
        conn,
        uid,
        to_cohort="beta-2",
        owner_decision_ref="card-retry",
    )

    assert retry_id == first_id
    assert conn.execute(
        "SELECT COUNT(*) FROM cohort_transitions"
        " WHERE user_id = ? AND to_cohort = 'beta-2'",
        (uid,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM notification_events"
        " WHERE user_id = ? AND kind = 'cohort_advanced'",
        (uid,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT current_size FROM cohort_state WHERE cohort_id = 'beta-2'"
    ).fetchone()[0] == 1


def test_earlier_admission_refreshes_every_open_cumulative_cohort_cache(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open-2")
    cohorts.open_cohort(conn, "beta-3", owner_decision_ref="card-open-3")
    cohorts.open_cohort(conn, "beta-15", owner_decision_ref="card-open-15")
    (uid,) = _users(conn, 1)

    cohorts.advance(
        conn,
        uid,
        to_cohort="beta-2",
        owner_decision_ref="card-transition",
    )

    states = conn.execute(
        "SELECT cohort_id, current_size, status FROM cohort_state"
        " ORDER BY max_size"
    ).fetchall()
    assert [tuple(row) for row in states] == [
        ("beta-2", 1, "open"),
        ("beta-3", 1, "open"),
        ("beta-15", 1, "open"),
    ]


def test_opening_later_cohorts_initializes_existing_cumulative_members(conn):
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-open-2")
    first, second = _users(conn, 2)
    cohorts.advance(
        conn,
        first,
        to_cohort="beta-2",
        owner_decision_ref="card-first",
    )
    cohorts.advance(
        conn,
        second,
        to_cohort="beta-2",
        owner_decision_ref="card-second",
    )

    cohorts.open_cohort(conn, "beta-3", owner_decision_ref="card-open-3")
    cohorts.open_cohort(conn, "beta-15", owner_decision_ref="card-open-15")

    states = conn.execute(
        "SELECT cohort_id, current_size, status FROM cohort_state"
        " WHERE cohort_id IN ('beta-3', 'beta-15')"
        " ORDER BY max_size"
    ).fetchall()
    assert [tuple(row) for row in states] == [
        ("beta-3", 2, "open"),
        ("beta-15", 2, "open"),
    ]


def test_reopening_full_cohort_recomputes_full_status(conn):
    cohorts.open_cohort(
        conn,
        "beta-2",
        owner_decision_ref="card-open",
        max_size=1,
    )
    (uid,) = _users(conn, 1)
    cohorts.advance(
        conn,
        uid,
        to_cohort="beta-2",
        owner_decision_ref="card-transition",
    )

    cohorts.open_cohort(
        conn,
        "beta-2",
        owner_decision_ref="card-reopen",
        max_size=1,
    )

    state = conn.execute(
        "SELECT current_size, max_size, status, owner_decision_ref"
        " FROM cohort_state WHERE cohort_id = 'beta-2'"
    ).fetchone()
    assert tuple(state) == (1, 1, "full", "card-reopen")


def test_reopening_cannot_lower_cap_below_committed_membership(conn):
    cohorts.open_cohort(
        conn,
        "beta-2",
        owner_decision_ref="card-open",
        max_size=2,
    )
    first, second = _users(conn, 2)
    cohorts.advance(
        conn,
        first,
        to_cohort="beta-2",
        owner_decision_ref="card-first",
    )
    cohorts.advance(
        conn,
        second,
        to_cohort="beta-2",
        owner_decision_ref="card-second",
    )

    with pytest.raises(cohorts.CohortCapBelowMembership):
        cohorts.open_cohort(
            conn,
            "beta-2",
            owner_decision_ref="card-invalid-lower-cap",
            max_size=1,
        )

    assert conn.in_transaction is False
    state = conn.execute(
        "SELECT current_size, max_size, status, owner_decision_ref"
        " FROM cohort_state WHERE cohort_id = 'beta-2'"
    ).fetchone()
    assert tuple(state) == (2, 2, "full", "card-open")


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
