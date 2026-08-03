"""GOV-1565 (GOV-1523 P4c-2 addendum): the account-deletion-request route.

The iOS client (GOV-1539 4c-3) ships an account-deletion *request* screen but
had no backend to call — it rendered an honest ``.routePending`` state. This
leg adds ``POST /api/beta/account/deletion-request`` so the request routes
through the backend account lifecycle (no client-side deletes). These tests
cover the AC end to end:

  * authed    — a live ``gw_beta_session`` queues an auditable deletion request
                and answers neutral 200 ``{"status": "ok"}``
  * unauthed  — a missing/garbage cookie is one neutral 401 with no
                account-existence signal, and writes nothing
  * flag-off  — every call is a constant 404 while ``beta_gate_enabled`` is off
                (fail closed, D1), even with an otherwise-valid session
  * lifecycle — the request is a real, asserted transition (an
                ``account_deletion_requests`` row), not a no-op 200
  * idempotent — a double-tap collapses onto one open row, still 200
  * zero PII  — no email / session token reaches storage or logs, and NO new
                ``beta_audit_log`` event is minted (audit.EVENTS ↔ CHECK enum
                stay the matched pair GOV-1664 pinned)
"""

from __future__ import annotations

import json
import logging

import pytest

import db
from accounts import service as accounts_service
from beta import allowlist, http_api, provision, service, sessions
from email_gateway import flags


# --- fixtures (mirror test_gov1538_magic_code / test_gov801_beta_gate) --------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov1565.db"
    db.apply_migrations(path)
    return path


@pytest.fixture()
def conn(db_path):
    c = db.open_db(db_path)
    yield c
    c.close()


def _enable_gate(conn):
    flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov1565")


def _provisioned_session(conn, email="member@example.com"):
    """Provision an account for ``email`` and mint a live session cookie value.

    Mirrors the real front door: allowlist the invite, bridge an accounts row
    (GOV-1663 ``provision``), then issue a beta session — the same shape the iOS
    client replays in the ``Cookie`` header.
    """
    allowlist.add(conn, email, owner_decision_ref="gov1565")
    provision.provision_account(conn, email)
    _, raw_session = sessions.issue(conn, email)
    return email, raw_session


def _cookie(raw_session):
    return f"{http_api.COOKIE_NAME}={raw_session}"


def _post_deletion(conn, *, cookie_header=None):
    return http_api.process_request(
        conn, method="POST", path=service.ACCOUNT_DELETION_REQUEST_ROUTE,
        raw_body=b"", cookie_header=cookie_header)


# --- migration ---------------------------------------------------------------

def test_migration_creates_deletion_requests_table(conn):
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(account_deletion_requests)")}
    assert {"request_id", "user_id", "status",
            "requested_utc", "updated_utc"} <= cols


# --- accounts lifecycle layer ------------------------------------------------

def test_request_deletion_writes_one_row_and_is_idempotent(conn):
    user_id = accounts_service.create_user(conn, email="del@example.com")
    assert accounts_service.request_deletion(conn, user_id) is True
    # a replay is a no-op: no second row, no error, returns False
    assert accounts_service.request_deletion(conn, user_id) is False
    rows = conn.execute(
        "SELECT status FROM account_deletion_requests WHERE user_id = ?",
        (user_id,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == "requested"
    assert accounts_service.has_open_deletion_request(conn, user_id) is True


# --- HTTP route: authed happy path -------------------------------------------

def test_authed_post_queues_request_and_returns_200(conn):
    _enable_gate(conn)
    email, raw_session = _provisioned_session(conn)
    user_id = accounts_service.find_user_by_email(conn, email)

    status, body, headers = _post_deletion(conn, cookie_header=_cookie(raw_session))

    assert status == 200
    assert body == {"status": "ok"}
    # lifecycle transition asserted — a real row, not a no-op 200
    assert accounts_service.has_open_deletion_request(conn, user_id) is True


# --- HTTP route: unauthenticated ---------------------------------------------

def test_missing_cookie_is_neutral_401_and_writes_nothing(conn):
    _enable_gate(conn)
    status, body, _ = _post_deletion(conn, cookie_header=None)
    assert status == 401
    assert body == {"error": "unauthorized"}
    assert conn.execute(
        "SELECT COUNT(*) FROM account_deletion_requests").fetchone()[0] == 0


def test_garbage_or_stale_cookie_is_neutral_401(conn):
    _enable_gate(conn)
    # a well-formed cookie whose token matches no live session
    status, body, _ = _post_deletion(
        conn, cookie_header=f"{http_api.COOKIE_NAME}=not-a-real-token")
    assert status == 401
    assert body == {"error": "unauthorized"}
    assert conn.execute(
        "SELECT COUNT(*) FROM account_deletion_requests").fetchone()[0] == 0


# --- HTTP route: fail closed while the flag is off ---------------------------

def test_flag_off_is_constant_404_even_with_valid_session(conn):
    # provision + session first, THEN leave the gate disabled (the shipped state)
    _, raw_session = _provisioned_session(conn)
    status, body, _ = _post_deletion(conn, cookie_header=_cookie(raw_session))
    assert status == 404
    assert body == {"error": "not_found"}
    assert conn.execute(
        "SELECT COUNT(*) FROM account_deletion_requests").fetchone()[0] == 0


# --- HTTP route: idempotent ---------------------------------------------------

def test_repeat_authed_post_is_idempotent_single_row(conn):
    _enable_gate(conn)
    email, raw_session = _provisioned_session(conn)
    user_id = accounts_service.find_user_by_email(conn, email)

    first = _post_deletion(conn, cookie_header=_cookie(raw_session))
    second = _post_deletion(conn, cookie_header=_cookie(raw_session))

    assert first[0] == 200 and second[0] == 200
    assert conn.execute(
        "SELECT COUNT(*) FROM account_deletion_requests WHERE user_id = ?",
        (user_id,)).fetchone()[0] == 1


# --- privacy: zero PII in storage or logs, no new audit event ----------------

def test_no_pii_in_storage_or_logs_and_no_new_audit_event(conn, caplog):
    _enable_gate(conn)
    email, raw_session = _provisioned_session(conn)

    with caplog.at_level(logging.DEBUG):
        status, _, _ = _post_deletion(conn, cookie_header=_cookie(raw_session))
    assert status == 200

    # nothing plaintext (email or the raw session token) reaches the new table
    dumped = json.dumps([dict(r) for r in conn.execute(
        "SELECT * FROM account_deletion_requests")])
    assert email not in dumped
    assert raw_session not in dumped

    # ...nor any emitted log line
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert email not in logged
    assert raw_session not in logged

    # the request mints NO beta_audit_log event: audit.EVENTS and the
    # beta_audit_log CHECK enum are a matched pair (GOV-1664). The append-only
    # account_deletion_requests row is the trail instead.
    events = {r[0] for r in conn.execute(
        "SELECT DISTINCT event FROM beta_audit_log")}
    assert not any("deletion" in e for e in events)
