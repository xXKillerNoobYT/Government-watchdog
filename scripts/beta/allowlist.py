"""Beta allowlist: owner-gated allow / check / revoke (0026 §1).

Only owner-approved emails may ever receive a magic link. Adding an email is an
owner decision — ``owner_decision_ref`` is required here AND non-null in-schema
(defense in depth, same posture as ``access_grants``/``feature_flags``).

Revoking an email is the single revocation lever the AC asks for: it flips the
allowlist row to 'revoked' AND revokes every live session for that email, so an
owner can lock someone out in one call with no token-invalidation machinery.
"""

from __future__ import annotations

import sqlite3

from beta import audit, common, sessions


class OwnerlessAllowlistChange(ValueError):
    """Allowlist add/revoke attempted without an ``owner_decision_ref``."""


def add(conn: sqlite3.Connection, email: str, *, owner_decision_ref: str,
        note: str | None = None, ip_hint: str | None = None) -> str:
    """Add (or re-activate) an allowlisted email; returns the normalized email.

    Idempotent by email: a repeat add re-activates a previously revoked row and
    clears ``revoked_utc`` (an owner re-inviting someone).
    """
    if not owner_decision_ref:
        raise OwnerlessAllowlistChange(email)
    norm = common.normalize_email(email)
    if not common.valid_email(norm):
        raise ValueError("invalid email")
    now = common.iso(common.utcnow())
    conn.execute(
        "INSERT INTO beta_allowlist (email, status, owner_decision_ref,"
        " added_utc, revoked_utc, note) VALUES (?, 'active', ?, ?, NULL, ?)"
        " ON CONFLICT(email) DO UPDATE SET status = 'active',"
        " owner_decision_ref = excluded.owner_decision_ref,"
        " added_utc = excluded.added_utc, revoked_utc = NULL,"
        " note = excluded.note",
        (norm, owner_decision_ref, now, note),
    )
    conn.commit()
    audit.record(conn, event="allowlist_added", email=norm, ip_hint=ip_hint,
                 detail=owner_decision_ref)
    return norm


def is_allowed(conn: sqlite3.Connection, email: str) -> bool:
    """True only if an 'active' allowlist row exists for the email."""
    row = conn.execute(
        "SELECT status FROM beta_allowlist WHERE email = ?",
        (common.normalize_email(email),)).fetchone()
    return row is not None and row["status"] == "active"


def decision_ref(conn: sqlite3.Connection, email: str) -> str | None:
    """The owner_decision_ref of an ACTIVE allowlist row, else None.

    Deliberately returns None for a revoked row as well as a missing one: the
    caller (:mod:`beta.provision`) uses this as the authority for an accounts
    approval, so "revoked" and "never added" must be indistinguishable to it.
    """
    row = conn.execute(
        "SELECT owner_decision_ref FROM beta_allowlist"
        " WHERE email = ? AND status = 'active'",
        (common.normalize_email(email),)).fetchone()
    return row["owner_decision_ref"] if row is not None else None


def revoke(conn: sqlite3.Connection, email: str, *, owner_decision_ref: str,
           ip_hint: str | None = None) -> bool:
    """Revoke an email and cascade-revoke its sessions; True if it was active."""
    if not owner_decision_ref:
        raise OwnerlessAllowlistChange(email)
    norm = common.normalize_email(email)
    cur = conn.execute(
        "UPDATE beta_allowlist SET status = 'revoked', revoked_utc = ?,"
        " owner_decision_ref = ? WHERE email = ? AND status = 'active'",
        (common.iso(common.utcnow()), owner_decision_ref, norm))
    conn.commit()
    if cur.rowcount != 1:
        return False
    sessions.revoke_all_for_email(conn, norm)
    audit.record(conn, event="allowlist_revoked", email=norm, ip_hint=ip_hint,
                 detail=owner_decision_ref)
    return True
