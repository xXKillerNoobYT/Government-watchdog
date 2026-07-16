"""Email consent + unsubscribe lifecycle (0025 §6, INV-8, AC-4/AC-6).

Consent is opt-in and starts absent: no ``consent_preferences`` row (or
``email_consent = 0``) means the email consent gate (email_gateway.outbox)
refuses to queue anything. Granting consent stores a FRESH
``secrets.token_urlsafe(32)`` unsubscribe token — a token is generated at
grant time, never re-armed after an unsubscribe (INV-8 "never reused"): each
re-consent rotates in a brand-new token and the old one stops matching.
"""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone

from notifications import service as notif


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def get_preferences(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute(
        "SELECT email_consent, notification_consent, unsubscribe_token,"
        " consented_utc, updated_utc FROM consent_preferences WHERE user_id = ?",
        (user_id,)).fetchone()
    if row is None:
        return None
    return {"email_consent": row[0], "notification_consent": row[1],
            "unsubscribe_token": row[2], "consented_utc": row[3],
            "updated_utc": row[4]}


def grant_email_consent(conn: sqlite3.Connection, user_id: str) -> str:
    """Record explicit email consent; returns the new unsubscribe token.

    Always rotates the unsubscribe token (INV-8: never reused) and emits the
    'consent_recorded' notification (AC-6).
    """
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    conn.execute(
        "INSERT INTO consent_preferences (user_id, email_consent,"
        " unsubscribe_token, consented_utc, updated_utc)"
        " VALUES (?, 1, ?, ?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET email_consent = 1,"
        " unsubscribe_token = excluded.unsubscribe_token,"
        " consented_utc = excluded.consented_utc,"
        " updated_utc = excluded.updated_utc",
        (user_id, token, now, now),
    )
    conn.commit()
    notif.notify_consent_recorded(conn, user_id)
    return token


def unsubscribe(conn: sqlite3.Connection, token: str) -> str | None:
    """Withdraw consent by unsubscribe token; returns the user_id or None.

    The token row keeps its value (audit: which token was used) but consent
    drops to 0, so the outbox gate closes immediately. Emits
    'unsubscribe_confirmed' (AC-6).
    """
    if not token:
        return None
    row = conn.execute(
        "SELECT user_id FROM consent_preferences WHERE unsubscribe_token = ?"
        " AND email_consent = 1", (token,)).fetchone()
    if row is None:
        return None
    user_id = row[0]
    conn.execute(
        "UPDATE consent_preferences SET email_consent = 0, updated_utc = ?"
        " WHERE user_id = ?", (_utcnow(), user_id))
    conn.commit()
    notif.notify_unsubscribe_confirmed(conn, user_id)
    return user_id
