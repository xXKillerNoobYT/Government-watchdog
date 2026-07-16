"""ACCT-2026 leg 2 (GOV-754): zero-leak civic-data gate — AC-1.

AC-1 RED-proof: neuter the tier check in ``accounts.gate.authorize`` (accept
any live session) and every non-approved 403 test here goes RED, because the
approved-path test proves the same endpoint DOES serve the civic marker when
authorized — the 403s are meaningful, not vacuous.
"""

from __future__ import annotations

import json

import pytest

from accounts import gate, service, sessions
from conftest import CIVIC_MARKER


@pytest.fixture()
def conn(acct2_conn):
    return acct2_conn


def _user_with_token(conn, email, tier=None, ref="card-x"):
    uid = service.create_user(conn, email=email)
    if tier == "approved":
        service.approve(conn, uid, owner_decision_ref=ref)
    elif tier == "revoked":
        service.approve(conn, uid, owner_decision_ref=ref)
        service.revoke(conn, uid, owner_decision_ref=ref)
    elif tier == "paused":
        service.approve(conn, uid, owner_decision_ref=ref)
        service.pause(conn, uid, owner_decision_ref=ref)
    _, token = sessions.issue_session(conn, uid)
    return uid, token


def test_approved_user_receives_civic_data(conn):
    _, token = _user_with_token(conn, "ok@example.com", tier="approved")
    status, body = gate.fetch_civic_statements(conn, token)
    assert status == 200
    assert CIVIC_MARKER in json.dumps(body), (
        "approved path must actually serve civic data, or the 403 tests are vacuous")


@pytest.mark.parametrize("tier", [None, "revoked", "paused"])
def test_non_approved_tiers_get_403_with_no_civic_data(conn, tier):
    # tier None = waitlisted (signup default) — the 'pending' family
    _, token = _user_with_token(conn, f"{tier or 'waitlisted'}@example.com",
                                tier=tier)
    status, body = gate.fetch_civic_statements(conn, token)
    assert status == 403
    assert body == gate.DENIED_BODY
    assert CIVIC_MARKER not in json.dumps(body)


def test_revocation_propagates_to_the_very_next_request(conn):
    uid, token = _user_with_token(conn, "flip@example.com", tier="approved")
    assert gate.fetch_civic_statements(conn, token)[0] == 200
    service.revoke(conn, uid, owner_decision_ref="card-r")
    # No re-login, no token churn — the per-request tier re-read must deny.
    # (revoke also kills sessions; check the gate itself with a fresh session)
    _, token2 = sessions.issue_session(conn, uid)
    status, body = gate.fetch_civic_statements(conn, token2)
    assert status == 403 and body == gate.DENIED_BODY


def test_bad_expired_revoked_and_missing_tokens_all_get_the_same_403(conn):
    uid, _ = _user_with_token(conn, "tok@example.com", tier="approved")
    _, expired = sessions.issue_session(conn, uid, ttl_seconds=0)
    _, revoked = sessions.issue_session(conn, uid)
    sessions.revoke_session(conn, raw_token=revoked)
    bodies = []
    for bad in ("garbage-token", expired, revoked, "", None):
        status, body = gate.fetch_civic_statements(conn, bad)
        assert status == 403
        bodies.append(json.dumps(body, sort_keys=True))
    assert len(set(bodies)) == 1, "denial bodies must be indistinguishable"
    assert CIVIC_MARKER not in "".join(bodies)


def test_guard_returns_principal_only_for_approved(conn):
    _, token = _user_with_token(conn, "p@example.com", tier="approved")
    status, principal = gate.guard_civic_request(conn, token)
    assert status == 200
    assert principal.tier == "approved"
    _, wl_token = _user_with_token(conn, "wl@example.com")
    status, body = gate.guard_civic_request(conn, wl_token)
    assert (status, body) == (403, gate.DENIED_BODY)
