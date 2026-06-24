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


# --- GOV-536: gate-2 must compare against the authoritative origin ref, not a
# possibly-stale local default branch. These build real git repos so the proof
# is data-driven (not a mock that asserts its own setup). ---

def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True, check=True)


def _init_work_repo(path):
    subprocess.run(["git", "init", "-b", "main", str(path)],
                   capture_output=True, text=True, check=True)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "Tester"], path)


def _commit(path, fname, content, msg):
    (path / fname).write_text(content)
    _git(["add", "."], path)
    _git(["commit", "-m", msg], path)


class TestCheckGate2OriginMain:
    """GOV-536 Finding 2: a stale local default branch ref makes
    `git branch --merged <local-default>` blind to upstream merges → the
    --apply lane becomes a silent permanent no-op. Gate 2 must consult
    origin/<default> (refreshed by fetch)."""

    def _origin_clone_with_truemerge(self, tmp_path, branch="gov-999-feature"):
        """origin bare repo whose main contains a --no-ff merge of `branch`;
        a work clone where BOTH local main and cached origin/main are rewound
        to the pre-merge base (so every local ref is blind to the merge).
        Returns (work_path, base_sha, branch)."""
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                       capture_output=True, text=True, check=True)
        work = tmp_path / "work"
        subprocess.run(["git", "clone", str(origin), str(work)],
                       capture_output=True, text=True, check=True)
        _git(["config", "user.email", "t@example.com"], work)
        _git(["config", "user.name", "Tester"], work)

        _commit(work, "f.txt", "base\n", "base")
        base_sha = _git(["rev-parse", "HEAD"], work).stdout.strip()
        _git(["push", "origin", "main"], work)

        _git(["checkout", "-b", branch], work)
        _commit(work, "g.txt", "feat\n", f"{branch} work")
        _git(["checkout", "main"], work)
        _git(["merge", "--no-ff", "-m", f"merge {branch}", branch], work)
        _git(["push", "origin", "main"], work)  # origin/main now has the merge

        # Rewind every LOCAL ref to base: real origin (bare) keeps the merge,
        # but this clone is now stale — exactly the operational-clone drift.
        _git(["reset", "--hard", base_sha], work)
        _git(["update-ref", "refs/remotes/origin/main", base_sha], work)
        return work, base_sha, branch

    def test_local_default_is_blind_old_behavior_red(self, tmp_path):
        """RED proof: comparing against the stale LOCAL main misses the merge."""
        work, _base, branch = self._origin_clone_with_truemerge(tmp_path)
        local_merged = _git(["branch", "--merged", "main"], work).stdout
        assert branch not in local_merged  # the original bug: blind

    def test_no_fetch_uses_stale_cached_origin_red(self, tmp_path):
        """RED proof: cached origin/main is also stale → gate2 still fails
        unless we fetch first (fetch is load-bearing, not cosmetic)."""
        work, _base, branch = self._origin_clone_with_truemerge(tmp_path)
        g = cm.check_gate2(branch, work, do_fetch=False)
        assert g.passed is False

    def test_fetch_refreshes_origin_main_gate_passes(self, tmp_path):
        """GREEN: with fetch, origin/main becomes authoritative and the
        genuinely-merged branch passes gate 2."""
        work, _base, branch = self._origin_clone_with_truemerge(tmp_path)
        g = cm.check_gate2(branch, work, do_fetch=True)
        assert g.passed is True
        assert "origin/main" in g.detail

    def test_resolve_merge_ref_prefers_origin(self, tmp_path):
        work, _base, branch = self._origin_clone_with_truemerge(tmp_path)
        ref = cm.resolve_merge_ref(work, "main", do_fetch=True)
        assert ref == "origin/main"

    def test_resolve_merge_ref_falls_back_to_local_when_no_remote(self, tmp_path):
        """Local-only repo (no origin): gate must still work, comparing
        against the local default branch."""
        repo = tmp_path / "local_only"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        ref = cm.resolve_merge_ref(repo, "main", do_fetch=False)
        assert ref == "main"
        # and a truly-merged local branch still passes against local main
        _git(["checkout", "-b", "gov-1-x"], repo)
        _commit(repo, "g.txt", "x\n", "x")
        _git(["checkout", "main"], repo)
        _git(["merge", "--no-ff", "-m", "m", "gov-1-x"], repo)
        g = cm.check_gate2("gov-1-x", repo, do_fetch=False)
        assert g.passed is True

    def test_squash_merged_branch_now_eligible(self, tmp_path):
        """GOV-537 (CTO decision A) supersedes the GOV-536-era preserve: a
        squash-merged branch whose single commit is fully contained upstream
        (git cherry → all `-`) AND whose issue has an anchored `GOV-NN: ...
        (#N)` squash subject is now gate-2 ELIGIBLE. This is the AC5 update of
        the former `test_squash_merged_branch_still_preserved`."""
        repo = tmp_path / "squash"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        _git(["checkout", "-b", "gov-999-stage-x-impl"], repo)
        _commit(repo, "g.txt", "feat\n", "feat")
        _git(["checkout", "main"], repo)
        _git(["merge", "--squash", "gov-999-stage-x-impl"], repo)
        _git(["commit", "-m", "GOV-999: Stage X impl (#123)"], repo)
        g = cm.check_gate2("gov-999-stage-x-impl", repo, do_fetch=False)
        assert g.passed is True
        assert "GOV-999" in g.detail and "contained" in g.detail


