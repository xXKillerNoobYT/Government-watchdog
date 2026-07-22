"""Magic tokens: issue + one-time consume (0026 §2).

15-minute TTL, single use. The raw token is emailed once; only its sha256 is
stored. Consumption is atomic: a conditional UPDATE stamps ``consumed_utc``
only if it is still NULL, so two concurrent verifies of the same token can
never both win — the loser sees ``rowcount == 0`` and is rejected.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from beta import common

MAGIC_TTL_SECONDS = 15 * 60


def issue(conn: sqlite3.Connection, email: str, *,
          ip_hint: str | None = None,
          ttl_seconds: int = MAGIC_TTL_SECONDS) -> str:
    """Mint a magic token for an email; returns the raw token (caller's copy)."""
    raw_token = common.new_raw_token()
    now = common.utcnow()
    conn.execute(
        "INSERT INTO beta_magic_tokens (token_id, email, token_hash,"
        " created_utc, expires_utc, ip_hint) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), common.normalize_email(email),
         common.token_hash(raw_token), common.iso(now),
         common.iso(now + timedelta(seconds=ttl_seconds)), ip_hint),
    )
    conn.commit()
    return raw_token


def consume(conn: sqlite3.Connection, raw_token: str, *,
            now: datetime | None = None) -> str | None:
    """Validate + one-time-consume a magic token; returns its email or None.

    None for: unknown, already-consumed, or expired. On success the row is
    marked consumed before returning, so the same link never works twice.
    """
    if not raw_token:
        return None
    row = conn.execute(
        "SELECT token_id, email, expires_utc, consumed_utc"
        " FROM beta_magic_tokens WHERE token_hash = ?",
        (common.token_hash(raw_token),)).fetchone()
    if row is None:
        return None
    if row["consumed_utc"] is not None:
        return None
    now_iso = common.iso(now or common.utcnow())
    if now_iso >= row["expires_utc"]:
        return None
    cur = conn.execute(
        "UPDATE beta_magic_tokens SET consumed_utc = ? WHERE token_id = ?"
        " AND consumed_utc IS NULL", (now_iso, row["token_id"]))
    conn.commit()
    if cur.rowcount != 1:
        return None  # lost the consume race — treat as already used
    return row["email"]
