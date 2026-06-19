#!/usr/bin/env python3
"""Government Watchdog local junk/log cleanup.

Default mode is a dry run. This tool removes only approved local junk/data/log
paths and skips git-tracked files unless explicitly told otherwise.

Policy:
- Tools/code may be versioned.
- Raw gathered data, crawler outputs, local databases, generated intermediate
  evidence, run logs, and junk caches stay local/vault-only.
- Logs older than 3 days and junk data that is not needed should be cleaned from
  the computer.
- Website/public data must be processed, reviewed, selected, and website-ready.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_RETENTION_DAYS = 3
REPO_ROOT = Path(__file__).resolve().parents[1]

# Vault scope decision (GOV-54, 2026-06-08):
# The Obsidian GOV vault project was renamed
#   Government-Watchdog -> "Government-Watchdog v1 Plans"
# as part of the intentional 2026-06-07 hard reset / v1 archival. An archived
# "v1 Plans" folder is review-only plans/evidence (it still holds Logs/, Crawler/,
# Raw-PDFs/, Paperclip-Backups/, Docs/, and a local DB), so a deletion tool must
# NOT be pointed at it. We therefore deliberately keep the vault OUT of cleanup
# scope: the only default root is this backend repo. If a future *active* vault
# ever needs routine cleanup, add it here via a reviewed code change — do not
# re-point cleanup at archived plans.
#
# Only these roots may be cleaned. Never add broad home/tmp roots here.
ALLOWED_ROOTS = [REPO_ROOT]

# Directories that are junk/local-output by policy. Existing tracked files inside
# these directories are reported and skipped unless --include-tracked is passed.
CLEAN_DIR_NAMES = {
    "Logs",
    "logs",
    "run-logs",
    "runs",
    "tmp",
    "temp",
    "cache",
    ".cache",
    "crawl-output",
    "crawl-outputs",
    "crawler-output",
    "crawler-outputs",
    "scrapes",
    "scraped-data",
    "source-cache",
    "raw-cache",
    "downloads",
    "exports",
    "generated",
    "artifacts",
}

# File suffixes that are local/junk unless deliberately tracked as fixtures.
CLEAN_SUFFIXES = {
    ".log",
    ".tmp",
    ".temp",
    ".cache",
    ".ndjson",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db-journal",
    ".db-wal",
    ".db-shm",
    ".duckdb",
    ".wal",
}

PROTECTED_DIR_NAMES = {
    ".git",
    ".github",
    ".venv",
    "venv",
    "node_modules",
    "Database/migrations",
}

# Owner-retained-artifact guard (GOV-272), mirroring cleanup_merged_worktrees.py
# gate-4. cleanup_junk keys its safety on git-*tracked* status, but a deliberately
# git-*ignored* retained-evidence dir (e.g. Logs/gov215-evidence/, .gitignore'd)
# is "untracked" to git and would otherwise be deleted by a blanket --apply.
# A path is treated as owner-retained — preserved, never deleted, even with
# --include-tracked — when EITHER:
#   1. any path segment contains a retained substring (e.g. "evidence"), or
#   2. a keep-marker file sits in the path or any ancestor up to the repo root.
# NOTE: being git-ignored is deliberately NOT sufficient to retain — this tool's
# whole job is to clean git-ignored junk (logs, caches, local DBs are all
# git-ignored). Only the explicit "evidence" segment or a keep-marker retains.
RETAINED_SEGMENT_SUBSTRINGS = ("evidence",)
KEEP_MARKER_NAMES = {".cleanup-keep"}

@dataclass
class Candidate:
    path: Path
    reason: str
    age_days: float
    tracked: bool
    size_bytes: int
    retained: bool = False


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_allowed(path: Path) -> None:
    if not any(is_under(path, root) for root in ALLOWED_ROOTS):
        raise SystemExit(f"Refusing to scan outside allowed GOV roots: {path}")


def git_tracked_files(repo_root: Path) -> set[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
    except Exception:
        return set()
    paths = set()
    for raw in result.stdout.split(b"\0"):
        if raw:
            paths.add((repo_root / raw.decode()).resolve())
    return paths


def file_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def protected(path: Path) -> bool:
    parts = set(path.parts)
    if parts & PROTECTED_DIR_NAMES:
        return True
    # Keep source docs/specs/code by default, even if old.
    if path.suffix in {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sql"}:
        if not any(part in CLEAN_DIR_NAMES for part in path.parts):
            return True
    return False


def age_days(path: Path, now: float) -> float:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    return max(0.0, (now - mtime) / 86400.0)


def tracked(path: Path, tracked_set: set[Path]) -> bool:
    resolved = path.resolve()
    if resolved in tracked_set:
        return True
    if path.is_dir():
        return any(is_under(t, resolved) for t in tracked_set)
    return False


def retained_evidence(path: Path, repo_root: Path) -> tuple[bool, str]:
    """Owner-retained-artifact guard (GOV-272), mirroring cleanup_merged_worktrees
    gate-4. Returns (is_retained, reason). A retained path must never be deleted.

    Retained when EITHER a path segment contains a retained substring
    (e.g. 'evidence'), OR a keep-marker file sits in the path or any ancestor
    directory up to (and including) the repo root.
    """
    resolved = path.resolve()
    root = repo_root.resolve()
    # Only scan segments the tool actually governs — those relative to repo_root.
    # Scanning the whole absolute path would let an unrelated ancestor dir (e.g.
    # ~/evidence-backups/) protect everything under it.
    try:
        scan_parts = resolved.relative_to(root).parts
    except ValueError:
        scan_parts = resolved.parts
    for part in scan_parts:
        low = part.lower()
        for needle in RETAINED_SEGMENT_SUBSTRINGS:
            if needle in low:
                return True, f"retained owner evidence: path segment '{part}' contains '{needle}'"
    cur = resolved if resolved.is_dir() else resolved.parent
    while is_under(cur, root):
        for marker in KEEP_MARKER_NAMES:
            if (cur / marker).exists():
                return True, f"retained owner evidence: keep-marker '{marker}' present in {cur}"
        if cur == root:
            break
        cur = cur.parent
    return False, ""


def iter_candidates(scan_roots: Iterable[Path], retention_days: int, include_tracked: bool) -> list[Candidate]:
    now = datetime.now(timezone.utc).timestamp()
    tracked_set = git_tracked_files(REPO_ROOT)
    candidates: list[Candidate] = []
    for root in scan_roots:
        root = root.expanduser().resolve()
        ensure_allowed(root)
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if protected(path):
                continue
            reason = None
            if path.is_dir() and path.name in CLEAN_DIR_NAMES:
                reason = f"junk directory '{path.name}'"
            elif path.is_file() and (path.suffix in CLEAN_SUFFIXES or any(part in CLEAN_DIR_NAMES for part in path.parts)):
                reason = "old local junk/log/db/cache file"
            if not reason:
                continue
            days = age_days(path, now)
            if days < retention_days:
                continue
            is_tracked = tracked(path, tracked_set)
            if is_tracked and not include_tracked:
                reason += " (git-tracked; report-only unless --include-tracked)"
            is_retained, retained_reason = retained_evidence(path, REPO_ROOT)
            if is_retained:
                reason += f" ({retained_reason}; preserved, never deleted)"
            candidates.append(Candidate(path, reason, days, is_tracked, file_size(path), retained=is_retained))
    # Delete children before parents.
    return sorted(candidates, key=lambda c: len(c.path.parts), reverse=True)


def remove_candidate(candidate: Candidate, include_tracked: bool, include_databases: bool, include_markdown_logs: bool) -> bool:
    # Owner-retained-evidence guard (GOV-272): never delete, not even with
    # --include-tracked. Re-derive at delete time so a directly-passed Candidate
    # is also protected, mirroring the cleanup_merged_worktrees gate-4 contract.
    if candidate.retained or retained_evidence(candidate.path, REPO_ROOT)[0]:
        return False
    if candidate.tracked and not include_tracked:
        return False
    db_suffixes = {".sqlite", ".sqlite3", ".db", ".duckdb", ".wal", ".db-journal", ".db-wal", ".db-shm"}
    if candidate.path.is_file() and candidate.path.suffix in db_suffixes and not include_databases:
        return False
    if candidate.path.is_file() and candidate.path.suffix == ".md" and any(part in CLEAN_DIR_NAMES for part in candidate.path.parts) and not include_markdown_logs:
        return False
    if candidate.path.is_dir():
        # Do not delete a directory if it still contains review-only markdown/
        # database files, or any owner-retained evidence (GOV-272). Without the
        # evidence check, wholesale rmtree of a parent (e.g. Logs/) would nuke a
        # retained subdir (e.g. Logs/gov215-evidence/) that survives as its own
        # candidate but lives under a deletable parent.
        for child in candidate.path.rglob("*"):
            if retained_evidence(child, REPO_ROOT)[0]:
                return False
            if child.is_file():
                if child.suffix in db_suffixes and not include_databases:
                    return False
                if child.suffix == ".md" and not include_markdown_logs:
                    return False
        shutil.rmtree(candidate.path)
    elif candidate.path.exists():
        candidate.path.unlink()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean GOV local junk/log data older than retention window.")
    parser.add_argument("--apply", action="store_true", help="Actually delete candidates. Default is dry-run.")
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS, help="Minimum age to delete/report. Default: 3.")
    parser.add_argument("--include-tracked", action="store_true", help="Allow deletion of git-tracked junk/log files. Use only after review.")
    parser.add_argument("--include-databases", action="store_true", help="Allow deletion of old local database files. Default is report-only because DBs may contain state/evidence.")
    parser.add_argument("--include-markdown-logs", action="store_true", help="Allow deletion of markdown notes under log folders. Default is report-only because Obsidian notes may contain evidence.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary.")
    parser.add_argument("--scan-root", action="append", type=Path, help="Additional approved scan root under an ALLOWED_ROOTS entry. Can repeat.")
    args = parser.parse_args()

    # Default scope is the backend repo only. The archived GOV v1 vault is
    # intentionally excluded (see ALLOWED_ROOTS note above, GOV-54).
    roots = [REPO_ROOT]
    if args.scan_root:
        roots.extend(args.scan_root)

    # Surface configured roots that do not exist. iter_candidates silently skips
    # missing roots (see `if not root.exists(): continue`), which would let a
    # renamed/moved root quietly drop out of cleanup coverage forever. A missing
    # root is a misconfiguration the operator must see, not swallow.
    missing_roots = []
    for r in roots:
        rp = Path(r).expanduser()
        if not rp.exists():
            missing_roots.append(str(rp))
    for mr in missing_roots:
        print(f"WARNING: configured scan root does not exist; coverage reduced: {mr}", file=sys.stderr)

    candidates = iter_candidates(roots, args.retention_days, args.include_tracked)
    deleted = []
    skipped = []
    for c in candidates:
        did_delete = False
        if args.apply:
            did_delete = remove_candidate(c, args.include_tracked, args.include_databases, args.include_markdown_logs)
        row = {
            "path": str(c.path),
            "reason": c.reason,
            "age_days": round(c.age_days, 2),
            "tracked": c.tracked,
            "size_bytes": c.size_bytes,
        }
        if did_delete:
            deleted.append(row)
        else:
            skipped.append(row)

    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "retention_days": args.retention_days,
        "include_tracked": args.include_tracked,
        "missing_roots": missing_roots,
        "candidate_count": len(candidates),
        "deleted_count": len(deleted),
        "skipped_or_reported_count": len(skipped),
        "retained_evidence_count": sum(1 for c in candidates if c.retained),
        "deleted_bytes": sum(x["size_bytes"] for x in deleted),
        "reported_bytes": sum(x["size_bytes"] for x in skipped),
        "deleted": deleted,
        "skipped_or_reported": skipped,
    }

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"GOV cleanup {summary['mode']} — retention {args.retention_days} days")
        print(f"Candidates: {len(candidates)} | Deleted: {len(deleted)} | Reported/skipped: {len(skipped)}")
        if missing_roots:
            print("\nWARNING — configured scan roots missing (coverage reduced):")
            for mr in missing_roots:
                print(f"- {mr}")
        if skipped:
            print("\nReported/skipped:")
            for item in skipped[:200]:
                print(f"- {item['path']} | {item['age_days']}d | tracked={item['tracked']} | {item['reason']}")
        if deleted:
            print("\nDeleted:")
            for item in deleted[:200]:
                print(f"- {item['path']} | {item['age_days']}d | {item['size_bytes']} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
