"""Beta waitlist intake (0026 §4).

Public — no allowlist required. Minimal by design: an email, an optional
area_interest, a timestamp, and a truncated ip_hint for abuse forensics.
"""

from __future__ import annotations

import sqlite3
import uuid

from beta import common


def add(conn: sqlite3.Connection, email: str, *,
        area_interest: str | None = None,
        ip_hint: str | None = None) -> str:
    """Record one waitlist request; returns the new ``request_id``."""
    request_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO beta_waitlist (request_id, email, area_interest,"
        " submitted_utc, ip_hint) VALUES (?, ?, ?, ?, ?)",
        (request_id, common.normalize_email(email), area_interest,
         common.iso(common.utcnow()), ip_hint),
    )
    conn.commit()
    return request_id
