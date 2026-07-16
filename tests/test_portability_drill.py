"""DEPLOY-2026 drill: adapter parity, access-decision parity, PORT-4 guards.

Proves AM-6 on synthetic fixtures without Docker: export → restore → verify
hashes equal AND access decisions identical, and that raw/secret data seeded into
the fixture never reaches an artifact (the RED condition, fail-closed).
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
import portability_drill as drill  # noqa: E402
from deploy_adapters import base  # noqa: E402


# --- fixture builder -------------------------------------------------------

@pytest.fixture()
def fixture_db(tmp_path):
    path = tmp_path / "synthetic_fixture.db"
    drill.build_synthetic_fixture(path)
    return path


# --- export determinism + column allowlist ---------------------------------

def test_export_is_deterministic(fixture_db):
    a = base.SqliteAdapter(fixture_db).export()
    b = base.SqliteAdapter(fixture_db).export()
    assert a.manifest_hash == b.manifest_hash
    assert a.class_hashes == b.class_hashes


def test_export_only_bcd_retention_classes(fixture_db):
    exp = base.SqliteAdapter(fixture_db).export()
    assert set(exp.streams) == {"b_derived_civic", "c_ai_outputs", "d_audit_ledger"}
    # Class (a) raw snapshots / (g) reviewer notes never appear.
    assert exp.tables().isdisjoint(base.EXCLUDED_TABLES)
    assert "transcripts" not in exp.tables()


def test_export_excludes_raw_and_free_text_columns(fixture_db):
    exp = base.SqliteAdapter(fixture_db).export()
    blob = exp.to_json()
    # Raw vault columns seeded in the fixture must not be present.
    for leaked in ("raw_local_path", "local_note_path", "transcript_path",
                   "full raw transcript text", "/Users/IA/vault/t.txt",
                   "Obsidian Vault"):
        assert leaked not in blob, f"raw data leaked into export: {leaked!r}"
    # reviewer_decisions.reason (free text, class g) is not exported.
    assert "synthetic drill promotion" not in blob


# --- round-trip parity (SQLite scale stand-in) -----------------------------

def test_sqlite_roundtrip_hashes_equal(fixture_db, tmp_path):
    source = base.SqliteAdapter(fixture_db)
    exp = source.export()
    target = base.SqliteAdapter(tmp_path / "scale.db")
    target.restore(exp)
    exp2 = target.export()
    assert exp.manifest_hash == exp2.manifest_hash
    assert exp.class_hashes == exp2.class_hashes


def test_access_decisions_identical_pre_post(fixture_db, tmp_path):
    source = base.SqliteAdapter(fixture_db)
    exp = source.export()
    target = base.SqliteAdapter(tmp_path / "scale.db")
    target.restore(exp)

    src_conn = source.access_view()
    tgt_conn = target.access_view()
    try:
        src = base.access_decisions(src_conn)
        tgt = base.access_decisions(tgt_conn)
    finally:
        src_conn.close()
        tgt_conn.close()

    assert base.decisions_hash(src) == base.decisions_hash(tgt)
    # The reviewer-internal gate serves the promoted statement on BOTH backends.
    assert src["reviewer_internal_ids"] == ["stmt1"]
    assert src["reviewer_internal_count"] == 1
    assert src["published_count"] == 0  # nothing owner-published (fail-closed).


# --- full drill via run_drill ----------------------------------------------

def test_run_drill_passes(tmp_path):
    report = drill.run_drill(backend="sqlite", workdir=tmp_path)
    v = report["verification"]
    assert report["passed"] is True
    assert v["manifest_hash_equal"] and v["per_class_hashes_equal"]
    assert v["access_decisions_equal"]
    assert v["export_leaks"] == [] and v["target_leaks"] == []
    # Metrics recorded + basis-labelled (AC-6).
    m = report["metrics"]
    assert m["drill_duration_s"]["basis"] == "MEASURED"
    assert m["restore_rto_s"]["basis"] == "MEASURED"
    assert m["restore_rpo_s"]["basis"] == "DERIVED"
    assert m["hash_verification_pass"]["value"] is True


def test_dry_run_plan_no_mutation():
    plan = drill.dry_run_plan()
    assert plan["mutation"] is False
    assert set(plan["retention_classes_exported"]) == {
        "b_derived_civic", "c_ai_outputs", "d_audit_ledger"}
    assert len(plan["steps"]) == 7


def test_cli_dry_run_default_is_no_mutation(capsys):
    rc = drill.main([])  # no --apply → dry-run
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mutation"] is False


# --- PORT-4 / RED conditions (fail-closed) ---------------------------------

def test_synthetic_guard_refuses_real_registry(tmp_path):
    real = db.REPO_ROOT / "Database" / "gov_watchdog.db"
    with pytest.raises(base.RealRegistryRefused):
        base.assert_synthetic_path(real)
    with pytest.raises(base.RealRegistryRefused):
        base.assert_synthetic_path("/Users/IA/Obsidian Vault/TownOfAlpine/x.db")


def test_leak_scan_catches_injected_raw_path(fixture_db):
    """RED: a raw path forced into an export stream must be caught."""
    exp = base.SqliteAdapter(fixture_db).export()
    exp.streams["b_derived_civic"]["statements"][0]["statement_text"] = (
        "/Users/IA/Obsidian Vault/leak.pdf")
    findings = base.scan_export_for_leaks(exp)
    assert findings, "leak scanner failed to catch an injected raw path"


def test_leak_scan_catches_excluded_table(fixture_db):
    exp = base.SqliteAdapter(fixture_db).export()
    exp.streams["b_derived_civic"]["transcripts"] = [{"id": 1}]
    findings = base.scan_export_for_leaks(exp)
    assert any("transcripts" in f for f in findings)


def test_leak_scan_catches_secret_value(fixture_db):
    exp = base.SqliteAdapter(fixture_db).export()
    exp.streams["d_audit_ledger"]["reviewer_decisions"][0]["reviewer_id"] = (
        "AKIAIOSFODNN7EXAMPLE")
    findings = base.scan_export_for_leaks(exp)
    assert any("secret-shaped" in f for f in findings)


def test_clean_fixture_export_has_no_leaks(fixture_db):
    exp = base.SqliteAdapter(fixture_db).export()
    assert base.scan_export_for_leaks(exp) == []
