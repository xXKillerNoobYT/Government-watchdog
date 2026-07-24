"""Focused tests for the composed reviewer web-access gate."""

from __future__ import annotations

import json

import pytest

import db
from accounts import service as accounts_service
from accounts import sessions as account_sessions
from beta import allowlist as beta_allowlist
from beta import http_api as beta_http_api
from beta import sessions as beta_sessions
from email_gateway import flags
from web_access import gate


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "web-access.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    yield connection
    connection.close()


def _approved_bearer(conn) -> str:
    user_id = accounts_service.create_user(conn, email="approved@local.test")
    accounts_service.approve(
        conn,
        user_id,
        owner_decision_ref="test-approved-bearer",
    )
    _, raw_token = account_sessions.issue_session(conn, user_id)
    return raw_token


def _active_beta_cookie(conn) -> str:
    email = "beta@local.test"
    beta_allowlist.add(
        conn,
        email,
        owner_decision_ref="test-active-beta",
    )
    _, raw_token = beta_sessions.issue(conn, email)
    return f"{beta_http_api.COOKIE_NAME}={raw_token}"


def _set_beta_gate(conn, *, enabled: bool, ref: str) -> None:
    flags.set_flag(
        conn,
        beta_http_api.BETA_GATE_FLAG,
        enabled=enabled,
        owner_decision_ref=ref,
    )


def test_approved_bearer_behavior_is_preserved_without_beta_flag(conn):
    raw_token = _approved_bearer(conn)

    status, principal = gate.guard_reviewer_request(
        conn,
        authorization=f"Bearer {raw_token}",
        cookie_header="theme=dark",
    )

    assert status == 200
    assert principal.credential_type == "bearer"


def test_active_beta_cookie_authorizes_when_owner_gate_is_enabled(conn):
    cookie = _active_beta_cookie(conn)
    _set_beta_gate(conn, enabled=True, ref="test-beta-on")

    status, principal = gate.guard_reviewer_request(
        conn,
        authorization=None,
        cookie_header=cookie,
    )

    assert status == 200
    assert principal.credential_type == "beta_cookie"


def test_cookie_and_bearer_together_fail_closed_even_when_both_are_valid(conn):
    bearer = _approved_bearer(conn)
    cookie = _active_beta_cookie(conn)
    _set_beta_gate(conn, enabled=True, ref="test-ambiguous-on")

    status, body = gate.guard_reviewer_request(
        conn,
        authorization=f"Bearer {bearer}",
        cookie_header=cookie,
    )

    assert (status, body) == (403, gate.DENIED_BODY)


@pytest.mark.parametrize(
    "authorization",
    [
        "Basic not-supported",
        "bearer wrong-case",
        "Bearer",
        "Bearer ",
        " ",
    ],
)
def test_cookie_and_any_nonempty_authorization_fail_closed(
    conn,
    authorization,
):
    cookie = _active_beta_cookie(conn)
    _set_beta_gate(conn, enabled=True, ref="test-malformed-ambiguous-on")

    status, body = gate.guard_reviewer_request(
        conn,
        authorization=authorization,
        cookie_header=cookie,
    )

    assert (status, body) == (403, gate.DENIED_BODY)


@pytest.mark.parametrize(
    "cookie_header",
    [
        (
            f"{beta_http_api.COOKIE_NAME}=first;"
            f" {beta_http_api.COOKIE_NAME}=second"
        ),
        (
            f"{beta_http_api.COOKIE_NAME}=same;"
            f" theme=dark; {beta_http_api.COOKIE_NAME}=same"
        ),
        f"{beta_http_api.COOKIE_NAME}",
        f"{beta_http_api.COOKIE_NAME}=",
    ],
)
def test_duplicate_or_malformed_beta_cookie_fails_closed(conn, cookie_header):
    _set_beta_gate(conn, enabled=True, ref="test-duplicate-cookie-on")

    status, body = gate.guard_reviewer_request(
        conn,
        authorization=None,
        cookie_header=cookie_header,
    )

    assert (status, body) == (403, gate.DENIED_BODY)


def test_all_beta_cookie_failures_return_one_indistinguishable_403(conn):
    _set_beta_gate(conn, enabled=True, ref="test-denials-on")

    _, expired = beta_sessions.issue(
        conn,
        "expired@local.test",
        ttl_seconds=0,
    )
    _, revoked = beta_sessions.issue(conn, "revoked@local.test")
    beta_sessions.revoke(conn, revoked)
    _, not_allowlisted = beta_sessions.issue(conn, "not-allowed@local.test")

    active_cookie = _active_beta_cookie(conn)

    cookie_headers = [
        None,
        f"{beta_http_api.COOKIE_NAME}=unknown-token",
        f"{beta_http_api.COOKIE_NAME}={expired}",
        f"{beta_http_api.COOKIE_NAME}={revoked}",
        f"{beta_http_api.COOKIE_NAME}={not_allowlisted}",
    ]

    denial_bodies = []
    for cookie_header in cookie_headers:
        status, body = gate.guard_reviewer_request(
            conn,
            authorization=None,
            cookie_header=cookie_header,
        )
        assert status == 403
        denial_bodies.append(json.dumps(body, sort_keys=True))

    # A latest disabled owner flag must invalidate an otherwise-live session.
    _set_beta_gate(conn, enabled=False, ref="test-denials-off")
    status, body = gate.guard_reviewer_request(
        conn,
        authorization=None,
        cookie_header=active_cookie,
    )
    assert status == 403
    denial_bodies.append(json.dumps(body, sort_keys=True))

    assert len(set(denial_bodies)) == 1
    assert json.loads(denial_bodies[0]) == gate.DENIED_BODY
