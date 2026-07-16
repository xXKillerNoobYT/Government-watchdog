"""Shared pytest fixtures for the MCP-service contract tests (GOV-731).

Fixtures are opt-in by name — no autouse, no import-time env mutation — so this
conftest is inert for every non-MCP test in the suite. The signing secret is set
per-test via ``monkeypatch`` (never a repo constant, INV-7).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402

MCP_SECRET = "unit-test-hmac-secret-not-committed"

# The full scope set a wide-open pilot grant would carry.
ALL_SCOPES = [
    "tool:list_job_inputs", "tool:get_statement", "tool:get_segment",
    "tool:get_provenance", "tool:get_policy_pack", "tool:submit_output",
    "resource:job.spec:read", "resource:evidence.statement:read",
    "resource:evidence.segment:read", "resource:evidence.provenance:read",
    "resource:policy.pack:read",
]

OUTPUT_SCHEMA_ID = "gov.output.summary"


def _seed(conn: sqlite3.Connection) -> None:
    """A clean, marker-free Alpine fixture: one job over one statement/segment.

    Every backing row also carries a raw path column (``raw_local_path``,
    ``local_note_path``, ``transcript_path``) so the allowlist (D3) has something
    to strip — the boundary tests assert none of it crosses.
    """
    conn.executescript(
        """
        INSERT INTO sources(source_id,name,scope,source_class,jurisdiction,scan_date,
            archive_url,raw_sha256,raw_local_path,local_note_path,verification_status)
        VALUES('src1','Alpine minutes','alpine','minutes','alpine','2026-06-23',
            'https://web.archive.org/web/2026/min.pdf','deadbeef',
            '/Users/IA/Obsidian Vault/TownOfAlpine/min.pdf',
            '/Users/IA/note.md','reviewed_source_linked');
        INSERT INTO transcripts(id,video_id,video_url,full_text,local_path,sha256,fetch_time_utc)
        VALUES(1,'vid1','https://youtube.com/watch?v=vid1','full transcript text',
            '/Users/IA/vault/t.txt','s','2026-06-23');
        INSERT INTO transcript_segments(segment_id,transcript_id,segment_index,
            timestamp_seconds,timestamp_human,segment_text,transcript_path)
        VALUES('seg1',1,0,12,'0:12','The council approved the budget for the quarter.',
            '/Users/IA/vault/t.txt');
        INSERT INTO statements(statement_id,segment_id,statement_text,
            verification_status,publication_state)
        VALUES('stmt1','seg1','The council approved the quarterly budget line.',
            'reviewed_source_linked','not_publishable');
        INSERT INTO statements(statement_id,segment_id,statement_text,
            verification_status,publication_state)
        VALUES('stmt_other','seg1','A statement NOT in job1 selector.',
            'reviewed_source_linked','not_publishable');
        INSERT INTO evidence_links(evidence_link_id,from_node_id,from_node_type,
            to_source_id,relation,locator_kind,page,transcript_path)
        VALUES('el1','stmt1','statement','src1','references','page',3,
            '/Users/IA/vault/t.txt');
        INSERT INTO mcp_jobs(job_id,area_id,job_kind,input_selector,
            policy_pack_id,policy_pack_version)
        VALUES('job1','alpine','summarize',
            '{"statement_ids":["stmt1"],"segment_ids":["seg1"]}','pack1','1.0.0');
        INSERT INTO mcp_jobs(job_id,area_id,job_kind,input_selector,
            policy_pack_id,policy_pack_version)
        VALUES('job2','alpine','summarize','{"statement_ids":[],"segment_ids":[]}',
            'pack1','1.0.0');
        INSERT INTO mcp_policy_packs(pack_id,version,kind,disclosure,rules_template,
            required_output_schema_id,content_hash)
        VALUES('pack1','1.0.0','lens','{"label":"neutral","description":"summarize"}',
            'Summarize neutrally.','gov.output.summary','packhash1');
        """
    )
    conn.commit()


@pytest.fixture()
def mcp_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_HMAC_SECRET", MCP_SECRET)
    # Register the pack's required output schema so submit_output can validate.
    from mcp_service import schemas

    if OUTPUT_SCHEMA_ID not in schemas.registered_ids():
        schemas.register(
            OUTPUT_SCHEMA_ID, "1.0.0",
            {"type": "object", "additionalProperties": False,
             "required": ["summary"], "properties": {"summary": {"type": "string"}}},
        )
    db_path = tmp_path / "mcp.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    _seed(conn)
    yield conn
    conn.close()


@pytest.fixture()
def mint(mcp_conn):
    """Factory: mint a grant/token for a job with a chosen scope set + ttl."""
    from mcp_service import capability

    def _mint(job_id="job1", scopes=None, ttl_seconds=3600, max_calls=0):
        return capability.mint_grant(
            mcp_conn, job_id=job_id,
            scopes=ALL_SCOPES if scopes is None else scopes,
            ttl_seconds=ttl_seconds, max_calls=max_calls,
        )

    return _mint


def good_output_args(job_id="job1"):
    return {
        "job_id": job_id, "output_kind": "summary",
        "body": {"summary": "The council approved the quarterly budget."},
        "claims": [{"source_anchor": "src1:p3", "confidence": "medium",
                    "uncertainty": "low"}],
        "policy_pack_id": "pack1", "policy_pack_version": "1.0.0",
    }
