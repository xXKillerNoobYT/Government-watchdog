# Workflow: Post-Merge Workspace + Merged-Branch Cleanup

**Owner:** AutomationOpsEngineer
**Script:** `scripts/cleanup_merged_worktrees.py`
**Tests:** `tests/test_cleanup_merged_worktrees.py`
**Origin:** GOV-213 (child of GOV-211)

---

## Target

Find per-issue git worktrees and merged local branches that are safe to remove after issues close. Default to dry-run; never auto-delete without explicit `--apply`.

## Trigger

- On-demand: `python3 scripts/cleanup_merged_worktrees.py`
- Future: can be wired into daily cleanup schedule or post-merge hooks (GOV-211 Child B, CTO-owned)

## Input contract

| Input | Source | Required |
|---|---|---|
| Git repo roots | `--repo` flag or defaults (`/Users/IA/Code/Government-watchdog`, `/Users/IA/Code/Government-watchdog-website`) | Yes (defaults provided) |
| Paperclip API URL | `--api-url` or `PAPERCLIP_API_URL` env var (default `http://127.0.0.1:3100`) | Yes for gate 1 |
| Mode | Default `dry-run`; pass `--apply` to execute removals | Yes |

## Output contract

| Output | Location |
|---|---|
| Human-readable summary | stdout |
| Machine-readable JSON | stdout with `--json` flag |
| Run log | `Logs/cleanup-merged-worktrees.log` |

## Log path + artifact path

- **Log:** `Logs/cleanup-merged-worktrees.log` (appended each run)
- **Artifacts:** JSON output via `--json` flag can be redirected to a file

## Expected success summary

```
GOV post-merge cleanup (dry-run)
Repos: /Users/IA/Code/Government-watchdog, /Users/IA/Code/Government-watchdog-website
Candidates: N | Remove: M branches, P worktrees | Preserve: Q | Failed: 0
```

## Safety quad-gate

All four gates must pass for any candidate to be eligible for removal:

| Gate | Check | Failure behavior |
|---|---|---|
| 1. Issue done | Paperclip API `GET /api/issues/{GOV-NNN}` returns `status=done` or `status=cancelled` | Preserve; log "issue not done" |
| 2. Branch merged | `git branch --merged main` includes the branch, or squash-merge evidence found, or no commits ahead of main | Preserve; log "not merged" |
| 3. Worktree clean | `git status --porcelain` empty + no unpushed commits (`git log @{u}..HEAD`) | Preserve; log "dirty worktree" |
| 4. Safe path | Path is a real git worktree/branch, not under protected segments (Obsidian Vault, .paperclip, Database, Source-Data, evidence, vault, etc.) and not main/master/develop | Preserve; log "unsafe path" |

**Any gate failure → preserve the candidate, log the reason, report as review-only.**

## Known failure patterns + retry policy

| Pattern | Behavior |
|---|---|
| Paperclip API unreachable | Gate 1 fails → preserve all candidates. Safe degradation. No retry needed — run again when API is available. |
| Worktree lock/race (`.git/worktrees/<name>/locked`) | Git worktree remove will fail → logged as failed removal. **Never force-remove.** Re-check on next run. |
| Branch delete fails (`-d` safety) | `git branch -d` refuses unmerged branches as a second safety layer. Logged. No retry — investigate manually. |
| Missing repo root | Logged as warning, skipped. Coverage-reduced alert in output. |

## Issue-creation threshold

Create a Paperclip issue when:
- 3+ consecutive runs show the same failed removal (stuck worktree lock)
- A branch extraction yields an issue ID that does not exist in Paperclip (orphan branch)
- A worktree path matches a protected segment (possible misconfiguration)

## Review cadence

- After each on-demand run: operator reviews stdout/log
- Weekly during active development: check `Logs/cleanup-merged-worktrees.log` for accumulating preserved candidates
- Before wiring into automated schedule: CTO reviews dry-run output

## Named owner

AutomationOpsEngineer (GOV-213). CTO coordinates trigger integration (GOV-211 Child B).

## Verification command + evidence path

```bash
# Run tests
python3 -m pytest tests/test_cleanup_merged_worktrees.py -v

# Dry-run against real repos
python3 scripts/cleanup_merged_worktrees.py --api-url http://127.0.0.1:3100

# JSON output for programmatic verification
python3 scripts/cleanup_merged_worktrees.py --api-url http://127.0.0.1:3100 --json

# Check log
cat Logs/cleanup-merged-worktrees.log
```

## Design decision

**New dedicated tool** rather than extending `cleanup_junk.py` because:
1. `cleanup_junk.py` handles file-age-based junk cleanup — fundamentally different safety model
2. This tool requires git operations + Paperclip API queries — different dependencies
3. The four-gate safety requirements warrant focused, independently testable code
4. Separate concerns = separate test suites, separate failure modes, clearer ownership
