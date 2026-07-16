"""Job-scoped HMAC capability tokens + grant store (CONTRACT-2026-MCP §3.3, D4).

A worker never holds ambient authority. It presents a signed token that names a
single job, an exact-match scope allowlist, and a budget envelope. The token is
``base64url(header.claims.mac)`` with an HMAC-SHA256 MAC over
``header.claims``; the signing secret comes from the environment / a local file
and is **never** committed (INV-7). Only the token *hash* is persisted
(``mcp_capability_grants.token_hash``), so the store leaks nothing usable.

``verify`` fail-closes to ``denied:capability`` on every failure mode the plan
enumerates: bad MAC, expired, revoked, wrong job, out-of-scope, exhausted
budget. Scopes are exact strings (``resource:<type>:read`` / ``tool:<name>``) —
no wildcards.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import Any

from .errors import DENY_BUDGET, DENY_CAPABILITY, MCPDenied

_HEADER = {"alg": "HS256", "typ": "gov-mcp-cap"}
_SECRET_ENV = "MCP_HMAC_SECRET"
_SECRET_FILE_ENV = "MCP_HMAC_SECRET_FILE"


def _secret() -> bytes:
    """Load the HMAC secret from env or a local file. Fail-closed if absent.

    Never falls back to a hardcoded/default key: no secret means no valid token
    can be minted or verified (INV-7 keeps the secret out of the repo).
    """
    raw = os.environ.get(_SECRET_ENV)
    if not raw:
        path = os.environ.get(_SECRET_FILE_ENV)
        if path and os.path.isfile(path):
            raw = open(path, encoding="utf-8").read().strip()
    if not raw:
        raise MCPDenied(
            DENY_CAPABILITY,
            f"no signing secret ({_SECRET_ENV} / {_SECRET_FILE_ENV} unset)",
        )
    return raw.encode("utf-8")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(header_b64: str, claims_b64: str) -> str:
    mac = hmac.new(_secret(), f"{header_b64}.{claims_b64}".encode("ascii"), hashlib.sha256)
    return _b64(mac.digest())


def _now() -> int:
    return int(time.time())


def mint_grant(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    scopes: list[str],
    max_calls: int = 0,
    max_input_units: int = 0,
    max_output_units: int = 0,
    ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """Create a grant row and return ``(grant_id, token)``.

    Only the token hash is stored. ``max_* == 0`` means that dimension is not
    capped in this leg (enforcement of unit budgets is GOV-718); ``max_calls > 0``
    is enforced here as a hard per-grant call ceiling.
    """
    grant_id = f"grant-{uuid.uuid4()}"
    exp = _now() + int(ttl_seconds)
    claims = {
        "grant_id": grant_id,
        "job_id": job_id,
        "scopes": list(scopes),
        "budget": {
            "max_calls": int(max_calls),
            "max_input_units": int(max_input_units),
            "max_output_units": int(max_output_units),
        },
        "exp": exp,
        "nonce": secrets.token_hex(8),
    }
    header_b64 = _b64(json.dumps(_HEADER, separators=(",", ":")).encode())
    claims_b64 = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    token = f"{header_b64}.{claims_b64}.{_sign(header_b64, claims_b64)}"
    conn.execute(
        "INSERT INTO mcp_capability_grants "
        "(grant_id, job_id, token_hash, scopes, max_calls, max_input_units, "
        " max_output_units, calls_used, expires_utc, revoked, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?)",
        (
            grant_id,
            job_id,
            _token_hash(token),
            json.dumps(sorted(scopes)),
            int(max_calls),
            int(max_input_units),
            int(max_output_units),
            _iso(exp),
            _iso(_now()),
        ),
    )
    conn.commit()
    return grant_id, token


def revoke(conn: sqlite3.Connection, grant_id: str) -> None:
    conn.execute("UPDATE mcp_capability_grants SET revoked = 1 WHERE grant_id = ?", (grant_id,))
    conn.commit()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(epoch: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _decode(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise MCPDenied(DENY_CAPABILITY, "malformed token")
    header_b64, claims_b64, mac = parts
    expected = _sign(header_b64, claims_b64)
    # Constant-time compare: a bad/forged MAC is a capability denial.
    if not hmac.compare_digest(expected, mac):
        raise MCPDenied(DENY_CAPABILITY, "bad signature")
    try:
        claims = json.loads(_unb64(claims_b64))
    except Exception:  # noqa: BLE001
        raise MCPDenied(DENY_CAPABILITY, "undecodable claims")
    if not isinstance(claims, dict):
        raise MCPDenied(DENY_CAPABILITY, "claims not an object")
    return claims


def verify(
    conn: sqlite3.Connection,
    token: str,
    *,
    required_scope: str,
    job_id: str,
    consume_call: bool = True,
) -> dict[str, Any]:
    """Authorize ``token`` for ``required_scope`` on ``job_id``; return grant ctx.

    Checks, all fail-closed to ``denied:capability`` (budget → ``denied:budget``):
    signature, expiry, grant exists + token hash matches (revocation-safe),
    not revoked, job match, scope membership (exact), and call budget. On success
    increments ``calls_used`` when ``consume_call``.
    """
    claims = _decode(token)
    if int(claims.get("exp", 0)) < _now():
        raise MCPDenied(DENY_CAPABILITY, "token expired")
    grant_id = claims.get("grant_id")
    row = conn.execute(
        "SELECT * FROM mcp_capability_grants WHERE grant_id = ?", (grant_id,)
    ).fetchone()
    if row is None:
        raise MCPDenied(DENY_CAPABILITY, "unknown grant")
    row = dict(row)
    # The presented token must be the exact one this grant was minted for.
    if not hmac.compare_digest(row["token_hash"], _token_hash(token)):
        raise MCPDenied(DENY_CAPABILITY, "token/grant mismatch")
    if row["revoked"]:
        raise MCPDenied(DENY_CAPABILITY, "grant revoked")
    if row["job_id"] != job_id or claims.get("job_id") != job_id:
        raise MCPDenied(DENY_CAPABILITY, "wrong job")
    scopes = set(claims.get("scopes", []))
    if required_scope not in scopes:
        raise MCPDenied(DENY_CAPABILITY, f"scope {required_scope!r} not granted")
    max_calls = int(row["max_calls"])
    used = int(row["calls_used"])
    if max_calls > 0 and used >= max_calls:
        raise MCPDenied(DENY_BUDGET, "call budget exhausted")
    if consume_call:
        conn.execute(
            "UPDATE mcp_capability_grants SET calls_used = calls_used + 1 WHERE grant_id = ?",
            (grant_id,),
        )
        conn.commit()
    return {
        "grant_id": grant_id,
        "job_id": job_id,
        "scopes": scopes,
        "seq": used + 1,
        "budget": claims.get("budget", {}),
    }
