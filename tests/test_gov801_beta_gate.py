"""GOV-801: gated-beta front door — magic-link auth + allowlist + waitlist.

Covers the AC end to end: tables/migrations, one-time-use tokens, the HttpOnly
7-day session cookie, per-email rate limits, allowlist revocation (session
cascade), the approved-email -> magic link -> session -> /#/app round trip, and
the structural privacy guarantees (no raw email/IP in the audit log).
"""

from __future__ import annotations

import http.client
import json
import re
import threading

import pytest

import db
from beta import (allowlist, audit, common, http_api, ratelimit, service,
                  sessions, tokens, waitlist)
from email_gateway import adapters, flags


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov801.db"
    db.apply_migrations(path)
    return path


@pytest.fixture()
def conn(db_path):
    c = db.open_db(db_path)
    yield c
    c.close()


def _enable_gate(conn):
    flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov801")


@pytest.fixture()
def capture(conn):
    """Register a real email adapter that records sends; unregister after.

    Enabling the email flag + registering exactly one real adapter makes
    ``resolve_adapter`` hand out this capturer, so the test can read the raw
    magic-link URL out of the (otherwise unsendable) email body.
    """
    sink: list[dict] = []

    class _Capturing:
        name = "capture"

        def send(self, *, to_email, subject, body_text, body_html):
            sink.append({"to": to_email, "subject": subject, "body": body_text})
            return "capture-ref"

    adapters.register_adapter("capture", _Capturing)
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="test-email-on")
    try:
        yield sink
    finally:
        adapters.unregister_adapter("capture")


# --- migration ---------------------------------------------------------------

def test_migration_creates_five_beta_tables(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"beta_allowlist", "beta_magic_tokens", "beta_sessions",
            "beta_waitlist", "beta_audit_log"} <= names


def test_audit_log_has_no_raw_email_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(beta_audit_log)")}
    assert "email" not in cols  # privacy is structural, not procedural
    assert {"email_hash", "ip_hint"} <= cols


# --- allowlist ---------------------------------------------------------------

def test_allowlist_add_check_and_owner_gate(conn):
    assert allowlist.is_allowed(conn, "A@Example.com") is False
    allowlist.add(conn, "A@Example.com", owner_decision_ref="GOV-784")
    assert allowlist.is_allowed(conn, "a@example.com") is True  # normalized
    with pytest.raises(allowlist.OwnerlessAllowlistChange):
        allowlist.add(conn, "b@example.com", owner_decision_ref="")


def test_allowlist_revoke_cascades_to_sessions(conn):
    email = "user@example.com"
    allowlist.add(conn, email, owner_decision_ref="GOV-784")
    _, raw = sessions.issue(conn, email)
    assert sessions.verify(conn, raw) == email
    assert allowlist.revoke(conn, email, owner_decision_ref="GOV-784") is True
    assert allowlist.is_allowed(conn, email) is False
    assert sessions.verify(conn, raw) is None  # cascade revoked the session
    # revoking an already-revoked email is a no-op
    assert allowlist.revoke(conn, email, owner_decision_ref="GOV-784") is False


def test_allowlist_readd_reactivates(conn):
    email = "again@example.com"
    allowlist.add(conn, email, owner_decision_ref="c1")
    allowlist.revoke(conn, email, owner_decision_ref="c2")
    allowlist.add(conn, email, owner_decision_ref="c3")
    assert allowlist.is_allowed(conn, email) is True
    row = conn.execute("SELECT revoked_utc FROM beta_allowlist WHERE email=?",
                       (email,)).fetchone()
    assert row["revoked_utc"] is None


# --- magic tokens: one-time-use + TTL ----------------------------------------

def test_token_is_single_use(conn):
    raw = tokens.issue(conn, "x@example.com")
    assert tokens.consume(conn, raw) == "x@example.com"
    assert tokens.consume(conn, raw) is None  # already used


def test_token_expires(conn):
    raw = tokens.issue(conn, "x@example.com", ttl_seconds=0)
    assert tokens.consume(conn, raw) is None


