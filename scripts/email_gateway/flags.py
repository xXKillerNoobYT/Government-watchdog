"""Append-only feature flags (0025 §10, D1/INV-5).

Current state = LATEST row per ``flag_name`` ordered ``(at_utc, flag_seq)``
(flag_seq tie-break, same rule as ``access_grants``). FAIL-CLOSED: no row
means off. Every append — enable AND disable — carries a non-null
``owner_decision_ref``; there is deliberately no UPDATE path and no env-var
override anywhere in this package.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

EMAIL_ADAPTER_FLAG = "email_adapter_enabled"


class OwnerlessFlagChange(ValueError):
    """Flag append attempted without an ``owner_decision_ref`` (D1)."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def is_enabled(conn: sqlite3.Connection, flag_name: str) -> bool:
    """Latest-row read, fail-closed: no row -> False."""
    row = conn.execute(
        "SELECT enabled FROM feature_flags WHERE flag_name = ?"
        " ORDER BY at_utc DESC, flag_seq DESC LIMIT 1", (flag_name,)
    ).fetchone()
    return bool(row[0]) if row else False


def set_flag(conn: sqlite3.Connection, flag_name: str, *, enabled: bool,
             owner_decision_ref: str, actor: str | None = None) -> int:
    """Append one flag row; returns ``flag_seq``. Owner-gated both directions."""
    if not owner_decision_ref:
        raise OwnerlessFlagChange(flag_name)
    cur = conn.execute(
        "INSERT INTO feature_flags (flag_name, enabled, owner_decision_ref,"
        " actor, at_utc) VALUES (?, ?, ?, ?, ?)",
        (flag_name, 1 if enabled else 0, owner_decision_ref, actor, _utcnow()),
    )
    conn.commit()
    return cur.lastrowid
