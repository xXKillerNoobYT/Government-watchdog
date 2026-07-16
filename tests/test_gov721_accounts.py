"""ACCT-2026 leg 2 (GOV-754): accounts service — INV-4/7/9/10.

RED-proof notes: neuter ``service.normalize_email`` to identity and the
INV-9 tests go RED; neuter the tier ORDER BY and the INV-4 tie-break test
goes RED; store raw tokens instead of hashes and the INV-10 tests go RED.
"""

from __future__ import annotations

import hashlib

import pytest
from argon2 import PasswordHasher

from accounts import service, sessions


@pytest.fixture()
def conn(acct2_conn):
    return acct2_conn


# --- INV-9: email normalization -------------------------------------------------

def test_email_normalized_lowercase_trim_before_storage(conn):
    uid = service.create_user(conn, email="  User@Example.COM ")
    row = conn.execute("SELECT email FROM users WHERE user_id = ?", (uid,)).fetchone()
    assert row["email"] == "user@example.com"


def test_duplicate_email_rejected_across_case_and_whitespace(conn):
    service.create_user(conn, email="dupe@example.com")
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with pytest.raises(service.DuplicateEmail):
        service.create_user(conn, email="  DUPE@example.COM ")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before


def test_lookup_normalizes_before_matching(conn):
    uid = service.create_user(conn, email="find@example.com")
    assert service.find_user_by_email(conn, " FIND@EXAMPLE.com  ") == uid


# --- create / waitlist -----------------------------------------------------------

def test_create_lands_on_waitlist_with_waitlisted_tier(conn):
    uid = service.create_user(conn, email="w@example.com", area_interest="alpine")
    assert service.current_tier(conn, uid) == "waitlisted"
    wl = conn.execute(
        "SELECT status, area_interest FROM waitlist_requests WHERE user_id = ?",
        (uid,)).fetchone()
    assert wl["status"] == "pending"
    assert wl["area_interest"] == "alpine"


# --- INV-4: append-only grants, latest-row tier ------------------------------------

def test_decisions_append_rows_and_latest_wins(conn):
    uid = service.create_user(conn, email="t@example.com")
    service.approve(conn, uid, owner_decision_ref="card-1")
    assert service.current_tier(conn, uid) == "approved"
    service.pause(conn, uid, owner_decision_ref="card-2")
    assert service.current_tier(conn, uid) == "paused"
    service.revoke(conn, uid, owner_decision_ref="card-3")
    assert service.current_tier(conn, uid) == "revoked"
    # append-only: all four rows (waitlisted + 3 decisions) still there
    n = conn.execute("SELECT COUNT(*) FROM access_grants WHERE user_id = ?",
                     (uid,)).fetchone()[0]
    assert n == 4


def test_tier_tie_break_on_identical_timestamp_uses_rowid(conn):
    uid = service.create_user(conn, email="tie@example.com")
    ts = "2026-07-16T12:00:00.000+00:00"
    for tier in ("approved", "revoked"):  # same granted_utc, later rowid wins
        conn.execute(
            "INSERT INTO access_grants (grant_id, user_id, tier,"
            " owner_decision_ref, granted_utc) VALUES (?, ?, ?, 'card-x', ?)",
            (f"g-{tier}", uid, tier, ts))
    conn.commit()
    assert service.current_tier(conn, uid) == "revoked"


@pytest.mark.parametrize("ref", [None, ""])
def test_ownerless_decision_rejected_at_service_layer(conn, ref):
    uid = service.create_user(conn, email="noref@example.com")
    before = conn.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0]
    for fn in (service.approve, service.revoke, service.pause):
        with pytest.raises(service.OwnerlessAccessDecision):
            fn(conn, uid, owner_decision_ref=ref)
    assert conn.execute("SELECT COUNT(*) FROM access_grants").fetchone()[0] == before


# --- INV-7 / D2: argon2id passwords --------------------------------------------

def test_password_stored_as_argon2id_phc_never_raw(conn):
    uid = service.create_user(conn, email="p@example.com", password="hunter2secret")
    stored = conn.execute("SELECT password_hash FROM users WHERE user_id = ?",
                          (uid,)).fetchone()[0]
    assert stored.startswith("$argon2id$")
    assert "hunter2secret" not in stored


def test_login_success_wrong_password_and_unknown_email(conn):
    service.create_user(conn, email="l@example.com", password="correct-horse")
    uid, token = service.login(conn, email=" L@EXAMPLE.COM ", password="correct-horse")
    assert sessions.verify_session(conn, token) == uid
    with pytest.raises(service.LoginFailed):
        service.login(conn, email="l@example.com", password="wrong")
    with pytest.raises(service.LoginFailed):
        service.login(conn, email="ghost@example.com", password="whatever")


def test_login_rehashes_weak_parameters(conn):
    """D2: check_needs_rehash upgrades old-parameter hashes lazily."""
    uid = service.create_user(conn, email="r@example.com")
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?",
                 (weak.hash("pw-to-upgrade"), uid))
    conn.commit()
    old = conn.execute("SELECT password_hash FROM users WHERE user_id = ?",
                       (uid,)).fetchone()[0]
    service.login(conn, email="r@example.com", password="pw-to-upgrade")
    new = conn.execute("SELECT password_hash FROM users WHERE user_id = ?",
                       (uid,)).fetchone()[0]
    assert new != old and new.startswith("$argon2id$")
    # and the upgraded hash still verifies on the default hasher
    PasswordHasher().verify(new, "pw-to-upgrade")


# --- INV-10: sessions store sha256 only -----------------------------------------

def test_raw_token_never_stored_only_sha256(conn):
    uid = service.create_user(conn, email="s@example.com")
    session_id, raw = sessions.issue_session(conn, uid)
    row = conn.execute("SELECT * FROM auth_sessions WHERE session_id = ?",
                       (session_id,)).fetchone()
    values = [str(v) for v in tuple(row)]
    assert raw not in values, "raw bearer token persisted (INV-10 violation)"
    assert row["token_hash"] == hashlib.sha256(raw.encode()).hexdigest()


def test_session_expiry_and_revocation(conn):
    uid = service.create_user(conn, email="e@example.com")
    _, expired = sessions.issue_session(conn, uid, ttl_seconds=0)
    assert sessions.verify_session(conn, expired) is None
    _, live = sessions.issue_session(conn, uid)
    assert sessions.verify_session(conn, live) == uid
    assert sessions.revoke_session(conn, raw_token=live) is True
    assert sessions.verify_session(conn, live) is None
    assert sessions.verify_session(conn, "not-a-token") is None
    assert sessions.verify_session(conn, "") is None


def test_access_revoke_kills_all_live_sessions(conn):
    uid = service.create_user(conn, email="k@example.com")
    service.approve(conn, uid, owner_decision_ref="card-a")
    _, t1 = sessions.issue_session(conn, uid)
    _, t2 = sessions.issue_session(conn, uid)
    service.revoke(conn, uid, owner_decision_ref="card-b")
    assert sessions.verify_session(conn, t1) is None
    assert sessions.verify_session(conn, t2) is None
