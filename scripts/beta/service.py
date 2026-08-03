"""Gated-beta orchestration: the transport-free logic cores (GOV-801).

Four flows the HTTP layer wraps. Everything enumeration-sensitive is neutral by
construction: :func:`request_magic_link` performs the same visible work — and
returns nothing — whether the email is allowlisted, rate-limited, or unknown,
so a caller cannot probe the allowlist. Route + redirect constants live here
(URL building is logic) so :mod:`.http_api` can import them one-directionally.
"""

from __future__ import annotations

import sqlite3

from beta import (allowlist, audit, common, mailer, provision, ratelimit,
                  sessions, tokens, waitlist)

# Route paths (also the HTTP dispatch table in http_api).
MAGIC_LINK_REQUEST_ROUTE = "/api/beta/magic-link/request"
MAGIC_LINK_VERIFY_ROUTE = "/api/beta/magic-link/verify"
MAGIC_LINK_CONSUME_ROUTE = "/api/beta/magic-link/consume"  # 6-digit code (GOV-1538)
WAITLIST_ROUTE = "/api/beta/waitlist"
SESSION_CURRENT_ROUTE = "/api/beta/sessions/current"
ACCOUNT_DELETION_REQUEST_ROUTE = "/api/beta/account/deletion-request"  # GOV-1565

# Client-side (hash-router) redirect targets for the verify GET.
APP_REDIRECT = "/#/app"
LOGIN_ERROR_REDIRECT = "/#/login?error=invalid_or_expired"

# Where the emailed magic link points. Loopback default — no public host is
# authorized yet; an owner enabling the beta supplies the real base URL.
DEFAULT_VERIFY_BASE_URL = "http://127.0.0.1:8801"

# Rate limits (per email, trailing hour) from the AC.
MAGIC_LINK_RATE_PER_HOUR = 5
WAITLIST_RATE_PER_HOUR = 3


def build_verify_url(raw_token: str, *, verify_base_url: str) -> str:
    """The URL emailed to the user; GET-ing it consumes the token."""
    return f"{verify_base_url}{MAGIC_LINK_VERIFY_ROUTE}?token={raw_token}"


def request_magic_link(conn: sqlite3.Connection, email: str, *,
                       ip_hint: str | None = None,
                       verify_base_url: str = DEFAULT_VERIFY_BASE_URL) -> None:
    """Issue + email a magic link IFF the email is allowlisted and under limit.

    Returns nothing in every case — the HTTP layer always answers a neutral
    200, so no path here reveals allowlist membership or the rate-limit state.
    """
    norm = common.normalize_email(email)
    if not common.valid_email(norm):
        audit.record(conn, event="magic_link_rejected", ip_hint=ip_hint,
                     detail="invalid_email")
        return
    audit.record(conn, event="magic_link_requested", email=norm,
                 ip_hint=ip_hint)
    if ratelimit.over_limit(conn, "beta_magic_tokens", norm,
                            limit=MAGIC_LINK_RATE_PER_HOUR):
        audit.record(conn, event="rate_limited", email=norm, ip_hint=ip_hint,
                     detail="magic_link")
        return
    if not allowlist.is_allowed(conn, norm):
        return  # neutral: never confirm or deny allowlist membership
    raw_token, raw_code = tokens.issue_with_code(conn, norm, ip_hint=ip_hint)
    verify_url = build_verify_url(raw_token, verify_base_url=verify_base_url)
    mailer.send_magic_link(conn, norm, verify_url=verify_url, code=raw_code)
    audit.record(conn, event="magic_link_sent", email=norm, ip_hint=ip_hint)


def verify_magic_link(conn: sqlite3.Connection, raw_token: str, *,
                      ip_hint: str | None = None) -> str | None:
    """Consume a magic token and issue a session; returns the raw session token.

    None on any failure (unknown/expired/reused token, or the email was
    allowlist-revoked between request and verify — re-checked here so a revoked
    invite cannot still be redeemed).
    """
    email = tokens.consume(conn, raw_token)
    if email is None:
        audit.record(conn, event="magic_link_rejected", ip_hint=ip_hint,
                     detail="invalid_or_expired")
        return None
    if not allowlist.is_allowed(conn, email):
        audit.record(conn, event="magic_link_rejected", email=email,
                     ip_hint=ip_hint, detail="not_allowed")
        return None
    provision.provision_account(conn, email)  # GOV-1663: bridge to accounts
    _, raw_session = sessions.issue(conn, email)
    audit.record(conn, event="magic_link_verified", email=email,
                 ip_hint=ip_hint)
    audit.record(conn, event="session_issued", email=email, ip_hint=ip_hint)
    return raw_session


