"""``GET /api/notifications`` — HTTP transport + FE wire adapter (GOV-771).

Named follow-up from the GOV-721 chain closeout: the merged service layer
(:mod:`notifications.service`) is wire-ready but was a Python callable only.
This module is a NEW sibling — the frozen serving surfaces (read_api,
ai_risk_gate, stage5_agenda_board, mcp_service) are untouched — that closes
the five recorded FE↔BE contract deltas:

1. HTTP transport: a stdlib loopback-only server exposing exactly one route,
   ``GET /api/notifications`` (idiom copied from :mod:`webhook_ingress`).
2. Kind rename at the wire: ``access_approved``/``access_revoked`` →
   ``account_approved``/``account_revoked`` (:data:`WIRE_KINDS`).
3. Row-field rename at the wire: ``notif_id``/``body_text``/``read_utc`` →
   ``id``/``body``/``read`` (boolean).
4. Server-authored ``title`` per kind — fixed lifecycle strings only
   (:data:`TITLES`); notification content never carries civic data.
5. Server-computed ``unread_count`` in the envelope (FE treats it as the
   authority and does not recompute).

Access model is unchanged from the service layer: session authentication is
enough (no approved tier — revocation notices must reach exactly the users the
civic gate locks out), own-rows-only, per-request token resolution, constant
401 body. The gate itself stays :func:`notifications.service.query_for_token`;
this module never re-implements it.

Two credentials, never both (GOV-1653, issue #135). Non-browser clients keep
sending ``Authorization: Bearer ...``; the browser beta flow holds only the
HttpOnly ``gw_beta_session`` cookie, which JavaScript cannot read and therefore
cannot promote into a bearer header. A request presenting BOTH a
``gw_beta_session`` and an ``Authorization`` header — in any validity
combination — is ambiguous about who is asking, so it is denied rather than
resolved by precedence. Repeated ``Authorization``/``Cookie`` headers and a
repeated ``gw_beta_session`` name are denied for the same reason. Cookie
identity resolution is delegated whole to :mod:`beta.cookie_auth`; this module
decides only *which* credential is being presented, never what it means.

Fail-closed activation (D1 pattern): the route answers only while the latest
``feature_flags`` row for ``notifications_http_enabled`` is enabled. No row —
the shipped state — means the endpoint answers a constant 404, so merging this
module activates nothing. Enabling is an owner-gated
:func:`email_gateway.flags.set_flag` append (``owner_decision_ref`` required),
expected alongside the GOV-723 cohort work that first needs the live read.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notifications import service  # noqa: E402

ROUTE = "/api/notifications"
NOTIFICATIONS_HTTP_FLAG = "notifications_http_enabled"
ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

#: Cap on how long one connection may hold a worker (GOV-1677). Without this a
#: client that opens a socket and never finishes its request line holds the
#: thread until the OS gives up. Matches ``beta/http_api.py``'s 15s; this route
#: only ever reads a request line and headers, so 15s is generous.
REQUEST_TIMEOUT_SECONDS = 15

# Backend kind -> FE wire kind (delta 2). A backend kind absent here (today:
# "system") is NOT wire-visible: the FE enum is fail-closed on unknown kinds,
# so dropping server-side keeps unread_count consistent with what renders.
WIRE_KINDS = {
    "access_approved": "account_approved",
    "access_revoked": "account_revoked",
    "cohort_advanced": "cohort_advanced",
    "consent_recorded": "consent_recorded",
    "unsubscribe_confirmed": "unsubscribe_confirmed",
}

# Server-authored titles per WIRE kind (delta 4). Fixed strings only — the
# same zero-civic-data rule the service-layer bodies follow by construction.
TITLES = {
    "account_approved": "Account approved",
    "account_revoked": "Access revoked",
    "cohort_advanced": "Cohort updated",
    "consent_recorded": "Consent recorded",
    "unsubscribe_confirmed": "Unsubscribed",
}

# Constant error bodies. Nothing request-derived may appear in them.
BODY_404 = {"error": "not_found"}
BODY_400 = {"error": "bad_request"}
BODY_401 = {"error": "invalid_session"}


class BindError(Exception):
    """Raised on an attempt to bind anywhere but the loopback interface."""


def to_wire_item(row: dict) -> dict | None:
    """Map one service-layer row to the FE ``NotificationItem`` shape.

    Returns None for kinds with no wire mapping (never rendered by the FE).
    """
    wire_kind = WIRE_KINDS.get(row["kind"])
    if wire_kind is None:
        return None
    return {
        "id": row["notif_id"],
        "kind": wire_kind,
        "title": TITLES[wire_kind],
        "body": row["body_text"],
        "created_utc": row["created_utc"],
        "read": row["read_utc"] is not None,
    }


def unread_wire_count(conn: sqlite3.Connection, user_id: str) -> int:
    """Authoritative unread count over wire-visible kinds only."""
    placeholders = ",".join("?" for _ in WIRE_KINDS)
    row = conn.execute(
        f"SELECT COUNT(*) FROM notification_events WHERE user_id = ?"
        f" AND read_utc IS NULL AND kind IN ({placeholders})",
        (user_id, *WIRE_KINDS),
    ).fetchone()
    return int(row[0])


def _wire_body(conn: sqlite3.Connection, user_id: str, rows: list) -> dict:
    """The 200 body for one already-authenticated user.

    Shared by both credential paths so the bearer and cookie lanes can never
    drift in what they expose: same wire mapping, same kind filtering, same
    authoritative unread count. Only the *authentication* differs between
    them; the projection is one implementation.
    """
    items = [w for w in map(to_wire_item, rows) if w is not None]
    return {"notifications": items,
            "unread_count": unread_wire_count(conn, user_id)}


def build_wire_envelope(conn: sqlite3.Connection, raw_token: str, *,
                        unread_only: bool = False,
                        limit: int = DEFAULT_LIMIT) -> tuple[int, dict]:
    """The FE ``NotificationResponse`` envelope for one bearer token.

    Delegates the session gate and the own-rows read entirely to
    :func:`service.query_for_token` (single authority); this function only
    adapts the 200 body. The extra ``verify_session`` call exists solely to
    name the user for the unread count — if it disagrees (token expired
    between the two per-request resolutions), fail closed with the same
    constant 401.
    """
    status, body = service.query_for_token(
        conn, raw_token, unread_only=unread_only, limit=limit)
    if status != 200:
        return status, dict(BODY_401)
    from accounts import sessions  # deferred, same idiom as the service layer
    user_id = sessions.verify_session(conn, raw_token)
    if user_id is None:
        return 401, dict(BODY_401)
    return 200, _wire_body(conn, user_id, body["notifications"])


def build_cookie_envelope(conn: sqlite3.Connection, raw_cookie_token: str, *,
                          unread_only: bool = False,
                          limit: int = DEFAULT_LIMIT) -> tuple[int, dict]:
    """The same envelope for one ``gw_beta_session`` cookie value.

    The entire identity question — beta flag, live session, live allowlist,
    exact canonical account — belongs to :mod:`beta.cookie_auth` and is
    re-asked here on every request. This function never sees the email, never
    creates an account, and cannot tell the failure modes apart: it receives a
    ``user_id`` or nothing, and nothing is the constant 401.

    Once a ``user_id`` exists the read is :func:`service.query` — the very
    same own-rows query the bearer lane reaches through
    :func:`service.query_for_token`, so "own rows only" is enforced in one
    place for both credentials.
    """
    from beta import cookie_auth  # deferred: keeps this package import-leaf

    user_id = cookie_auth.resolve_token_user_id(conn, raw_cookie_token)
    if user_id is None:
        return 401, dict(BODY_401)
    rows = service.query(conn, user_id=user_id,
                         unread_only=unread_only, limit=limit)
    return 200, _wire_body(conn, user_id, rows)


def _parse_query(query: str) -> tuple[bool, int] | None:
    """Strict ``unread_only``/``limit`` parsing; None means 400."""
    params = parse_qs(query, keep_blank_values=True)
    if set(params) - {"unread_only", "limit"}:
        return None
    unread_only = False
    if "unread_only" in params:
        value = params["unread_only"][-1]
        if value not in ("0", "1"):
            return None
        unread_only = value == "1"
    limit = DEFAULT_LIMIT
    if "limit" in params:
        try:
            limit = int(params["limit"][-1])
        except ValueError:
            return None
        if not 1 <= limit <= MAX_LIMIT:
            return None
    return unread_only, limit


def _bearer_token(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]
    return ""


def _header_values(raw: str | list[str] | None) -> list[str]:
    """Normalize one header argument to the list of values actually sent.

    Accepts both shapes on purpose. A socket handler passes
    ``self.headers.get_all(name)`` so that a REPEATED header stays visible as
    two entries — ``.get()`` would silently return the first and hide exactly
    the ambiguity we are required to reject. Every existing caller (and every
    unit test) passes a plain string, which is the one-value case.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [value for value in raw if value is not None]


