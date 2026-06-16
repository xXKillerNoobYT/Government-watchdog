"""Tests for scripts/clean_merged_worktrees.py."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test
import importlib
import sys
import os

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import clean_merged_worktrees as cmw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MAIN_ROOT = "/fake/repo"
WORKTREE_BASE = f"{MAIN_ROOT}/.paperclip/worktrees"


def _wt(slug: str, branch: str | None = None, head: str = "abc1234", locked: bool = False):
    return cmw.WorktreeInfo(
        path=f"{WORKTREE_BASE}/{slug}",
        branch=branch or slug,
        head=head,
        locked=locked,
    )


MERGED_PRS = {
    "GOV-100-merged-clean": {"number": 10, "mergedAt": "2026-06-15T00:00:00Z"},
    "GOV-101-merged-dirty": {"number": 11, "mergedAt": "2026-06-15T01:00:00Z"},
    "GOV-102-merged-locked": {"number": 12, "mergedAt": "2026-06-15T02:00:00Z"},
    "orphan-merged-branch": {"number": 13, "mergedAt": "2026-06-15T03:00:00Z"},
}


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------

class TestDiscoverCandidates:
    def test_merged_clean_worktree_is_candidate(self):
        worktrees = [_wt("GOV-100-merged-clean")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 1
        assert result.candidates[0].branch == "GOV-100-merged-clean"
        assert result.candidates[0].pr_number == 10

    def test_unmerged_worktree_is_skipped(self):
        worktrees = [_wt("GOV-999-not-merged")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason_skip == "no merged PR found"

    def test_locked_worktree_is_skipped(self):
        worktrees = [_wt("GOV-102-merged-locked", locked=True)]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason_skip == "worktree is locked"

    def test_dirty_worktree_is_skipped(self):
        worktrees = [_wt("GOV-101-merged-dirty")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=True):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 0
        assert len(result.skipped) == 1
        assert result.skipped[0].reason_skip == "uncommitted changes in worktree"

    def test_main_root_worktree_never_candidate(self):
        wt = cmw.WorktreeInfo(path=MAIN_ROOT, branch="main", head="abc", locked=False)
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                [wt], MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 0
        assert len(result.skipped) == 0

    def test_protected_branch_never_candidate(self):
        worktrees = [_wt("main-worktree", branch="main")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 0

    def test_orphan_branches_included(self):
        mock_result = MagicMock()
        mock_result.stdout = "main\norphan-merged-branch\nGOV-100-merged-clean\n"
        with patch.object(cmw, "has_uncommitted_changes", return_value=False), \
             patch.object(cmw, "run", return_value=mock_result):
            result = cmw.discover_candidates(
                [], MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=True,
            )
        orphan_branches = [c.branch for c in result.candidates]
        assert "orphan-merged-branch" in orphan_branches
        assert "GOV-100-merged-clean" in orphan_branches
        assert "main" not in orphan_branches


# ---------------------------------------------------------------------------
# Dry-run safety tests
# ---------------------------------------------------------------------------

class TestDryRunSafety:
    def test_dry_run_does_not_remove(self):
        worktrees = [_wt("GOV-100-merged-clean")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        with patch.object(cmw, "remove_worktree") as mock_rm, \
             patch.object(cmw, "delete_local_branch") as mock_del, \
             patch.object(cmw, "delete_remote_branch") as mock_rdel:
            cmw.execute_reap(result, dry_run=True)
            mock_rm.assert_not_called()
            mock_del.assert_not_called()
            mock_rdel.assert_not_called()


# ---------------------------------------------------------------------------
# Apply-mode tests
# ---------------------------------------------------------------------------

class TestApplyMode:
    def test_apply_removes_worktree_and_branch(self):
        worktrees = [_wt("GOV-100-merged-clean")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        with patch.object(cmw, "remove_worktree", return_value=True) as mock_rm, \
             patch.object(cmw, "delete_local_branch", return_value=True) as mock_del, \
             patch.object(cmw, "remote_branch_exists", return_value=False):
            cmw.execute_reap(result, dry_run=False)
            mock_rm.assert_called_once()
            mock_del.assert_called_once_with("GOV-100-merged-clean")

    def test_apply_deletes_remote_if_exists(self):
        worktrees = [_wt("GOV-100-merged-clean")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        with patch.object(cmw, "remove_worktree", return_value=True), \
             patch.object(cmw, "delete_local_branch", return_value=True), \
             patch.object(cmw, "remote_branch_exists", return_value=True), \
             patch.object(cmw, "delete_remote_branch", return_value=True) as mock_rdel:
            cmw.execute_reap(result, dry_run=False)
            mock_rdel.assert_called_once_with("GOV-100-merged-clean")

    def test_apply_stops_on_worktree_remove_failure(self):
        worktrees = [_wt("GOV-100-merged-clean")]
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                worktrees, MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        with patch.object(cmw, "remove_worktree", return_value=False), \
             patch.object(cmw, "delete_local_branch") as mock_del:
            cmw.execute_reap(result, dry_run=False)
            mock_del.assert_not_called()
        assert len(result.errors) == 1

    def test_orphan_branch_only_deletes_branch(self):
        candidate = cmw.ReapCandidate(
            worktree=None,
            branch="orphan-merged-branch",
            pr_number=13,
            merged_at="2026-06-15T03:00:00Z",
        )
        result = cmw.ReapResult(candidates=[candidate])
        with patch.object(cmw, "remove_worktree") as mock_rm, \
             patch.object(cmw, "delete_local_branch", return_value=True), \
             patch.object(cmw, "remote_branch_exists", return_value=False):
            cmw.execute_reap(result, dry_run=False)
            mock_rm.assert_not_called()
        assert len(result.reaped) == 1


# ---------------------------------------------------------------------------
# Idempotency test
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_second_run_finds_nothing(self):
        with patch.object(cmw, "has_uncommitted_changes", return_value=False):
            result = cmw.discover_candidates(
                [], MERGED_PRS, Path(MAIN_ROOT), include_orphan_branches=False,
            )
        assert len(result.candidates) == 0
        assert len(result.skipped) == 0


# ---------------------------------------------------------------------------
# Porcelain parsing
# ---------------------------------------------------------------------------

class TestParseWorktrees:
    SAMPLE_PORCELAIN = (
        "worktree /fake/repo\n"
        "HEAD abc1234\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /fake/repo/.paperclip/worktrees/GOV-100-test\n"
        "HEAD def5678\n"
        "branch refs/heads/GOV-100-test\n"
        "\n"
        "worktree /fake/repo/.paperclip/worktrees/GOV-101-locked\n"
        "HEAD ghi9012\n"
        "branch refs/heads/GOV-101-locked\n"
        "locked\n"
    )

    def test_parses_all_worktrees(self):
        mock_result = MagicMock()
        mock_result.stdout = self.SAMPLE_PORCELAIN
        with patch.object(cmw, "run", return_value=mock_result):
            wts = cmw.parse_worktrees()
        assert len(wts) == 3
        assert wts[0].branch == "main"
        assert wts[1].branch == "GOV-100-test"
        assert not wts[1].locked
        assert wts[2].branch == "GOV-101-locked"
        assert wts[2].locked