def test_token_unknown_is_none(conn):
    assert tokens.consume(conn, "not-a-real-token") is None
    assert tokens.consume(conn, "") is None


def test_token_raw_never_stored(conn):
    raw = tokens.issue(conn, "x@example.com")
    stored = conn.execute(
        "SELECT token_hash FROM beta_magic_tokens").fetchone()["token_hash"]
    assert stored == common.sha256_hex(raw)
    assert raw not in stored


# --- sessions ----------------------------------------------------------------

def test_session_verify_revoke(conn):
    _, raw = sessions.issue(conn, "s@example.com")
    assert sessions.verify(conn, raw) == "s@example.com"
    assert sessions.revoke(conn, raw) is True
    assert sessions.verify(conn, raw) is None
    assert sessions.revoke(conn, raw) is False  # already revoked


def test_session_expiry(conn):
    _, raw = sessions.issue(conn, "s@example.com", ttl_seconds=0)
    assert sessions.verify(conn, raw) is None


def test_session_default_ttl_is_seven_days(conn):
    assert sessions.BETA_TTL_SECONDS == 7 * 24 * 3600


# --- rate limiting -----------------------------------------------------------

def test_magic_link_rate_limit_five_per_hour(conn):
    allowlist.add(conn, "rl@example.com", owner_decision_ref="c")
    for _ in range(service.MAGIC_LINK_RATE_PER_HOUR):
        service.request_magic_link(conn, "rl@example.com")
    # 6th request is over the limit: no new token minted
    service.request_magic_link(conn, "rl@example.com")
    count = conn.execute(
        "SELECT COUNT(*) FROM beta_magic_tokens WHERE email=?",
        ("rl@example.com",)).fetchone()[0]
    assert count == service.MAGIC_LINK_RATE_PER_HOUR
    assert ratelimit.over_limit(conn, "beta_magic_tokens", "rl@example.com",
                                limit=service.MAGIC_LINK_RATE_PER_HOUR) is True


def test_waitlist_rate_limit_three_per_hour(conn):
    for _ in range(service.WAITLIST_RATE_PER_HOUR):
        service.join_waitlist(conn, "wl@example.com")
    service.join_waitlist(conn, "wl@example.com")  # over limit
    count = conn.execute(
        "SELECT COUNT(*) FROM beta_waitlist WHERE email=?",
        ("wl@example.com",)).fetchone()[0]
    assert count == service.WAITLIST_RATE_PER_HOUR


# --- service flow: neutrality (no allowlist enumeration) ---------------------

def test_request_for_non_allowlisted_mints_nothing(conn):
    service.request_magic_link(conn, "stranger@example.com")
    assert conn.execute("SELECT COUNT(*) FROM beta_magic_tokens").fetchone()[0] == 0


def test_request_for_allowlisted_mints_and_sends(conn, capture):
    allowlist.add(conn, "vip@example.com", owner_decision_ref="c")
    service.request_magic_link(conn, "vip@example.com")
    assert conn.execute("SELECT COUNT(*) FROM beta_magic_tokens").fetchone()[0] == 1
    assert len(capture) == 1 and capture[0]["to"] == "vip@example.com"


def test_revoked_invite_cannot_be_redeemed(conn):
    email = "revoked@example.com"
    allowlist.add(conn, email, owner_decision_ref="c")
    raw = tokens.issue(conn, email)
    allowlist.revoke(conn, email, owner_decision_ref="c")
    # token is still valid+unconsumed, but the email is no longer allowlisted
    assert service.verify_magic_link(conn, raw) is None


# --- privacy: audit log carries no raw email / no raw IP ---------------------

