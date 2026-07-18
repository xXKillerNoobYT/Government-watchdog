"""Shared helpers for the gated-beta front door (GOV-801).

Central home for the two privacy primitives the whole package leans on:
``email_hash`` (audit identity that is never the address) and ``ip_hint`` (a
truncated, non-reversible sha256 — never a raw IP). Also the single source of
``token_hash`` so every ``beta_*`` token surface stores exactly one shape.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

#: Truncated-sha256 length for ip_hint. Coarse on purpose: enough to bucket
#: repeat callers for rate-limit forensics, too short to reverse to an address.
IP_HINT_LEN = 16


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """ISO-8601 with millisecond precision — the repo's TEXT-timestamp shape."""
    return dt.isoformat(timespec="milliseconds")


def normalize_email(email: str | None) -> str:
    """Lowercase + trim. The ONLY form that ever touches the DB or a hash."""
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    """Deliberately minimal: a normalized address with an ``@`` and no spaces."""
    return bool(email) and "@" in email and " " not in email


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def token_hash(raw_token: str) -> str:
    """sha256 of a raw token. Raw tokens are never stored — only this digest."""
    return sha256_hex(raw_token)


def new_raw_token() -> str:
    """A fresh URL-safe token; the caller's only copy (never stored/logged)."""
    return secrets.token_urlsafe(32)


def email_hash(email: str | None) -> str | None:
    """Audit-log identity: correlate a subject's events without storing them."""
    norm = normalize_email(email)
    return sha256_hex(norm) if norm else None


def ip_hint(ip: str | None) -> str | None:
    """A truncated, non-reversible fingerprint of a client IP (or None)."""
    if not ip:
        return None
    return sha256_hex(ip)[:IP_HINT_LEN]
