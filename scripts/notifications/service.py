"""In-app notification writer + query endpoint (AC-6, GOV-754 leg 2).

One writer (:func:`record`), five lifecycle emitters matching the AC-6 kinds
(account approved, account revoked, cohort advanced, consent recorded,
unsubscribe confirmed), one reader (:func:`query`), and one
session-authenticated endpoint (:func:`query_for_token`).

Access model: a user may read ONLY their own notifications, and session
authentication is enough — no approved tier required. This is deliberate:
"your account was approved/revoked" must be visible to exactly the users the
civic-data gate (accounts.gate) locks out, and notification bodies carry no
civic data by construction (fixed lifecycle strings composed here).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

KINDS = frozenset({
    "access_approved", "access_revoked", "cohort_advanced",
    "consent_recorded", "unsubscribe_confirmed", "system",
})


class UnknownNotificationKind(ValueError):
    """Raised when ``kind`` is not in the 0025 ``notification_events`` enum."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record(conn: sqlite3.Connection, *, user_id: str, kind: str,
           body_text: str) -> str:
    """Append one notification event; returns the new ``notif_id``."""
    if kind not in KINDS:
        raise UnknownNotificationKind(kind)
    notif_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO notification_events (notif_id, user_id, kind, body_text,"
        " created_utc) VALUES (?, ?, ?, ?, ?)",
        (notif_id, user_id, kind, body_text, _utcnow()),
    )
    conn.commit()
    return notif_id


# --- the five AC-6 lifecycle emitters (fixed strings; no civic data) --------

def notify_access_approved(conn: sqlite3.Connection, user_id: str) -> str:
    return record(conn, user_id=user_id, kind="access_approved",
                  body_text="Your account has been approved for beta access.")


def notify_access_revoked(conn: sqlite3.Connection, user_id: str) -> str:
    return record(conn, user_id=user_id, kind="access_revoked",
                  body_text="Your beta access has been revoked.")


def notify_cohort_advanced(conn: sqlite3.Connection, user_id: str,
                           to_cohort: str) -> str:
    return record(conn, user_id=user_id, kind="cohort_advanced",
                  body_text=f"Your account was added to cohort {to_cohort}.")


def notify_consent_recorded(conn: sqlite3.Connection, user_id: str) -> str:
    return record(conn, user_id=user_id, kind="consent_recorded",
                  body_text="Your email consent preference was recorded.")


def notify_unsubscribe_confirmed(conn: sqlite3.Connection, user_id: str) -> str:
    return record(conn, user_id=user_id, kind="unsubscribe_confirmed",
                  body_text="You have been unsubscribed from email.")


# --- readers -----------------------------------------------------------------

def query(conn: sqlite3.Connection, *, user_id: str, unread_only: bool = False,
          limit: int = 50) -> list[dict]:
    """Newest-first notifications for one user (own-rows-only by contract)."""
    sql = ("SELECT notif_id, kind, body_text, read_utc, created_utc"
           " FROM notification_events WHERE user_id = ?")
    if unread_only:
        sql += " AND read_utc IS NULL"
    sql += " ORDER BY created_utc DESC, rowid DESC LIMIT ?"
    return [
        {"notif_id": r[0], "kind": r[1], "body_text": r[2],
         "read_utc": r[3], "created_utc": r[4]}
        for r in conn.execute(sql, (user_id, limit))
    ]


def mark_read(conn: sqlite3.Connection, *, user_id: str, notif_id: str) -> bool:
    """Mark one of the user's OWN notifications read; False if not theirs."""
    cur = conn.execute(
        "UPDATE notification_events SET read_utc = ?"
        " WHERE notif_id = ? AND user_id = ? AND read_utc IS NULL",
        (_utcnow(), notif_id, user_id),
    )
    conn.commit()
    return cur.rowcount == 1


def query_for_token(conn: sqlite3.Connection, raw_token: str, *,
                    unread_only: bool = False,
                    limit: int = 50) -> tuple[int, dict]:
    """Session-authenticated notification endpoint.

    Resolves the bearer token via ``accounts.sessions`` on every call (no
    caching); any live session may read its OWN notifications regardless of
    tier — approval/revocation notices must reach non-approved users. Returns
    ``(200, {"notifications": [...]})`` or ``(401, {"error": ...})`` with a
    constant body that leaks nothing.
    """
    from accounts import sessions  # deferred: keep this package a leaf at import time

    user_id = sessions.verify_session(conn, raw_token)
    if user_id is None:
        return 401, {"error": "invalid_session"}
    return 200, {"notifications": query(conn, user_id=user_id,
                                        unread_only=unread_only, limit=limit)}
