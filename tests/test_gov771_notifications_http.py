"""GOV-771: HTTP transport + FE wire adapter for ``GET /api/notifications``.

Acceptance (issue card): a contract test proving the FE ``NotificationItem`` /
``NotificationResponse`` shape (website ``src/types/notification.ts``) parses
the REAL endpoint output for all five kinds, over an actual HTTP round-trip;
session-gate coverage suitable for a RED-proof; feature-flag fail-closed (D1);
zero civic data on the wire.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime

import pytest

from accounts import cohorts, consent, service as accounts_service, sessions
from email_gateway import flags
from notifications import http_api, service as notif
from conftest import CIVIC_MARKER, seed_civic_marker_statement

import db

# The FE contract, transcribed from website main src/types/notification.ts.
# If either side changes shape, exactly one of these constants moves.
FE_NOTIFICATION_KINDS = {
    "account_approved", "account_revoked", "cohort_advanced",
    "consent_recorded", "unsubscribe_confirmed",
}
FE_ITEM_FIELDS = {"id", "kind", "title", "body", "created_utc", "read"}
FE_ENVELOPE_FIELDS = {"notifications", "unread_count"}


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov771.db"
    db.apply_migrations(path)
    conn = db.open_db(path)
    seed_civic_marker_statement(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(db_path):
    conn = db.open_db(db_path)
    yield conn
    conn.close()


def _enable(conn):
    flags.set_flag(conn, http_api.NOTIFICATIONS_HTTP_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov771")


def _user_with_all_five_kinds(conn) -> tuple[str, str]:
    """One user run through the REAL lifecycle flows; returns (uid, token)."""
    uid = accounts_service.create_user(conn, email="wire@example.com")
    accounts_service.approve(conn, uid, owner_decision_ref="card-a")
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-o")
    cohorts.advance(conn, uid, to_cohort="beta-2", owner_decision_ref="card-t")
    consent_token = consent.grant_email_consent(conn, uid)
    consent.unsubscribe(conn, consent_token)
    accounts_service.revoke(conn, uid, owner_decision_ref="card-r")
    _, token = sessions.issue_session(conn, uid)
    return uid, token


def _get(port: int, path: str = http_api.ROUTE,
         token: str | None = None) -> tuple[int, dict, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw), raw
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8")
        return err.code, json.loads(raw), raw


@pytest.fixture()
def live_server(db_path):
    """The real thing: http_api.serve on an ephemeral loopback port."""
    server = http_api.serve(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    thread.join(timeout=5)
    # shutdown() only stops the serve_forever loop; the listening socket stays
    # open until server_close(). Without it the fd leaks for the rest of the
    # session and pytest reports ResourceWarning: unclosed socket.
    server.server_close()


# --- the acceptance contract test -------------------------------------------

def test_fe_contract_parses_real_endpoint_output_all_five_kinds(
        conn, live_server):
    _enable(conn)
    uid, token = _user_with_all_five_kinds(conn)
    # a backend-only kind must never reach the wire nor the unread count
    notif.record(conn, user_id=uid, kind="system", body_text="internal note")

    status, envelope, raw = _get(live_server, token=token)
    assert status == 200

    # NotificationResponse: exact envelope, server-authoritative unread_count
    assert set(envelope) == FE_ENVELOPE_FIELDS
    assert envelope["unread_count"] == 5
    assert len(envelope["notifications"]) == 5

    # every item parses as NotificationItem, strictly
    for item in envelope["notifications"]:
        assert set(item) == FE_ITEM_FIELDS
        assert isinstance(item["id"], str) and item["id"]
        assert item["kind"] in FE_NOTIFICATION_KINDS
        assert isinstance(item["title"], str) and item["title"]
        assert isinstance(item["body"], str) and item["body"]
        assert isinstance(item["read"], bool)
        datetime.fromisoformat(item["created_utc"])  # ISO-8601 or raises

    # all five kinds present exactly once — renames included
    assert sorted(i["kind"] for i in envelope["notifications"]) == sorted(
        FE_NOTIFICATION_KINDS)

    # zero civic data on the wire (bodies are fixed lifecycle strings)
    assert CIVIC_MARKER not in raw
    assert "internal note" not in raw


# --- delta 2 + 3 + 4: the wire adapter itself --------------------------------

def test_wire_item_field_and_kind_mapping():
    row = {"notif_id": "n-1", "kind": "access_approved",
           "body_text": "Your account has been approved for beta access.",
           "read_utc": None, "created_utc": "2026-07-16T00:00:00.000+00:00"}
    assert http_api.to_wire_item(row) == {
        "id": "n-1", "kind": "account_approved", "title": "Account approved",
        "body": "Your account has been approved for beta access.",
        "created_utc": "2026-07-16T00:00:00.000+00:00", "read": False,
    }
    row["read_utc"] = "2026-07-16T01:00:00.000+00:00"
    assert http_api.to_wire_item(row)["read"] is True
    assert http_api.to_wire_item({**row, "kind": "system"}) is None


def test_every_wire_kind_has_a_title():
    assert set(http_api.TITLES) == set(http_api.WIRE_KINDS.values())
    assert set(http_api.WIRE_KINDS.values()) == FE_NOTIFICATION_KINDS


# --- delta 5: unread_count authority -----------------------------------------

def test_unread_count_tracks_mark_read_and_ignores_system(conn):
    _enable(conn)
    uid, token = _user_with_all_five_kinds(conn)
    notif.record(conn, user_id=uid, kind="system", body_text="internal")

    status, envelope = http_api.process_request(
        conn, path=http_api.ROUTE, authorization=f"Bearer {token}")
    assert (status, envelope["unread_count"]) == (200, 5)

    first = envelope["notifications"][0]["id"]
    assert notif.mark_read(conn, user_id=uid, notif_id=first) is True
    _, envelope = http_api.process_request(
        conn, path=http_api.ROUTE, authorization=f"Bearer {token}")
    assert envelope["unread_count"] == 4
    read_flags = {i["id"]: i["read"] for i in envelope["notifications"]}
    assert read_flags[first] is True
    assert sum(1 for v in read_flags.values() if not v) == 4


# --- session gate (RED-proof target) ------------------------------------------

def test_session_gate_constant_401_for_every_failure_mode(conn):
    _enable(conn)
    uid, _ = _user_with_all_five_kinds(conn)
    _, expired = sessions.issue_session(conn, uid, ttl_seconds=0)
    sid, revoked = sessions.issue_session(conn, uid)
    sessions.revoke_session(conn, session_id=sid)

    bodies = set()
    for auth in (None, "Bearer garbage", f"Bearer {expired}",
                 f"Bearer {revoked}", "NotBearer scheme"):
        status, body = http_api.process_request(
            conn, path=http_api.ROUTE, authorization=auth)
        assert status == 401
        bodies.add(json.dumps(body, sort_keys=True))
    assert bodies == {json.dumps(http_api.BODY_401, sort_keys=True)}


def test_own_rows_only_over_http(conn, live_server):
    _enable(conn)
    uid_a = accounts_service.create_user(conn, email="a@example.com")
    uid_b = accounts_service.create_user(conn, email="b@example.com")
    notif.record(conn, user_id=uid_a, kind="cohort_advanced", body_text="A row")
    notif.record(conn, user_id=uid_b, kind="cohort_advanced", body_text="B row")
    _, token_a = sessions.issue_session(conn, uid_a)

    status, envelope, raw = _get(live_server, token=token_a)
    assert status == 200
    assert [i["body"] for i in envelope["notifications"]] == ["A row"]
    assert "B row" not in raw


# --- D1 feature flag: fail-closed ---------------------------------------------

def test_flag_absent_means_route_does_not_exist(conn):
    uid, token = _user_with_all_five_kinds(conn)
    status, body = http_api.process_request(
        conn, path=http_api.ROUTE, authorization=f"Bearer {token}")
    assert (status, body) == (404, http_api.BODY_404)


def test_flag_disabled_row_stays_closed_and_latest_row_wins(conn):
    uid, token = _user_with_all_five_kinds(conn)
    flags.set_flag(conn, http_api.NOTIFICATIONS_HTTP_FLAG, enabled=False,
                   owner_decision_ref="card-off")
    status, _ = http_api.process_request(
        conn, path=http_api.ROUTE, authorization=f"Bearer {token}")
    assert status == 404
    _enable(conn)
    status, _ = http_api.process_request(
        conn, path=http_api.ROUTE, authorization=f"Bearer {token}")
    assert status == 200
    flags.set_flag(conn, http_api.NOTIFICATIONS_HTTP_FLAG, enabled=False,
                   owner_decision_ref="card-off-again")
    status, _ = http_api.process_request(
        conn, path=http_api.ROUTE, authorization=f"Bearer {token}")
    assert status == 404


def test_flag_off_is_indistinguishable_from_unknown_route(conn):
    uid, token = _user_with_all_five_kinds(conn)
    off = http_api.process_request(conn, path=http_api.ROUTE,
                                   authorization=f"Bearer {token}")
    _enable(conn)
    unknown = http_api.process_request(conn, path="/api/other",
                                       authorization=f"Bearer {token}")
    assert off == unknown == (404, http_api.BODY_404)


# --- transport hygiene ----------------------------------------------------------

def test_strict_query_params(conn):
    _enable(conn)
    uid, token = _user_with_all_five_kinds(conn)
    auth = f"Bearer {token}"
    for query in ("?limit=0", "?limit=999", "?limit=abc", "?unread_only=2",
                  "?surprise=1"):
        status, body = http_api.process_request(
            conn, path=http_api.ROUTE + query, authorization=auth)
        assert (status, body) == (400, http_api.BODY_400), query
    status, envelope = http_api.process_request(
        conn, path=http_api.ROUTE + "?unread_only=1&limit=2",
        authorization=auth)
    assert status == 200
    assert len(envelope["notifications"]) == 2
    assert all(i["read"] is False for i in envelope["notifications"])


def test_serve_refuses_non_loopback_bind(db_path):
    for host in ("0.0.0.0", "192.168.1.10", "example.com", ""):
        with pytest.raises(http_api.BindError):
            http_api.serve(db_path, host=host)
