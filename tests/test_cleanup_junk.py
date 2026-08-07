"""Tests for cleanup_junk.py — local junk/log cleanup tool.

Focus: the GOV-272 owner-retained-evidence guard. cleanup_junk keys its safety
on git-*tracked* status, but a deliberately git-*ignored* retained-evidence dir
(e.g. Logs/gov215-evidence/) is untracked to git and a blanket --apply would
delete it. The guard mirrors cleanup_merged_worktrees.py gate-4: never delete a
path whose segments contain 'evidence', or that sits under a keep-marker.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup_junk as cj


# --------------------------------------------------------------------------
# retained_evidence() — pure unit tests
# --------------------------------------------------------------------------
class TestRetainedEvidence:
    def test_evidence_segment_is_retained(self, tmp_path):
        p = tmp_path / "Logs" / "gov215-evidence" / "dryrun-20260616T054118Z.json"
        p.parent.mkdir(parents=True)
        p.write_text("{}")
        is_ret, reason = cj.retained_evidence(p, tmp_path)
        assert is_ret is True
        assert "evidence" in reason

    def test_evidence_segment_case_insensitive(self, tmp_path):
        p = tmp_path / "Logs" / "GOV215-EVIDENCE" / "capture.json"
        p.parent.mkdir(parents=True)
        p.write_text("{}")
        assert cj.retained_evidence(p, tmp_path)[0] is True

    def test_keep_marker_retains_dir_and_children(self, tmp_path):
        keep_dir = tmp_path / "Logs" / "retained-captures"
        keep_dir.mkdir(parents=True)
        (keep_dir / ".cleanup-keep").write_text("")
        child = keep_dir / "old-run.log"
        child.write_text("x")
        assert cj.retained_evidence(keep_dir, tmp_path)[0] is True
        assert cj.retained_evidence(child, tmp_path)[0] is True

    def test_ordinary_log_not_retained(self, tmp_path):
        p = tmp_path / "Logs" / "crawl-20260101.log"
        p.parent.mkdir(parents=True)
        p.write_text("x")
        is_ret, reason = cj.retained_evidence(p, tmp_path)
        assert is_ret is False
        assert reason == ""

    def test_gitignored_non_evidence_json_not_retained(self, tmp_path):
        # post-merge-cleanup-*.json is git-ignored AND operational — must stay
        # cleanable. Being git-ignored is NOT a retain signal.
        p = tmp_path / "Logs" / "post-merge-cleanup-20260616.json"
        p.parent.mkdir(parents=True)
        p.write_text("{}")
        assert cj.retained_evidence(p, tmp_path)[0] is False

    def test_owner_retained_gov_watchdog_db_is_retained(self, tmp_path):
        p = tmp_path / "Database" / "gov_watchdog.db"
        p.parent.mkdir(parents=True)
        p.write_text("sqlite bytes")
        is_ret, reason = cj.retained_evidence(p, tmp_path)
        assert is_ret is True
        assert "GOV-693" in reason


# --------------------------------------------------------------------------
# Integration — iter_candidates + remove_candidate against a temp tree
# --------------------------------------------------------------------------
def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A temp repo root with REPO_ROOT/ALLOWED_ROOTS pointed at it."""
    monkeypatch.setattr(cj, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cj, "ALLOWED_ROOTS", [tmp_path])
    # Avoid depending on a real git checkout: treat nothing as tracked.
    monkeypatch.setattr(cj, "git_tracked_files", lambda root: set())
    logs = tmp_path / "Logs"
    logs.mkdir()
    return tmp_path


def test_blanket_apply_preserves_evidence_deletes_ordinary_log(repo):
    logs = repo / "Logs"
    evidence = logs / "gov215-evidence" / "dryrun-20260616T054118Z.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"kept": true}')
    ordinary = logs / "crawl-20260101.log"
    ordinary.write_text("noise")
    for f in (evidence, ordinary):
        _age(f, 13)

    candidates = cj.iter_candidates([repo], retention_days=3, include_tracked=False)
    by_name = {c.path.name: c for c in candidates}

    # Both surface as candidates...
    assert "dryrun-20260616T054118Z.json" in by_name
    assert "crawl-20260101.log" in by_name
    # ...but the evidence file is flagged retained, the ordinary log is not.
    assert by_name["dryrun-20260616T054118Z.json"].retained is True
    assert by_name["crawl-20260101.log"].retained is False

    # Simulate a blanket --apply --include-tracked over every candidate.
    for c in candidates:
        cj.remove_candidate(c, include_tracked=True, include_databases=True,
                            include_markdown_logs=True)

    assert evidence.exists(), "retained evidence must survive blanket --apply"
    assert not ordinary.exists(), "ordinary old log should be deleted"


def test_parent_dir_with_evidence_child_is_preserved(repo):
    # If the whole Logs/ dir is itself a junk-dir candidate, wholesale rmtree must
    # refuse because it contains a retained evidence subdir.
    logs = repo / "Logs"
    evidence = logs / "gov215-evidence" / "capture.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}")
    _age(logs, 30)
    _age(evidence, 30)

    dir_candidate = cj.Candidate(
        path=logs, reason="junk directory 'Logs'", age_days=30.0,
        tracked=False, size_bytes=10,
    )
    deleted = cj.remove_candidate(dir_candidate, include_tracked=True,
                                  include_databases=True, include_markdown_logs=True)
    assert deleted is False
    assert evidence.exists()


