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

#: Digits in the numeric sign-in code — the universal-link fallback (GOV-1538).
CODE_DIGITS = 6


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


def new_numeric_code(digits: int = CODE_DIGITS) -> str:
    """A cryptographically-random zero-padded numeric code (OTP fallback).

    ``secrets.randbelow`` (not ``random``) so the code is unguessable; the
    zero-pad keeps the full space (e.g. ``"004217"`` is valid). Hashed with
    :func:`token_hash` before storage — the raw code, like a raw token, is
    emailed once and never persisted.
    """
    return f"{secrets.randbelow(10 ** digits):0{digits}d}"


def email_hash(email: str | None) -> str | None:
    """Audit-log identity: correlate a subject's events without storing them."""
    norm = normalize_email(email)
    return sha256_hex(norm) if norm else None


def ip_hint(ip: str | None) -> str | None:
    """A truncated, non-reversible fingerprint of a client IP (or None)."""
    if not ip:
        return None
    return sha256_hex(ip)[:IP_HINT_LEN]


def parse_content_length(raw: str | None) -> int | None:
    """Caller-supplied ``Content-Length`` -> a usable byte count, or None.

    Returns ``0`` when the header is absent or empty, a non-negative int when it
    parses, and **None when the value is present but unusable** — non-numeric,
    negative, or otherwise not a plain integer.

    Both failure modes were live before GOV-1667 and both were reachable by an
    unauthenticated caller, because the header is read before any gate:

    * ``Content-Length: abc`` raised ``ValueError`` inside the request handler.
      The connection died with a traceback and **no response at all**.
    * ``Content-Length: -1`` slipped past a ``length > CAP`` guard (-1 is not
      greater than anything) and reached ``rfile.read(-1)``, which reads **until
      EOF** — so the size cap it was supposed to enforce did not apply. Measured:
      a positive over-cap value returned 413 while ``-1`` returned 401, i.e. it
      got past the guard and into the handler.

    None is returned rather than 0-vs-error so the CALLER decides the status,
    which matters: answering 400 here would leak that the surface exists while
    the beta gate is off, and INV-2 says every request must be 404 then.
    """
    if raw is None or raw == "":
        return 0
    try:
        length = int(raw)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None

