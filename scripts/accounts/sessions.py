"""Bearer sessions: issue / verify / revoke (0025 §11, INV-10).

Raw tokens come from ``secrets.token_urlsafe(32)`` and are returned to the
caller EXACTLY ONCE at issue time; only the sha256 hex digest is ever stored
(``auth_sessions.token_hash``). There is no way to recover a raw token from
the database, and no function here logs one.

Verification checks hash + not-revoked + not-expired. Tier is NOT checked
here — that is the zero-leak gate's job (``accounts.gate``), which re-resolves
the latest ``access_grants`` row on every request so revocation propagates
without any token-invalidation machinery.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

DEFAULT_TTL_SECONDS = 24 * 3600


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_session(conn: sqlite3.Connection, user_id: str, *,
                  ttl_seconds: int = DEFAULT_TTL_SECONDS) -> tuple[str, str]:
    """Mint a session; returns ``(session_id, raw_token)``.

    The raw token is the caller's only copy — it is never stored or logged.
    """
    raw_token = secrets.token_urlsafe(32)
    session_id = str(uuid.uuid4())
    now = _utcnow()
    conn.execute(
        "INSERT INTO auth_sessions (session_id, user_id, token_hash,"
        " issued_utc, expires_utc) VALUES (?, ?, ?, ?, ?)",
        (session_id, user_id, token_hash(raw_token), _iso(now),
         _iso(now + timedelta(seconds=ttl_seconds))),
    )
    conn.commit()
    return session_id, raw_token


def verify_session(conn: sqlite3.Connection, raw_token: str, *,
                   now: datetime | None = None) -> str | None:
    """Resolve a raw bearer token to a ``user_id``, or None.

    None for: unknown token, revoked session, expired session. Callers must
    treat all three identically (fail-closed, no enumeration signal).
    """
    if not raw_token:
        return None
    row = conn.execute(
        "SELECT user_id, expires_utc, revoked_utc FROM auth_sessions"
        " WHERE token_hash = ?", (token_hash(raw_token),)
    ).fetchone()
    if row is None:
        return None
    user_id, expires_utc, revoked_utc = row[0], row[1], row[2]
    if revoked_utc is not None:
        return None
    if _iso(now or _utcnow()) >= expires_utc:
        return None
    return user_id


def revoke_session(conn: sqlite3.Connection, *, session_id: str | None = None,
                   raw_token: str | None = None) -> bool:
    """Revoke by session_id or by the raw token itself; True if a row changed."""
    if (session_id is None) == (raw_token is None):
        raise ValueError("pass exactly one of session_id / raw_token")
    where, key = (("session_id", session_id) if session_id is not None
                  else ("token_hash", token_hash(raw_token)))
    cur = conn.execute(
        f"UPDATE auth_sessions SET revoked_utc = ? WHERE {where} = ?"
        " AND revoked_utc IS NULL", (_iso(_utcnow()), key))
    conn.commit()
    return cur.rowcount == 1


def revoke_all_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    """Revoke every live session for a user (used on access revocation)."""
    cur = conn.execute(
        "UPDATE auth_sessions SET revoked_utc = ? WHERE user_id = ?"
        " AND revoked_utc IS NULL", (_iso(_utcnow()), user_id))
    conn.commit()
    return cur.rowcount