def test_keep_marker_dir_preserved_under_apply(repo):
    keep_dir = repo / "Logs" / "owner-retained"
    keep_dir.mkdir(parents=True)
    (keep_dir / ".cleanup-keep").write_text("")
    artifact = keep_dir / "old.log"
    artifact.write_text("x")
    _age(artifact, 20)

    candidates = cj.iter_candidates([repo], retention_days=3, include_tracked=False)
    art = next(c for c in candidates if c.path.name == "old.log")
    assert art.retained is True
    cj.remove_candidate(art, include_tracked=True, include_databases=True,
                        include_markdown_logs=True)
    assert artifact.exists()


# --------------------------------------------------------------------------
# GOV-1694 — dry-run preview parity + single-pass idempotency
# --------------------------------------------------------------------------
def _run_main(monkeypatch, argv):
    """Invoke cleanup_junk.main() with argv and return the parsed --json summary."""
    monkeypatch.setattr(sys, "argv", ["cleanup_junk.py", "--json", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cj.main()
    assert rc == 0
    return json.loads(buf.getvalue())


def _paths(rows):
    return {r["path"] for r in rows}


@pytest.fixture
def mixed_tree(repo):
    """A realistic Logs/ tree: stale .json captures (deletable) plus a markdown
    note, a local .db, and a retained evidence subdir (all preserved)."""
    logs = repo / "Logs"
    # deletable stale routine captures
    for i in range(4):
        f = logs / f"dryrun-2026080{i}.json"
        f.write_text('{"i": %d}' % i)
        _age(f, 10)
    nested = logs / "cache"
    nested.mkdir()
    for i in range(3):
        f = nested / f"c{i}.json"
        f.write_text("{}")
        _age(f, 10)
    _age(nested, 10)
    # preserved: markdown note under a log dir
    md = logs / "notes.md"
    md.write_text("# review-only")
    _age(md, 10)
    # preserved: local database
    db = logs / "state.db"
    db.write_text("sqlite bytes")
    _age(db, 10)
    # preserved: owner-retained evidence subdir
    ev = logs / "gov1-evidence" / "capture.json"
    ev.parent.mkdir(parents=True)
    ev.write_text("{}")
    _age(ev, 10)
    _age(ev.parent, 10)
    _age(logs, 10)
    return {"logs": logs, "md": md, "db": db, "evidence": ev, "nested": nested}


def test_dry_run_preview_is_nonempty_and_matches_apply(monkeypatch, mixed_tree):
    # GOV-1694 symptom 1: the dry-run `deleted` list must be a faithful preview
    # of exactly what --apply removes, not an always-empty list.
    dry = _run_main(monkeypatch, ["--retention-days", "3"])
    assert dry["mode"] == "dry-run"
    assert dry["deleted"], "dry-run must preview what apply would delete, not report 0"
    assert dry["deleted_count"] > 0
    assert dry["deleted_bytes"] > 0

    applied = _run_main(monkeypatch, ["--retention-days", "3", "--apply"])
    assert applied["mode"] == "apply"

    # Byte-identical path set between the dry-run preview and the apply removal.
    assert _paths(dry["deleted"]) == _paths(applied["deleted"])


def test_apply_is_single_pass_idempotent(monkeypatch, mixed_tree):
    # GOV-1694 symptom 2: a second --apply over the same tree must delete nothing.
    first = _run_main(monkeypatch, ["--retention-days", "3", "--apply"])
    assert first["deleted_count"] > 0
    second = _run_main(monkeypatch, ["--retention-days", "3", "--apply"])
    assert second["deleted_count"] == 0, "apply must reach its fixed point in one pass"
    assert second["deleted"] == []


def test_preview_and_apply_preserve_md_db_and_evidence(monkeypatch, mixed_tree):
    # The existing safety gates (retained evidence / .db / .md) stay green: none
    # of the preserved kinds appear in the delete preview, and all survive apply.
    dry = _run_main(monkeypatch, ["--retention-days", "3"])
    deleted_paths = _paths(dry["deleted"])
    assert str(mixed_tree["md"]) not in deleted_paths
    assert str(mixed_tree["db"]) not in deleted_paths
    assert str(mixed_tree["evidence"]) not in deleted_paths

    _run_main(monkeypatch, ["--retention-days", "3", "--apply"])
    assert mixed_tree["md"].exists()
    assert mixed_tree["db"].exists()
    assert mixed_tree["evidence"].exists()
    # ...but the stale captures are gone.
    assert not (mixed_tree["logs"] / "dryrun-20260800.json").exists()
    assert not mixed_tree["nested"].exists()


def test_would_delete_is_pure_no_side_effects(mixed_tree):
    # would_delete must not touch the filesystem — calling it leaves every path
    # in place, including the deletable ones.
    logs = mixed_tree["logs"]
    before = {str(p) for p in logs.rglob("*")}
    candidates = cj.iter_candidates([logs.parent], retention_days=3, include_tracked=False)
    for c in candidates:
        cj.would_delete(c, include_tracked=False, include_databases=False,
                        include_markdown_logs=False)
    after = {str(p) for p in logs.rglob("*")}
    assert before == after
