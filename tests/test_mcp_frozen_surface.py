"""§6.8 frozen surfaces + D1 transport binding.

The frozen serving surfaces are imported by the MCP layer, never modified — this
asserts a byte-0 diff against `origin/main` for the three named files. Also
exercises the stdio JSON-RPC 2.0 binding end-to-end (D1) to prove the transport
adapter carries the same guarantees as a direct service call, and confirms the
migration is additive (no ALTER on landed tables).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from mcp_service import jsonrpc

ROOT = Path(__file__).resolve().parent.parent
FROZEN = ["scripts/read_api.py", "scripts/ai_risk_gate.py", "scripts/stage5_agenda_board.py"]


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


@pytest.mark.skipif(
    _git("rev-parse", "--verify", "origin/main").returncode != 0,
    reason="origin/main not available in this checkout",
)
def test_frozen_surfaces_byte0_diff_vs_origin_main():
    diff = _git("diff", "origin/main", "--", *FROZEN)
    assert diff.returncode == 0
    assert diff.stdout.strip() == "", f"frozen surface modified:\n{diff.stdout}"


def test_migration_is_additive_no_alter():
    sql = (ROOT / "Database/migrations/0021_mcp_service.sql").read_text(encoding="utf-8")
    upper = "\n".join(
        l for l in sql.splitlines() if not l.lstrip().startswith("--")).upper()
    assert "ALTER TABLE" not in upper, "migration must not ALTER existing tables"
    # Exactly the six planned mcp_* tables are created.
    creates = [l for l in upper.splitlines() if "CREATE TABLE" in l]
    assert len(creates) == 6


def test_jsonrpc_tools_list_needs_no_auth(mcp_conn):
    resp = jsonrpc.handle_request(mcp_conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"list_job_inputs", "get_statement", "get_segment",
                     "get_provenance", "get_policy_pack", "submit_output"}


def test_jsonrpc_tools_call_round_trip(mcp_conn, mint):
    _, token = mint()
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
           "params": {"name": "get_statement", "job_id": "job1", "token": token,
                      "arguments": {"job_id": "job1", "statement_id": "stmt1"}}}
    resp = jsonrpc.handle_request(mcp_conn, req)
    assert resp["result"]["statement_id"] == "stmt1"


def test_jsonrpc_denial_maps_to_error(mcp_conn, mint):
    _, token = mint(scopes=["tool:get_segment"])
    req = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
           "params": {"name": "get_statement", "job_id": "job1", "token": token,
                      "arguments": {"job_id": "job1", "statement_id": "stmt1"}}}
    resp = jsonrpc.handle_request(mcp_conn, req)
    assert "error" in resp and resp["error"]["data"]["deny_code"] == "denied:capability"


def test_jsonrpc_serve_stdio_line_protocol(mcp_conn, mint):
    _, token = mint()
    line = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                       "params": {"name": "list_job_inputs", "job_id": "job1",
                                  "token": token, "arguments": {"job_id": "job1"}}})
    out = io.StringIO()
    jsonrpc.serve_stdio(mcp_conn, io.StringIO(line + "\n"), out)
    resp = json.loads(out.getvalue().strip())
    assert resp["id"] == 9 and "stmt1" in json.dumps(resp["result"])
