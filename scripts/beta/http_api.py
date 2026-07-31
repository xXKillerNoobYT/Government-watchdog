"""HTTP transport for the gated-beta front door (GOV-801).

Five loopback-only routes wiring the GOV-799 landing to :mod:`beta.service`:

  * POST   /api/beta/magic-link/request  -> neutral 200 (never reveals allowlist)
  * GET    /api/beta/magic-link/verify   -> 302 /#/app + Set-Cookie, or 302 back
  * POST   /api/beta/magic-link/consume  -> 200 + Set-Cookie (6-digit code), or
                                            one neutral 401 (GOV-1538 fallback)
  * POST   /api/beta/waitlist            -> neutral 200
  * DELETE /api/beta/sessions/current    -> 200 + cleared cookie (sign out)
  * POST   /api/beta/account/deletion-request -> authed 200 (queues a deletion
                                            request), neutral 401 if unauthed
                                            (GOV-1565)

Fail-closed activation (D1, same idiom as notifications/http_api): every route
is checked against the owner-gated ``beta_gate_enabled`` feature flag FIRST, so
while the flag is off — or absent, the shipped state — the whole surface is
indistinguishable from routes that do not exist (constant 404). Merging this
module activates nothing.

Bind guard: :func:`serve` refuses any host but loopback (GATE-PUB / INV-4 — no
public exposure, ever; public deploy is a later owner card).

Session cookie: HttpOnly + Secure + SameSite=Strict, 7-day Max-Age, Path=/. The raw
value is the browser's only copy — the DB stores only its sha256 (sessions.py).
Strict is safe here (GOV-1543 F1): the cookie is SET on the magic-link 302
(SameSite never restricts setting), and every later authenticated call is a
same-origin fetch('/api/…') where Strict cookies always ride.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beta import common, service, sessions  # noqa: E402
from email_gateway import flags  # noqa: E402

BETA_GATE_FLAG = "beta_gate_enabled"
ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})
COOKIE_NAME = "gw_beta_session"

# Constant bodies — nothing request-derived may appear in them.
BODY_OK = {"status": "ok"}
BODY_404 = {"error": "not_found"}
BODY_400 = {"error": "bad_request"}
# One neutral rejection for the code consume: a wrong/expired code and a
# never-requested (or non-allowlisted) email are indistinguishable here.
BODY_401 = {"error": "invalid_code"}
# Neutral rejection for an authed route reached without a live session — carries
# no account-existence signal (GOV-1565).
BODY_401_UNAUTH = {"error": "unauthorized"}


class BindError(Exception):
    """Raised on an attempt to bind anywhere but the loopback interface."""


# --- cookie helpers ----------------------------------------------------------

def build_session_cookie(raw_token: str, *,
                         max_age: int = sessions.BETA_TTL_SECONDS) -> str:
    """The Set-Cookie value for a fresh session (HttpOnly + Secure + Strict)."""
    return (f"{COOKIE_NAME}={raw_token}; Max-Age={max_age}; Path=/;"
            " HttpOnly; Secure; SameSite=Strict")


def clear_session_cookie() -> str:
    """The Set-Cookie value that immediately expires the session cookie."""
    return (f"{COOKIE_NAME}=; Max-Age=0; Path=/;"
            " HttpOnly; Secure; SameSite=Strict")


def cookie_token(cookie_header: str | None) -> str | None:
    """Extract the raw session token from a Cookie header, or None."""
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


# --- pure request core (no sockets) ------------------------------------------

def _json_body(raw_body: bytes) -> dict | None:
    """Parse a JSON object body, or None if absent/malformed/not-an-object."""
    if not raw_body:
        return None
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def process_request(conn: sqlite3.Connection, *, method: str, path: str,
                    raw_body: bytes = b"", cookie_header: str | None = None,
                    ip_hint: str | None = None,
                    verify_base_url: str = service.DEFAULT_VERIFY_BASE_URL
                    ) -> tuple[int, dict, dict]:
    """The unit-testable heart of the surface: ``(status, body, headers)``.

    The feature flag is checked before the method/route even matter, so a
    disabled or absent flag yields a constant 404 for every request.
    """
    if not flags.is_enabled(conn, BETA_GATE_FLAG):
        return 404, dict(BODY_404), {}
    route = urlsplit(path).path

    if method == "POST" and route == service.MAGIC_LINK_REQUEST_ROUTE:
        body = _json_body(raw_body)
        if body is None or not isinstance(body.get("email"), str):
            return 400, dict(BODY_400), {}
        service.request_magic_link(conn, body["email"], ip_hint=ip_hint,
                                   verify_base_url=verify_base_url)
        return 200, dict(BODY_OK), {}

    if method == "GET" and route == service.MAGIC_LINK_VERIFY_ROUTE:
        params = parse_qs(urlsplit(path).query)
        token = (params.get("token") or [""])[0]
        raw_session = service.verify_magic_link(conn, token, ip_hint=ip_hint)
        if raw_session is None:
            return 302, {}, {"Location": service.LOGIN_ERROR_REDIRECT}
        return 302, {}, {"Location": service.APP_REDIRECT,
                         "Set-Cookie": build_session_cookie(raw_session)}

    if method == "POST" and route == service.MAGIC_LINK_CONSUME_ROUTE:
        # 6-digit code fallback (GOV-1538). Unlike verify's GET redirect, this
        # is an app-driven fetch: a JSON {email, code} in, a session cookie on
        # success (200) or one neutral 401 on any failure — no allowlist signal.
        body = _json_body(raw_body)
        if (body is None or not isinstance(body.get("email"), str)
                or not isinstance(body.get("code"), str)):
            return 400, dict(BODY_400), {}
        raw_session = service.consume_code(conn, body["email"], body["code"],
                                           ip_hint=ip_hint)
        if raw_session is None:
            return 401, dict(BODY_401), {}
        return 200, dict(BODY_OK), {"Set-Cookie": build_session_cookie(raw_session)}

    if method == "POST" and route == service.WAITLIST_ROUTE:
        body = _json_body(raw_body)
        if body is None or not isinstance(body.get("email"), str):
            return 400, dict(BODY_400), {}
        area = body.get("area_interest")
        service.join_waitlist(conn, body["email"],
                              area_interest=area if isinstance(area, str) else None,
                              ip_hint=ip_hint)
        return 200, dict(BODY_OK), {}

    if method == "DELETE" and route == service.SESSION_CURRENT_ROUTE:
        service.sign_out(conn, cookie_token(cookie_header), ip_hint=ip_hint)
        return 200, dict(BODY_OK), {"Set-Cookie": clear_session_cookie()}

    if method == "POST" and route == service.ACCOUNT_DELETION_REQUEST_ROUTE:
        # Authed via the session cookie the iOS client replays: a live session
        # queues an auditable deletion request (neutral 200); a missing/invalid
        # session is one neutral 401 with no account-existence signal. GOV-1565.
        if not service.request_account_deletion(
                conn, cookie_token(cookie_header), ip_hint=ip_hint):
            return 401, dict(BODY_401_UNAUTH), {}
        return 200, dict(BODY_OK), {}

    return 404, dict(BODY_404), {}


# --- socket layer ------------------------------------------------------------

def make_handler(db_path: Path, *,
                 verify_base_url: str = service.DEFAULT_VERIFY_BASE_URL):
    """Build a request-handler class bound to one DB path."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
            pass

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            # ip_hint is computed at the boundary — a raw IP never crosses into
            # the service or audit layers (privacy: truncated hash only).
            ip_hint = common.ip_hint(
                self.client_address[0] if self.client_address else None)
            conn = _open(db_path)
            try:
                status, payload, headers = process_request(
                    conn, method=method, path=self.path, raw_body=raw,
                    cookie_header=self.headers.get("Cookie"), ip_hint=ip_hint,
                    verify_base_url=verify_base_url)
            finally:
                conn.close()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def _open(db_path: Path) -> sqlite3.Connection:
    import db  # deferred: scripts/db.py, resolvable via the sys.path insert
    return db.open_db(db_path)


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 8801,
          verify_base_url: str = service.DEFAULT_VERIFY_BASE_URL) -> HTTPServer:
    """Create (do not yet serve) an HTTPServer bound to loopback only.

    Refuses any non-loopback host — GATE-PUB / INV-4. Returns the server so a
    caller/test can ``serve_forever`` or ``handle_request`` then shut down.
    """
    if host not in ALLOWED_BIND_HOSTS:
        raise BindError(
            f"refusing to bind beta gate to {host!r}; loopback only (127.0.0.1)")
    return HTTPServer((host, port),
                      make_handler(db_path, verify_base_url=verify_base_url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Loopback-only gated-beta front door (GOV-801)")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--verify-base-url",
                        default=service.DEFAULT_VERIFY_BASE_URL)
    args = parser.parse_args(argv)
    server = serve(args.db, host=args.host, port=args.port,
                   verify_base_url=args.verify_base_url)
    print(f"beta gate listening on http://{args.host}:{args.port}",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
