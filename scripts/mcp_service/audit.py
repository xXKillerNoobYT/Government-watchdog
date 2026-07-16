"""Per-call audit envelope (CONTRACT-2026-MCP §3.4, LED-1 subset).

Exactly one ``mcp_audit_events`` row is written for every resource read, tool
call, and denial — allow or deny, no exceptions. The row carries the LED-1
cost-envelope subset so GOV-720's ledger can aggregate per-area (AREA-2) without
re-instrumenting: ``area_id NULL`` is the unattributable shared pool. Request and
response are stored as content hashes, never bodies, so the audit trail holds no
raw evidence text.

Audit-ID format is ``mcp-<uuid4>`` with a per-grant monotonic ``seq`` (the seq is
supplied by the capability check, which owns the grant's call counter).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


def hash_obj(obj: Any) -> str:
    """Stable content hash of a JSON-ish object (canonical, sorted keys)."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record(
    conn: sqlite3.Connection,
    *,
    kind: str,
    name: str,
    outcome: str,
    grant_id: str | None = None,
    seq: int | None = None,
    job_id: str | None = None,
    area_id: str | None = None,
    schema_id: str | None = None,
    schema_version: str | None = None,
    request_hash: str | None = None,
    response_hash: str | None = None,
    error_code: str | None = None,
    latency_ms: int | None = None,
    queue_wait_s: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    input_units: int = 0,
    output_units: int = 0,
    direct_cost_units: int = 0,
    cache_hit: bool = False,
    retry_count: int = 0,
    policy_version: str | None = None,
    lens_version: str | None = None,
) -> str:
    """Write one audit row; return the ``mcp-<uuid4>`` audit id."""
    audit_id = f"mcp-{uuid.uuid4()}"
    conn.execute(
        "INSERT INTO mcp_audit_events ("
        " audit_id, grant_id, seq, job_id, area_id, kind, name, schema_id,"
        " schema_version, request_hash, response_hash, outcome, error_code,"
        " latency_ms, queue_wait_s, provider, model, input_units, output_units,"
        " direct_cost_units, cache_hit, retry_count, policy_version, lens_version,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            audit_id, grant_id, seq, job_id, area_id, kind, name, schema_id,
            schema_version, request_hash, response_hash, outcome, error_code,
            latency_ms, queue_wait_s, provider, model, int(input_units),
            int(output_units), int(direct_cost_units), 1 if cache_hit else 0,
            int(retry_count), policy_version, lens_version, _utcnow(),
        ),
    )
    conn.commit()
    return audit_id
