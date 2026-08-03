"""Shared helpers for the gated-beta front door (GOV-801).

Central home for the two privacy primitives the whole package leans on:
``email_hash`` and ``ip_hint``. Both are **stable pseudonyms, not confidentiality
controls** — see their docstrings. Also the single source of ``token_hash`` so
every ``beta_*`` token surface stores exactly one shape.

CORRECTED 2026-07-31 (GOV-1668, C8 security hunt). This module previously
described ``ip_hint`` as "non-reversible" and "too short to reverse to an
address". Both claims were false, and the reasoning behind them was inverted.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

#: Truncated-sha256 length for ip_hint. Enough to bucket repeat callers for
#: rate-limit forensics. It does NOT make the value hard to reverse — see
#: :func:`ip_hint`. Truncation buys ambiguity only when the input space is large
#: enough to collide, and IPv4's is not.
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
    """Audit-log identity: correlate a subject's events without storing the address.

    **A stable pseudonym, not a confidentiality control.** The digest is
    unsalted, so anyone holding a candidate address can confirm a match by
    hashing it — and on this system the candidate list is not hypothetical: it
    is ``beta_allowlist`` and ``beta_waitlist``, in the same database file. A
    reader who can see the audit log can already see the addresses.

    What it does buy, and what INV-6 actually rests on: the plaintext address
    never enters ``beta_audit_log``, so the audit trail cannot be the *source*
    of a leak, and a caller cannot push an address in by mistake. Strengthening
    this to a keyed digest is #204 — it needs a key-management decision.
    """
    norm = normalize_email(email)
    return sha256_hex(norm) if norm else None


def ip_hint(ip: str | None) -> str | None:
    """A stable pseudonym for a client IP — **reversible, and that is measured**.

    Do not read this as anonymisation. Measured 2026-07-31 on this machine:
    a specific address was recovered from its hint by enumerating a single /16
    in **0.04 s**, and the entire IPv4 space (2**32) takes ~0.6 core-hours in
    plain CPython — seconds on a GPU.

    The old docstring claimed the 64-bit truncation made it "too short to
    reverse", which has the reasoning backwards: truncation creates ambiguity
    only when the input space is large enough to collide into it. Across all
    2**32 IPv4 addresses the expected number of 64-bit collisions is ~0.5, so
    the preimage is effectively unique. **The security parameter is the size of
    the input domain, not the length of the digest**, and IPv4's domain is tiny.

    What it is genuinely for: bucketing repeat callers for rate-limit forensics,
    stably, without the raw address being what sits in the column. Keeping that
    property honest is what :func:`ip_hint`'s tests pin. A keyed digest would
    make it a real control; that decision is #204.
    """
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

