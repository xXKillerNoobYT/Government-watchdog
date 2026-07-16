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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notifications import service  # noqa: E402

ROUTE = "/api/notifications"
NOTIFICATIONS_HTTP_FLAG = "notifications_http_enabled"
ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

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
    items = [w for w in map(to_wire_item, body["notifications"])
             if w is not None]
    return 200, {"notifications": items,
                 "unread_count": unread_wire_count(conn, user_id)}


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


def process_request(conn: sqlite3.Connection, *, path: str,
                    authorization: str | None) -> tuple[int, dict]:
    """Pure request core (no sockets) — the unit-testable heart of the route.

    Order matters: the feature flag is checked before the token is even
    looked at, so while the flag is off (or absent — fail-closed) the
    endpoint is indistinguishable from a route that does not exist.
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
    return build_wire_envelope(conn, _bearer_token(authorization),
                               unread_only=unread_only, limit=limit)


def make_handler(db_path: Path):
    """Build a request-handler class bound to one DB path."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
            pass

        def do_GET(self):
            conn = _open(db_path)
            try:
                status, payload = process_request(
                    conn, path=self.path,
                    authorization=self.headers.get("Authorization"))
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
          port: int = 8771) -> HTTPServer:
    """Create (do not yet serve) an HTTPServer bound to loopback only.

    Refuses any non-loopback host — GATE-PUB / INV-4, same guard as ingress.
    Returns the server so a caller/test can ``serve_forever`` or
    ``handle_request`` then shut down.
    """
    if host not in ALLOWED_BIND_HOSTS:
        raise BindError(
            f"refusing to bind notifications API to {host!r};"
            " loopback only (127.0.0.1)"
        )
    return HTTPServer((host, port), make_handler(db_path))


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
