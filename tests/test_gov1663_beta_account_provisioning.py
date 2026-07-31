"""GOV-1663 — the beta front door provisions an accounts row (resolves #192 auth half).

Owner decision 2026-07-30: a verified beta sign-in creates/approves the user in
the accounts lane, carrying the beta allowlist's own ``owner_decision_ref``, so
``accounts.gate`` remains the single civic gate.

The tests are ordered by what they defend, hardest first. The one that matters
most is :func:`test_revoked_account_is_not_reopened_by_beta_signin`: provisioning
may only ever open a door that was never opened, never reopen one an owner shut.
Every assertion here was proved red — see the PR body for the exact failures.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from accounts import gate, service as accounts_service, sessions as acct_sessions  # noqa: E402
from beta import allowlist, provision, service as beta_service, tokens  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[1] / "Database" / "migrations"
OWNER_REF = "GOV-1663-owner-card"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    for name in ("0025_accounts_cohorts_notifications.sql", "0026_beta_gate.sql",
                 "0027_beta_magic_code.sql"):
        c.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
    c.commit()
    yield c
    c.close()


def _grants(conn, user_id):
    return [r["tier"] for r in conn.execute(
        "SELECT tier FROM access_grants WHERE user_id = ?"
        " ORDER BY granted_utc, rowid", (user_id,))]


def _signin(conn, email):
    """Full front-door redemption: allowlisted email -> raw session token."""
    raw_token = tokens.issue(conn, email)
    return beta_service.verify_magic_link(conn, raw_token)


# --- the security property: provisioning opens, it never REopens -------------

def test_revoked_account_is_not_reopened_by_beta_signin(conn):
    """An owner revoked them in the accounts lane. The beta door must not undo it.

    This is the asymmetry the module exists to hold. The lanes deny
    independently: a stale-but-active allowlist row must not resurrect an
    account an owner closed.
    """
    allowlist.add(conn, "rev@example.com", owner_decision_ref=OWNER_REF)
    user_id = provision.provision_account(conn, "rev@example.com")
    accounts_service.revoke(conn, user_id, owner_decision_ref="GOV-revoke-card")
    assert accounts_service.current_tier(conn, user_id) == "revoked"

    _signin(conn, "rev@example.com")  # allowlist still active; sign-in succeeds

    assert accounts_service.current_tier(conn, user_id) == "revoked"
    assert _grants(conn, user_id) == ["waitlisted", "approved", "revoked"]


def test_paused_account_is_not_reopened_by_beta_signin(conn):
    allowlist.add(conn, "paused@example.com", owner_decision_ref=OWNER_REF)
    user_id = provision.provision_account(conn, "paused@example.com")
    accounts_service.pause(conn, user_id, owner_decision_ref="GOV-pause-card")

    _signin(conn, "paused@example.com")

    assert accounts_service.current_tier(conn, user_id) == "paused"


def test_revoked_account_is_denied_civic_data_after_beta_signin(conn):
    """The property that actually matters, asserted at the gate rather than the tier."""
    allowlist.add(conn, "rev2@example.com", owner_decision_ref=OWNER_REF)
    user_id = provision.provision_account(conn, "rev2@example.com")
    accounts_service.revoke(conn, user_id, owner_decision_ref="GOV-revoke-card")

    _signin(conn, "rev2@example.com")

    _, raw = acct_sessions.issue_session(conn, user_id)
    status, body = gate.guard_civic_request(conn, raw)
    assert status == 403
    assert body == gate.DENIED_BODY


# --- fail-closed: no allowlist authority, no grant ---------------------------

def test_no_active_allowlist_row_writes_no_approval(conn):
    """provision_account is independently fail-closed, not merely called safely.

    beta.service re-checks the allowlist before calling this, so in production
    the guard is redundant — which is exactly why it needs its own test: a
    caller added later must not be able to mint an approval.
    """
    user_id = provision.provision_account(conn, "stranger@example.com")

    assert user_id is not None
    assert accounts_service.current_tier(conn, user_id) == "waitlisted"
    assert "approved" not in _grants(conn, user_id)


def test_revoked_allowlist_row_yields_no_decision_ref(conn):
    allowlist.add(conn, "gone@example.com", owner_decision_ref=OWNER_REF)
    allowlist.revoke(conn, "gone@example.com", owner_decision_ref="GOV-revoke-card")

    assert allowlist.decision_ref(conn, "gone@example.com") is None


def test_decision_ref_absent_for_unknown_email(conn):
    assert allowlist.decision_ref(conn, "nobody@example.com") is None


# --- the approval carries real owner authority -------------------------------

def test_grant_carries_the_allowlist_owner_decision_ref(conn):
    """Never a synthetic approval: the ref is the owner's, copied verbatim."""
    allowlist.add(conn, "ok@example.com", owner_decision_ref=OWNER_REF)

    user_id = provision.provision_account(conn, "ok@example.com")

    row = conn.execute(
        "SELECT tier, owner_decision_ref, note FROM access_grants"
        " WHERE user_id = ? ORDER BY granted_utc DESC, rowid DESC LIMIT 1",
        (user_id,)).fetchone()
    assert row["tier"] == "approved"
    assert row["owner_decision_ref"] == OWNER_REF
    assert row["note"] == provision.GRANT_NOTE


