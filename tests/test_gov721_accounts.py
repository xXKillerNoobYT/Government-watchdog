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
    # Far-future stamp: must sort AFTER the waitlisted row create_user just
    # wrote with the real clock (the original 2026-07-16T12:00 literal became
    # a time bomb the moment the wall clock passed it — GOV-771 repair).
    ts = "2099-01-01T00:00:00.000+00:00"
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


# --- GOV-1674 (C4): the empty string is not a credential -------------------------
#
# Found by a C4 audit asking which public callables no test CALLS. `set_password`
# had zero references in the whole tests/ tree — and it is the only exit from the
# passwordless posture, i.e. the single function that turns a NULL-hash row
# `login` always refuses into a row `login` accepts.
#
# Measured on main @ ca4d40c, before the fix:
#   set_password(conn, uid, "")            -> accepted, wrote a real argon2 hash
#   login(email, "")                       -> SUCCEEDED
#   set_password(conn, "no-such-id", "pw") -> returned normally, changed nothing
#
# The cause is one expression: `_HASHER.hash(password) if password is not None`.
# "" is falsy in Python but hashes to a perfectly valid PHC string, so the guard
# that was watching for None never saw it.

def test_set_password_rejects_empty_and_leaves_the_row_passwordless(conn):
    uid = service.create_user(conn, email="empty-sp@example.com")
    with pytest.raises(service.InvalidPassword):
        service.set_password(conn, uid, "")
    stored = conn.execute("SELECT password_hash FROM users WHERE user_id = ?",
                          (uid,)).fetchone()[0]
    assert stored is None, "a refused set_password must not alter the row"


def test_empty_password_never_becomes_a_working_login_credential(conn):
    """The end-to-end this exists to prevent, not just the raised exception.

    Asserting only that `set_password` raises would still pass if some other
    path wrote a hash of "". What matters is that `login(email, "")` cannot
    succeed — before the fix it did.
    """
    service.create_user(conn, email="nolo@example.com")
    with pytest.raises(service.InvalidPassword):
        service.set_password(conn, service.find_user_by_email(
            conn, "nolo@example.com"), "")
    with pytest.raises(service.LoginFailed):
        service.login(conn, email="nolo@example.com", password="")


def test_set_password_on_unknown_user_raises_rather_than_silently_no_op(conn):
    before = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    with pytest.raises(service.UnknownUser):
        service.set_password(conn, "no-such-user-id", "a-real-password")
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before


def test_create_user_refuses_empty_password_but_still_allows_none(conn):
    with pytest.raises(service.InvalidPassword):
        service.create_user(conn, email="ce@example.com", password="")
    assert service.find_user_by_email(conn, "ce@example.com") is None, \
        "a refused create_user must not leave a half-built account behind"
    # None remains the supported passwordless posture (provision.py depends on it).
    uid = service.create_user(conn, email="cn@example.com", password=None)
    assert conn.execute("SELECT password_hash FROM users WHERE user_id = ?",
                        (uid,)).fetchone()[0] is None


def test_set_password_still_sets_a_working_credential(conn):
    """OVER-CORRECTION guard: the fix must not break the function's actual job.

    A guard that rejects everything would pass all four tests above. This is the
    one that fails if `set_password` stops working, and it is also the first test
    this repo has ever had for the happy path.
    """
    service.create_user(conn, email="works@example.com")
    uid = service.find_user_by_email(conn, "works@example.com")
    service.set_password(conn, uid, "a-real-password")
    logged_in, token = service.login(conn, email="works@example.com",
                                     password="a-real-password")
    assert logged_in == uid
    assert sessions.verify_session(conn, token) == uid


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


# --- GOV-1673 (C1b): INV-4's SECOND half — append-only as a code property ----

def test_no_code_path_updates_or_deletes_access_grants():
    """INV-4 says `access_grants` IS append-only, not merely that it accumulates.

    `test_decisions_append_rows_and_latest_wins` proves rows survive when the
    SERVICE API is used. It cannot fail on a convenience helper added later —
    a `correct_grant()` doing an UPDATE would pass it while destroying the audit
    trail the tier resolution depends on. INV-4 is the reason
    `current_tier` can trust "latest row wins"; if a row can be rewritten, the
    history stops being evidence.

    Swept across the whole `scripts/` tree, not just `accounts/`: the table is
    read from seven files, and a helper in any of them breaks the invariant
    equally. Matched on the SQL verb ADJACENT to the table name — prose and
    docstrings that merely mention the rule must not trip it (learned three
    times over: GOV-1665, GOV-1667, GOV-1672).
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "scripts"
    offenders = []
    for module in sorted(root.rglob("*.py")):
        text = module.read_text(encoding="utf-8").upper()
        for verb in ("UPDATE ACCESS_GRANTS", "DELETE FROM ACCESS_GRANTS"):
            if verb in text:
                offenders.append(f"{module.relative_to(root)}: {verb}")
    assert offenders == [], offenders

    # ...and the append path is still present, so this cannot pass vacuously
    # by the table having been renamed out from under the sweep.
    service_src = (root / "accounts" / "service.py").read_text(encoding="utf-8")
    assert "INSERT INTO access_grants" in service_src
