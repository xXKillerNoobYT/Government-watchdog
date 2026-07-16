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


# --- GOV-736 additive helpers (routing / budget / lens seeding) ----------------

def seed_local_routing(
    conn,
    *,
    provider_id="fake",
    kind="fake",
    cap_units=1000,
    model="fake-1",
    job_kind="lens_analysis",
    context_class="local_only",
    enabled=True,
    budget=True,
    policy=True,
    packs=True,
):
    """Make a local provider routable + seed the lens packs, all additive.

    Mirrors what the CLI composition root does, so the routing/budget/lens tests
    share one fixture. Every step is opt-out via a flag so a test can, e.g.,
    register a provider with NO budget (BUD-5) or a cap of 0 (AM-11).
    """
    from mcp_service import budget as budget_mod, lenses
    from mcp_service.providers import base as pbase

    if packs:
        lenses.seed_lens_packs(conn)
    pbase.register_provider(conn, provider_id=provider_id, kind=kind,
                            budget_cap_units=cap_units if budget else 0)
    if enabled:
        conn.execute(
            "UPDATE mcp_provider_registry SET enabled=1, budget_cap_units=? "
            "WHERE provider_id=?", (cap_units if budget else 0, provider_id))
    if budget:
        budget_mod.create_budget(
            conn, budget_id=f"budget-{provider_id}", provider_id=provider_id,
            cap_units=cap_units, window_kind="total", area_id="alpine")
    if policy:
        import json as _json
        conn.execute(
            "INSERT OR IGNORE INTO mcp_routing_policies "
            "(policy_id, version, job_kind, context_class, provider_preference, "
            " model, max_output_units, created_utc) "
            "VALUES (?, '1.0.0', ?, ?, ?, ?, 50, '2026-07-16T00:00:00.000+00:00')",
            (f"policy-{provider_id}", job_kind, context_class,
             _json.dumps([provider_id]), model))
    conn.commit()


@pytest.fixture()
def routed(mcp_conn):
    """mcp_conn with a callable local 'fake' provider + lens packs + policy."""
    seed_local_routing(mcp_conn)
    return mcp_conn


def fake_adapter(provider_id="fake", model="fake-1"):
    from mcp_service.providers.fake import FakeAdapter

    return FakeAdapter(provider_id=provider_id, model=model)


# --- GOV-743 additive helpers (LEDGER-2026 area-economics seeding) --------------

ECON_PERIOD = "2026-07"


def _seed_envelope(conn, envelope_id, area_id, period):
    """Seed the webhook_source + event_envelope FK chain for an event_job."""
    ts = f"{period}-01T00:00:00.000+00:00"
    conn.execute(
        "INSERT OR IGNORE INTO webhook_sources (source_key, secret_ref, active, created_at)"
        " VALUES ('econ-src', 'ref:econ', 1, ?)",
        (ts,),
    )
    conn.execute(
        "INSERT INTO event_envelopes (envelope_id, received_at, source_key,"
        " canonical_payload, payload_sha256, source_hash, area_id, event_kind,"
        " policy_version, dedupe_key) VALUES (?, ?, 'econ-src', '{}', ?, ?, ?,"
        " 'ingest', 'p1', ?)",
        (envelope_id, ts, f"h{envelope_id}", f"sh{envelope_id}", area_id,
         f"dk-{envelope_id}"),
    )


