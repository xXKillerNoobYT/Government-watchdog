"""§6.3 schema fail-closed: a malformed / unknown-field tool call is rejected with
`denied:schema` and audited, and every resource/tool schema is registered with a
`{schema_id, semver}`.
"""

from __future__ import annotations

import pytest

from mcp_service import contracts, schemas, service
from mcp_service.errors import DENY_SCHEMA, MCPDenied


def test_all_contract_schemas_registered_with_semver():
    ids = schemas.registered_ids()
    for sid in ("gov.evidence.statement", "gov.evidence.segment",
                "gov.evidence.provenance", "gov.job.spec", "gov.policy.pack"):
        assert "1.0.0" in ids.get(sid, []), f"{sid} not registered @1.0.0"
    for spec in contracts.TOOLS.values():
        assert "1.0.0" in ids.get(spec.req_schema_id, [])
        assert "1.0.0" in ids.get(spec.res_schema_id, [])


def test_unknown_field_rejected(mcp_conn, mint):
    _, token = mint()
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1", "extra": "x"},
                          token, job_id="job1")
    assert exc.value.code == DENY_SCHEMA


def test_missing_required_field_rejected(mcp_conn, mint):
    _, token = mint()
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement", {"job_id": "job1"},
                          token, job_id="job1")
    assert exc.value.code == DENY_SCHEMA


def test_wrong_type_rejected(mcp_conn, mint):
    _, token = mint()
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": 123}, token, job_id="job1")
    assert exc.value.code == DENY_SCHEMA


def test_enum_violation_rejected(mcp_conn, mint):
    _, token = mint()
    from conftest import good_output_args  # type: ignore

    args = good_output_args()
    args["claims"][0]["confidence"] = "not-an-enum-value"
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "submit_output", args, token, job_id="job1")
    assert exc.value.code == DENY_SCHEMA


def test_schema_denial_is_audited(mcp_conn, mint):
    _, token = mint()
    with pytest.raises(MCPDenied):
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1", "extra": 1},
                          token, job_id="job1")
    row = mcp_conn.execute(
        "SELECT outcome, error_code FROM mcp_audit_events ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "deny" and row["error_code"] == DENY_SCHEMA
