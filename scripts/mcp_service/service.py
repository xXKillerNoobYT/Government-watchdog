"""The guarded request path (CONTRACT-2026-MCP §3). One choke-point, one audit row.

`call_tool` and `read_resource` funnel through :func:`_guarded`, which enforces
the full boundary in a fixed order and writes **exactly one** audit row per call,
allow or deny:

1. capability check (job-scoped HMAC token, scope allowlist, budget) — D4;
2. request JSON-Schema validation (fail-closed on unknown fields) — §3.2;
3. build the payload via the allowlisted resource/tool handler — D3;
4. response JSON-Schema validation;
5. redaction scan over the serialized payload (frozen scanners) — D2;
6. audit-envelope row with the LED-1 subset — §3.4.

Any :class:`MCPDenied` short-circuits to a deny audit row and re-raises. No MCP
tool writes a canonical table (INV-1/3); the only write is ``submit_output`` into
staging.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

from . import audit, capability, contracts, redaction, resources, schemas, tools
from .errors import DENY_UNSUPPORTED, MCPDenied

SEMVER = contracts.SEMVER


def _area_id(conn: sqlite3.Connection, job_id: str) -> str | None:
    row = conn.execute("SELECT area_id FROM mcp_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row["area_id"] if row else None


def _peek_grant_id(token: str) -> str | None:
    """Best-effort grant id for a *deny* audit row when capability itself fails.

    Never trusts the token — this is only for attributing a denial in the audit
    trail; authorization always runs through :func:`capability.verify`.
    """
    try:
        import base64
        import json

        claims_b64 = token.split(".")[1]
        pad = "=" * (-len(claims_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(claims_b64 + pad))
        return claims.get("grant_id")
    except Exception:  # noqa: BLE001
        return None


def _guarded(
    conn: sqlite3.Connection,
    *,
    kind: str,
    name: str,
    scope: str,
    job_id: str,
    token: str,
    req_args: dict[str, Any],
    req_schema_id: str | None,
    res_schema_id: str,
    build: Callable[[dict[str, Any]], dict[str, Any]],
    inject_grant: bool = False,
) -> dict[str, Any]:
    t0 = time.monotonic()
    grant: dict[str, Any] | None = None
    request_hash = audit.hash_obj(req_args)
    try:
        grant = capability.verify(conn, token, required_scope=scope, job_id=job_id)
        if req_schema_id:
            schemas.validate(req_args, req_schema_id, SEMVER)
        # A job_id carried in the request body must match the session job.
        if req_args.get("job_id") not in (None, job_id):
            raise MCPDenied(DENY_UNSUPPORTED, "request job_id mismatches session job")
        args = dict(req_args)
        if inject_grant:
            args["_grant_id"] = grant["grant_id"]
        payload = build(args)
        schemas.validate(payload, res_schema_id, SEMVER)
        redaction.assert_clean(payload)
        audit.record(
            conn, kind=kind, name=name, outcome="allow",
            grant_id=grant["grant_id"], seq=grant["seq"], job_id=job_id,
            area_id=_area_id(conn, job_id), schema_id=res_schema_id, schema_version=SEMVER,
            request_hash=request_hash, response_hash=audit.hash_obj(payload),
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return payload
    except MCPDenied as denied:
        audit.record(
            conn, kind=kind, name=name, outcome="deny", error_code=denied.code,
            grant_id=grant["grant_id"] if grant else _peek_grant_id(token),
            seq=grant["seq"] if grant else None, job_id=job_id,
            area_id=_area_id(conn, job_id), schema_id=res_schema_id, schema_version=SEMVER,
            request_hash=request_hash,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        raise


def call_tool(
    conn: sqlite3.Connection, tool_name: str, args: dict[str, Any], token: str, *, job_id: str
) -> dict[str, Any]:
    spec = contracts.TOOLS.get(tool_name)
    if spec is None:
        # Unknown tool: still audited as a deny (no handler, no silent 404).
        audit.record(
            conn, kind="tool", name=str(tool_name), outcome="deny",
            error_code=DENY_UNSUPPORTED, job_id=job_id,
            grant_id=_peek_grant_id(token), request_hash=audit.hash_obj(args),
        )
        raise MCPDenied(DENY_UNSUPPORTED, f"unknown tool {tool_name!r}")
    handler = tools.HANDLERS[tool_name]
    return _guarded(
        conn, kind="tool", name=tool_name, scope=spec.scope, job_id=job_id, token=token,
        req_args=args, req_schema_id=spec.req_schema_id, res_schema_id=spec.res_schema_id,
        build=lambda a: handler(conn, job_id, a),
        inject_grant=(tool_name == "submit_output"),
    )


_RESOURCE_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "job.spec": lambda conn, job_id, rid: resources.job_spec(conn, job_id),
    "evidence.statement": lambda conn, job_id, rid: resources.statement(conn, job_id, rid),
    "evidence.segment": lambda conn, job_id, rid: resources.segment(conn, job_id, rid),
    "evidence.provenance": lambda conn, job_id, rid: resources.provenance(conn, job_id, rid),
}

_RESOURCE_SCHEMA: dict[str, str] = {
    "job.spec": "gov.job.spec",
    "evidence.statement": "gov.evidence.statement",
    "evidence.segment": "gov.evidence.segment",
    "evidence.provenance": "gov.evidence.provenance",
}


def read_resource(
    conn: sqlite3.Connection, resource_type: str, resource_id: str, token: str, *, job_id: str
) -> dict[str, Any]:
    """Direct resource read through the same guarded path as tools."""
    scope = contracts.RESOURCE_SCOPES.get(resource_type)
    builder = _RESOURCE_BUILDERS.get(resource_type)
    if scope is None or builder is None:
        audit.record(
            conn, kind="resource", name=str(resource_type), outcome="deny",
            error_code=DENY_UNSUPPORTED, job_id=job_id, grant_id=_peek_grant_id(token),
        )
        raise MCPDenied(DENY_UNSUPPORTED, f"unknown resource type {resource_type!r}")
    return _guarded(
        conn, kind="resource", name=resource_type, scope=scope, job_id=job_id, token=token,
        req_args={"resource_id": resource_id}, req_schema_id=None,
        res_schema_id=_RESOURCE_SCHEMA[resource_type],
        build=lambda a: builder(conn, job_id, resource_id),
    )
