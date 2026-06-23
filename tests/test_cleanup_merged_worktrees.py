"""Tests for cleanup_merged_worktrees.py — post-merge workspace cleanup tool."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cleanup_merged_worktrees as cm


class TestExtractIssueId:
    def test_standard_prefix(self):
        assert cm.extract_issue_id("GOV-131-reviewer-identity-registry") == "GOV-131"

    def test_lowercase(self):
        assert cm.extract_issue_id("gov93-allowlist-gate") == "GOV-93"

    def test_hyphenated(self):
        assert cm.extract_issue_id("GOV-67-stage-1-15-ceo") == "GOV-67"

    def test_no_issue(self):
        assert cm.extract_issue_id("main") is None
        assert cm.extract_issue_id("feature-branch") is None

    def test_multiple_matches_returns_first(self):
        assert cm.extract_issue_id("gov-10-depends-gov-20") == "GOV-10"


class TestGateResult:
    def test_pass(self):
        g = cm.GateResult(True, "ok")
        assert g.passed is True

    def test_fail(self):
        g = cm.GateResult(False, "not merged")
        assert g.passed is False


class TestCandidate:
    def _make(self, g1=True, g2=True, g3=True, g4=True):
        return cm.Candidate(
            repo_root="/tmp/repo",
            branch="gov-1-test",
            issue_id="GOV-1",
            worktree_path=None,
            gate1_issue_done=cm.GateResult(g1, "g1"),
            gate2_merged=cm.GateResult(g2, "g2"),
            gate3_clean=cm.GateResult(g3, "g3"),
            gate4_safe_path=cm.GateResult(g4, "g4"),
        )

    def test_all_pass(self):
        c = self._make()
        assert c.all_gates_pass is True
        assert c.action == "remove"

    def test_gate1_fail(self):
        c = self._make(g1=False)
        assert c.all_gates_pass is False
        assert c.action == "preserve"

    def test_gate2_fail(self):
        c = self._make(g2=False)
        assert c.all_gates_pass is False
        assert c.action == "preserve"

    def test_gate3_fail(self):
        c = self._make(g3=False)
        assert c.all_gates_pass is False
        assert c.action == "preserve"

    def test_gate4_fail(self):
        c = self._make(g4=False)
        assert c.all_gates_pass is False
        assert c.action == "preserve"

    def test_multiple_fail(self):
        c = self._make(g1=False, g3=False)
        assert c.all_gates_pass is False


class TestCheckGate1:
    @patch("cleanup_merged_worktrees.query_issue_status")
    def test_done(self, mock_query):
        mock_query.return_value = {"status": "done", "title": "t"}
        g = cm.check_gate1("GOV-1", "http://localhost:3100")
        assert g.passed is True

    @patch("cleanup_merged_worktrees.query_issue_status")
    def test_cancelled(self, mock_query):
        mock_query.return_value = {"status": "cancelled", "title": "t"}
        g = cm.check_gate1("GOV-1", "http://localhost:3100")
        assert g.passed is True

    @patch("cleanup_merged_worktrees.query_issue_status")
    def test_in_progress(self, mock_query):
        mock_query.return_value = {"status": "in_progress", "title": "t"}
        g = cm.check_gate1("GOV-1", "http://localhost:3100")
        assert g.passed is False

    @patch("cleanup_merged_worktrees.query_issue_status")
    def test_api_unreachable(self, mock_query):
        mock_query.return_value = None
        g = cm.check_gate1("GOV-1", "http://localhost:3100")
        assert g.passed is False

    def test_no_issue_id(self):
        g = cm.check_gate1(None, "http://localhost:3100")
        assert g.passed is False


class TestQueryIssueStatusFallback:
    """F1: a dead PAPERCLIP_API_URL must self-heal to localhost, not silently
    preserve every candidate."""

    def test_primary_success_no_fallback(self):
        with patch("cleanup_merged_worktrees._fetch_issue") as mock_fetch:
            mock_fetch.return_value = {"status": "done", "title": "t"}
            info = cm.query_issue_status("GOV-1", "http://dead-tunnel:3100")
            assert info == {"status": "done", "title": "t"}
            mock_fetch.assert_called_once_with("GOV-1", "http://dead-tunnel:3100")

    def test_dead_tunnel_falls_back_to_localhost(self):
        with patch("cleanup_merged_worktrees._fetch_issue") as mock_fetch:
            # primary (dead tunnel) returns None, localhost fallback returns done
            mock_fetch.side_effect = [None, {"status": "done", "title": "t"}]
            info = cm.query_issue_status(
                "GOV-67", "http://chancellor-dom-consumers-figures.trycloudflare.com:3100")
            assert info == {"status": "done", "title": "t"}
            assert mock_fetch.call_count == 2
            assert mock_fetch.call_args_list[1][0][1] == cm.LOCALHOST_FALLBACK

    def test_no_double_fetch_when_already_localhost(self):
        with patch("cleanup_merged_worktrees._fetch_issue") as mock_fetch:
            mock_fetch.return_value = None
            info = cm.query_issue_status("GOV-1", cm.LOCALHOST_FALLBACK)
            assert info is None
            mock_fetch.assert_called_once()

    def test_localhost_trailing_slash_not_double_fetched(self):
        with patch("cleanup_merged_worktrees._fetch_issue") as mock_fetch:
            mock_fetch.return_value = None
            info = cm.query_issue_status("GOV-1", cm.LOCALHOST_FALLBACK + "/")
            assert info is None
            mock_fetch.assert_called_once()

    def test_both_unreachable_returns_none_safe(self):
        with patch("cleanup_merged_worktrees._fetch_issue") as mock_fetch:
            mock_fetch.return_value = None  # both primary and fallback fail
            info = cm.query_issue_status("GOV-1", "http://dead-tunnel:3100")
            assert info is None  # gate 1 will preserve (safe degradation)
            assert mock_fetch.call_count == 2


class TestCheckGate4:
    def test_protected_branch(self):
        g = cm.check_gate4(None, "main", Path("/tmp/repo"))
        assert g.passed is False

    def test_protected_branch_master(self):
        g = cm.check_gate4(None, "master", Path("/tmp/repo"))
        assert g.passed is False

    def test_normal_branch(self):
        with patch("cleanup_merged_worktrees._run_git") as mock_git:
            mock_git.return_value = MagicMock(stdout="")
            g = cm.check_gate4(None, "gov-1-test", Path("/tmp/repo"))
            assert g.passed is True

    def test_vault_path_rejected(self):
        g = cm.check_gate4("/Users/IA/Documents/Obsidian Vault/some-worktree",
                           "gov-1-test", Path("/tmp/repo"))
        assert g.passed is False

    def test_paperclip_path_rejected(self):
        g = cm.check_gate4("/Users/IA/.paperclip/something",
                           "gov-1-test", Path("/tmp/repo"))
        assert g.passed is False

    def test_evidence_path_rejected(self):
        g = cm.check_gate4("/some/path/evidence/worktree",
                           "gov-1-test", Path("/tmp/repo"))
        assert g.passed is False


class TestDryRunDefault:
    def test_dry_run_does_not_delete(self):
        log = MagicMock()
        candidates = [cm.Candidate(
            repo_root="/tmp/repo",
            branch="gov-1-test",
            issue_id="GOV-1",
            worktree_path=None,
            gate1_issue_done=cm.GateResult(True, "done"),
            gate2_merged=cm.GateResult(True, "merged"),
            gate3_clean=cm.GateResult(True, "clean"),
            gate4_safe_path=cm.GateResult(True, "safe"),
        )]
        result = cm.execute_cleanup(candidates, apply=False, log=log)
        assert result["removed_branches"] == ["gov-1-test"]
        assert result["failed"] == []

    def test_preserved_on_gate_fail(self):
        log = MagicMock()
        candidates = [cm.Candidate(
            repo_root="/tmp/repo",
            branch="gov-2-dirty",
            issue_id="GOV-2",
            worktree_path="/tmp/wt",
            gate1_issue_done=cm.GateResult(True, "done"),
            gate2_merged=cm.GateResult(False, "not merged"),
            gate3_clean=cm.GateResult(True, "clean"),
            gate4_safe_path=cm.GateResult(True, "safe"),
        )]
        result = cm.execute_cleanup(candidates, apply=False, log=log)
        assert result["removed_branches"] == []
        assert len(result["preserved"]) == 1
        assert "gate2" in result["preserved"][0]["reasons"][0]


class TestIdempotency:
    def test_double_dry_run_same_result(self):
        log = MagicMock()
        candidates = [cm.Candidate(
            repo_root="/tmp/repo",
            branch="gov-1-test",
            issue_id="GOV-1",
            worktree_path=None,
            gate1_issue_done=cm.GateResult(True, "done"),
            gate2_merged=cm.GateResult(True, "merged"),
            gate3_clean=cm.GateResult(True, "clean"),
            gate4_safe_path=cm.GateResult(True, "safe"),
        )]
        r1 = cm.execute_cleanup(candidates, apply=False, log=log)
        r2 = cm.execute_cleanup(candidates, apply=False, log=log)
        assert r1["removed_branches"] == r2["removed_branches"]
        assert r1["preserved"] == r2["preserved"]


class TestScopeEnforcement:
    def test_no_vault_branch_extraction(self):
        assert cm.extract_issue_id("vault-backup-branch") is None

    def test_protected_path_constant(self):
        assert "Obsidian Vault" in cm.PROTECTED_PATH_SEGMENTS
        assert ".paperclip" in cm.PROTECTED_PATH_SEGMENTS
        assert "Database" in cm.PROTECTED_PATH_SEGMENTS
        assert "Source-Data" in cm.PROTECTED_PATH_SEGMENTS
