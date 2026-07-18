"""Per-email sliding-window rate limits (GOV-801).

Stateless: the limit is derived by counting rows already written to the
operational table within the trailing window, so there is no separate counter
to keep consistent. Magic-link requests are counted from ``beta_magic_tokens``;
waitlist joins from ``beta_waitlist``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from beta import common

DEFAULT_WINDOW_SECONDS = 3600

# Which timestamp column bounds the window per table. Fixed map — the table
# name is never caller-supplied, so interpolating it below is safe.
_WINDOW_COLUMN = {
    "beta_magic_tokens": "created_utc",
    "beta_waitlist": "submitted_utc",
}


def count_in_window(conn: sqlite3.Connection, table: str, email: str, *,
                    now: datetime | None = None,
                    window_seconds: int = DEFAULT_WINDOW_SECONDS) -> int:
    """Rows for ``email`` in ``table`` within the trailing ``window_seconds``."""
    column = _WINDOW_COLUMN[table]
    cutoff = common.iso((now or common.utcnow())
                        - timedelta(seconds=window_seconds))
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE email = ? AND {column} >= ?",
        (common.normalize_email(email), cutoff)).fetchone()
    return int(row[0])


def over_limit(conn: sqlite3.Connection, table: str, email: str, *, limit: int,
               now: datetime | None = None,
               window_seconds: int = DEFAULT_WINDOW_SECONDS) -> bool:
    """True if a new row would exceed ``limit`` in the trailing window."""
    return count_in_window(conn, table, email, now=now,
                           window_seconds=window_seconds) >= limit
