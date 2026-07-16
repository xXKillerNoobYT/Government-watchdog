"""§6.1 boundary red-team (AM-9): raw paths / PII / reviewer notes cannot cross.

Two layers proven: the deny-by-default allowlist strips raw *columns*
structurally (clean path), and the frozen scanners fail closed on a raw marker or
PII injected into an allowlisted *value* (adversarial path), emitting a
`denied:redaction` audit row.
"""

from __future__ import annotations

import json

import pytest

from mcp_service import service
from mcp_service.errors import DENY_REDACTION, MCPDenied


def _dump(obj) -> str:
    return json.dumps(obj)


def test_clean_statement_carries_no_raw_column(mcp_conn, mint):
    _, token = mint()
    st = service.call_tool(mcp_conn, "get_statement",
                           {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    blob = _dump(st)
    for marker in ("/Users/", "Obsidian Vault", "TownOfAlpine", "transcript_path",
                   "raw_local_path", "local_note_path", "local_path"):
        assert marker not in blob
    # Only allowlisted keys exist.
    assert set(st).issubset({
        "statement_id", "text", "segment_id", "agenda_item_id", "timestamp_seconds",
        "timestamp_human", "verification_status", "publication_state", "evidence_links"})


def test_provenance_strips_vault_paths(mcp_conn, mint):
    _, token = mint()
    prov = service.call_tool(mcp_conn, "get_provenance",
                             {"job_id": "job1", "source_id": "src1"}, token, job_id="job1")
    blob = _dump(prov)
    assert "/Users/" not in blob and "note.md" not in blob
    assert prov["archive_url"].startswith("https://")  # public URL still crosses


def test_raw_path_in_allowlisted_value_denied(mcp_conn, mint):
    # Adversarial: a raw path leaks into the statement TEXT (an allowlisted field).
    mcp_conn.execute(
        "UPDATE statements SET statement_text = ? WHERE statement_id = 'stmt1'",
        ("see /Users/IA/Obsidian Vault/TownOfAlpine/secret.pdf",),
    )
    mcp_conn.commit()
    _, token = mint()
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_REDACTION


def test_pii_in_allowlisted_value_denied(mcp_conn, mint):
    mcp_conn.execute(
        "UPDATE statements SET statement_text = ? WHERE statement_id = 'stmt1'",
        ("resident SSN 123-45-6789 was read aloud",),
    )
    mcp_conn.commit()
    _, token = mint()
    with pytest.raises(MCPDenied) as exc:
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    assert exc.value.code == DENY_REDACTION


def test_redaction_denial_is_audited(mcp_conn, mint):
    mcp_conn.execute(
        "UPDATE statements SET statement_text = ? WHERE statement_id = 'stmt1'",
        ("call 555-123-4567 for details",),
    )
    mcp_conn.commit()
    _, token = mint()
    with pytest.raises(MCPDenied):
        service.call_tool(mcp_conn, "get_statement",
                          {"job_id": "job1", "statement_id": "stmt1"}, token, job_id="job1")
    row = mcp_conn.execute(
        "SELECT outcome, error_code FROM mcp_audit_events ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "deny" and row["error_code"] == DENY_REDACTION
