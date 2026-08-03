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
import pathlib
import time
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


# --- C1b drift guard: the audit event enum exists twice (GOV-1664, #193) -----

def _schema_audit_events(conn) -> set[str]:
    """The event names the LIVE schema accepts, parsed from its own DDL.

    Read from ``sqlite_master`` rather than from the migration file on disk: it
    pins what the database actually enforces, which is what ``audit.record``
    collides with. A migration file can be superseded, renamed or shadowed by a
    later ALTER and this still tracks reality.
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table'"
        " AND name = 'beta_audit_log'").fetchone()[0]
    check = re.search(r"CHECK\s*\(\s*event\s+IN\s*\((.*?)\)\s*\)", ddl,
                      re.DOTALL | re.IGNORECASE)
    assert check, f"no CHECK(event IN (...)) found in beta_audit_log DDL:\n{ddl}"
    return set(re.findall(r"'([^']+)'", check.group(1)))


def test_audit_events_match_the_schema_enum(conn):
    """``audit.EVENTS`` and the ``beta_audit_log`` CHECK enum are one list, twice.

    They are hand-maintained in two places (#193) and the failure is asymmetric.
    A name in Python that SQL rejects raises ``sqlite3.IntegrityError`` from
    inside :func:`audit.record` — which runs on the *failure* branches of sign-in
    (``service.py`` request/rate-limit/reject paths), i.e. a crash in the audit
    trail of an access gate, on the paths that execute when something is already
    going wrong. And because the log is append-only with no update path, a write
    lost that way is not recoverable later.
    """
    assert audit.EVENTS == _schema_audit_events(conn)


def test_every_declared_audit_event_is_actually_insertable(conn):
    """Set equality is necessary but not sufficient — prove each name INSERTs.

    Equality would still pass if both sides drifted identically, or if the CHECK
    were parsed correctly but semantically unenforced. This writes one row per
    declared event and lets the database be the judge.
    """
    for event in sorted(audit.EVENTS):
        audit.record(conn, event=event, email="drift@example.com",
                     ip_hint=None, detail="c1b-drift-guard")
    written = {r["event"] for r in conn.execute(
        "SELECT DISTINCT event FROM beta_audit_log")}
    assert written == audit.EVENTS


def test_an_event_outside_the_enum_is_refused_before_sql(conn):
    """The Python guard fires first, so SQL never sees an unknown name."""
    with pytest.raises(audit.UnknownAuditEvent):
        audit.record(conn, event="magic_link_teleported", email="x@example.com")
    assert conn.execute("SELECT COUNT(*) FROM beta_audit_log").fetchone()[0] == 0


# --- C1b: pin the contract's unpinned invariants (GOV-1665) ------------------
#
# Docs/gov801-access-gate-contract.md §5 states eight numbered invariants. The
# C1b drift sweep on 2026-07-31 mapped each to the suite and found FOUR with no
# test behind them: INV-1, INV-3's schema half, INV-6's second half, and INV-8.
# They all hold in the code today — this pins them so they keep holding.

#: Every key any /api/beta/* body is allowed to contain. Deliberately tiny: the
#: surface answers with fixed status constants, never with data.
ALLOWED_BODY_KEYS = {"status", "error"}

#: The full dispatch table, so INV-1 is checked EXHAUSTIVELY rather than route
#: by route. A route added later is covered the moment it is added here — and if
#: someone adds a route and forgets, `test_dispatch_table_covers_every_route`
#: below fails, so the omission cannot pass silently either.
ALL_BETA_REQUESTS = [
    ("POST", service.MAGIC_LINK_REQUEST_ROUTE, b'{"email":"inv@example.com"}'),
    ("POST", service.MAGIC_LINK_REQUEST_ROUTE, b"not json"),
    ("GET", service.MAGIC_LINK_VERIFY_ROUTE + "?token=nope", b""),
    ("POST", service.MAGIC_LINK_CONSUME_ROUTE, b'{"email":"inv@example.com","code":"000000"}'),
    ("POST", service.MAGIC_LINK_CONSUME_ROUTE, b"{}"),
    ("POST", service.WAITLIST_ROUTE, b'{"email":"inv@example.com"}'),
    ("DELETE", service.SESSION_CURRENT_ROUTE, b""),
    ("GET", "/api/beta/does-not-exist", b""),
]


def test_no_beta_route_body_can_carry_civic_data(conn):
    """INV-1, exhaustively: every /api/beta/* body is a fixed status constant.

    The area's headline promise is that the front door serves ZERO civic data.
    Existing tests assert specific routes return `BODY_OK`, which is a weaker
    claim: a route added tomorrow that returned a record would satisfy all of
    them. This walks the whole dispatch table and asserts every body's key set
    is a subset of {status, error} — so a body carrying a record, an email, or
    an allowlist flag fails regardless of which route grew it.
    """
    _enable_gate(conn)
    allowlist.add(conn, "inv@example.com", owner_decision_ref="GOV-1665")

    for method, path, raw in ALL_BETA_REQUESTS:
        status, body, _headers = http_api.process_request(
            conn, method=method, path=path, raw_body=raw)
        assert isinstance(body, dict), f"{method} {path} returned {type(body)}"
        assert set(body) <= ALLOWED_BODY_KEYS, (
            f"{method} {path} -> {status} leaked non-status keys: "
            f"{sorted(set(body) - ALLOWED_BODY_KEYS)}")
        for value in body.values():
            assert isinstance(value, str) and len(value) <= 32, (
                f"{method} {path} -> {status} body value looks like data, "
                f"not a status constant: {value!r}")


def test_dispatch_table_covers_every_route(conn):
    """INV-1's own guard: the table above must list every route the module serves.

    Without this, INV-1 could silently stop being exhaustive — someone adds a
    route to `service.py`, does not add it here, and the sweep above still
    passes while covering less than it claims.
    """
    declared = {name: value for name, value in vars(service).items()
                if name.endswith("_ROUTE")}
    covered = {path.split("?")[0] for _m, path, _b in ALL_BETA_REQUESTS}
    missing = {name: value for name, value in declared.items()
               if value not in covered}
    assert not missing, f"routes declared but not swept by INV-1: {missing}"


def test_schema_refuses_an_ownerless_allowlist_row(conn):
    """INV-3's SECOND half: the DB refuses it too, not just the service layer.

    `test_allowlist_add_check_and_owner_gate` proves the Python guard raises.
    The contract claims defence in depth — that `owner_decision_ref` is NOT NULL
    in-schema as well — and that half was asserted nowhere. Written as a direct
    INSERT so it bypasses the service guard entirely and lets the database
    answer.
    """
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO beta_allowlist (email, status, owner_decision_ref,"
            " added_utc) VALUES (?, 'active', NULL, ?)",
            ("ownerless@example.com", common.iso(common.utcnow())))
        conn.commit()
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM beta_allowlist").fetchone()[0] == 0


def test_audit_module_has_no_update_or_delete_path(conn):
    """INV-6's SECOND half: append-only is a property of the CODE, not a wish.

    The 'no raw email/IP' half is tested; 'there is deliberately no update or
    delete path' was not. Holding today by absence is exactly the kind of
    property that a future convenience helper erases without anyone noticing,
    so it is pinned by sweeping the module source — same idiom as the existing
    SameSite sweep at the end of this file.
    """
    import pathlib

    # Sweep the whole `beta` package, not just audit.py: the property is "no
    # code writes to this table except by appending", and a helper added in a
    # sibling module would break it just as thoroughly.
    #
    # Matched with the SQL keyword adjacent to the table name. An earlier
    # version searched for bare "UPDATE " / "DELETE " and failed on audit.py's
    # OWN DOCSTRING, which says "there is deliberately no update or delete
    # path" — a prose sentence describing the guarantee tripped the guard that
    # enforces it. Keyword+table adjacency cannot be triggered by prose.
    package = pathlib.Path(audit.__file__).parent
    offenders = []
    for module in sorted(package.glob("*.py")):
        text = module.read_text(encoding="utf-8").upper()
        for verb in ("UPDATE BETA_AUDIT_LOG", "DELETE FROM BETA_AUDIT_LOG"):
            if verb in text:
                offenders.append(f"{module.name}: {verb}")
    assert offenders == [], offenders

    # And the append path itself is still there, so this cannot pass vacuously
    # by the table having been renamed out from under the sweep.
    assert "INSERT INTO BETA_AUDIT_LOG" in (
        pathlib.Path(audit.__file__).read_text(encoding="utf-8").upper())


def test_no_email_leaves_the_machine_while_the_adapter_flag_is_off(conn):
    """INV-8: with the flag off, a registered real adapter is NOT reachable.

    The `capture` fixture deliberately turns the flag ON so other tests can read
    the magic-link body. That leaves the shipped state — flag OFF, null adapter,
    nothing leaves the machine — untested. Here the capturer is registered
    WITHOUT enabling the flag: `resolve_adapter` must still hand back the null
    adapter, so the sink stays empty even on the happy path that would otherwise
    send two emails.
    """
    sink: list[dict] = []

    class _Capturing:
        name = "capture-off"

        def send(self, *, to_email, subject, body_text, body_html):
            sink.append({"to": to_email})
            return "should-never-happen"

    adapters.register_adapter("capture-off", _Capturing)
    try:
        _enable_gate(conn)
        allowlist.add(conn, "quiet@example.com", owner_decision_ref="GOV-1665")

        service.request_magic_link(conn, "quiet@example.com")
        service.join_waitlist(conn, "quiet@example.com")

        assert sink == [], f"email escaped with the adapter flag OFF: {sink}"
    finally:
        adapters.unregister_adapter("capture-off")


# --- #185: re-adding without a note must not destroy it (GOV-1666) ----------

def test_readd_without_note_preserves_the_existing_note(conn):
    """The real owner workflow: allow --note ... / revoke / allow (no --note).

    `--note` is optional on the CLI, so the third command carries None. Before
    GOV-1666 that NULL overwrote the stored note — silently, with no audit of
    the erasure, since `allowlist_added` records only `owner_decision_ref`.
    """
    allowlist.add(conn, "note@example.com", owner_decision_ref="GOV-784",
                  note="pilot reviewer, Alpine")
    allowlist.revoke(conn, "note@example.com", owner_decision_ref="GOV-900")

    allowlist.add(conn, "note@example.com", owner_decision_ref="GOV-901")

    row = conn.execute("SELECT note, status, owner_decision_ref FROM"
                       " beta_allowlist WHERE email = ?",
                       ("note@example.com",)).fetchone()
    assert row["note"] == "pilot reviewer, Alpine"
    assert row["status"] == "active"          # re-activation still works
    assert row["owner_decision_ref"] == "GOV-901"  # ...and the ref DOES update


def test_readd_with_a_new_note_replaces_it(conn):
    allowlist.add(conn, "note2@example.com", owner_decision_ref="GOV-784",
                  note="first")
    allowlist.add(conn, "note2@example.com", owner_decision_ref="GOV-901",
                  note="second")

    row = conn.execute("SELECT note FROM beta_allowlist WHERE email = ?",
                       ("note2@example.com",)).fetchone()
    assert row["note"] == "second"


def test_empty_string_note_clears_it_deliberately(conn):
    """Erasing stays possible — it just has to be asked for."""
    allowlist.add(conn, "note3@example.com", owner_decision_ref="GOV-784",
                  note="to be removed")
    allowlist.add(conn, "note3@example.com", owner_decision_ref="GOV-901",
                  note="")

    row = conn.execute("SELECT note FROM beta_allowlist WHERE email = ?",
                       ("note3@example.com",)).fetchone()
    assert row["note"] == ""


def test_first_add_without_a_note_stores_null(conn):
    """COALESCE must not invent a value when there is no prior row."""
    allowlist.add(conn, "note4@example.com", owner_decision_ref="GOV-784")

    row = conn.execute("SELECT note FROM beta_allowlist WHERE email = ?",
                       ("note4@example.com",)).fetchone()
    assert row["note"] is None


# --- GOV-1667 (C7b hunt): the front door had NO body cap at all -------------

def test_front_door_body_is_capped(conn, live_server, monkeypatch):
    """`http_api` accepted an arbitrarily large body before GOV-1667.

    `intake_api` had `_READ_CAP`; this surface had nothing — `rfile.read(length)`
    with no bound, so one unauthenticated POST could make the process buffer as
    much as it cared to declare. Added because the red proof for the fix caught
    that removing `MAX_BODY_BYTES` changed nothing observable: the cap existed
    but nothing was watching it.
    """
    _enable_gate(conn)
    monkeypatch.setattr(http_api, "MAX_BODY_BYTES", 64)

    client = http.client.HTTPConnection("127.0.0.1", live_server)
    client.request("POST", service.MAGIC_LINK_REQUEST_ROUTE,
                   body=b"x" * 4096,
                   headers={"Content-Type": "application/json"})
    response = client.getresponse()
    response.read()
    client.close()

    assert response.status == 413


def test_front_door_normal_body_is_unaffected_by_the_cap(conn, live_server):
    """The cap must not be so tight it rejects real traffic.

    Pairs with the test above: a guard that returns 413 for everything would
    satisfy it while breaking the surface entirely.
    """
    _enable_gate(conn)

    client = http.client.HTTPConnection("127.0.0.1", live_server)
    client.request("POST", service.MAGIC_LINK_REQUEST_ROUTE,
                   body=json.dumps({"email": "capped@example.com"}).encode(),
                   headers={"Content-Type": "application/json"})
    response = client.getresponse()
    response.read()
    client.close()

    assert response.status == 200


# --- GOV-1668 (C8 hunt): pin what the pseudonyms ACTUALLY guarantee ---------
#
# These pin the properties the system relies on — determinism and normalisation
# — and deliberately do NOT pin "non-reversible", because it is not true. The
# old docstrings claimed it; C8 measured otherwise (see #204).

def test_ip_hint_is_deterministic_and_bucketing():
    """Rate-limit forensics need the SAME address to map to the SAME hint.

    This is the property the column exists for, and the one a future change
    (e.g. adding a per-call salt) would silently destroy while still looking
    like a privacy improvement.
    """
    assert common.ip_hint("203.0.113.47") == common.ip_hint("203.0.113.47")
    assert common.ip_hint("203.0.113.47") != common.ip_hint("203.0.113.48")
    assert common.ip_hint(None) is None
    assert common.ip_hint("") is None
    assert len(common.ip_hint("203.0.113.47")) == common.IP_HINT_LEN


def test_ip_hint_is_reversible_and_the_docs_must_not_claim_otherwise():
    """The honest counterpart: a hint IS recoverable from a small search space.

    Asserted rather than assumed, so the claim in the contract and docstrings
    stays anchored to something executable. If a keyed digest lands (#204) this
    test SHOULD start failing — that is the signal to update INV-6, not to
    delete the test quietly.
    """
    import hashlib

    target = "203.0.113.47"
    hint = common.ip_hint(target)

    recovered = [f"203.0.113.{n}" for n in range(256)
                 if hashlib.sha256(f"203.0.113.{n}".encode()).hexdigest()
                 [:common.IP_HINT_LEN] == hint]

    assert recovered == [target], recovered
    # ...and the module must not re-assert the disproved claim.
    import inspect
    doc = inspect.getdoc(common.ip_hint) or ""
    assert "non-reversible" not in doc.lower()


def test_email_hash_normalises_before_hashing(conn):
    """INV-9 at the audit boundary: one address, one pseudonym, any casing."""
    assert common.email_hash("A@Example.COM") == common.email_hash(" a@example.com ")
    assert common.email_hash(None) is None
    assert common.email_hash("") is None


def test_audit_row_carries_the_pseudonym_not_the_address(conn):
    """What INV-6 genuinely guarantees: plaintext never enters the column."""
    audit.record(conn, event="waitlist_joined", email="Subject@Example.com",
                 ip_hint=common.ip_hint("203.0.113.9"))

    row = conn.execute("SELECT email_hash, ip_hint FROM beta_audit_log").fetchone()
    assert row["email_hash"] == common.email_hash("subject@example.com")
    assert "@" not in row["email_hash"]
    assert row["ip_hint"] == common.ip_hint("203.0.113.9")
    assert "203.0.113.9" not in (row["ip_hint"] or "")


def db_path_for_thread_test():
    """A migrated DB with the gate ON, outside the fixture (needs no cleanup)."""
    import tempfile
    path = pathlib.Path(tempfile.mkdtemp()) / "gov1669.db"
    db.apply_migrations(path)
    conn = db.open_db(path)
    flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov1669")
    conn.close()
    return path


# --- GOV-1669 (C9 hunt): one stalled client must not take the gate down -----

def test_handler_has_a_finite_connection_timeout():
    """BaseHTTPRequestHandler.timeout defaults to None, which means NEVER.

    socketserver only calls ``settimeout`` when this is not None, so leaving the
    default lets one connection hold the handler forever.
    """
    handler = http_api.make_handler(pathlib.Path("/nonexistent.db"))
    assert handler.timeout is not None
    assert 0 < handler.timeout <= 120


def test_front_door_is_threaded_so_one_client_cannot_block_the_rest():
    """The measured defect, as a test: a stalled client must not block others.

    Before GOV-1669 this surface was a plain single-threaded ``HTTPServer``.
    Measured then: baseline 200 in 0.004 s; with ONE client that sent 7 bytes of
    a promised 100, a second client got nothing and timed out at 6 s. Here the
    second client must still be served while the first is deliberately stuck.
    """
    import socket

    server = http_api.serve(db_path_for_thread_test(), port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stalled = socket.create_connection(("127.0.0.1", port))
    try:
        stalled.sendall(
            b"POST /api/beta/waitlist HTTP/1.1\r\nHost: t\r\n"
            b"Content-Length: 100\r\n\r\npartial")   # 7 of 100, then silence
        time.sleep(0.3)

        client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        client.request("GET", "/api/beta/nope")
        response = client.getresponse()
        response.read()
        client.close()
        assert response.status == 404      # served WHILE the other is stuck
    finally:
        stalled.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


# --- GOV-1670 (#203): the front door had the same leak, added by GOV-1667 ----

def test_front_door_oversize_is_404_while_the_gate_is_off(conn):
    """The cap GOV-1667 added put its 413 before the flag check — same leak.

    Recorded plainly because this loop filed #203 against `intake_api` one
    iteration AFTER introducing an identical instance here. Both are fixed
    together.
    """
    status, body, _ = http_api.process_request(
        conn, method="POST", path=service.WAITLIST_ROUTE, oversize=True)

    assert (status, body) == (404, http_api.BODY_404)


def test_front_door_oversize_is_413_once_the_gate_is_on(conn):
    _enable_gate(conn)

    status, body, _ = http_api.process_request(
        conn, method="POST", path=service.WAITLIST_ROUTE, oversize=True)

    assert (status, body) == (413, http_api.BODY_413)