def test_audit_never_stores_raw_email_or_ip(conn):
    ip_hint = common.ip_hint("203.0.113.7")
    allowlist.add(conn, "priv@example.com", owner_decision_ref="c")
    service.request_magic_link(conn, "priv@example.com", ip_hint=ip_hint)
    rows = conn.execute("SELECT email_hash, ip_hint, event FROM beta_audit_log"
                        ).fetchall()
    assert rows  # events were recorded
    dumped = json.dumps([dict(r) for r in rows])
    assert "priv@example.com" not in dumped
    assert "203.0.113.7" not in dumped
    for r in rows:
        if r["ip_hint"] is not None:
            assert len(r["ip_hint"]) == common.IP_HINT_LEN
        if r["email_hash"] is not None:
            assert r["email_hash"] == common.sha256_hex("priv@example.com")


def test_audit_rejects_unknown_event(conn):
    with pytest.raises(audit.UnknownAuditEvent):
        audit.record(conn, event="not_a_real_event")


# --- HTTP: fail-closed flag --------------------------------------------------

def test_flag_absent_every_route_is_404(conn):
    for method, path in [
            ("POST", service.MAGIC_LINK_REQUEST_ROUTE),
            ("GET", service.MAGIC_LINK_VERIFY_ROUTE + "?token=x"),
            ("POST", service.WAITLIST_ROUTE),
            ("DELETE", service.SESSION_CURRENT_ROUTE)]:
        status, body, _ = http_api.process_request(
            conn, method=method, path=path, raw_body=b"{}")
        assert (status, body) == (404, http_api.BODY_404)


def test_flag_disabled_latest_row_wins(conn):
    _enable_gate(conn)
    status, _, _ = http_api.process_request(
        conn, method="POST", path=service.WAITLIST_ROUTE,
        raw_body=b'{"email":"a@b.com"}')
    assert status == 200
    flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=False,
                   owner_decision_ref="off")
    status, _, _ = http_api.process_request(
        conn, method="POST", path=service.WAITLIST_ROUTE,
        raw_body=b'{"email":"a@b.com"}')
    assert status == 404


# --- HTTP: request/waitlist bodies -------------------------------------------

def test_magic_link_request_neutral_200_and_bad_body_400(conn):
    _enable_gate(conn)
    # missing email -> 400
    status, body, _ = http_api.process_request(
        conn, method="POST", path=service.MAGIC_LINK_REQUEST_ROUTE,
        raw_body=b'{"nope":1}')
    assert (status, body) == (400, http_api.BODY_400)
    # well-formed but non-allowlisted -> neutral 200, nothing minted
    status, body, _ = http_api.process_request(
        conn, method="POST", path=service.MAGIC_LINK_REQUEST_ROUTE,
        raw_body=b'{"email":"ghost@example.com"}')
    assert (status, body) == (200, http_api.BODY_OK)
    assert conn.execute("SELECT COUNT(*) FROM beta_magic_tokens").fetchone()[0] == 0


def test_waitlist_endpoint_records(conn):
    _enable_gate(conn)
    status, body, _ = http_api.process_request(
        conn, method="POST", path=service.WAITLIST_ROUTE,
        raw_body=b'{"email":"join@example.com","area_interest":"alpine"}')
    assert (status, body) == (200, http_api.BODY_OK)
    row = conn.execute("SELECT email, area_interest FROM beta_waitlist"
                       ).fetchone()
    assert (row["email"], row["area_interest"]) == ("join@example.com", "alpine")


# --- HTTP: verify redirect + cookie ------------------------------------------

