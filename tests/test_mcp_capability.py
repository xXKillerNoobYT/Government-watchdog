"""§6.4 capability: expired / revoked / wrong-job / out-of-scope / budget tokens
are all denied, and a job-scoped read of a non-job statement is denied (context
minimization).
"""

from __future__ import annotations

import pytest

from mcp_service import capability, service
from mcp_service.errors import DENY_BUDGET, DENY_CAPABILITY, DENY_NOT_FOUND, MCPDenied


def test_valid_token_reads_authorized_statement(mcp_conn, mint):
    _, token = mint()
    st = service.call_tool(mcp_conn, "get_statement",
                           {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert st["statement_id"] == "stmt1"


def test_expired_token_denied(mcp_conn, mint):
    _, token = mint(ttl_seconds=-1)  # already expired
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_CAPABILITY


def test_revoked_token_denied(mcp_conn, mint):
    grant_id, token = mint()
    capability.revoke(mcp_conn, grant_id)
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_CAPABILITY


def test_wrong_job_denied(mcp_conn, mint):
    # Token minted for job2 but presented as a job1 session.
    _, token = mint(job_id="job2")
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_CAPABILITY


def test_out_of_scope_denied(mcp_conn, mint):
    # Grant lacks the get_statement tool scope.
    _, token = mint(scopes=["tool:get_segment"])
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_CAPABILITY


def test_tampered_signature_denied(mcp_conn, mint):
    _, token = mint()
    header, claims, mac = token.split(".")
    forged = f"{header}.{claims}.{'A' * len(mac)}"
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, forged, job_id="job1")
    assert exc.value.code == DENY_CAPABILITY


def test_context_minimization_non_job_statement_denied(mcp_conn, mint):
    # stmt_other exists in the DB but is NOT in job1's input selector.
    _, token = mint()
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt_other"}, token, job_id="job1")
    assert exc.value.code == DENY_NOT_FOUND


def test_call_budget_exhausted_denied(mcp_conn, mint):
    _, token = mint(max_calls=1)
    service.call_tool(mcp_conn, "get_statement",
                      {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_BUDGET


def test_secret_absent_fails_closed(mcp_conn, mint, monkeypatch):
    _, token = mint()
    monkeypatch.delenv("MCP_HMAC_SECRET", raising=False)
    monkeypatch.delenv("MCP_HMAC_SECRET_FILE", raising=False)
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_CAPABILITY


def test_grant_store_holds_hash_not_token(mcp_conn, mint):
    _, token = mint()
    stored = mcp_conn.execute("SELECT token_hash FROM mcp_capability_grants").fetchone()["token_hash"]
    assert stored != token and len(stored) == 64  # sha256 hex, not the token