def process_request(conn: sqlite3.Connection, *, path: str,
                    authorization: str | list[str] | None = None,
                    cookie_header: str | list[str] | None = None,
                    ) -> tuple[int, dict]:
    """Pure request core (no sockets) — the unit-testable heart of the route.

    Order matters: the feature flag is checked before any credential is even
    looked at, so while the flag is off (or absent — fail-closed) the endpoint
    is indistinguishable from a route that does not exist. Query-string
    validation keeps its existing position ahead of authentication so the
    bearer contract shipped in GOV-771 is byte-unchanged.

    Credential arbitration, in order, every branch ending in the SAME constant
    401 body:

    1. a repeated ``Authorization`` or ``Cookie`` header — ambiguous;
    2. a ``gw_beta_session`` that is repeated or not one of our tokens;
    3. a well-formed cookie alongside ANY ``Authorization`` value — two
       claimed identities, so neither is honored;
    4. otherwise exactly one credential shape is present and is resolved by
       its own lane.
    """
    from email_gateway import flags  # deferred, keeps import-time leaf-ness
    if not flags.is_enabled(conn, NOTIFICATIONS_HTTP_FLAG):
        return 404, dict(BODY_404)
    parts = urlsplit(path)
    if parts.path != ROUTE:
        return 404, dict(BODY_404)
    parsed = _parse_query(parts.query)
    if parsed is None:
        return 400, dict(BODY_400)
    unread_only, limit = parsed

    from beta import cookie_auth  # deferred: keeps this package import-leaf

    auth_values = _header_values(authorization)
    cookie_values = _header_values(cookie_header)
    if len(auth_values) > 1 or len(cookie_values) > 1:
        return 401, dict(BODY_401)

    scan = cookie_auth.scan_session_cookie(
        cookie_values[0] if cookie_values else None)
    if scan.invalid:
        return 401, dict(BODY_401)
    if scan.token is not None:
        if auth_values:
            return 401, dict(BODY_401)
        return build_cookie_envelope(conn, scan.token,
                                     unread_only=unread_only, limit=limit)

    return build_wire_envelope(
        conn, _bearer_token(auth_values[0] if auth_values else None),
        unread_only=unread_only, limit=limit)


