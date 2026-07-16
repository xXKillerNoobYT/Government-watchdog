"""ACCT-2026 Leg 1 (GOV-753): migration 0025 in-schema gates are load-bearing.

Every test here pins a constraint that lives IN the schema of
``Database/migrations/0025_accounts_cohorts_notifications.sql`` (plan v0.2,
GOV-721), so a buggy or bypassed service layer still cannot write the
forbidden row:

* ownerless approve/revoke/pause ``access_grants`` rows (plan §3 CHECK),
* ownerless ``feature_flags`` rows — fail-closed email activation (D1/INV-5),
* ownerless ``cohort_transitions`` rows (INV-3),
* duplicate ``auth_sessions.token_hash`` (INV-10 hashed-token storage),
* out-of-enum tier/status/kind values across all five enum CHECKs.

RED-proof (GOV-738/743 pattern): each gate test asserts ``IntegrityError`` on
the violating insert. Neuter the corresponding constraint in the migration
file and the insert succeeds, so the test goes RED — neuter evidence recorded
on GOV-753. Idempotency is proven both through the ``schema_migrations``
ledger path and by raw double-application of the 0025 statements.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_0025 = ROOT / "Database" / "migrations" / "0025_accounts_cohorts_notifications.sql"

TABLES_0025 = [
    "users", "waitlist_requests", "access_grants", "cohort_state",
    "cohort_transitions", "consent_preferences", "notification_events",
    "email_outbox", "email_delivery_log", "feature_flags", "auth_sessions",
]

TS = "2026-07-16T00:00:00Z"


@pytest.fixture()
def acct_conn(tmp_path):
    """A fully migrated DB (whole chain, so 0025 applies in real sequence)."""
    db_path = tmp_path / "acct.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    conn.execute(
        "INSERT INTO users (user_id, email, created_utc) VALUES ('u1', 'a@example.com', ?)",
        (TS,),
    )
    conn.commit()
    yield conn
    conn.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- migration shape -------------------------------------------------------

def test_all_eleven_tables_and_ledger_row_exist(acct_conn):
    names = {
        row["name"] for row in acct_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing = [t for t in TABLES_0025 if t not in names]
    assert not missing, f"0025 tables missing: {missing}"
    ledger = acct_conn.execute(
        "SELECT version FROM schema_migrations WHERE version = ?",
        ("0025_accounts_cohorts_notifications",),
    ).fetchone()
    assert ledger is not None


def test_migration_idempotent_via_ledger_and_raw_double_apply(tmp_path):
    # Ledger path: a second apply_migrations run must be a clean no-op.
    db_path = tmp_path / "twice.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)

    # Raw path: every 0025 statement re-executed on an already-built schema
    # must succeed on its own IF NOT EXISTS guard (no ledger shielding it).
    raw = sqlite3.connect(tmp_path / "raw.db")
    stmts = db._statements(MIGRATION_0025.read_text(encoding="utf-8"))
    assert stmts, "0025 parsed to zero statements"
    for _ in range(2):
        for stmt in stmts:
            raw.execute(stmt)
    raw.close()


def test_0025_is_additive_only():
    """Plan AC-8 shape guard: CREATE-only, no ALTER/DROP/UPDATE on anything."""
    stmts = db._statements(MIGRATION_0025.read_text(encoding="utf-8"))
    for stmt in stmts:
        head = " ".join(stmt.split()[:5]).upper()
        assert head.startswith(("CREATE TABLE IF NOT EXISTS",
                                "CREATE INDEX IF NOT EXISTS")), (
            f"non-additive or non-idempotent statement in 0025: {stmt[:80]}")


# --- §3 access_grants: ownerless approve/revoke/pause rejected in-schema ----

@pytest.mark.parametrize("tier", ["approved", "revoked", "paused"])
def test_access_grants_ownerless_decision_tier_rejected(acct_conn, tier):
    before = _count(acct_conn, "access_grants")
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO access_grants (grant_id, user_id, tier, granted_utc)"
            " VALUES (?, 'u1', ?, ?)", (f"g-{tier}", tier, TS))
    assert _count(acct_conn, "access_grants") == before


@pytest.mark.parametrize("tier", ["none", "waitlisted", "pending"])
def test_access_grants_non_decision_tiers_allow_null_ref(acct_conn, tier):
    acct_conn.execute(
        "INSERT INTO access_grants (grant_id, user_id, tier, granted_utc)"
        " VALUES (?, 'u1', ?, ?)", (f"g-{tier}", tier, TS))


def test_access_grants_decision_tier_with_owner_ref_accepted(acct_conn):
    acct_conn.execute(
        "INSERT INTO access_grants (grant_id, user_id, tier, owner_decision_ref,"
        " granted_utc) VALUES ('g-ok', 'u1', 'approved', 'card:GOV-999', ?)", (TS,))
    assert _count(acct_conn, "access_grants") == 1


# --- §10 feature_flags: fail-closed, every row carries an owner decision ----

def test_feature_flags_owner_decision_ref_not_null(acct_conn):
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO feature_flags (flag_name, enabled, at_utc)"
            " VALUES ('email_adapter_enabled', 1, ?)", (TS,))
    assert _count(acct_conn, "feature_flags") == 0


def test_feature_flags_enabled_is_strictly_boolean(acct_conn):
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO feature_flags (flag_name, enabled, owner_decision_ref,"
            " at_utc) VALUES ('email_adapter_enabled', 2, 'card:GOV-999', ?)", (TS,))


def test_feature_flags_owned_row_accepted_and_fail_closed_default(acct_conn):
    # Fail-closed (INV-5): a fresh schema has NO flag rows — off by default.
    assert _count(acct_conn, "feature_flags") == 0
    acct_conn.execute(
        "INSERT INTO feature_flags (flag_name, enabled, owner_decision_ref, at_utc)"
        " VALUES ('email_adapter_enabled', 1, 'card:GOV-999', ?)", (TS,))
    assert _count(acct_conn, "feature_flags") == 1


# --- §5 cohort_transitions: every transition carries an owner decision ------

def test_cohort_transitions_owner_decision_ref_not_null(acct_conn):
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO cohort_transitions (user_id, from_cohort, to_cohort, at_utc)"
            " VALUES ('u1', 'beta-2', 'beta-3', ?)", (TS,))
    assert _count(acct_conn, "cohort_transitions") == 0


def test_cohort_transitions_owned_row_accepted(acct_conn):
    acct_conn.execute(
        "INSERT INTO cohort_transitions (user_id, from_cohort, to_cohort,"
        " owner_decision_ref, at_utc) VALUES ('u1', NULL, 'beta-2', 'card:GOV-999', ?)",
        (TS,))
    assert _count(acct_conn, "cohort_transitions") == 1


# --- §11 auth_sessions: hashed tokens, one session per hash -----------------

def test_auth_sessions_token_hash_unique(acct_conn):
    acct_conn.execute(
        "INSERT INTO auth_sessions (session_id, user_id, token_hash, issued_utc,"
        " expires_utc) VALUES ('s1', 'u1', 'hash-a', ?, ?)", (TS, TS))
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO auth_sessions (session_id, user_id, token_hash, issued_utc,"
            " expires_utc) VALUES ('s2', 'u1', 'hash-a', ?, ?)", (TS, TS))
    assert _count(acct_conn, "auth_sessions") == 1


# --- enum CHECKs across the five enum-bearing tables ------------------------

@pytest.mark.parametrize("table,insert_sql", [
    ("access_grants",
     "INSERT INTO access_grants (grant_id, user_id, tier, owner_decision_ref,"
     " granted_utc) VALUES ('g-bad', 'u1', 'galaxy', 'card:x', '{ts}')"),
    ("waitlist_requests",
     "INSERT INTO waitlist_requests (request_id, user_id, submitted_utc, status)"
     " VALUES ('w-bad', 'u1', '{ts}', 'galaxy')"),
    ("cohort_state",
     "INSERT INTO cohort_state (cohort_id, max_size, status)"
     " VALUES ('beta-2', 2, 'galaxy')"),
    ("notification_events",
     "INSERT INTO notification_events (notif_id, user_id, kind, body_text,"
     " created_utc) VALUES ('n-bad', 'u1', 'galaxy', 'x', '{ts}')"),
    ("email_outbox",
     "INSERT INTO email_outbox (outbox_id, user_id, template_id, subject,"
     " body_text, status, queued_utc)"
     " VALUES ('o-bad', 'u1', 't', 's', 'b', 'galaxy', '{ts}')"),
    ("email_delivery_log",
     "INSERT INTO email_delivery_log (log_id, outbox_id, event_kind, recorded_utc)"
     " VALUES ('l-bad', 'o1', 'galaxy', '{ts}')"),
])
def test_enum_checks_reject_out_of_enum_values(acct_conn, table, insert_sql):
    # email_delivery_log needs a real outbox parent so ONLY the enum can fail.
    acct_conn.execute(
        "INSERT INTO email_outbox (outbox_id, user_id, template_id, subject,"
        " body_text, queued_utc) VALUES ('o1', 'u1', 't', 's', 'b', ?)", (TS,))
    before = _count(acct_conn, table)
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(insert_sql.format(ts=TS))
    assert _count(acct_conn, table) == before


# --- UNIQUE identity gates ---------------------------------------------------

def test_users_email_unique(acct_conn):
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO users (user_id, email, created_utc)"
            " VALUES ('u2', 'a@example.com', ?)", (TS,))


def test_consent_unsubscribe_token_unique(acct_conn):
    acct_conn.execute(
        "INSERT INTO users (user_id, email, created_utc) VALUES ('u2', 'b@example.com', ?)",
        (TS,))
    acct_conn.execute(
        "INSERT INTO consent_preferences (user_id, email_consent, unsubscribe_token)"
        " VALUES ('u1', 1, 'tok-a')")
    with pytest.raises(sqlite3.IntegrityError):
        acct_conn.execute(
            "INSERT INTO consent_preferences (user_id, email_consent, unsubscribe_token)"
            " VALUES ('u2', 1, 'tok-a')")
