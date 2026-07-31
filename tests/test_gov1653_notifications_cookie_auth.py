"""GOV-1653 (issue #135): bind ``/api/notifications`` to ``gw_beta_session``.

One test per acceptance criterion on the card, plus the resolver unit tests
that keep :mod:`beta.cookie_auth` honest.

Two deliberate test-design choices, because getting either wrong would produce
a suite that passes for the wrong reason:

* **The allowlist-revocation case revokes by direct SQL, not via**
  ``allowlist.revoke``. The public helper *cascade-revokes the email's beta
  sessions*, so a test using it would be denied by the session gate and would
  still pass with the allowlist re-check deleted. Revoking only the allowlist
  row leaves a live session behind, which is the sole arrangement that
  actually exercises step 3 of the resolver.
* **Failure modes are compared byte-for-byte against each other**, not merely
  asserted to be 401. "Indistinguishable" is a claim about the response, and
  a status-only assertion would not notice a body that named the cause.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import db
import export_web_artifact as ewa
from accounts import service as accounts_service, sessions as account_sessions
from beta import allowlist, common as beta_common, cookie_auth
from beta import http_api as beta_http_api
from beta import sessions as beta_sessions
from email_gateway import flags
from notifications import http_api, service as notif
from conftest import seed_civic_marker_statement

OWNER_REF = "test-card-gov1653"
BETA_EMAIL = "browser@example.com"
OTHER_EMAIL = "someone-else@example.com"

CONSTANT_401 = (401, {"error": "invalid_session"})


# --- fixtures ---------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "gov1653.db"
    db.apply_migrations(path)
    conn = db.open_db(path)
    seed_civic_marker_statement(conn)
    conn.close()
    return path


@pytest.fixture()
def conn(db_path: Path):
    conn = db.open_db(db_path)
    yield conn
    conn.close()


def _enable_notifications(conn: sqlite3.Connection) -> None:
    flags.set_flag(conn, http_api.NOTIFICATIONS_HTTP_FLAG, enabled=True,
                   owner_decision_ref=OWNER_REF)


def _enable_beta(conn: sqlite3.Connection, *, enabled: bool = True) -> None:
    flags.set_flag(conn, cookie_auth.BETA_GATE_FLAG, enabled=enabled,
                   owner_decision_ref=OWNER_REF)


def _browser_user(conn: sqlite3.Connection, *, email: str = BETA_EMAIL,
                  notifications: int = 2) -> tuple[str, str]:
    """An allowlisted beta user WITH a canonical account. -> (uid, cookie)."""
    uid = accounts_service.create_user(conn, email=email)
    allowlist.add(conn, email, owner_decision_ref=OWNER_REF)
    for index in range(notifications):
        notif.record(conn, user_id=uid, kind="access_approved",
                     body_text=f"lifecycle notice {index}")
    _, raw_token = beta_sessions.issue(conn, email)
    return uid, raw_token


def _call(conn: sqlite3.Connection, *, cookie=None, authorization=None,
          path: str = http_api.ROUTE) -> tuple[int, dict]:
    return http_api.process_request(conn, path=path,
                                    authorization=authorization,
                                    cookie_header=cookie)


def _cookie(raw_token: str) -> str:
    return f"{cookie_auth.COOKIE_NAME}={raw_token}"


# --- AC1: the flag is evaluated before any credential -----------------------


def test_flag_absent_is_404_even_with_a_perfectly_valid_cookie(conn):
    """The shipped state. A valid credential must not make the route exist."""
    _enable_beta(conn)
    _, raw_token = _browser_user(conn)
    assert _call(conn, cookie=_cookie(raw_token)) == (404, {"error": "not_found"})


def test_flag_disabled_is_404_even_with_a_perfectly_valid_cookie(conn):
    _enable_beta(conn)
    _, raw_token = _browser_user(conn)
    flags.set_flag(conn, http_api.NOTIFICATIONS_HTTP_FLAG, enabled=False,
                   owner_decision_ref=OWNER_REF)
    assert _call(conn, cookie=_cookie(raw_token)) == (404, {"error": "not_found"})


# --- AC2: the bearer contract is unchanged ----------------------------------


def test_valid_bearer_alone_still_returns_200(conn):
    _enable_notifications(conn)
    uid = accounts_service.create_user(conn, email="api-client@example.com")
    notif.record(conn, user_id=uid, kind="access_approved", body_text="hello")
    _, token = account_sessions.issue_session(conn, uid)

    status, body = _call(conn, authorization=f"Bearer {token}")

    assert status == 200
    assert set(body) == {"notifications", "unread_count"}
    assert body["unread_count"] == 1


def test_bearer_path_works_with_the_beta_gate_off(conn):
    """The beta flag governs the COOKIE lane only; it must not gate bearers."""
    _enable_notifications(conn)
    _enable_beta(conn, enabled=False)
    uid = accounts_service.create_user(conn, email="api-client@example.com")
    notif.record(conn, user_id=uid, kind="access_approved", body_text="hello")
    _, token = account_sessions.issue_session(conn, uid)

    status, _ = _call(conn, authorization=f"Bearer {token}")

    assert status == 200


# --- AC3/AC4: the cookie lane serves own rows, and only own rows ------------


def test_valid_cookie_returns_that_accounts_notifications(conn):
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn, notifications=2)

    status, body = _call(conn, cookie=_cookie(raw_token))

    assert status == 200
    assert set(body) == {"notifications", "unread_count"}
    assert body["unread_count"] == 2
    assert len(body["notifications"]) == 2
    assert set(body["notifications"][0]) == {
        "id", "kind", "title", "body", "created_utc", "read"}


def test_another_users_rows_never_appear_and_never_move_the_unread_count(conn):
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn, notifications=2)

    other_uid = accounts_service.create_user(conn, email=OTHER_EMAIL)
    for index in range(5):
        notif.record(conn, user_id=other_uid, kind="access_revoked",
                     body_text=f"not yours {index}")

    status, body = _call(conn, cookie=_cookie(raw_token))

    assert status == 200
    assert body["unread_count"] == 2, "unread count leaked another user's rows"
    assert len(body["notifications"]) == 2
    assert not any("not yours" in item["body"]
                   for item in body["notifications"])


# --- AC5: a live session with no canonical account creates nothing ----------


def test_cookie_without_a_canonical_account_is_401_and_creates_nothing(conn):
    """Beta admission is not account creation."""
    _enable_notifications(conn)
    _enable_beta(conn)
    allowlist.add(conn, "ghost@example.com", owner_decision_ref=OWNER_REF)
    _, raw_token = beta_sessions.issue(conn, "ghost@example.com")

    before_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    before_notifs = conn.execute(
        "SELECT COUNT(*) FROM notification_events").fetchone()[0]

    assert _call(conn, cookie=_cookie(raw_token)) == CONSTANT_401

    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before_users
    assert conn.execute(
        "SELECT COUNT(*) FROM notification_events").fetchone()[0] == before_notifs


def test_near_miss_emails_do_not_resolve(conn):
    """Exact normalized equality only — no aliasing, no plus-address folding."""
    _enable_notifications(conn)
    _enable_beta(conn)
    accounts_service.create_user(conn, email="real@example.com")
    # Every near-miss below is a VALID address that merely resembles the real
    # one — an address the allowlist would refuse outright (e.g. one with a
    # space) could never reach the account lookup and would prove nothing.
    for near in ("real+tag@example.com", "real@example.com.evil.test",
                 "xreal@example.com", "real@xexample.com"):
        allowlist.add(conn, near, owner_decision_ref=OWNER_REF)
        _, raw_token = beta_sessions.issue(conn, near)
        assert _call(conn, cookie=_cookie(raw_token)) == CONSTANT_401, near


# --- AC6: every cookie failure mode is byte-identical -----------------------


def _failure_modes(conn) -> dict[str, str | None]:
    """Build one live example of each denial cause. -> {label: cookie header}"""
    _enable_notifications(conn)
    _enable_beta(conn)

    # unknown: well-formed, never issued
    unknown = beta_common.new_raw_token()

    # expired: issued already past its TTL
    _, expired = beta_sessions.issue(conn, BETA_EMAIL, ttl_seconds=-1)

    # revoked: issued then revoked
    accounts_service.create_user(conn, email=BETA_EMAIL)
    allowlist.add(conn, BETA_EMAIL, owner_decision_ref=OWNER_REF)
    _, revoked = beta_sessions.issue(conn, BETA_EMAIL)
    beta_sessions.revoke(conn, revoked)

    # unmapped: live + allowlisted, but no canonical account
    allowlist.add(conn, "ghost@example.com", owner_decision_ref=OWNER_REF)
    _, unmapped = beta_sessions.issue(conn, "ghost@example.com")

    return {
        "missing": None,
        "malformed": f"{cookie_auth.COOKIE_NAME}=not a token!",
        "empty_value": f"{cookie_auth.COOKIE_NAME}=",
        "unknown": _cookie(unknown),
        "expired": _cookie(expired),
        "revoked": _cookie(revoked),
        "unmapped": _cookie(unmapped),
    }


def test_all_cookie_failure_modes_are_byte_equivalent(conn):
    modes = _failure_modes(conn)
    seen = {}
    for label, cookie in modes.items():
        status, body = _call(conn, cookie=cookie)
        seen[label] = (status, json.dumps(body, sort_keys=True))
    distinct = set(seen.values())
    assert distinct == {(401, json.dumps(CONSTANT_401[1], sort_keys=True))}, (
        f"failure modes are distinguishable: {seen}")


def test_beta_flag_revocation_kills_a_live_cookie(conn):
    """Turning the beta off must retire cookies minted while it was on."""
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn)
    assert _call(conn, cookie=_cookie(raw_token))[0] == 200

    _enable_beta(conn, enabled=False)

    assert _call(conn, cookie=_cookie(raw_token)) == CONSTANT_401


def test_allowlist_revocation_kills_a_still_live_session(conn):
    """The allowlist is re-checked per request, not only at sign-in.

    Revoked by direct SQL on purpose: ``allowlist.revoke`` also cascade-revokes
    the email's sessions, which would let the SESSION gate deny the request and
    leave this test green even if the allowlist re-check were removed.
    """
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn)
    assert _call(conn, cookie=_cookie(raw_token))[0] == 200

    conn.execute(
        "UPDATE beta_allowlist SET status = 'revoked' WHERE email = ?",
        (BETA_EMAIL,))
    conn.commit()

    assert beta_sessions.verify(conn, raw_token) == BETA_EMAIL, (
        "precondition: the session must still be live, or this test proves "
        "the session gate rather than the allowlist re-check")
    assert _call(conn, cookie=_cookie(raw_token)) == CONSTANT_401


# --- AC7: cookie + bearer, in every validity combination --------------------


def test_cookie_plus_bearer_is_denied_in_every_validity_combination(conn):
    _enable_notifications(conn)
    _enable_beta(conn)
    uid, raw_token = _browser_user(conn)
    _, good_bearer = account_sessions.issue_session(conn, uid)

    good_cookie = _cookie(raw_token)
    bad_cookie = _cookie(beta_common.new_raw_token())

    for cookie in (good_cookie, bad_cookie):
        for bearer in (f"Bearer {good_bearer}", "Bearer nope", "", "Basic x"):
            assert _call(conn, cookie=cookie, authorization=bearer) == CONSTANT_401, (
                f"cookie={cookie!r} bearer={bearer!r} was not denied")


# --- AC8: repeated headers and repeated cookie names ------------------------


def test_repeated_authorization_header_is_denied(conn):
    _enable_notifications(conn)
    uid = accounts_service.create_user(conn, email="api-client@example.com")
    _, token = account_sessions.issue_session(conn, uid)

    assert _call(conn, authorization=[f"Bearer {token}",
                                      f"Bearer {token}"]) == CONSTANT_401


def test_repeated_cookie_header_is_denied(conn):
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn)

    assert _call(conn, cookie=[_cookie(raw_token),
                               _cookie(raw_token)]) == CONSTANT_401


def test_duplicate_named_cookie_in_one_header_is_denied(conn):
    """Two values under one name is ambiguous; picking either one is a guess.

    BOTH orderings are asserted on purpose. With the real token second, a
    last-wins parser (``SimpleCookie``'s behavior) would authenticate; with it
    first, a first-wins parser would. Asserting only one ordering yields a
    test that stays green against the very weakening it exists to forbid —
    the bogus value would simply fail the session lookup and return the same
    401 for the wrong reason.
    """
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn)
    other = beta_common.new_raw_token()

    assert _call(conn, cookie=f"{_cookie(other)}; {_cookie(raw_token)}"
                 ) == CONSTANT_401, "last-wins parser would have authenticated"
    assert _call(conn, cookie=f"{_cookie(raw_token)}; {_cookie(other)}"
                 ) == CONSTANT_401, "first-wins parser would have authenticated"


# --- AC9: unrelated cookies are inert ---------------------------------------


def test_ordinary_cookies_do_not_disturb_the_cookie_credential(conn):
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn, notifications=2)

    header = f"theme=dark; {_cookie(raw_token)}; tz=MST"
    status, body = _call(conn, cookie=header)

    assert status == 200
    assert body["unread_count"] == 2


def test_ordinary_cookies_do_not_disturb_a_bearer_credential(conn):
    """A browser cookie jar must not turn a valid bearer call into a denial."""
    _enable_notifications(conn)
    uid = accounts_service.create_user(conn, email="api-client@example.com")
    notif.record(conn, user_id=uid, kind="access_approved", body_text="hi")
    _, token = account_sessions.issue_session(conn, uid)

    status, _ = _call(conn, cookie="theme=dark; tz=MST",
                      authorization=f"Bearer {token}")

    assert status == 200


# --- resolver unit tests ----------------------------------------------------


def test_cookie_name_matches_the_beta_transport():
    """Drift guard: the auth helper and the Set-Cookie writer must agree."""
    assert cookie_auth.COOKIE_NAME == beta_http_api.COOKIE_NAME


def test_a_freshly_minted_real_token_is_well_formed():
    """Binds the charset guard to the ACTUAL generator.

    If ``new_raw_token`` ever changes alphabet, this fails loudly here instead
    of silently denying every browser at runtime.
    """
    for _ in range(50):
        scan = cookie_auth.scan_session_cookie(
            f"{cookie_auth.COOKIE_NAME}={beta_common.new_raw_token()}")
        assert scan.token is not None and not scan.invalid


@pytest.mark.parametrize("header", [None, "", "theme=dark", "gw_beta_session",
                                    "other=1; theme=dark"])
def test_scan_reports_absent_for_headers_without_our_cookie(header):
    scan = cookie_auth.scan_session_cookie(header)
    assert scan.absent and scan.token is None and not scan.invalid


@pytest.mark.parametrize("value", ["", "has space", "quote\"d", "tab\there",
                                   "plus+sign", "slash/es", "pad=="])
def test_scan_reports_invalid_for_values_we_did_not_mint(value):
    scan = cookie_auth.scan_session_cookie(
        f"{cookie_auth.COOKIE_NAME}={value}")
    assert scan.invalid and scan.token is None


def test_a_semicolon_truncates_rather_than_smuggling(conn):
    """A ';' is the cookie DELIMITER, so it cannot hide inside a value.

    ``gw_beta_session=<real>;colon`` therefore presents ``<real>`` and a
    separate bare token — there is no way to append trailing bytes to a token
    and have them travel with it. Recorded as checked-and-safe so the next
    reader does not mistake the truncation for a parser bug.
    """
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn, notifications=1)

    scan = cookie_auth.scan_session_cookie(f"{_cookie(raw_token)};trailing")
    assert scan.token == raw_token

    # ...and a truncated token is simply an unknown one: denied, not accepted.
    assert _call(conn, cookie=f"{cookie_auth.COOKIE_NAME}="
                              f"{raw_token[:-4]};rest") == CONSTANT_401


def test_scan_accepts_the_rfc6265_quoted_form():
    token = beta_common.new_raw_token()
    scan = cookie_auth.scan_session_cookie(
        f'{cookie_auth.COOKIE_NAME}="{token}"')
    assert scan.token == token


def test_resolver_never_returns_an_email(conn):
    """The email must not escape the module — callers get a user_id or None."""
    _enable_beta(conn)
    uid, raw_token = _browser_user(conn)
    assert cookie_auth.resolve_token_user_id(conn, raw_token) == uid
    assert cookie_auth.resolve_user_id(conn, _cookie(raw_token)) == uid


# --- AC10: the real Cookie header over real HTTP, on the built artifact -----


def _load_run_module(staged: Path):
    run_path = staged / "service" / "run.py"
    spec = importlib.util.spec_from_file_location("gw_artifact_run_1653",
                                                  run_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _http_get(port: int, *, cookie: str | None = None,
              headers: list[tuple[str, str]] | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/notifications")
    if cookie is not None:
        req.add_header("Cookie", cookie)
    for name, value in headers or []:
        # add_header replaces; the capitalized form appends a genuine
        # duplicate, which is the whole point of the repeated-header case.
        req.headers[name] = value
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


@pytest.fixture()
def artifact_server(db_path: Path):
    """The generated artifact's own run.py, serving on a loopback port."""
    conn = db.open_db(db_path)
    _enable_notifications(conn)
    _enable_beta(conn)
    _, raw_token = _browser_user(conn, notifications=3)
    conn.close()

    files = ewa.stage_files(db_path, backend_commit="0" * 40,
                            generated_at_utc="2026-01-01T00:00:00Z")
    root = db_path.parent / "artifact"
    ewa.extract_to(files, root)

    module = _load_run_module(root)
    server = module.serve(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], raw_token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_artifact_serves_a_real_cookie_over_real_http(artifact_server):
    """AC10 — the header path, not just the pure function.

    This is the assertion that would have caught the original defect: the
    router parsed Cookie and dropped it, so every layer below could be correct
    while the browser still got a 401.
    """
    port, raw_token = artifact_server
    status, body = _http_get(port, cookie=_cookie(raw_token))

    assert status == 200
    assert body["unread_count"] == 3
    assert len(body["notifications"]) == 3


def test_artifact_denies_a_bogus_cookie_over_real_http(artifact_server):
    port, _ = artifact_server
    status, body = _http_get(port, cookie=_cookie(beta_common.new_raw_token()))
    assert (status, body) == CONSTANT_401


def test_artifact_denies_cookie_plus_bearer_over_real_http(artifact_server):
    port, raw_token = artifact_server
    status, body = _http_get(port, cookie=_cookie(raw_token),
                             headers=[("Authorization", "Bearer anything")])
    assert (status, body) == CONSTANT_401


def _raw_http_get(port: int, header_lines: list[str]) -> tuple[int, dict]:
    """A hand-built request, because urllib CANNOT send a duplicate header.

    ``urllib.request.Request`` stores headers in a dict, so the repeated-header
    case is unreachable through it. Testing AC8 only at the pure-function level
    would leave the socket layer unproven — and the socket layer is exactly
    where ``.get()`` vs ``.get_all()`` decides whether the duplicate is even
    visible to the code that rejects it.
    """
    request = "GET /api/notifications HTTP/1.1\r\n" + "".join(
        line + "\r\n" for line in [f"Host: 127.0.0.1:{port}",
                                   *header_lines, "Connection: close"]) + "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request.encode("ascii"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(body.decode("utf-8"))


def test_artifact_denies_a_genuinely_duplicated_header_over_real_http(
        artifact_server):
    """AC8 at the socket layer — the case urllib cannot reach."""
    port, raw_token = artifact_server

    # sanity: the same request with ONE cookie header succeeds, so a failure
    # below is caused by the duplication and nothing else.
    assert _raw_http_get(port, [f"Cookie: {_cookie(raw_token)}"])[0] == 200

    duplicated_cookie = _raw_http_get(
        port, [f"Cookie: {_cookie(raw_token)}",
               f"Cookie: {_cookie(raw_token)}"])
    assert duplicated_cookie == CONSTANT_401

    duplicated_auth = _raw_http_get(
        port, ["Authorization: Bearer x", "Authorization: Bearer x"])
    assert duplicated_auth == CONSTANT_401


def test_artifact_response_never_echoes_the_cookie(artifact_server):
    """No credential, email, or lookup detail may appear in any response."""
    port, raw_token = artifact_server
    for cookie in (_cookie(raw_token), _cookie(beta_common.new_raw_token())):
        _, body = _http_get(port, cookie=cookie)
        blob = json.dumps(body)
        assert raw_token not in blob
        assert BETA_EMAIL not in blob
        assert cookie_auth.COOKIE_NAME not in blob
