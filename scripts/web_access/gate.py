"""Fail-closed reviewer-web authorization across supported session types.

The reviewer export endpoint accepts exactly one credential family:

* an approved-account bearer token, delegated unchanged to
  :mod:`accounts.gate`; or
* the owner-gated beta session cookie, verified against its live session,
  active allowlist row, and latest ``beta_gate_enabled`` feature flag.

Supplying both credential families is ambiguous and therefore denied before
either one is evaluated.  Every denial returns the same constant body from
``accounts.gate`` so callers cannot distinguish missing, unknown, expired,
revoked, disabled, or non-approved credentials.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from accounts import gate as accounts_gate
from beta import allowlist as beta_allowlist
from beta import http_api as beta_http_api
from beta import sessions as beta_sessions
from email_gateway import flags

DENIED_BODY = accounts_gate.DENIED_BODY


@dataclass(frozen=True)
class ReviewerPrincipal:
    """An authorized reviewer identity kept entirely on the server side."""

    credential_type: Literal["bearer", "beta_cookie"]
    subject: str


def _bearer_token(authorization: str | None) -> str | None:
    """Preserve the existing case-sensitive ``Bearer `` transport contract."""
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]
    return None


def _beta_cookie_token(cookie_header: str | None) -> tuple[bool, str | None]:
    """Return ``(present, token)`` without accepting duplicate beta cookies.

    ``SimpleCookie`` keeps only one value when a Cookie header repeats a name.
    That behavior is convenient for ordinary preferences but unsafe for an
    identity credential, because the server would silently choose between two
    caller-supplied sessions.  Count the named cookie first, then reuse the
    existing beta transport parser only for the single-value case.
    """
    if not cookie_header:
        return False, None

    values: list[str | None] = []
    for segment in cookie_header.split(";"):
        name, separator, value = segment.strip().partition("=")
        if name.strip() != beta_http_api.COOKIE_NAME:
            continue
        values.append(value.strip() if separator else None)

    if not values:
        return False, None
    if len(values) != 1 or not values[0]:
        return True, None

    token = beta_http_api.cookie_token(cookie_header)
    if token is None or token != values[0]:
        return True, None
    return True, token


def authorize_reviewer(
    conn: sqlite3.Connection,
    *,
    authorization: str | None,
    cookie_header: str | None,
    now: datetime | None = None,
) -> ReviewerPrincipal | None:
    """Resolve exactly one supported credential to a reviewer principal.

    Bearer authorization remains delegated to ``accounts.gate``.  Cookie
    authorization rechecks every owner-controlled beta gate on every request;
    no session or allowlist decision is cached.
    """
    authorization_present = authorization not in (None, "")
    bearer_token = _bearer_token(authorization)
    cookie_present, cookie_token = _beta_cookie_token(cookie_header)

    # Never guess which identity the caller intended when both credential
    # families are present, even if one of them is malformed or duplicated.
    if authorization_present and cookie_present:
        return None

    if authorization_present:
        if bearer_token is None:
            return None
        principal = accounts_gate.authorize(conn, bearer_token, now=now)
        if principal is None:
            return None
        return ReviewerPrincipal(
            credential_type="bearer",
            subject=principal.user_id,
        )

    if not cookie_present or cookie_token is None:
        return None
    if not flags.is_enabled(conn, beta_http_api.BETA_GATE_FLAG):
        return None
    email = beta_sessions.verify(conn, cookie_token, now=now)
    if email is None or not beta_allowlist.is_allowed(conn, email):
        return None
    return ReviewerPrincipal(
        credential_type="beta_cookie",
        subject=email,
    )


def guard_reviewer_request(
    conn: sqlite3.Connection,
    *,
    authorization: str | None,
    cookie_header: str | None,
    now: datetime | None = None,
) -> tuple[int, ReviewerPrincipal | dict]:
    """Return ``(200, principal)`` or the one constant reviewer denial."""
    principal = authorize_reviewer(
        conn,
        authorization=authorization,
        cookie_header=cookie_header,
        now=now,
    )
    if principal is None:
        return 403, dict(DENIED_BODY)
    return 200, principal
