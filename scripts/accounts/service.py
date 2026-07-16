"""Account lifecycle: create, approve/revoke/pause, tier check, login.

* INV-9 — emails are normalized (lowercase + trim) HERE, before every lookup
  and before the ``users.email`` UNIQUE check ever sees a value.
* INV-7/D2 — passwords are argon2id PHC strings via ``argon2-cffi``; the raw
  password is verified and (maybe) re-hashed in memory, never logged, never
  stored, never included in any exception message.
* INV-4 — ``access_grants`` is append-only; the current tier is always the
  latest row per user ordered ``(granted_utc, rowid)`` (rowid tie-break
  because ISO-8601 TEXT timestamps can collide within a millisecond).
* Approve/revoke/pause require a non-null ``owner_decision_ref`` at this
  layer AND in-schema (0025 §3 CHECK) — defense in depth, GOV-753 leg 1.

Revoking access also revokes every live session (belt) even though the
zero-leak gate re-reads the tier per request anyway (suspenders, AC-1).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from accounts import sessions
from notifications import service as notif

_HASHER = PasswordHasher()  # argon2id, library defaults (D2: fine at beta-15)

DECISION_TIERS = frozenset({"approved", "revoked", "paused"})
TIERS = frozenset({"none", "waitlisted", "pending"}) | DECISION_TIERS


class DuplicateEmail(ValueError):
    """Raised when the normalized email already has an account."""


class OwnerlessAccessDecision(ValueError):
    """Approve/revoke/pause attempted without an ``owner_decision_ref``."""


class LoginFailed(ValueError):
    """Unknown email, no password set, or wrong password — indistinguishable."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_email(email: str) -> str:
    """INV-9: lowercase + trim. The ONLY form that touches the DB."""
    return email.strip().lower()


# --- create / waitlist -------------------------------------------------------

def create_user(conn: sqlite3.Connection, *, email: str,
                password: str | None = None,
                area_interest: str | None = None) -> str:
    """Create a user + waitlist request + initial 'waitlisted' grant.

    Returns the new ``user_id``. Signup always lands on the waitlist
    (GATED_BETA_ACCESS_WORKFLOW): approval is a separate owner decision.
    """
    norm = normalize_email(email)
    if not norm or "@" not in norm:
        raise ValueError("invalid email")
    user_id = str(uuid.uuid4())
    now = _utcnow()
    try:
        conn.execute(
            "INSERT INTO users (user_id, email, password_hash, created_utc)"
            " VALUES (?, ?, ?, ?)",
            (user_id, norm,
             _HASHER.hash(password) if password is not None else None, now),
        )
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise DuplicateEmail(norm) from exc
    conn.execute(
        "INSERT INTO waitlist_requests (request_id, user_id, area_interest,"
        " submitted_utc) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), user_id, area_interest, now),
    )
    conn.execute(
        "INSERT INTO access_grants (grant_id, user_id, tier, granted_utc)"
        " VALUES (?, ?, 'waitlisted', ?)", (str(uuid.uuid4()), user_id, now),
    )
    conn.commit()
    return user_id


def find_user_by_email(conn: sqlite3.Connection, email: str) -> str | None:
    row = conn.execute("SELECT user_id FROM users WHERE email = ?",
                       (normalize_email(email),)).fetchone()
    return row[0] if row else None


# --- tier check (INV-4 latest-row rule) ---------------------------------------

def current_tier(conn: sqlite3.Connection, user_id: str) -> str:
    """Latest ``access_grants`` row ordered (granted_utc, rowid); 'none' if none."""
    row = conn.execute(
        "SELECT tier FROM access_grants WHERE user_id = ?"
        " ORDER BY granted_utc DESC, rowid DESC LIMIT 1", (user_id,)
    ).fetchone()
    return row[0] if row else "none"


def _append_grant(conn: sqlite3.Connection, user_id: str, tier: str, *,
                  owner_decision_ref: str | None, reviewer_id: str | None,
                  note: str | None) -> str:
    if tier in DECISION_TIERS and not owner_decision_ref:
        raise OwnerlessAccessDecision(tier)
    grant_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO access_grants (grant_id, user_id, tier,"
        " owner_decision_ref, reviewer_id, granted_utc, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (grant_id, user_id, tier, owner_decision_ref, reviewer_id,
         _utcnow(), note),
    )
    return grant_id


def approve(conn: sqlite3.Connection, user_id: str, *, owner_decision_ref: str,
            reviewer_id: str | None = None, note: str | None = None) -> str:
    grant_id = _append_grant(conn, user_id, "approved",
                             owner_decision_ref=owner_decision_ref,
                             reviewer_id=reviewer_id, note=note)
    conn.execute(
        "UPDATE waitlist_requests SET status = 'approved' WHERE user_id = ?"
        " AND status = 'pending'", (user_id,))
    conn.commit()
    notif.notify_access_approved(conn, user_id)
    return grant_id


def revoke(conn: sqlite3.Connection, user_id: str, *, owner_decision_ref: str,
           reviewer_id: str | None = None, note: str | None = None) -> str:
    grant_id = _append_grant(conn, user_id, "revoked",
                             owner_decision_ref=owner_decision_ref,
                             reviewer_id=reviewer_id, note=note)
    conn.execute(
        "UPDATE waitlist_requests SET status = 'revoked' WHERE user_id = ?"
        " AND status = 'approved'", (user_id,))
    conn.commit()
    sessions.revoke_all_for_user(conn, user_id)
    notif.notify_access_revoked(conn, user_id)
    return grant_id


def pause(conn: sqlite3.Connection, user_id: str, *, owner_decision_ref: str,
          reviewer_id: str | None = None, note: str | None = None) -> str:
    grant_id = _append_grant(conn, user_id, "paused",
                             owner_decision_ref=owner_decision_ref,
                             reviewer_id=reviewer_id, note=note)
    conn.commit()
    return grant_id


# --- passwords + login (INV-7, D2) --------------------------------------------

def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?",
                 (_HASHER.hash(password), user_id))
    conn.commit()


def login(conn: sqlite3.Connection, *, email: str, password: str,
          ttl_seconds: int = sessions.DEFAULT_TTL_SECONDS) -> tuple[str, str]:
    """Verify credentials and mint a session; returns ``(user_id, raw_token)``.

    Runs ``check_needs_rehash`` on success (D2) so future argon2 parameter
    bumps migrate hashes lazily, with no schema change. All failure modes
    raise the same :class:`LoginFailed` with a constant message.
    """
    row = conn.execute(
        "SELECT user_id, password_hash FROM users WHERE email = ?",
        (normalize_email(email),)).fetchone()
    if row is None or row[1] is None:
        raise LoginFailed("login failed")
    user_id, stored = row[0], row[1]
    try:
        _HASHER.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError) as exc:
        raise LoginFailed("login failed") from exc
    if _HASHER.check_needs_rehash(stored):
        conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?",
                     (_HASHER.hash(password), user_id))
    conn.execute("UPDATE users SET last_login_utc = ? WHERE user_id = ?",
                 (_utcnow(), user_id))
    conn.commit()
    _, raw_token = sessions.issue_session(conn, user_id, ttl_seconds=ttl_seconds)
    return user_id, raw_token
