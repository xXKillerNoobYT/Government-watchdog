"""Magic tokens: issue + one-time consume (0026 §2, +6-digit code GOV-1538).

15-minute TTL, single use. The raw token is emailed once; only its sha256 is
stored. Consumption is atomic: a conditional UPDATE stamps ``consumed_utc``
only if it is still NULL, so two concurrent verifies of the same token can
never both win — the loser sees ``rowcount == 0`` and is rejected.

GOV-1538 adds a numeric-code fallback for sign-in (universal links need the
Phase-3 domain's AASA file, which does not exist yet). :func:`issue_with_code`
mints the link token AND a 6-digit code on the SAME row (``code_hash``); either
credential redeems that one row, so consuming one invalidates the other. A wrong
code bumps ``code_attempts`` and the code is refused past
:data:`MAX_CODE_ATTEMPTS`, so the 10**6 space cannot be brute-forced inside the
15-minute window.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from beta import common

MAGIC_TTL_SECONDS = 15 * 60

#: Failed numeric-code guesses tolerated per token before the code is dead.
MAX_CODE_ATTEMPTS = 5


def _insert(conn: sqlite3.Connection, email: str, *, ip_hint: str | None,
            ttl_seconds: int, token_hash: str, code_hash: str | None) -> None:
    """Insert one magic-token row (optionally code-bearing); commit."""
    now = common.utcnow()
    conn.execute(
        "INSERT INTO beta_magic_tokens (token_id, email, token_hash,"
        " created_utc, expires_utc, ip_hint, code_hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), common.normalize_email(email), token_hash,
         common.iso(now), common.iso(now + timedelta(seconds=ttl_seconds)),
         ip_hint, code_hash),
    )
    conn.commit()


def issue(conn: sqlite3.Connection, email: str, *,
          ip_hint: str | None = None,
          ttl_seconds: int = MAGIC_TTL_SECONDS) -> str:
    """Mint a link-only magic token for an email; returns the raw token."""
    raw_token = common.new_raw_token()
    _insert(conn, email, ip_hint=ip_hint, ttl_seconds=ttl_seconds,
            token_hash=common.token_hash(raw_token), code_hash=None)
    return raw_token


def issue_with_code(conn: sqlite3.Connection, email: str, *,
                    ip_hint: str | None = None,
                    ttl_seconds: int = MAGIC_TTL_SECONDS) -> tuple[str, str]:
    """Mint a link token AND a 6-digit code on one row; returns ``(token, code)``.

    Both are the caller's only copy of their respective secret (only sha256
    digests are stored). Either redeems the same one-time row — see
    :func:`consume` (link) and :func:`consume_code` (code).
    """
    raw_token = common.new_raw_token()
    raw_code = common.new_numeric_code()
    _insert(conn, email, ip_hint=ip_hint, ttl_seconds=ttl_seconds,
            token_hash=common.token_hash(raw_token),
            code_hash=common.token_hash(raw_code))
    return raw_token, raw_code


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


def consume_code(conn: sqlite3.Connection, email: str, raw_code: str, *,
                 now: datetime | None = None) -> str | None:
    """Validate + one-time-consume the numeric code for ``email``; email or None.

    None (indistinguishably, so the transport can answer one neutral error) for:
    no outstanding code-bearing token for the email (never requested / not
    allowlisted / already consumed / expired), the attempt cap reached, or a
    wrong code. Only the newest live code-bearing row for the email is checked —
    a fresh request supersedes the prior code (standard OTP semantics). A wrong
    guess bumps ``code_attempts`` on that row; a correct code atomically stamps
    ``consumed_utc`` (the same one-time gate the magic link uses).
    """
    norm = common.normalize_email(email)
    if not norm or not raw_code:
        return None
    row = conn.execute(
        "SELECT token_id, code_hash, code_attempts, expires_utc"
        " FROM beta_magic_tokens"
        " WHERE email = ? AND code_hash IS NOT NULL AND consumed_utc IS NULL"
        " ORDER BY created_utc DESC, rowid DESC LIMIT 1", (norm,)).fetchone()
    if row is None:
        return None
    now_iso = common.iso(now or common.utcnow())
    if now_iso >= row["expires_utc"]:
        return None
    if row["code_attempts"] >= MAX_CODE_ATTEMPTS:
        return None  # cap reached — the code is dead even if later guessed right
    if common.token_hash(raw_code) != row["code_hash"]:
        conn.execute(
            "UPDATE beta_magic_tokens SET code_attempts = code_attempts + 1"
            " WHERE token_id = ?", (row["token_id"],))
        conn.commit()
        return None
    cur = conn.execute(
        "UPDATE beta_magic_tokens SET consumed_utc = ? WHERE token_id = ?"
        " AND consumed_utc IS NULL", (now_iso, row["token_id"]))
    conn.commit()
    if cur.rowcount != 1:
        return None  # lost the consume race — treat as already used
    return norm
