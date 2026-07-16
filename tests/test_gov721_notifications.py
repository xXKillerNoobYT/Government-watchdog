"""ACCT-2026 leg 2 (GOV-754): in-app notifications — AC-6.

All five lifecycle kinds must be emitted by the real service flows (not just
writable by hand), the reader is own-rows-only, and the session-authenticated
endpoint works for NON-approved users — "your access was revoked" must reach
exactly the people the civic gate locks out.
"""

from __future__ import annotations

import json

import pytest

from accounts import cohorts, consent, service, sessions
from notifications import service as notif
from conftest import CIVIC_MARKER


@pytest.fixture()
def conn(acct2_conn):
    return acct2_conn


def _kinds(conn, uid):
    return [r[0] for r in conn.execute(
        "SELECT kind FROM notification_events WHERE user_id = ?"
        " ORDER BY created_utc, rowid", (uid,))]


def test_all_five_lifecycle_kinds_emitted_by_real_flows(conn):
    uid = service.create_user(conn, email="life@example.com")
    service.approve(conn, uid, owner_decision_ref="card-a")          # access_approved
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-o")
    cohorts.advance(conn, uid, to_cohort="beta-2",
                    owner_decision_ref="card-t")                     # cohort_advanced
    token = consent.grant_email_consent(conn, uid)                   # consent_recorded
    consent.unsubscribe(conn, token)                                 # unsubscribe_confirmed
    service.revoke(conn, uid, owner_decision_ref="card-r")           # access_revoked
    assert set(_kinds(conn, uid)) == {
        "access_approved", "cohort_advanced", "consent_recorded",
        "unsubscribe_confirmed", "access_revoked",
    }


def test_unknown_kind_rejected(conn):
    uid = service.create_user(conn, email="k@example.com")
    with pytest.raises(notif.UnknownNotificationKind):
        notif.record(conn, user_id=uid, kind="marketing_blast", body_text="no")


def test_query_unread_only_and_mark_read_own_rows_only(conn):
    uid = service.create_user(conn, email="q@example.com")
    other = service.create_user(conn, email="other@example.com")
    nid = notif.record(conn, user_id=uid, kind="system", body_text="hello")
    assert [n["notif_id"] for n in notif.query(conn, user_id=uid,
                                               unread_only=True)] == [nid]
    # someone else cannot mark my notification read
    assert notif.mark_read(conn, user_id=other, notif_id=nid) is False
    assert notif.mark_read(conn, user_id=uid, notif_id=nid) is True
    assert notif.query(conn, user_id=uid, unread_only=True) == []


def test_query_for_token_serves_own_rows_to_non_approved_sessions(conn):
    uid = service.create_user(conn, email="me@example.com")   # waitlisted
    other = service.create_user(conn, email="them@example.com")
    notif.record(conn, user_id=uid, kind="system", body_text="mine")
    notif.record(conn, user_id=other, kind="system", body_text="theirs")
    _, token = sessions.issue_session(conn, uid)
    status, body = notif.query_for_token(conn, token)
    assert status == 200
    texts = [n["body_text"] for n in body["notifications"]]
    assert texts == ["mine"], "own rows only"
    assert notif.query_for_token(conn, "bad-token")[0] == 401


def test_notification_bodies_carry_no_civic_marker_through_all_flows(conn):
    uid = service.create_user(conn, email="clean@example.com")
    service.approve(conn, uid, owner_decision_ref="card-a")
    cohorts.open_cohort(conn, "beta-2", owner_decision_ref="card-o")
    cohorts.advance(conn, uid, to_cohort="beta-2", owner_decision_ref="card-t")
    consent.grant_email_consent(conn, uid)
    dump = json.dumps([tuple(r) for r in conn.execute(
        "SELECT body_text FROM notification_events")])
    assert CIVIC_MARKER not in dump
