"""Beta cookie sessions: issue / verify / revoke (0026 §3).

7-day sessions. The raw cookie value comes from ``secrets.token_urlsafe(32)``
and is handed to the browser exactly once; only its sha256 digest is stored
(``beta_sessions.token_hash``). Verification is hash + not-revoked + unexpired.

Revocation has two shapes: one session (sign-out) and every session for an
email (allowlist revocation cascade).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from beta import common

BETA_TTL_SECONDS = 7 * 24 * 3600


def issue(conn: sqlite3.Connection, email: str, *,
          ttl_seconds: int = BETA_TTL_SECONDS) -> tuple[str, str]:
    """Mint a session; returns ``(session_id, raw_token)``.

    The raw token is the caller's only copy — never stored, never logged.
    """
    raw_token = common.new_raw_token()
    session_id = str(uuid.uuid4())
    now = common.utcnow()
    conn.execute(
        "INSERT INTO beta_sessions (session_id, email, token_hash, issued_utc,"
        " expires_utc) VALUES (?, ?, ?, ?, ?)",
        (session_id, common.normalize_email(email), common.token_hash(raw_token),
         common.iso(now), common.iso(now + timedelta(seconds=ttl_seconds))),
    )
    conn.commit()
    return session_id, raw_token


def verify(conn: sqlite3.Connection, raw_token: str, *,
           now: datetime | None = None) -> str | None:
    """Resolve a raw cookie value to a normalized email, or None.

    None for: unknown, revoked, or expired — callers treat all three the same
    (fail-closed, no enumeration signal).
    """
    if not raw_token:
        return None
    row = conn.execute(
        "SELECT email, expires_utc, revoked_utc FROM beta_sessions"
        " WHERE token_hash = ?", (common.token_hash(raw_token),)).fetchone()
    if row is None:
        return None
    if row["revoked_utc"] is not None:
        return None
    if common.iso(now or common.utcnow()) >= row["expires_utc"]:
        return None
    return row["email"]


def revoke(conn: sqlite3.Connection, raw_token: str) -> bool:
    """Revoke one session by its raw cookie value; True if a live row changed."""
    if not raw_token:
        return False
    cur = conn.execute(
        "UPDATE beta_sessions SET revoked_utc = ? WHERE token_hash = ?"
        " AND revoked_utc IS NULL",
        (common.iso(common.utcnow()), common.token_hash(raw_token)))
    conn.commit()
    return cur.rowcount == 1


def revoke_all_for_email(conn: sqlite3.Connection, email: str) -> int:
    """Revoke every live session for an email (allowlist-revocation cascade)."""
    cur = conn.execute(
        "UPDATE beta_sessions SET revoked_utc = ? WHERE email = ?"
        " AND revoked_utc IS NULL",
        (common.iso(common.utcnow()), common.normalize_email(email)))
    conn.commit()
    return cur.rowcount