def test_reinvite_ref_supersedes_on_a_fresh_provision(conn):
    """A re-invite under a NEW owner card is the ref that lands, not the old one."""
    allowlist.add(conn, "re@example.com", owner_decision_ref=OWNER_REF)
    allowlist.revoke(conn, "re@example.com", owner_decision_ref="GOV-revoke-card")
    allowlist.add(conn, "re@example.com", owner_decision_ref="GOV-reinvite-card")

    user_id = provision.provision_account(conn, "re@example.com")

    row = conn.execute(
        "SELECT owner_decision_ref FROM access_grants WHERE user_id = ?"
        " AND tier = 'approved' ORDER BY granted_utc DESC, rowid DESC LIMIT 1",
        (user_id,)).fetchone()
    assert row["owner_decision_ref"] == "GOV-reinvite-card"


# --- passwordless posture (owner direction 2026-07-30) -----------------------

def test_provisioned_user_has_no_password_hash(conn):
    allowlist.add(conn, "pw@example.com", owner_decision_ref=OWNER_REF)

    user_id = provision.provision_account(conn, "pw@example.com")

    row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?",
                       (user_id,)).fetchone()
    assert row["password_hash"] is None


def test_provisioned_user_cannot_password_login(conn):
    """Passwordless is enforced, not merely unset: every password is refused."""
    allowlist.add(conn, "pw2@example.com", owner_decision_ref=OWNER_REF)
    provision.provision_account(conn, "pw2@example.com")

    for attempt in ("", "hunter2", "correct horse battery staple"):
        with pytest.raises(accounts_service.LoginFailed):
            accounts_service.login(conn, email="pw2@example.com", password=attempt)


# --- idempotence -------------------------------------------------------------

def test_repeated_signin_appends_at_most_one_approval(conn):
    allowlist.add(conn, "many@example.com", owner_decision_ref=OWNER_REF)

    for _ in range(5):
        _signin(conn, "many@example.com")

    user_id = accounts_service.find_user_by_email(conn, "many@example.com")
    assert _grants(conn, user_id).count("approved") == 1
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_provision_is_idempotent_when_called_directly(conn):
    allowlist.add(conn, "idem@example.com", owner_decision_ref=OWNER_REF)

    first = provision.provision_account(conn, "idem@example.com")
    second = provision.provision_account(conn, "idem@example.com")

    assert first == second
    assert _grants(conn, first).count("approved") == 1


# --- both redemption paths are wired -----------------------------------------

def test_magic_link_signin_provisions(conn):
    allowlist.add(conn, "link@example.com", owner_decision_ref=OWNER_REF)

    assert _signin(conn, "link@example.com") is not None

    user_id = accounts_service.find_user_by_email(conn, "link@example.com")
    assert accounts_service.current_tier(conn, user_id) == "approved"


def test_numeric_code_signin_also_provisions(conn):
    """The code path is a separate branch and would silently miss the bridge."""
    allowlist.add(conn, "code@example.com", owner_decision_ref=OWNER_REF)
    _, raw_code = tokens.issue_with_code(conn, "code@example.com")

    assert beta_service.consume_code(conn, "code@example.com", raw_code) is not None

    user_id = accounts_service.find_user_by_email(conn, "code@example.com")
    assert accounts_service.current_tier(conn, user_id) == "approved"


def test_failed_redemption_provisions_nothing(conn):
    """A wrong code must not create an account as a side effect."""
    allowlist.add(conn, "bad@example.com", owner_decision_ref=OWNER_REF)
    tokens.issue_with_code(conn, "bad@example.com")

    assert beta_service.consume_code(conn, "bad@example.com", "000000") is None

    assert accounts_service.find_user_by_email(conn, "bad@example.com") is None


# --- the authorization half is genuinely closed ------------------------------

def test_provisioned_user_is_authorized_for_civic_data(conn):
    """End to end: beta sign-in -> accounts session -> gate returns a Principal.

    This is #192's authorization half proved. The TRANSPORT half is out of
    scope by design: `accounts.gate` reads `auth_sessions`, and a beta cookie
    lives in `beta_sessions`. Reconciling that is where #125/#181 collide, and
    nothing here touches an HTTP surface.
    """
    allowlist.add(conn, "civic@example.com", owner_decision_ref=OWNER_REF)
    _signin(conn, "civic@example.com")
    user_id = accounts_service.find_user_by_email(conn, "civic@example.com")

    _, raw = acct_sessions.issue_session(conn, user_id)
    status, principal = gate.guard_civic_request(conn, raw)

    assert status == 200
    assert principal.user_id == user_id
    assert principal.tier == "approved"


def test_normalized_email_is_the_only_form_that_lands(conn):
    allowlist.add(conn, "Mixed@Example.COM", owner_decision_ref=OWNER_REF)

    user_id = provision.provision_account(conn, "  MIXED@example.com  ")

    row = conn.execute("SELECT email FROM users WHERE user_id = ?",
                       (user_id,)).fetchone()
    assert row["email"] == "mixed@example.com"
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_invalid_address_creates_nothing(conn):
    assert provision.provision_account(conn, "not-an-email") is None
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_uuid_shape_of_returned_user_id(conn):
    allowlist.add(conn, "shape@example.com", owner_decision_ref=OWNER_REF)
    user_id = provision.provision_account(conn, "shape@example.com")
    assert uuid.UUID(user_id)
