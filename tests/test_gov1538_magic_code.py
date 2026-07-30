"""GOV-1538 (GOV-1523 P4c-2): 6-digit sign-in code fallback for the beta gate.

The universal-link path needs the not-yet-existing Phase-3 domain's AASA file,
so v1 delivers a 6-digit numeric code in the SAME magic-link email and adds a
``POST /api/beta/magic-link/consume`` route that redeems it for a bearer
session. These tests cover the AC end to end:

  * request  — the emailed body carries a link AND a 6-digit code
  * consume  — a correct code issues the 7-day session cookie
  * expiry   — an expired code is refused
  * invalid  — a wrong code fails, bumps the per-token attempt counter, and the
               code dies once the cap is hit (brute-force bound on 10**6)
  * neutral  — a non-allowlisted email consume is indistinguishable from a wrong
               code (same 401), so the route leaks no allowlist membership
  * fail-closed — every consume answers a constant 404 while the flag is off

The link and the code share ONE ``beta_magic_tokens`` row, so redeeming either
consumes the other. Only sha256 digests are stored; the raw code, like the raw
token and raw session, is the caller's only copy.
"""

from __future__ import annotations

import http.client
import json
import re
import threading

import pytest

import db
from beta import allowlist, common, http_api, service, sessions, tokens
from email_gateway import adapters, flags

CODE_RE = re.compile(r"code in the app:\s*(\d{6})")


# --- fixtures (mirrors test_gov801_beta_gate) --------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov1538.db"
    db.apply_migrations(path)
    return path


@pytest.fixture()
def conn(db_path):
    c = db.open_db(db_path)
    yield c
    c.close()


def _enable_gate(conn):
    flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov1538")


@pytest.fixture()
def capture(conn):
    """Register a real email adapter that records sends; unregister after."""
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


# --- migration: additive code columns ----------------------------------------

