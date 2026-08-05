"""Phase 1 smoke test: schema apply, idempotency, sample insert.

See Docs/phase1-spec.md §4.8.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def test_apply_creates_all_tables(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        tables = _table_names(conn)
    assert tables == {
        "documents",
        "transcripts",
        "meetings",
        "meeting_documents",
        "embeddings",
        "crawl_runs",
        "sources",  # GOV-74: source registry
        "schema_migrations",  # GOV-74 §6: idempotent migration ledger
        "agenda_items",  # GOV-81: Slice 2 B — 1.07 §1 agenda_item node
        "transcript_segments",  # GOV-81: Slice 2 B — 1.07 §1 addressable segment rows
        "statements",  # GOV-82: Slice 2 C — 1.07 §1/§2 statement node
        "evidence_links",  # GOV-82: Slice 2 C — 1.07 §1.4/§2 exact-source pointer
        "persons",  # GOV-83: Slice 2 D — 1.07 §3 person node (gated identity)
        "roles",  # GOV-83: Slice 2 D — 1.07 §1 role node
        "served_in_role",  # GOV-83: Slice 2 D — 1.07 §1.2 person→role edge
        "speaker_attributions",  # GOV-83: Slice 2 D — 1.07 §3 safe attribution record
        "made_statement",  # GOV-83: Slice 2 D — 1.07 §3.4 gated person→statement edge
        "outcomes",  # GOV-83: Slice 2 D — 1.07 §4 later-outcome node
        "outcome_updates",  # GOV-83: Slice 2 D — 1.07 §4.2 forward-only update edge
        "ai_extraction_runs",  # GOV-89: Slice 3 B — Lane 2 AI-gateway run ledger
        "ai_verification_results",  # GOV-90: Slice 3 C — Lane 3 verification verdict ledger
        "ai_risk_flags",  # GOV-91: Slice 3 D — Lane 4 risk-layer findings ledger
        "reviewer_decisions",  # GOV-91: Slice 3 D — Lane 5 reviewer-gate audit ledger
        "agenda_threads",  # GOV-98: Slice 4 Prereq-0 — concept-map agenda_thread node
        "topics",  # GOV-98: Slice 4 Prereq-0 — flat topic node (tree via topic_rollup)
        "concept_edges",  # GOV-98: Slice 4 Prereq-0 — generic forward-linking typed edges
        "node_label_aliases",  # GOV-98 addendum: plain-language label layer (§A.7)
        "reviewer_identities",  # GOV-131: reviewer-identity registry (Lane-5 allowlist SoT)
        "completeness_gaps",  # GOV-125: first-class completeness-gap layer (plan §3)
        "webhook_sources",  # GOV-733: CTRL-2026 §3.1 — registered ingress principals
        "event_envelopes",  # GOV-733: CTRL-2026 §3.1 — WRITE-ONCE signed-event record
        "event_dedupe_hits",  # GOV-733: CTRL-2026 §3.1 — append-only replay ledger
        "event_jobs",  # GOV-733: CTRL-2026 §3.1 — micro-job LED-1 rows
        "job_transitions",  # GOV-733: CTRL-2026 §3.1 — append-only state-machine audit
        "paperclip_outbox",  # GOV-733: CTRL-2026 §3.1 — bounded safe hand-off to Paperclip
        # GOV-731 (CONTRACT-2026-MCP §4): six additive mcp_* service-layer tables.
        "mcp_jobs",
        "mcp_capability_grants",
        "mcp_policy_packs",
        "mcp_job_outputs",
        "mcp_audit_events",
        "mcp_provider_registry",
        # GOV-736 (PLAN-2026-AI §3): four additive routing/budget/health tables.
        "mcp_budgets",
        "mcp_budget_events",
        "mcp_routing_policies",
        "mcp_provider_health",
        # GOV-743 (LEDGER-2026 §1): nine additive area-economics tables.
        "areas",
        "area_state",
        "area_transitions",
        "area_funding_entries",
        "area_funding_policy",
        "area_entitlements",
        "ledger_fixed_costs",
        "ledger_reviewer_work",
        "ledger_report_runs",
        # GOV-753 (ACCT-2026 v0.2 / GOV-721 leg 1): eleven additive
        # accounts/cohorts/notifications tables.
        "users",
        "waitlist_requests",
        "access_grants",
        "cohort_state",
        "cohort_transitions",
        "consent_preferences",
        "notification_events",
        "email_outbox",
        "email_delivery_log",
        "feature_flags",
        "auth_sessions",
        # GOV-801 (0026): five additive gated-beta front-door tables.
        "beta_allowlist",
        "beta_magic_tokens",
        "beta_sessions",
        "beta_waitlist",
        "beta_audit_log",
        # GOV-1575 (0028 / GOV-1566 B2): supplied-file record + provenance model.
        "supplied_files",
        # GOV-1577 (0029 / GOV-1566 B4): supplied-file → area/meeting/agenda linkage.
        "supplied_file_links",
        # GOV-1578 (0030 / GOV-1566 B5): supersede versioning + red-flag tables.
        "supplied_file_dependencies",
        "supplied_file_supersede_events",
        # GOV-1565 (0032 / GOV-1523 P4c-2 addendum): account-deletion request lifecycle record.
        "account_deletion_requests",
        # GOV-1684 (0033 / Stage 5 R1/Slice 1): civic source-version preservation + typed lineage.
        "source_versions",
        # GOV-1685 (0034 / Stage 5 R1/Slice 2): late-change detection + structured
        # before/after source diff over a preserved version pair.
        "source_version_changes",
        "source_version_diff_segments",
    }


def test_apply_is_idempotent(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    db.apply_migrations(fresh_db)  # must not raise
    with db.open_db(fresh_db) as conn:
        tables = _table_names(conn)
    assert "documents" in tables


def test_sample_insert_round_trip(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?)",
            ("https://alpinewy.gov/example.pdf", "Raw-PDFs/2026/alpinewy/example.pdf", "0" * 64, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT source_url, sha256 FROM documents WHERE source_url = ?",
            ("https://alpinewy.gov/example.pdf",),
        ).fetchone()
    assert row["source_url"] == "https://alpinewy.gov/example.pdf"
    assert row["sha256"] == "0" * 64


def test_unique_source_url(fresh_db: Path) -> None:
    import sqlite3

    db.apply_migrations(fresh_db)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?)",
            ("https://alpinewy.gov/dup.pdf", "Raw-PDFs/2026/alpinewy/dup.pdf", "1" * 64, now),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
                "VALUES (?, ?, ?, ?)",
                ("https://alpinewy.gov/dup.pdf", "Raw-PDFs/2026/alpinewy/dup.pdf", "1" * 64, now),
            )
            conn.commit()
