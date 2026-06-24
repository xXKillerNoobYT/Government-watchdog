#!/usr/bin/env python3
"""Post-merge workspace + merged-branch cleanup tool.

Finds per-issue git worktrees and merged local branches safe to remove.
Default mode: dry-run (report only, never delete).  Pass --apply to act.

Safety quad-gate — clean ONLY when ALL four gates pass:
  1. Owning Paperclip issue is done (or cancelled with no unmerged work).
  2. Branch is verified merged into default branch.
  3. Worktree (if any) is clean: no uncommitted/untracked, no unpushed commits.
  4. Path is a real git worktree/branch — never evidence/vault/data.

Any gate fail -> preserve, log reason, emit review-only candidate.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

LOG_DIR = Path(__file__).resolve().parents[1] / "Logs"
LOG_FILE = LOG_DIR / "cleanup-merged-worktrees.log"

# Canonical local control-plane. Used as both the default API base AND a
# last-resort fallback when the configured `--api-url`/`PAPERCLIP_API_URL`
# (e.g. a stale Cloudflare tunnel) is unreachable. See query_issue_status.
LOCALHOST_FALLBACK = "http://127.0.0.1:3100"

PAPERCLIP_API = os.environ.get("PAPERCLIP_API_URL", LOCALHOST_FALLBACK)

ISSUE_RE = re.compile(r"(?i)\bgov[- ]?(\d+)\b")

PROTECTED_PATH_SEGMENTS = frozenset({
    "Docs", "Source-Data", "Paperclip-Backups", "Raw-PDFs",
    "Crawler", "Obsidian Vault", "Database", "vault",
    "source-cache", "raw-cache", "evidence", "local-db",
    ".paperclip", "transcripts",
})

KNOWN_REPO_ROOTS = [
    Path("/Users/IA/Code/Government-watchdog"),
    Path("/Users/IA/Code/Government-watchdog-website"),
]


def _setup_logging(log_path: Path, verbose: bool) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("cleanup-merged-worktrees")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG if verbose else logging.WARNING)
    logger.addHandler(sh)
    return logger


def _run_git(args: list[str], cwd: Path | str | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def extract_issue_id(branch_name: str) -> Optional[str]:
    m = ISSUE_RE.search(branch_name)
    if m:
        return f"GOV-{m.group(1)}"
    return None


@dataclass
class GateResult:
    passed: bool
    detail: str


@dataclass
class Candidate:
    repo_root: str
    branch: str
    issue_id: Optional[str]
    worktree_path: Optional[str]
    gate1_issue_done: GateResult
    gate2_merged: GateResult
    gate3_clean: GateResult
    gate4_safe_path: GateResult
    all_gates_pass: bool = False
    action: str = "preserve"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.all_gates_pass = all([
            self.gate1_issue_done.passed,
            self.gate2_merged.passed,
            self.gate3_clean.passed,
            self.gate4_safe_path.passed,
        ])
        self.action = "remove" if self.all_gates_pass else "preserve"


def _fetch_issue(issue_id: str, api_url: str) -> Optional[dict]:
    url = f"{api_url}/api/issues/{issue_id}"
    try:
        req = Request(url, method="GET")
        req.add_header("Accept", "application/json")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return {"status": data.get("status"), "title": data.get("title", "")}
    except (URLError, OSError, json.JSONDecodeError, KeyError):
        return None


def query_issue_status(issue_id: str, api_url: str) -> Optional[dict]:
    """Look up a Paperclip issue, with a localhost self-heal fallback.

    The configured base (`--api-url` / `PAPERCLIP_API_URL`) may point at a stale
    Cloudflare tunnel. If the primary lookup fails and the base is not already
    the canonical localhost, retry once against `LOCALHOST_FALLBACK` so gate 1
    can still resolve a real status instead of silently preserving every
    candidate. If both fail we return None and gate 1 preserves (safe).
    """
    info = _fetch_issue(issue_id, api_url)
    if info is None and api_url.rstrip("/") != LOCALHOST_FALLBACK:
        info = _fetch_issue(issue_id, LOCALHOST_FALLBACK)
    return info


def check_gate1(issue_id: Optional[str], api_url: str) -> GateResult:
    if issue_id is None:
        return GateResult(False, "no issue ID extractable from branch name")
    info = query_issue_status(issue_id, api_url)
    if info is None:
        return GateResult(False, f"Paperclip API unreachable or issue {issue_id} not found")
    status = info["status"]
    if status == "done":
        return GateResult(True, f"{issue_id} status=done")
    if status == "cancelled":
        return GateResult(True, f"{issue_id} status=cancelled (merge check in gate 2)")
    return GateResult(False, f"{issue_id} status={status} (not done/cancelled)")


def resolve_default_branch(repo_root: Path) -> str:
    """Name of the repo's default branch (e.g. "main"), best-effort."""
    try:
        name = _run_git(
            ["symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_root, check=False,
        ).stdout.strip().replace("refs/remotes/origin/", "")
        return name or "main"
    except Exception:
        return "main"


def _ref_exists(ref: str, repo_root: Path) -> bool:
    r = _run_git(["rev-parse", "--verify", "--quiet", ref],
                 cwd=repo_root, check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def resolve_merge_ref(repo_root: Path, default_branch: str,
                      do_fetch: bool = True) -> str:
    """Authoritative ref to test "merged into default" against.

    Prefer the remote-tracking ref ``origin/<default>`` over the local
    ``<default>`` branch. An operational clone's local default can lag
    ``origin`` by many commits — it may even be checked out on an unrelated
    feature branch — which makes ``git branch --merged <local-default>``
    blind to anything merged upstream after the clone last synced. That
    turns the ``--apply`` lane into a silent permanent no-op (same failure
    shape as the GOV-503/F1 dead-tunnel default).

    A best-effort ``git fetch origin <default>`` refreshes
    ``origin/<default>`` first. The fetch only updates remote-tracking refs
    in ``.git`` — it never touches the working tree or any local branch, so
    it is safe under dry-run. If there is no remote / it is unreachable, or
    ``origin/<default>`` does not exist, fall back to the local branch so the
    gate still works in local-only repos and tests.
    """
    if do_fetch:
        _run_git(["fetch", "origin", default_branch], cwd=repo_root, check=False)
    remote_ref = f"origin/{default_branch}"
    if _ref_exists(remote_ref, repo_root):
        return remote_ref
    return default_branch


def check_gate2(branch: str, repo_root: Path,
                do_fetch: bool = True) -> GateResult:
    default_branch = resolve_default_branch(repo_root)
    merge_ref = resolve_merge_ref(repo_root, default_branch, do_fetch=do_fetch)

    try:
        result = _run_git(["branch", "--merged", merge_ref], cwd=repo_root)
        merged_branches = {
            line.strip().lstrip("* ") for line in result.stdout.splitlines()
        }
        if branch in merged_branches:
            return GateResult(True, f"branch tip is ancestor of {merge_ref}")
    except subprocess.CalledProcessError:
        pass

    try:
        result = _run_git(
            ["log", "--oneline", merge_ref, "--grep", branch[:40]],
            cwd=repo_root, check=False,
        )
        if result.stdout.strip():
            return GateResult(True, f"squash-merge evidence found in {merge_ref} log")
    except Exception:
        pass

    try:
        result = _run_git(
            ["log", f"{merge_ref}..{branch}", "--oneline"],
            cwd=repo_root, check=False,
        )
        if result.returncode == 0 and not result.stdout.strip():
            return GateResult(True, f"no commits ahead of {merge_ref}")
    except Exception:
        pass

    return GateResult(False, f"branch not verified merged into {merge_ref}")


def check_gate3(branch: str, worktree_path: Optional[str],
                repo_root: Path) -> GateResult:
    if worktree_path is None:
        return GateResult(True, "no worktree; branch-only candidate (clean by definition)")

    wt = Path(worktree_path)
    if not wt.exists():
        return GateResult(True, "worktree path no longer exists on disk")

    try:
        status = _run_git(["status", "--porcelain"], cwd=wt)
        if status.stdout.strip():
            lines = status.stdout.strip().splitlines()
            return GateResult(False, f"worktree has {len(lines)} uncommitted/untracked change(s)")
    except subprocess.CalledProcessError as e:
        return GateResult(False, f"git status failed in worktree: {e}")

    try:
        unpushed = _run_git(["log", "@{u}..HEAD", "--oneline"], cwd=wt, check=False)
        if unpushed.returncode == 0 and unpushed.stdout.strip():
            count = len(unpushed.stdout.strip().splitlines())
            return GateResult(False, f"worktree has {count} unpushed commit(s)")
    except Exception:
        pass

    return GateResult(True, "worktree is clean; no uncommitted or unpushed changes")


def check_gate4(worktree_path: Optional[str], branch: str,
                repo_root: Path) -> GateResult:
    if worktree_path:
        wt = Path(worktree_path).resolve()
        wt_str_lower = str(wt).lower()
        hit = {seg for seg in PROTECTED_PATH_SEGMENTS if seg.lower() in wt_str_lower}
        if hit:
            return GateResult(False, f"worktree path contains protected segment(s): {hit}")

        known = False
        for root in KNOWN_REPO_ROOTS:
            try:
                wt.relative_to(root.resolve())
                known = True
                break
            except ValueError:
                pass
        if not known:
            wt_list = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root, check=False)
            if str(wt) not in wt_list.stdout:
                return GateResult(False, f"worktree path {wt} is not a git-registered worktree")

    if branch in ("main", "master", "develop"):
        return GateResult(False, f"refusing to clean default/protected branch '{branch}'")

    return GateResult(True, "path is a real git worktree/branch, not evidence/vault/data")


def discover_candidates(repo_root: Path, api_url: str,
                        log: logging.Logger) -> list[Candidate]:
    candidates: list[Candidate] = []
    repo_root = repo_root.resolve()

    if not repo_root.exists():
        log.warning("repo root does not exist: %s", repo_root)
        return []

    wt_result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root, check=False)
    worktree_map: dict[str, str] = {}
    current_wt = None
    for line in wt_result.stdout.splitlines():
        if line.startswith("worktree "):
            current_wt = line[len("worktree "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].replace("refs/heads/", "")
            if current_wt:
                worktree_map[branch] = current_wt
            current_wt = None

    branch_result = _run_git(["branch", "--format=%(refname:short)"], cwd=repo_root, check=False)
    all_branches = [b.strip() for b in branch_result.stdout.splitlines() if b.strip()]

    seen_branches = set()
    for branch in all_branches:
        if branch in ("main", "master", "develop"):
            continue
        seen_branches.add(branch)
        issue_id = extract_issue_id(branch)
        wt_path = worktree_map.get(branch)

        log.info("evaluating branch=%s issue=%s worktree=%s", branch, issue_id, wt_path)

        g1 = check_gate1(issue_id, api_url)
        g2 = check_gate2(branch, repo_root)
        g3 = check_gate3(branch, wt_path, repo_root)
        g4 = check_gate4(wt_path, branch, repo_root)

        c = Candidate(
            repo_root=str(repo_root),
            branch=branch,
            issue_id=issue_id,
            worktree_path=wt_path,
            gate1_issue_done=g1,
            gate2_merged=g2,
            gate3_clean=g3,
            gate4_safe_path=g4,
        )
        log.info("  gate1=%s gate2=%s gate3=%s gate4=%s → %s",
                 g1.passed, g2.passed, g3.passed, g4.passed, c.action)
        candidates.append(c)

    return candidates


def remove_worktree(worktree_path: str, repo_root: Path,
                    log: logging.Logger) -> bool:
    try:
        _run_git(["worktree", "remove", worktree_path], cwd=repo_root)
        log.info("removed worktree: %s", worktree_path)
        return True
    except subprocess.CalledProcessError as e:
        log.error("failed to remove worktree %s: %s", worktree_path, e.stderr)
        return False


def remove_branch(branch: str, repo_root: Path,
                  log: logging.Logger) -> bool:
    try:
        _run_git(["branch", "-d", branch], cwd=repo_root)
        log.info("deleted branch: %s", branch)
        return True
    except subprocess.CalledProcessError as e:
        log.error("failed to delete branch %s: %s", branch, e.stderr)
        return False


def execute_cleanup(candidates: list[Candidate], apply: bool,
                    log: logging.Logger) -> dict:
    removed_worktrees: list[str] = []
    removed_branches: list[str] = []
    failed: list[dict] = []
    preserved: list[dict] = []

    for c in candidates:
        if not c.all_gates_pass:
            reasons = []
            if not c.gate1_issue_done.passed:
                reasons.append(f"gate1: {c.gate1_issue_done.detail}")
            if not c.gate2_merged.passed:
                reasons.append(f"gate2: {c.gate2_merged.detail}")
            if not c.gate3_clean.passed:
                reasons.append(f"gate3: {c.gate3_clean.detail}")
            if not c.gate4_safe_path.passed:
                reasons.append(f"gate4: {c.gate4_safe_path.detail}")
            preserved.append({
                "branch": c.branch, "issue": c.issue_id,
                "worktree": c.worktree_path, "reasons": reasons,
            })
            log.info("PRESERVE %s — %s", c.branch, "; ".join(reasons))
            continue

        if not apply:
            log.info("DRY-RUN would remove: branch=%s worktree=%s", c.branch, c.worktree_path)
            removed_branches.append(c.branch)
            if c.worktree_path:
                removed_worktrees.append(c.worktree_path)
            continue

        repo = Path(c.repo_root)
        if c.worktree_path:
            if not remove_worktree(c.worktree_path, repo, log):
                failed.append({"branch": c.branch, "worktree": c.worktree_path,
                                "error": "worktree removal failed"})
                continue
            removed_worktrees.append(c.worktree_path)

        if not remove_branch(c.branch, repo, log):
            failed.append({"branch": c.branch, "error": "branch deletion failed"})
            continue
        removed_branches.append(c.branch)

    return {
        "removed_worktrees": removed_worktrees,
        "removed_branches": removed_branches,
        "preserved": preserved,
        "failed": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Post-merge workspace + merged-branch cleanup tool. "
                    "Finds per-issue git worktrees and merged branches safe to remove.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually remove worktrees/branches. Default is dry-run.")
    parser.add_argument("--repo", action="append", type=Path,
                        help="Git repo root to scan. Repeatable. "
                             "Default: both GOV repos.")
    parser.add_argument("--api-url", default=PAPERCLIP_API,
                        help="Paperclip API base URL for issue status queries.")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON output.")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug-level logging.")
    parser.add_argument("--log-file", type=Path, default=LOG_FILE,
                        help="Path to the log file.")
    args = parser.parse_args()

    log = _setup_logging(args.log_file, args.verbose)

    repos = args.repo if args.repo else KNOWN_REPO_ROOTS
    mode = "apply" if args.apply else "dry-run"
    log.info("=== cleanup-merged-worktrees %s | repos=%s | api=%s ===",
             mode, [str(r) for r in repos], args.api_url)

    all_candidates: list[Candidate] = []
    for repo in repos:
        all_candidates.extend(discover_candidates(repo, args.api_url, log))

    result = execute_cleanup(all_candidates, args.apply, log)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "repos_scanned": [str(r) for r in repos],
        "total_candidates": len(all_candidates),
        "would_remove_branches": len(result["removed_branches"]),
        "would_remove_worktrees": len(result["removed_worktrees"]),
        "preserved_count": len(result["preserved"]),
        "failed_count": len(result["failed"]),
        "removed_branches": result["removed_branches"],
        "removed_worktrees": result["removed_worktrees"],
        "preserved": result["preserved"],
        "failed": result["failed"],
        "candidates": [asdict(c) for c in all_candidates],
    }

    log.info("summary: candidates=%d remove=%d preserve=%d failed=%d",
             len(all_candidates), len(result["removed_branches"]),
             len(result["preserved"]), len(result["failed"]))

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"GOV post-merge cleanup ({mode})")
        print(f"Repos: {', '.join(str(r) for r in repos)}")
        print(f"Candidates: {len(all_candidates)} | "
              f"Remove: {len(result['removed_branches'])} branches, "
              f"{len(result['removed_worktrees'])} worktrees | "
              f"Preserve: {len(result['preserved'])} | "
              f"Failed: {len(result['failed'])}")

        if result["removed_branches"]:
            verb = "Removed" if args.apply else "Would remove"
            print(f"\n{verb} branches:")
            for b in result["removed_branches"]:
                print(f"  - {b}")
        if result["removed_worktrees"]:
            verb = "Removed" if args.apply else "Would remove"
            print(f"\n{verb} worktrees:")
            for w in result["removed_worktrees"]:
                print(f"  - {w}")
        if result["preserved"]:
            print("\nPreserved (gate failures):")
            for p in result["preserved"]:
                print(f"  - {p['branch']} ({p['issue'] or 'no-issue'})")
                for r in p["reasons"]:
                    print(f"      {r}")
        if result["failed"]:
            print("\nFailed removals (escalate to CEO/CTO):")
            for f in result["failed"]:
                print(f"  - {f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