def test_migration_adds_code_columns_to_magic_tokens(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(beta_magic_tokens)")}
    assert {"code_hash", "code_attempts"} <= cols


# --- code generation ---------------------------------------------------------

def test_new_numeric_code_is_zero_padded_six_digits():
    for _ in range(200):
        code = common.new_numeric_code()
        assert len(code) == common.CODE_DIGITS and code.isdigit()


# --- tokens layer: issue_with_code + consume_code ----------------------------

def test_issue_with_code_stores_only_hashes(conn):
    raw_token, raw_code = tokens.issue_with_code(conn, "x@example.com")
    row = conn.execute(
        "SELECT token_hash, code_hash FROM beta_magic_tokens").fetchone()
    assert row["token_hash"] == common.sha256_hex(raw_token)
    assert row["code_hash"] == common.sha256_hex(raw_code)
    # neither raw secret is stored anywhere
    dumped = json.dumps([dict(r) for r in conn.execute(
        "SELECT * FROM beta_magic_tokens")])
    assert raw_token not in dumped and raw_code not in dumped


def test_consume_code_happy_path(conn):
    _, raw_code = tokens.issue_with_code(conn, "A@Example.com")
    assert tokens.consume_code(conn, "a@example.com", raw_code) == "a@example.com"


def test_consume_code_is_single_use(conn):
    _, raw_code = tokens.issue_with_code(conn, "x@example.com")
    assert tokens.consume_code(conn, "x@example.com", raw_code) == "x@example.com"
    assert tokens.consume_code(conn, "x@example.com", raw_code) is None


def test_link_click_invalidates_the_code(conn):
    raw_token, raw_code = tokens.issue_with_code(conn, "x@example.com")
    assert tokens.consume(conn, raw_token) == "x@example.com"  # redeem the link
    assert tokens.consume_code(conn, "x@example.com", raw_code) is None  # row gone


def test_code_redeem_invalidates_the_link(conn):
    raw_token, raw_code = tokens.issue_with_code(conn, "x@example.com")
    assert tokens.consume_code(conn, "x@example.com", raw_code) == "x@example.com"
    assert tokens.consume(conn, raw_token) is None


def test_consume_code_expired(conn):
    _, raw_code = tokens.issue_with_code(conn, "x@example.com", ttl_seconds=0)
    assert tokens.consume_code(conn, "x@example.com", raw_code) is None


def test_consume_code_unknown_email_is_none(conn):
    tokens.issue_with_code(conn, "known@example.com")
    assert tokens.consume_code(conn, "stranger@example.com", "000000") is None
    assert tokens.consume_code(conn, "", "000000") is None


def test_wrong_code_increments_attempts_then_locks(conn):
    _, raw_code = tokens.issue_with_code(conn, "x@example.com")
    wrong = "654321" if raw_code != "654321" else "123456"
    for i in range(tokens.MAX_CODE_ATTEMPTS):
        assert tokens.consume_code(conn, "x@example.com", wrong) is None
        attempts = conn.execute(
            "SELECT code_attempts FROM beta_magic_tokens WHERE email=?",
            ("x@example.com",)).fetchone()[0]
        assert attempts == i + 1
    # cap reached: even the CORRECT code is now refused (brute-force bound)
    assert tokens.consume_code(conn, "x@example.com", raw_code) is None


def test_newest_code_supersedes_the_prior_one(conn):
    _, old_code = tokens.issue_with_code(conn, "x@example.com")
    _, new_code = tokens.issue_with_code(conn, "x@example.com")
    # only the freshest outstanding code is checked (standard OTP semantics)
    assert tokens.consume_code(conn, "x@example.com", old_code) is None
    assert tokens.consume_code(conn, "x@example.com", new_code) == "x@example.com"


# --- service layer: request emails both, consume issues a session ------------

def test_request_emails_link_and_six_digit_code(conn, capture):
    allowlist.add(conn, "vip@example.com", owner_decision_ref="c")
    service.request_magic_link(conn, "vip@example.com")
    assert len(capture) == 1
    body = capture[0]["body"]
    assert "token=" in body and CODE_RE.search(body)


def test_service_consume_code_issues_session(conn, capture):
    allowlist.add(conn, "flow@example.com", owner_decision_ref="c")
    service.request_magic_link(conn, "flow@example.com")
    code = CODE_RE.search(capture[0]["body"]).group(1)
    raw_session = service.consume_code(conn, "flow@example.com", code)
    assert raw_session is not None
    assert sessions.verify(conn, raw_session) == "flow@example.com"


def test_service_consume_code_denies_revoked_invite(conn, capture):
    email = "revoked@example.com"
    allowlist.add(conn, email, owner_decision_ref="c")
    service.request_magic_link(conn, email)
    code = CODE_RE.search(capture[0]["body"]).group(1)
    allowlist.revoke(conn, email, owner_decision_ref="c")
    # code is still valid+unconsumed, but the email is no longer allowlisted
    assert service.consume_code(conn, email, code) is None


def test_consume_code_neutral_for_non_allowlisted(conn):
    # a non-allowlisted request mints nothing, so a consume finds no code —
    # indistinguishable from a wrong code (both -> None)
    service.request_magic_link(conn, "ghost@example.com")
    assert conn.execute(
        "SELECT COUNT(*) FROM beta_magic_tokens").fetchone()[0] == 0
    assert service.consume_code(conn, "ghost@example.com", "000000") is None


# --- HTTP: fail-closed flag + neutral responses ------------------------------

def test_consume_route_is_404_while_flag_off(conn):
    status, body, _ = http_api.process_request(
        conn, method="POST", path=service.MAGIC_LINK_CONSUME_ROUTE,
        raw_body=b'{"email":"a@b.com","code":"000000"}')
    assert (status, body) == (404, http_api.BODY_404)


def test_consume_bad_body_is_400(conn):
    _enable_gate(conn)
    for raw in (b'{"email":"a@b.com"}', b'{"code":"000000"}', b'{}',
                b'{"email":"a@b.com","code":123456}'):
        status, body, _ = http_api.process_request(
            conn, method="POST", path=service.MAGIC_LINK_CONSUME_ROUTE,
            raw_body=raw)
        assert (status, body) == (400, http_api.BODY_400)


def test_consume_success_sets_session_cookie_200(conn, capture):
    _enable_gate(conn)
    allowlist.add(conn, "ok@example.com", owner_decision_ref="c")
    service.request_magic_link(conn, "ok@example.com")
    code = CODE_RE.search(capture[0]["body"]).group(1)
    status, body, headers = http_api.process_request(
        conn, method="POST", path=service.MAGIC_LINK_CONSUME_ROUTE,
        raw_body=json.dumps({"email": "ok@example.com", "code": code}).encode())
    assert (status, body) == (200, http_api.BODY_OK)
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(http_api.COOKIE_NAME + "=")
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie


def test_consume_wrong_code_is_neutral_401_no_cookie(conn, capture):
    _enable_gate(conn)
    allowlist.add(conn, "ok@example.com", owner_decision_ref="c")
    service.request_magic_link(conn, "ok@example.com")
    status, body, headers = http_api.process_request(
        conn, method="POST", path=service.MAGIC_LINK_CONSUME_ROUTE,
        raw_body=b'{"email":"ok@example.com","code":"999999"}')
    assert (status, body) == (401, http_api.BODY_401)
    assert "Set-Cookie" not in headers


def test_consume_unknown_email_401_matches_wrong_code(conn):
    _enable_gate(conn)
    # never-requested email -> identical 401 to a wrong code (no enumeration)
    status, body, headers = http_api.process_request(
        conn, method="POST", path=service.MAGIC_LINK_CONSUME_ROUTE,
        raw_body=b'{"email":"nobody@example.com","code":"000000"}')
    assert (status, body) == (401, http_api.BODY_401)
    assert "Set-Cookie" not in headers


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


def test_end_to_end_code_sign_in_issues_session(conn, capture, live_server):
    """approved email -> emailed code -> POST consume -> session cookie."""
    _enable_gate(conn)
    allowlist.add(conn, "e2e@example.com", owner_decision_ref="GOV-1538")

    # 1. request over HTTP (neutral 200); the capturer holds the email body
    status, _, _ = _http(live_server, "POST",
                         service.MAGIC_LINK_REQUEST_ROUTE,
                         json_body={"email": "e2e@example.com"})
    assert status == 200
    code = CODE_RE.search(capture[0]["body"]).group(1)

    # 2. POST the code -> 200 + Set-Cookie session
    status, headers, _ = _http(
        live_server, "POST", service.MAGIC_LINK_CONSUME_ROUTE,
        json_body={"email": "e2e@example.com", "code": code})
    assert status == 200
    raw_cookie = headers["Set-Cookie"].split(";", 1)[0]
    session_value = raw_cookie.split("=", 1)[1]
    assert sessions.verify(conn, session_value) == "e2e@example.com"

    # 3. the code is single-use — a replay is a neutral 401
    status, headers, _ = _http(
        live_server, "POST", service.MAGIC_LINK_CONSUME_ROUTE,
        json_body={"email": "e2e@example.com", "code": code})
    assert status == 401 and "Set-Cookie" not in headers