def make_handler(db_path: Path):
    """Build a request-handler class bound to one DB path."""

    class Handler(BaseHTTPRequestHandler):
        #: Bounds how long one client may hold a worker thread (GOV-1677).
        timeout = REQUEST_TIMEOUT_SECONDS

        def log_message(self, *args):  # silence default stderr spam
            pass

        def do_GET(self):
            conn = _open(db_path)
            try:
                # get_all (not get): a repeated credential header must stay
                # visible to the arbitration above instead of collapsing to
                # its first value.
                status, payload = process_request(
                    conn, path=self.path,
                    authorization=self.headers.get_all("Authorization"),
                    cookie_header=self.headers.get_all("Cookie"))
            finally:
                conn.close()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _open(db_path: Path) -> sqlite3.Connection:
    import db  # deferred: scripts/db.py, resolvable via the sys.path insert
    return db.open_db(db_path)


def serve(db_path: Path, *, host: str = "127.0.0.1",
          port: int = 8771) -> ThreadingHTTPServer:
    """Create (do not yet serve) a server bound to loopback only.

    Refuses any non-loopback host — GATE-PUB / INV-4, same guard as ingress.
    Returns the server so a caller/test can ``serve_forever`` or
    ``handle_request`` then shut down.

    THREADING (GOV-1677): this route was still a plain ``HTTPServer``, which
    serves one request at a time, so a single client that opened a socket and
    never finished its request line denied the whole API. Measured, not feared:
    baseline 200 requests in 1.669s, then one silent socket blocked the next
    client for the full 6.003s timeout.

    ``beta/http_api.py`` was fixed for exactly this in GOV-1669 and this route
    was missed — the same defect on the second of two parallel HTTP surfaces.

    Threading is safe here on the same terms beta's docstring sets out: the
    handler shares no mutable state. ``do_GET`` opens and closes its own sqlite
    connection per request, and the closure captures only ``db_path``, an
    immutable ``Path``. (``intake_api`` remains deliberately un-threaded — its
    handler closes over a ``RawObjectStore`` whose ``_append_link`` appends to a
    shared ledger file that is not established as thread-safe, #206.)
    """
    if host not in ALLOWED_BIND_HOSTS:
        raise BindError(
            f"refusing to bind notifications API to {host!r};"
            " loopback only (127.0.0.1)"
        )
    server = ThreadingHTTPServer((host, port), make_handler(db_path))
    server.daemon_threads = True   # a stalled worker must not block shutdown
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Loopback-only GET /api/notifications server (GOV-771)")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args(argv)
    server = serve(args.db, host=args.host, port=args.port)
    print(f"notifications API listening on http://{args.host}:{args.port}{ROUTE}",
          file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
