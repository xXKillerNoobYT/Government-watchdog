"""Per-request zero-leak civic-data gate (AC-1, GOV-754 leg 2).

The ONE authorization path civic-data endpoints call. On EVERY request it
resolves raw bearer token -> ``auth_sessions`` (hash, live, unexpired) ->
user -> LATEST ``access_grants`` tier — nothing is cached, so an owner
revocation propagates to the very next request with no token-invalidation
machinery.

Fail-closed contract: only ``tier == 'approved'`` passes. Everything else —
``pending``, ``revoked``, ``paused``, ``waitlisted``, ``none``, unknown
token, expired or revoked session, missing token — gets the SAME constant
403 body (:data:`DENIED_BODY`). One indistinguishable denial: the body
carries no civic data, no tier, no user id, no existence signal.

This module is additive; the four frozen serving surfaces are untouched
(AC-8/INV-1). Future serving legs import :func:`guard_civic_request` (or the
worked example :func:`fetch_civic_statements`) instead of rolling their own
auth.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from accounts import service, sessions

#: The one body every denied request receives. Constant on purpose (AC-1).
DENIED_BODY = {"error": "access_denied"}


@dataclass(frozen=True)
class Principal:
    user_id: str
    tier: str


def authorize(conn: sqlite3.Connection, raw_token: str | None, *,
              now: datetime | None = None) -> Principal | None:
    """token -> session -> user -> latest tier; Principal only if 'approved'."""
    if not raw_token:
        return None
    user_id = sessions.verify_session(conn, raw_token, now=now)
    if user_id is None:
        return None
    tier = service.current_tier(conn, user_id)
    if tier != "approved":
        return None
    return Principal(user_id=user_id, tier=tier)


def guard_civic_request(conn: sqlite3.Connection, raw_token: str | None, *,
                        now: datetime | None = None
                        ) -> tuple[int, dict | Principal]:
    """Endpoint-shaped wrapper: ``(200, Principal)`` or ``(403, DENIED_BODY)``.

    Callers MUST return the 403 body verbatim — never enrich it.
    """
    principal = authorize(conn, raw_token, now=now)
    if principal is None:
        return 403, dict(DENIED_BODY)
    return 200, principal


def fetch_civic_statements(conn: sqlite3.Connection, raw_token: str | None, *,
                           limit: int = 20) -> tuple[int, dict]:
    """Worked example of a gated civic-data endpoint (AC-1 reference shape).

    Approved principals get reviewed statements; everyone else gets the
    constant 403 body. The civic query lives BELOW the gate so no code path
    reaches it unauthorized.
    """
    status, principal_or_body = guard_civic_request(conn, raw_token)
    if status != 200:
        return status, principal_or_body  # DENIED_BODY, no civic fields
    rows = conn.execute(
        "SELECT statement_id, statement_text, verification_status"
        " FROM statements WHERE verification_status = 'reviewed_source_linked'"
        " ORDER BY statement_id LIMIT ?", (limit,)).fetchall()
    return 200, {
        "statements": [
            {"statement_id": r[0], "statement_text": r[1],
             "verification_status": r[2]} for r in rows
        ],
    }