# --- GOV-537: guarded squash-merge detector for the gate-2 reclaim lane.
# Every GOV PR merges via squash; the squash commit is not an ancestor of the
# branch and the branch name is absent from the `GOV-NN: ... (#N)` subject, so
# the older heuristics never fire. The detector reclaims a branch ONLY when its
# issue id has an anchored squash subject AND every branch commit is already
# patch-equivalent upstream (`git cherry`). All real-git-repo tests. ---

class TestSquashMergeDetector:

    def _squash_merge(self, repo, branch, subject, fname="g.txt",
                      content="feat\n"):
        """Create `branch` off current main with one commit, squash-merge it
        into main under `subject`. Leaves HEAD on main; branch retained."""
        _git(["checkout", "-b", branch], repo)
        _commit(repo, fname, content, f"{branch} work")
        _git(["checkout", "main"], repo)
        _git(["merge", "--squash", branch], repo)
        _git(["commit", "-m", subject], repo)

    def test_ac1_contained_squash_eligible(self, tmp_path):
        """AC1: a contained squash-merged branch with a `GOV-NN: ... (#N)`
        subject → gate 2 PASS (reclaim eligible)."""
        repo = tmp_path / "ac1"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        self._squash_merge(repo, "gov-1000-impl", "GOV-1000: impl (#84)")
        g = cm.check_gate2("gov-1000-impl", repo, do_fetch=False)
        assert g.passed is True
        assert "GOV-1000" in g.detail

    def test_ac2_two_branches_one_issue_wip_preserved(self, tmp_path):
        """AC2 (decisive non-tautology): two branches share GOV-2000; one is
        squash-merged + contained → eligible; the other carries an extra
        unmerged commit → `git cherry` `+` → PRESERVED. Gate 1 (issue status)
        cannot protect the WIP branch here — only containment can."""
        repo = tmp_path / "ac2"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        # WIP branch off base, BEFORE the squash lands, with its own commit.
        _git(["checkout", "-b", "gov-2000-wip"], repo)
        _commit(repo, "wip.txt", "wip\n", "gov-2000-wip unmerged work")
        _git(["checkout", "main"], repo)
        # Sibling branch, squash-merged and contained.
        self._squash_merge(repo, "gov-2000-merged", "GOV-2000: done (#85)")

        merged = cm.check_gate2("gov-2000-merged", repo, do_fetch=False)
        wip = cm.check_gate2("gov-2000-wip", repo, do_fetch=False)
        assert merged.passed is True   # contained → eligible
        assert wip.passed is False     # has `+` commit → preserved

    def test_ac3_body_mention_of_other_issue_not_matched(self, tmp_path):
        """AC3: a squash subject for GOV-9999 whose BODY merely mentions the
        branch's GOV-3000 must NOT falsely match (anchor `^GOV-NN`)."""
        repo = tmp_path / "ac3"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        _git(["checkout", "-b", "gov-3000-impl"], repo)
        _commit(repo, "g.txt", "feat\n", "gov-3000-impl work")
        _git(["checkout", "main"], repo)
        _git(["merge", "--squash", "gov-3000-impl"], repo)
        # Subject is a DIFFERENT issue; body mentions GOV-3000.
        _git(["commit", "-m",
              "GOV-9999: unrelated change (#90)\n\nIncidentally relates to GOV-3000."],
             repo)
        g = cm.check_gate2("gov-3000-impl", repo, do_fetch=False)
        assert g.passed is False  # body-mention must not satisfy the anchor

    def test_ac4_gate1_still_required(self, tmp_path):
        """AC4: a contained squash-merged branch whose issue is NOT done is
        still preserved — the detector only sets gate 2; gate 1 is enforced
        independently at the Candidate level."""
        repo = tmp_path / "ac4"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        self._squash_merge(repo, "gov-4000-impl", "GOV-4000: impl (#86)")
        g2 = cm.check_gate2("gov-4000-impl", repo, do_fetch=False)
        assert g2.passed is True  # gate 2 alone is satisfied
        # but with gate 1 failing (issue not done), the candidate is preserved:
        c = cm.Candidate(
            repo_root=str(repo), branch="gov-4000-impl", issue_id="GOV-4000",
            worktree_path=None,
            gate1_issue_done=cm.GateResult(False, "GOV-4000 status=in_progress"),
            gate2_merged=g2,
            gate3_clean=cm.GateResult(True, "clean"),
            gate4_safe_path=cm.GateResult(True, "safe"),
        )
        assert c.all_gates_pass is False
        assert c.action == "preserve"

    def test_ac5_squash_subject_but_uncontained_preserved(self, tmp_path):
        """AC5 variant: a branch whose squash subject exists in main but which
        carries an extra post-merge commit (uncontained) → PRESERVED. Keeps the
        uncontained-preserve guarantee alongside the new eligible behavior."""
        repo = tmp_path / "ac5"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        self._squash_merge(repo, "gov-5000-impl", "GOV-5000: impl (#87)")
        # Extra unmerged commit lands on the branch AFTER the squash.
        _git(["checkout", "gov-5000-impl"], repo)
        _commit(repo, "h.txt", "more\n", "post-merge work Z")
        _git(["checkout", "main"], repo)
        g = cm.check_gate2("gov-5000-impl", repo, do_fetch=False)
        assert g.passed is False  # `+` commit → not contained → preserved

    def test_ac7_red_proof_neuter_containment_guard(self, tmp_path,
                                                    monkeypatch):
        """AC7 RED-proof: neutering the containment guard (force it True) makes
        the AC2 WIP branch FALSELY eligible — proving the guard, not the
        subject match, is what prevents the false delete. Restored by
        monkeypatch teardown (byte-identical source)."""
        repo = tmp_path / "ac7"
        _init_work_repo(repo)
        _commit(repo, "f.txt", "base\n", "base")
        _git(["checkout", "-b", "gov-7000-wip"], repo)
        _commit(repo, "wip.txt", "wip\n", "gov-7000-wip unmerged work")
        _git(["checkout", "main"], repo)
        self._squash_merge(repo, "gov-7000-merged", "GOV-7000: done (#88)")

        # Sanity: with the real guard, the WIP branch is preserved.
        assert cm.check_gate2("gov-7000-wip", repo, do_fetch=False).passed is False

        # Neuter the guard → the WIP branch (subject matches GOV-7000) now
        # falsely passes gate 2. This is the regression the guard prevents.
        monkeypatch.setattr(cm, "_branch_content_contained",
                            lambda *a, **k: True)
        assert cm.check_gate2("gov-7000-wip", repo, do_fetch=False).passed is True
