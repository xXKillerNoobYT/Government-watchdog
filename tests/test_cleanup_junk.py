"""Tests for cleanup_junk.py — local junk/log cleanup tool.

Focus: the GOV-272 owner-retained-evidence guard. cleanup_junk keys its safety
on git-*tracked* status, but a deliberately git-*ignored* retained-evidence dir
(e.g. Logs/gov215-evidence/) is untracked to git and a blanket --apply would
delete it. The guard mirrors cleanup_merged_worktrees.py gate-4: never delete a
path whose segments contain 'evidence', or that sits under a keep-marker.
"""
from __future__ import annotations

import os
import sys
import time
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