def seed_economics(conn):
    """Deterministic synthetic area-economics fixture (no network, no registry data).

    Two towns under one county under one state; a handful of event_jobs and
    mcp_audit_events carrying MEASURED cost units for period 2026-07; owner-set
    fixed cost, funding, reviewer-work, and a designed entitlement. Everything is
    invented test data — never real registry rows.
    """
    from economics import areas

    areas.create_area(conn, area_id="wy", kind="state", name="Wyoming")
    areas.create_area(conn, area_id="lincoln", kind="county", name="Lincoln County",
                      parent_area_id="wy")
    areas.create_area(conn, area_id="alpine", kind="town", name="Alpine",
                      parent_area_id="lincoln")
    areas.create_area(conn, area_id="etna", kind="town", name="Etna",
                      parent_area_id="lincoln")

    period = ECON_PERIOD
    ts = f"{period}-05T00:00:00.000+00:00"

    # event_jobs: 3 alpine (lane 2), 2 etna, 1 shared-pool (area_id NULL).
    plan = [
        ("alpine", "2_extraction", 1.0, 0.5),
        ("alpine", "2_extraction", 2.0, 0.25),
        ("alpine", "5_review", 0.5, 0.1),
        ("etna", "2_extraction", 1.5, 0.4),
        ("etna", "2_extraction", 1.0, 0.2),
        (None, "2_extraction", 3.0, 1.0),
    ]
    for i, (area_id, lane, cpu_s, qw) in enumerate(plan, start=1):
        _seed_envelope(conn, i, area_id, period)
        conn.execute(
            "INSERT INTO event_jobs (envelope_id, lane, area_id, state, enqueued_at,"
            " finished_at, queue_wait_s, cpu_s, retry_count, cache_hit)"
            " VALUES (?, ?, ?, 'done', ?, ?, ?, ?, 0, 0)",
            (i, lane, area_id, ts, ts, qw, cpu_s),
        )

    # mcp_audit_events: MEASURED direct_cost_units + latency for F1 / SLO-3.
    audits = [
        ("alpine", 100, 40, 60, 120),
        ("alpine", 150, 55, 90, 200),
        ("etna", 80, 30, 45, 90),
        (None, 300, 120, 180, 500),
    ]
    for j, (area_id, direct, inp, outp, latency) in enumerate(audits, start=1):
        conn.execute(
            "INSERT INTO mcp_audit_events (audit_id, area_id, kind, name, outcome,"
            " latency_ms, provider, model, input_units, output_units,"
            " direct_cost_units, cache_hit, retry_count, created_at)"
            " VALUES (?, ?, 'tool', 'summarize', 'allow', ?, 'fake', 'fake-1', ?, ?,"
            " ?, 0, 0, ?)",
            (f"aud-{j}", area_id, latency, inp, outp, direct, ts),
        )

    # OWNER-SET fixed cost for the period.
    conn.execute(
        "INSERT INTO ledger_fixed_costs (period, fixed_total_units, weight_basis,"
        " basis, created_utc) VALUES (?, 1000, 'document_share', 'OWNER-SET', ?)",
        (period, ts),
    )
    # OWNER-SET funding + safety factor for alpine (F-ELIG).
    conn.execute(
        "INSERT INTO area_funding_entries (entry_id, area_id, period, amount_units,"
        " basis, created_utc) VALUES ('f1', 'alpine', ?, 5000, 'OWNER-SET', ?)",
        (period, ts),
    )
    conn.execute(
        "INSERT INTO area_funding_policy (area_id, safety_factor, updated_utc)"
        " VALUES ('alpine', 1.5, ?)",
        (ts,),
    )
    # LED-2 reviewer work: a MEASURED batch for alpine.
    conn.execute(
        "INSERT INTO ledger_reviewer_work (batch_id, area_id, period,"
        " reviewer_minutes, decision_count, per_decision_units, correction_rate,"
        " rejection_rate, source_coverage_rate, basis, created_utc)"
        " VALUES ('b1', 'alpine', ?, 45.0, 10, 5.0, 0.1, 0.05, 0.9, 'MEASURED', ?)",
        (period, ts),
    )
    # GATE-P designed entitlement for alpine (schema-only; inert).
    conn.execute(
        "INSERT INTO area_entitlements (entitlement_id, area_id, tier, state,"
        " owner_decision_ref, created_utc) VALUES ('ent1', 'alpine', 'tier-a',"
        " 'designed', NULL, ?)",
        (ts,),
    )
    conn.commit()
    return period


@pytest.fixture()
def econ_conn(tmp_path):
    """A migrated DB seeded with the synthetic area-economics fixture."""
    db_path = tmp_path / "econ.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    seed_economics(conn)
    yield conn
    conn.close()