def consume_code(conn: sqlite3.Connection, email: str, code: str, *,
                 ip_hint: str | None = None) -> str | None:
    """Redeem the 6-digit code for ``email`` and issue a session (GOV-1538).

    Returns the raw session token, or None on any failure — bad/expired/reused
    code, attempt cap reached, or the email was allowlist-revoked between
    request and consume (re-checked here, exactly like :func:`verify_magic_link`,
    so a revoked invite cannot be redeemed by code either). The caller answers a
    single neutral error for every None so the code path leaks no allowlist
    signal.
    """
    verified = tokens.consume_code(conn, email, code)
    if verified is None:
        audit.record(conn, event="magic_link_rejected",
                     email=common.normalize_email(email) or None,
                     ip_hint=ip_hint, detail="invalid_or_expired_code")
        return None
    if not allowlist.is_allowed(conn, verified):
        audit.record(conn, event="magic_link_rejected", email=verified,
                     ip_hint=ip_hint, detail="not_allowed")
        return None
    provision.provision_account(conn, verified)  # GOV-1663: bridge to accounts
    _, raw_session = sessions.issue(conn, verified)
    audit.record(conn, event="magic_link_verified", email=verified,
                 ip_hint=ip_hint)
    audit.record(conn, event="session_issued", email=verified, ip_hint=ip_hint)
    return raw_session


def join_waitlist(conn: sqlite3.Connection, email: str, *,
                  area_interest: str | None = None,
                  ip_hint: str | None = None) -> None:
    """Record a waitlist request + confirmation email, under a per-email limit.

    Neutral like the magic-link request: always returns nothing; the HTTP layer
    answers a constant 200.
    """
    norm = common.normalize_email(email)
    if not common.valid_email(norm):
        return
    if ratelimit.over_limit(conn, "beta_waitlist", norm,
                            limit=WAITLIST_RATE_PER_HOUR):
        audit.record(conn, event="rate_limited", email=norm, ip_hint=ip_hint,
                     detail="waitlist")
        return
    waitlist.add(conn, norm, area_interest=area_interest, ip_hint=ip_hint)
    mailer.send_waitlist_confirmation(conn, norm)
    audit.record(conn, event="waitlist_joined", email=norm, ip_hint=ip_hint)


def sign_out(conn: sqlite3.Connection, raw_session_token: str | None, *,
             ip_hint: str | None = None) -> bool:
    """Revoke the caller's session; True if a live session was revoked."""
    if not raw_session_token:
        return False
    email = sessions.verify(conn, raw_session_token)  # name the subject to audit
    if not sessions.revoke(conn, raw_session_token):
        return False
    audit.record(conn, event="session_revoked", email=email, ip_hint=ip_hint)
    return True


def request_account_deletion(conn: sqlite3.Connection,
                             raw_session_token: str | None, *,
                             ip_hint: str | None = None) -> bool:
    """Queue a deletion request for the caller's authenticated account.

    Resolves the ``gw_beta_session`` cookie to its account — via the same
    accounts lane the front door provisions on sign-in (GOV-1663) — and records
    an idempotent, auditable deletion request against it
    (:func:`accounts.service.request_deletion`). No hard delete: the gated beta
    queues the request for owner action, matching the iOS request screen
    (GOV-1539 AC#3).

    Returns False when the session is missing/invalid or resolves to no account
    (the HTTP layer answers a neutral 401 with no account-existence signal), and
    True once the request is recorded — including an idempotent repeat, which is
    still a success for the caller.

    Privacy: the raw token never leaves this call and is never logged; only the
    resolved ``user_id`` (a uuid, not PII) touches storage. No new audit event
    is minted — the append-only ``account_deletion_requests`` row is the trail
    (see 0032 / ``provision.py``), keeping ``audit.EVENTS`` and the
    ``beta_audit_log`` CHECK enum the matched pair GOV-1664 pinned.
    """
    email = sessions.verify(conn, raw_session_token)
    if email is None:
        return False
    # Deferred import keeps `beta` a leaf at import time (mirrors provision.py).
    from accounts import service as accounts_service
    user_id = accounts_service.find_user_by_email(conn, email)
    if user_id is None:
        return False
    accounts_service.request_deletion(conn, user_id)
    return True
