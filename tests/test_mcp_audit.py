"""§6.6 audit completeness: every call — allow or deny — produces exactly one
audit row with the LED-1 subset populated, `mcp-<uuid4>` id, and a monotonic
per-grant seq. Request/response are stored as hashes, never raw bodies.
"""

from __future__ import annotations

import pytest

from mcp_service import service
from mcp_service.errors import MCPDenied

LED1_COLUMNS = {
    "audit_id", "grant_id", "seq", "job_id", "area_id", "kind", "name", "schema_id",
    "schema_version", "request_hash", "response_hash", "outcome", "error_code",
    "latency_ms", "input_units", "output_units", "direct_cost_units", "cache_hit",
    "retry_count", "created_at",
}


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM mcp_audit_events").fetchone()[0]


def test_allowed_call_writes_exactly_one_row(mcp_conn, mint):
    _, token = mint()
    before = _count(mcp_conn)
    service.call_tool(mcp_conn, "get_statement",
                      {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert _count(mcp_conn) == before + 1
    row = dict(mcp_conn.execute(
        "SELECT * FROM mcp_audit_events ORDER BY created_at DESC LIMIT 1").fetchone())
    assert row["outcome"] == "allow"
    assert row["audit_id"].startswith("mcp-")
    assert LED1_COLUMNS.issubset(set(row))
    assert row["request_hash"].startswith("sha256:")
    assert row["response_hash"].startswith("sha256:")
    assert row["area_id"] == "alpine"


def test_denied_call_also_writes_one_row(mcp_conn, mint):
    _, token = mint(scopes=["tool:get_segment"])  # missing get_statement scope
    before = _count(mcp_conn)
    with pytest.raises(MCPDenied):
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert _count(mcp_conn) == before + 1
    row = mcp_conn.execute(
        "SELECT outcome, error_code, response_hash FROM mcp_audit_events "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row["outcome"] == "deny"
    assert row["error_code"].startswith("denied:")
    assert row["response_hash"] is None  # nothing served → no response hash


def test_audit_stores_no_raw_body(mcp_conn, mint):
    _, token = mint()
    service.call_tool(mcp_conn, "get_statement",
                      {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    # The statement text must never appear verbatim in the audit trail.
    blob = " ".join(
        str(v) for row in mcp_conn.execute("SELECT * FROM mcp_audit_events")
        for v in tuple(row)
    )
    assert "quarterly budget line" not in blob


def test_seq_is_monotonic_per_grant(mcp_conn, mint):
    _, token = mint()
    for _ in range(3):
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    seqs = [r["seq"] for r in mcp_conn.execute(
        "SELECT seq FROM mcp_audit_events WHERE outcome='allow' ORDER BY seq")]
    assert seqs == [1, 2, 3]