def test_verify_success_sets_httponly_cookie_and_redirects_to_app(conn):
    _enable_gate(conn)
    allowlist.add(conn, "flow@example.com", owner_decision_ref="c")
    raw = tokens.issue(conn, "flow@example.com")
    status, _, headers = http_api.process_request(
        conn, method="GET",
        path=service.MAGIC_LINK_VERIFY_ROUTE + f"?token={raw}")
    assert status == 302
    assert headers["Location"] == service.APP_REDIRECT
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(http_api.COOKIE_NAME + "=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie
    assert f"Max-Age={sessions.BETA_TTL_SECONDS}" in cookie


def test_verify_bad_token_redirects_to_login_no_cookie(conn):
    _enable_gate(conn)
    status, _, headers = http_api.process_request(
        conn, method="GET",
        path=service.MAGIC_LINK_VERIFY_ROUTE + "?token=garbage")
    assert status == 302
    assert headers["Location"] == service.LOGIN_ERROR_REDIRECT
    assert "Set-Cookie" not in headers


def test_sign_out_revokes_and_clears_cookie(conn):
    _enable_gate(conn)
    _, raw = sessions.issue(conn, "out@example.com")
    status, body, headers = http_api.process_request(
        conn, method="DELETE", path=service.SESSION_CURRENT_ROUTE,
        cookie_header=f"{http_api.COOKIE_NAME}={raw}")
    assert (status, body) == (200, http_api.BODY_OK)
    assert "Max-Age=0" in headers["Set-Cookie"]
    assert sessions.verify(conn, raw) is None


# --- bind guard --------------------------------------------------------------

def test_serve_refuses_non_loopback(db_path):
    for host in ("0.0.0.0", "192.168.1.10", "example.com", ""):
        with pytest.raises(http_api.BindError):
            http_api.serve(db_path, host=host)


# --- end-to-end over a real HTTP round trip ----------------------------------

def _http(port, method, path, *, json_body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", port)
    headers = {}
    body = None
    if json_body is not None:
        body = json.dumps(json_body)
        headers["Content-Type"] = "application/json"
    if cookie is not None:
        headers["Cookie"] = cookie
    c.request(method, path, body=body, headers=headers)
    resp = c.getresponse()
    raw = resp.read().decode("utf-8")
    hdrs = {k: v for k, v in resp.getheaders()}
    c.close()
    return resp.status, hdrs, raw


@pytest.fixture()
def live_server(db_path):
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


def test_end_to_end_approved_email_to_session(conn, capture, live_server):
    """approved email -> magic link -> session cookie -> use it -> sign out."""
    _enable_gate(conn)
    allowlist.add(conn, "e2e@example.com", owner_decision_ref="GOV-784")

    # 1. request the magic link over HTTP (neutral 200)
    status, _, _ = _http(live_server, "POST",
                         service.MAGIC_LINK_REQUEST_ROUTE,
                         json_body={"email": "e2e@example.com"})
    assert status == 200

    # the capturing adapter recorded the (otherwise unsendable) email body
    assert len(capture) == 1
    token = re.search(r"[?&]token=([^\s]+)", capture[0]["body"]).group(1)

    # 2. GET the verify link -> 302 to /#/app + Set-Cookie
    status, headers, _ = _http(
        live_server, "GET",
        service.MAGIC_LINK_VERIFY_ROUTE + f"?token={token}")
    assert status == 302
    assert headers["Location"] == service.APP_REDIRECT
    raw_cookie = headers["Set-Cookie"].split(";", 1)[0]  # gw_beta_session=...
    session_value = raw_cookie.split("=", 1)[1]

    # 3. the session resolves to the approved email
    assert sessions.verify(conn, session_value) == "e2e@example.com"

    # 4. the magic link is single-use — a replay fails to /#/login
    status, headers, _ = _http(
        live_server, "GET",
        service.MAGIC_LINK_VERIFY_ROUTE + f"?token={token}")
    assert status == 302 and headers["Location"] == service.LOGIN_ERROR_REDIRECT

    # 5. sign out clears + revokes the session
    status, _, _ = _http(live_server, "DELETE",
                         service.SESSION_CURRENT_ROUTE, cookie=raw_cookie)
    assert status == 200
    assert sessions.verify(conn, session_value) is None


def test_no_cookie_is_lax_or_missing_samesite():
    """GOV-1544 F1 regression: every Set-Cookie this module emits is Strict.

    Guards the GOV-802 acceptance criterion (AC said Strict; Lax must never
    come back) at the helper level and by a whole-module source sweep.
    """
    import inspect

    for cookie in (http_api.build_session_cookie("tok"),
                   http_api.clear_session_cookie()):
        assert "SameSite=Strict" in cookie
        assert "SameSite=Lax" not in cookie
    source = inspect.getsource(http_api)
    assert "SameSite=Lax" not in source
