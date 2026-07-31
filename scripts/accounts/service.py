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


class InvalidPassword(ValueError):
    """An empty password is never a credential (GOV-1674).

    ``password_hash IS NULL`` means *passwordless*, and ``login`` refuses such a
    row with a constant :class:`LoginFailed`. ``""`` used to take a different
    path: it is falsy in Python but ``PasswordHasher().hash("")`` is a perfectly
    valid argon2 PHC string, so the empty string became a **working credential**
    and ``login(email, "")`` succeeded. The distinction the code drew was
    ``password is not None``, which is exactly one character of intent away from
    what it needed to draw.

    Deliberately narrow: this rejects the empty string only. **Minimum length,
    complexity, and reuse rules are product policy and belong to the owner**,
    not to a fail-closed guard — inventing them here would be deciding something
    that was never asked.
    """


class UnknownUser(ValueError):
    """A user-scoped write matched no row (GOV-1674).

    ``UPDATE ... WHERE user_id = ?`` against a nonexistent id affects zero rows
    and returns normally, so a caller that believed it had set a credential got
    no signal at all. On a security-relevant write, silence is the wrong answer.
    """


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

    ``password=None`` is the passwordless posture and stores ``NULL``.
    ``password=""`` is refused (:class:`InvalidPassword`) rather than quietly
    coerced to ``NULL`` — a caller that passed the wrong thing should learn
    that, not get a silently different account than it asked for.
    """
    norm = normalize_email(email)
    if not norm or "@" not in norm:
        raise ValueError("invalid email")
    if password is not None and not password:
        raise InvalidPassword("password must be non-empty; pass None for passwordless")
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
    """Set a user's password. The ONLY exit from the passwordless posture.

    Fail-closed on both arguments (GOV-1674):

    * empty ``password`` → :class:`InvalidPassword`. This function is what turns
      a ``NULL``-hash row that ``login`` always refuses into a row ``login``
      accepts, so an empty value here mints a trivially reachable account.
      ``None`` is refused for the same reason — un-setting a password is not
      this function's job.
    * ``user_id`` matching no row → :class:`UnknownUser`, instead of the silent
      zero-row ``UPDATE`` that previously returned as if it had succeeded.
    """
    if not password:
        raise InvalidPassword("password must be non-empty")
    cur = conn.execute("UPDATE users SET password_hash = ? WHERE user_id = ?",
                       (_HASHER.hash(password), user_id))
    if cur.rowcount != 1:
        conn.rollback()
        raise UnknownUser(user_id)
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
