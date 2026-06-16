#!/usr/bin/env python3
"""Reap git worktrees and branches whose PRs have been merged.

Designed for Paperclip coding workflows where each issue gets a worktree under
.paperclip/worktrees/<slug>.  Safe defaults: dry-run only, skip unmerged/dirty/
locked worktrees, never touch main.

Works with any GitHub repo — not hardcoded to Government Watchdog.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

WORKTREE_SUBDIR = os.path.join(".paperclip", "worktrees")
PROTECTED_BRANCHES = {"main", "master", "develop"}


@dataclass
class WorktreeInfo:
    path: str
    branch: str
    head: str
    locked: bool = False


@dataclass
class ReapCandidate:
    worktree: WorktreeInfo | None
    branch: str
    pr_number: int | None = None
    merged_at: str | None = None
    reason_skip: str | None = None
    reaped: bool = False
    remote_deleted: bool = False


@dataclass
class ReapResult:
    candidates: list[ReapCandidate] = field(default_factory=list)
    skipped: list[ReapCandidate] = field(default_factory=list)
    reaped: list[ReapCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", file=sys.stderr)


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def get_repo_root() -> Path:
    r = run(["git", "rev-parse", "--show-toplevel"])
    return Path(r.stdout.strip())


def get_main_worktree_root() -> Path:
    """Return the root of the main (bare/non-linked) worktree."""
    r = run(["git", "worktree", "list", "--porcelain"])
    for block in r.stdout.strip().split("\n\n"):
        lines = block.strip().splitlines()
        path_line = [l for l in lines if l.startswith("worktree ")]
        if path_line:
            p = path_line[0].removeprefix("worktree ")
            if not any(l.strip() == "linked" for l in lines):
                return Path(p)
    return get_repo_root()


def parse_worktrees() -> list[WorktreeInfo]:
    r = run(["git", "worktree", "list", "--porcelain"])
    worktrees: list[WorktreeInfo] = []
    for block in r.stdout.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.strip().splitlines()
        path = ""
        head = ""
        branch = ""
        locked = False
        for line in lines:
            if line.startswith("worktree "):
                path = line.removeprefix("worktree ")
            elif line.startswith("HEAD "):
                head = line.removeprefix("HEAD ")
            elif line.startswith("branch "):
                branch = line.removeprefix("branch refs/heads/")
            elif line.strip() == "locked":
                locked = True
        if path and branch:
            worktrees.append(WorktreeInfo(path=path, branch=branch, head=head, locked=locked))
    return worktrees


def fetch_merged_prs(repo: str) -> dict[str, dict]:
    """Return {branch_name: {number, mergedAt}} for all merged PRs."""
    r = run([
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "merged",
        "--limit", "500",
        "--json", "number,headRefName,mergedAt",
    ])
    prs = json.loads(r.stdout)
    return {pr["headRefName"]: {"number": pr["number"], "mergedAt": pr["mergedAt"]} for pr in prs}


def get_repo_nwo() -> str:
    """Get owner/repo from the git remote."""
    r = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    return r.stdout.strip()


def has_uncommitted_changes(worktree_path: str) -> bool:
    r = run(["git", "-C", worktree_path, "status", "--porcelain"], check=False)
    return bool(r.stdout.strip())


def remote_branch_exists(branch: str) -> bool:
    r = run(["git", "ls-remote", "--heads", "origin", branch], check=False)
    return bool(r.stdout.strip())


def remove_worktree(path: str, force: bool = False) -> bool:
    cmd = ["git", "worktree", "remove", path]
    if force:
        cmd.append("--force")
    r = run(cmd, check=False)
    if r.returncode != 0:
        log(f"Failed to remove worktree {path}: {r.stderr.strip()}", "ERROR")
        return False
    return True


def delete_local_branch(branch: str) -> bool:
    r = run(["git", "branch", "-D", branch], check=False)
    if r.returncode != 0:
        log(f"Failed to delete local branch {branch}: {r.stderr.strip()}", "WARN")
        return False
    return True


def delete_remote_branch(branch: str) -> bool:
    r = run(["git", "push", "origin", "--delete", branch], check=False)
    if r.returncode != 0:
        log(f"Remote branch {branch} already gone or failed: {r.stderr.strip()}", "WARN")
        return False
    return True


def discover_candidates(
    worktrees: list[WorktreeInfo],
    merged_prs: dict[str, dict],
    main_root: Path,
    include_orphan_branches: bool = True,
) -> ReapResult:
    result = ReapResult()

    worktree_branches = set()
    for wt in worktrees:
        worktree_branches.add(wt.branch)

        if Path(wt.path) == main_root:
            continue
        if wt.branch in PROTECTED_BRANCHES:
            continue
        if WORKTREE_SUBDIR not in wt.path:
            continue

        pr_info = merged_prs.get(wt.branch)
        candidate = ReapCandidate(
            worktree=wt,
            branch=wt.branch,
            pr_number=pr_info["number"] if pr_info else None,
            merged_at=pr_info["mergedAt"] if pr_info else None,
        )

        if not pr_info:
            candidate.reason_skip = "no merged PR found"
            result.skipped.append(candidate)
            continue

        if wt.locked:
            candidate.reason_skip = "worktree is locked"
            result.skipped.append(candidate)
            continue

        if has_uncommitted_changes(wt.path):
            candidate.reason_skip = "uncommitted changes in worktree"
            result.skipped.append(candidate)
            continue

        result.candidates.append(candidate)

    if include_orphan_branches:
        r = run(["git", "branch", "--format=%(refname:short)"])
        all_local = {b.strip() for b in r.stdout.strip().splitlines() if b.strip()}
        orphan_branches = all_local - worktree_branches - PROTECTED_BRANCHES
        for branch in sorted(orphan_branches):
            pr_info = merged_prs.get(branch)
            if pr_info:
                result.candidates.append(ReapCandidate(
                    worktree=None,
                    branch=branch,
                    pr_number=pr_info["number"],
                    merged_at=pr_info["mergedAt"],
                ))

    return result


def execute_reap(result: ReapResult, dry_run: bool = True) -> ReapResult:
    for c in result.candidates:
        label = f"PR #{c.pr_number}" if c.pr_number else "orphan"
        wt_label = c.worktree.path if c.worktree else "(no worktree)"

        if dry_run:
            log(f"[DRY-RUN] Would reap: branch={c.branch} ({label}) worktree={wt_label}")
            continue

        if c.worktree:
            log(f"Removing worktree: {c.worktree.path}")
            if not remove_worktree(c.worktree.path):
                result.errors.append(f"worktree remove failed: {c.worktree.path}")
                continue

        log(f"Deleting local branch: {c.branch}")
        if not delete_local_branch(c.branch):
            result.errors.append(f"branch delete failed: {c.branch}")
            continue

        c.reaped = True

        if remote_branch_exists(c.branch):
            log(f"Deleting remote branch: {c.branch}")
            c.remote_deleted = delete_remote_branch(c.branch)
        else:
            log(f"Remote branch already gone: {c.branch}")
            c.remote_deleted = True

        result.reaped.append(c)

    return result


def print_summary(result: ReapResult, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    log(f"--- Summary ({mode}) ---")
    log(f"Candidates (merged, clean): {len(result.candidates)}")
    log(f"Skipped (unmerged/dirty/locked): {len(result.skipped)}")
    if not dry_run:
        log(f"Reaped: {len(result.reaped)}")
        log(f"Errors: {len(result.errors)}")

    if result.skipped:
        log("Skipped details:")
        for s in result.skipped:
            log(f"  {s.branch}: {s.reason_skip}")

    if result.candidates and dry_run:
        log("Would reap:")
        for c in result.candidates:
            wt = c.worktree.path if c.worktree else "(branch only)"
            log(f"  {c.branch} (PR #{c.pr_number}) -> {wt}")

    if result.errors:
        log("Errors:")
        for e in result.errors:
            log(f"  {e}", "ERROR")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reap merged worktrees and branches.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually remove worktrees and delete branches (default: dry-run)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Show what would be cleaned without making changes (default)",
    )
    parser.add_argument(
        "--skip-orphan-branches", action="store_true",
        help="Only clean worktrees, skip standalone orphan branches",
    )
    parser.add_argument(
        "--repo", type=str, default=None,
        help="GitHub owner/repo (auto-detected from remote if omitted)",
    )
    args = parser.parse_args()

    dry_run = not args.apply

    log("Starting merged-worktree reaper")
    log(f"Mode: {'DRY-RUN' if dry_run else 'APPLY'}")

    repo = args.repo or get_repo_nwo()
    log(f"Repo: {repo}")

    main_root = get_main_worktree_root()
    log(f"Main worktree: {main_root}")

    log("Fetching merged PRs from GitHub...")
    merged_prs = fetch_merged_prs(repo)
    log(f"Found {len(merged_prs)} merged PRs")

    log("Parsing local worktrees...")
    worktrees = parse_worktrees()
    log(f"Found {len(worktrees)} worktrees")

    result = discover_candidates(
        worktrees, merged_prs, main_root,
        include_orphan_branches=not args.skip_orphan_branches,
    )
    result = execute_reap(result, dry_run=dry_run)

    print_summary(result, dry_run)

    if dry_run and result.candidates:
        log("Re-run with --apply to execute cleanup.")

    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
