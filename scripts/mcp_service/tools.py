"""Tool handlers (CONTRACT-2026-MCP §3.2). Pure builders; no auth/audit here.

Each handler receives the validated request args and the session ``job_id`` and
returns the response payload (already allowlist-projected by the resource
builder). Authorization, schema validation, redaction and audit are applied by
:mod:`.service` around these — a handler that is called has already passed the
capability check. ``submit_output`` is the only write; it lands in the
``mcp_job_outputs`` staging table and touches no canonical table (D5).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from . import resources, schemas
from .errors import DENY_NOT_FOUND, DENY_SCHEMA, MCPDenied

SEMVER = "1.0.0"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def list_job_inputs(conn: sqlite3.Connection, job_id: str, args: dict[str, Any]) -> dict[str, Any]:
    spec = resources.job_spec(conn, job_id)
    return {"job_id": job_id, "resource_uris": spec.get("input_uris", [])}


def get_statement(conn: sqlite3.Connection, job_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return resources.statement(conn, job_id, args["statement_id"])


def get_segment(conn: sqlite3.Connection, job_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return resources.segment(conn, job_id, args["segment_id"])


def get_provenance(conn: sqlite3.Connection, job_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return resources.provenance(conn, job_id, args["source_id"])


def get_policy_pack(conn: sqlite3.Connection, job_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return resources.policy_pack(conn, args["pack_id"], args["version"])


def submit_output(conn: sqlite3.Connection, job_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Write worker output into the ``mcp_job_outputs`` staging table ONLY (D5).

    The output body is validated against the referenced pack's
    ``required_output_schema_id`` when that schema is registered — the pack, not
    the caller, dictates the shape. Canonical tables are never touched.
    """
    pack_id, version = args["policy_pack_id"], args["policy_pack_version"]
    pack_row = conn.execute(
        "SELECT required_output_schema_id FROM mcp_policy_packs WHERE pack_id = ? AND version = ?",
        (pack_id, version),
    ).fetchone()
    if pack_row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"policy pack {pack_id!r}@{version!r} not found")
    output_schema_id = pack_row["required_output_schema_id"]
    if output_schema_id in schemas.registered_ids():
        schemas.validate(args["body"], output_schema_id, SEMVER)
    elif output_schema_id:
        # The pack pins a schema the service does not know: fail closed rather
        # than accept an unvalidated body.
        raise MCPDenied(DENY_SCHEMA, f"unknown required_output_schema_id {output_schema_id!r}")

    output_id = f"out-{uuid.uuid4()}"
    conn.execute(
        "INSERT INTO mcp_job_outputs "
        "(output_id, job_id, grant_id, output_kind, body, claims, policy_pack_id, "
        " policy_pack_version, output_schema_id, review_state, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unreviewed', ?)",
        (
            output_id, job_id, args.get("_grant_id"), args["output_kind"],
            json.dumps(args["body"], separators=(",", ":")),
            json.dumps(args.get("claims", []), separators=(",", ":")),
            pack_id, version, output_schema_id, _utcnow(),
        ),
    )
    conn.commit()
    return {"output_id": output_id, "job_id": job_id, "review_state": "unreviewed"}


HANDLERS = {
    "list_job_inputs": list_job_inputs,
    "get_statement": get_statement,
    "get_segment": get_segment,
    "get_provenance": get_provenance,
    "get_policy_pack": get_policy_pack,
    "submit_output": submit_output,
}
