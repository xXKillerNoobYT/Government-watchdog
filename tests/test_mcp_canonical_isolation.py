"""§6.5 canonical isolation (AM-3 pattern): a write tool changes ZERO canonical
rows. `submit_output` lands only in the `mcp_job_outputs` staging table (D5), so
the write-once promotion/anchoring lanes and every canonical table are untouched
(INV-1/INV-3).
"""

from __future__ import annotations

import hashlib

import pytest

from mcp_service import service
from mcp_service.errors import DENY_SCHEMA, MCPDenied

CANONICAL_TABLES = ["statements", "evidence_links", "sources", "transcript_segments",
                    "transcripts", "agenda_items"]


def _snapshot(conn) -> str:
    h = hashlib.sha256()
    for table in CANONICAL_TABLES:
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
            h.update(repr(tuple(row)).encode("utf-8"))
        h.update(f"|{table}|".encode())
    return h.hexdigest()


def test_submit_output_writes_staging_only(mcp_conn, mint):
    from conftest import good_output_args

    before = _snapshot(mcp_conn)
    staged_before = mcp_conn.execute("SELECT COUNT(*) FROM mcp_job_outputs").fetchone()[0]

    _, token = mint()
    res = service.call_tool(mcp_conn, "submit_output", good_output_args(), token, job_id="job1")

    after = _snapshot(mcp_conn)
    staged_after = mcp_conn.execute("SELECT COUNT(*) FROM mcp_job_outputs").fetchone()[0]

    assert before == after, "canonical tables changed after submit_output"
    assert staged_after == staged_before + 1
    row = mcp_conn.execute(
        "SELECT review_state, output_id FROM mcp_job_outputs WHERE output_id = ?",
        (res["output_id"],),
    ).fetchone()
    assert row["review_state"] == "unreviewed"  # staged, not promoted


def test_output_body_validated_against_pack_schema(mcp_conn, mint):
    from conftest import good_output_args

    _, token = mint()
    args = good_output_args()
    args["body"] = {"wrong_field": "x"}  # violates pack's required_output_schema
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "submit_output", args, token, job_id="job1")
    assert exc.value.code == DENY_SCHEMA
    # The rejected write left staging empty.
    assert mcp_conn.execute("SELECT COUNT(*) FROM mcp_job_outputs").fetchone()[0] == 0


def test_no_canonical_write_across_all_reads(mcp_conn, mint):
    _, token = mint()
    before = _snapshot(mcp_conn)
    service.call_tool(mcp_conn, "list_job_inputs", {"job_id": "job1"}, token, job_id="job1")
    service.call_tool(mcp_conn, "get_statement",
                      {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    service.call_tool(mcp_conn, "get_segment",
                      {"job_id": "job1", "segment_id": "seg1"}, token, job_id="job1")
    service.call_tool(mcp_conn, "get_provenance",
                      {"job_id": "job1", "source_id": "src1"}, token, job_id="job1")
    assert _snapshot(mcp_conn) == before
