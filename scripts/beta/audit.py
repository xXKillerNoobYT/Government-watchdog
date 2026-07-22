"""Append-only beta audit log (GOV-801, 0026 §5).

One writer, INSERT only — there is deliberately no update or delete path. The
row carries ``email_hash`` (never the raw address) and ``ip_hint`` (never a raw
IP), so no caller can push a plaintext identifier into the audit trail even by
mistake: ``record`` accepts a raw email and hashes it here, and accepts an
already-truncated ``ip_hint``.
"""

from __future__ import annotations

import sqlite3
import uuid

from beta import common

EVENTS = frozenset({
    "magic_link_requested", "magic_link_sent", "magic_link_verified",
    "magic_link_rejected", "session_issued", "session_revoked",
    "waitlist_joined", "allowlist_added", "allowlist_revoked", "rate_limited",
})


class UnknownAuditEvent(ValueError):
    """Raised when ``event`` is not in the 0026 ``beta_audit_log`` enum."""


def record(conn: sqlite3.Connection, *, event: str, email: str | None = None,
           ip_hint: str | None = None, detail: str | None = None) -> str:
    """Append one audit row; returns the new ``audit_id``.

    ``email`` is hashed in-place (:func:`common.email_hash`) — the plaintext
    never reaches a column. ``ip_hint`` must already be truncated
    (:func:`common.ip_hint`); this function does not see a raw IP.
    """
    if event not in EVENTS:
        raise UnknownAuditEvent(event)
    audit_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO beta_audit_log (audit_id, event, email_hash, ip_hint,"
        " detail, at_utc) VALUES (?, ?, ?, ?, ?, ?)",
        (audit_id, event, common.email_hash(email), ip_hint, detail,
         common.iso(common.utcnow())),
    )
    conn.commit()
    return audit_id
