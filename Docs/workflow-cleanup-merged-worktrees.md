# Workflow: Post-Merge Workspace + Merged-Branch Cleanup

**Owner:** AutomationOpsEngineer
**Script:** `scripts/cleanup_merged_worktrees.py`
**Tests:** `tests/test_cleanup_merged_worktrees.py`
**Origin:** GOV-213 (child of GOV-211)

---

## Target

Find per-issue git worktrees and merged local branches that are safe to remove after issues close. Default to dry-run; never auto-delete without explicit `--apply`.

## Trigger

The cleanup script is invoked from three integration points. All three call
the same script with the same default (`--dry-run`), so the safety quad-gate
in `cleanup_merged_worktrees.py` is enforced uniformly. There is no
duplicate cleanup logic in any trigger.

### Trigger A — Event-driven: GitHub Actions post-merge hook (DRY-RUN ONLY)

Defined per-repo in `.github/workflows/post-merge-cleanup.yml`:

| Repo | Workflow path | Runner labels |
|---|---|---|
| Backend (`xXKillerNoobYT/Government-watchdog`) | `.github/workflows/post-merge-cleanup.yml` | `self-hosted, macOS, ARM64, government-watchdog, gov-backend` |
| Website (`xXKillerNoobYT/Government-watchdog-website`) | `.github/workflows/post-merge-cleanup.yml` | `self-hosted, macOS, ARM64, government-watchdog, gov-website` |

- **Fires on:** `push` to `main` (which is the moment a PR merges on GitHub) plus `workflow_dispatch`.
- **Runs:** `python3 scripts/cleanup_merged_worktrees.py --api-url http://127.0.0.1:3100 --json` (Backend); Website invokes the same script at its absolute Backend path because both checkouts live on the same self-hosted Mac runner host.
- **Mode:** dry-run only. CI MUST NOT pass `--apply`. The per-job artifact (`Logs/post-merge-cleanup-<ts>.json`) is uploaded for review.
- **Cross-repo coverage:** the script's `KNOWN_REPO_ROOTS` sweeps both repos in a single invocation, so either repo's merge event fully covers both checkouts. Duplicate runs from near-simultaneous merges are serialised via the `concurrency: post-merge-cleanup` group.
- **Why dry-run only:** per `CTO_WORKFLOWS.md` hard stop, no `--apply` from automation without a CEO-approved plan.

### Trigger B — Cadence-driven: Paperclip daily routine (GATED APPLY LANE)

Owned by the existing Paperclip routine `804d7f7c-89c4-47a1-9146-32245c31ae6a`
(*Daily GOV local data cleanup review*), assignee
**AutomationOpsEngineer** (`b9611d2e-d5d0-438e-9081-99f94cd65f06`).

The routine description is extended to add a **post-merge cleanup lane**
after the existing junk-cleanup steps:

1. Dry-run: `python3 /Users/IA/Code/Government-watchdog/scripts/cleanup_merged_worktrees.py --api-url http://127.0.0.1:3100 --json`
2. Review preserved candidates and any failed removals.
3. If review confirms safety, run `--apply` once per day; otherwise leave preserved candidates as review-only.
4. Comment the day's outcome on the routine's execution issue.

- **Fires on:** daily routine cadence.
- **Mode:** dry-run → human review → optional `--apply` by the assignee.
- **Why this is the apply lane:** the assignee already reviews the daily junk-cleanup output before applying. Reusing that reviewer-gated lane keeps `--apply` inside a CEO-approved, AutomationOps-owned process and out of CI.

### Trigger C — Coding-process awareness: post-`done` agent expectation

When a coder agent moves an issue to `done` after a merged PR, no manual
cleanup step is required. The combination of Trigger A (immediate dry-run
visibility on the post-merge push) and Trigger B (daily reviewer-gated
apply) covers the cleanup automatically. Agents must NOT call the cleanup
script themselves during normal issue close-out — that would bypass the
reviewer gate and the concurrency group.

### Which fires when

| Event | A (CI dry-run) | B (daily apply lane) | C (agent close-out) |
|---|---|---|---|
| Agent closes issue without a merged PR | n/a | preserves (gate 2 fails) | no script call |
| PR merges to `main` | fires immediately, dry-run only | swept on next daily run | no script call |
| Manual local merge (no PR) | does not fire | swept on next daily run | no script call |
| Orphan branch (no Paperclip issue) | dry-run flags review-only | preserved (gate 1 fails), threshold may create issue | no script call |
| Worktree lock / dirty / unsafe path | preserved at any trigger (gate 3 or 4) | preserved at any trigger | no script call |

### Safety quad-gate enforcement at integration

Every trigger calls the same script binary. The script is the sole owner of
gate evaluation (Issue done, Branch merged, Worktree clean, Safe path). No
trigger may pre-filter, bypass, or rewrite the gates. CI cannot pass
`--apply`. Trigger B's `--apply` invocation is the only place removals
occur, and only after AutomationOpsEngineer review.

## Input contract

| Input | Source | Required |
|---|---|---|
| Git repo roots | `--repo` flag or defaults (`/Users/IA/Code/Government-watchdog`, `/Users/IA/Code/Government-watchdog-website`) | Yes (defaults provided) |
| Paperclip API URL | `--api-url` or `PAPERCLIP_API_URL` env var (default `http://127.0.0.1:3100`) | Yes for gate 1 |

> **GOV-503 / F1 — dead-tunnel landmine + self-heal.** The runner host's
> `PAPERCLIP_API_URL` env var points at a stale Cloudflare tunnel
> (`chancellor-dom-consumers-figures.trycloudflare.com:3100`). Because the env
> var overrides the `127.0.0.1:3100` literal default, any invocation that omits
> `--api-url` inherited the dead base → gate 1 returned `None` → **every**
> candidate was preserved, making the daily apply lane a permanent no-op.
> Mitigations now in place: (1) the daily routine and this doc pin
> `--api-url http://127.0.0.1:3100`; (2) `query_issue_status` self-heals — if
> the configured base is unreachable it retries once against
> `LOCALHOST_FALLBACK` (`127.0.0.1:3100`) before giving up. If both fail it
> still returns `None` and gate 1 preserves (safe degradation unchanged).
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
| 2. Branch merged | `git branch --merged origin/<default>` includes the branch, OR a loose squash-grep / no-commits-ahead match, OR the **guarded squash-merge detector** (GOV-537): the branch's `GOV-NN` id has an anchored `^GOV-NN … (#N)` squash subject in `origin/<default>` **and** `git cherry` reports zero `+` commits (all branch content patch-equivalent upstream) | Preserve; log "not verified merged into origin/<default>" |
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

### Failure handling at integration points

| Failure | Trigger A (CI) | Trigger B (daily routine) |
|---|---|---|
| Paperclip API unreachable from runner host | Job still succeeds (gate 1 preserves all); artifact records zero candidates eligible for removal | Routine continues; assignee notes degraded run and does not run `--apply` |
| Cleanup script exits non-zero | CI job fails LOUDLY; artifact still uploaded if produced; CTO is notified via the standard CI-failure path | Routine outputs the error; assignee files a `routine-execution` issue if the failure repeats |
| GitHub Actions runner offline | Workflow queues until runner returns; cadence trigger (B) still covers the gap on the next daily run | Routine fires regardless of CI availability |
| Concurrency collision (two near-simultaneous merges) | Serialised by the `post-merge-cleanup` concurrency group — second run waits, then re-evaluates with fresh state | Daily routine runs at a fixed time; no collision |
| `--apply` accidentally added to CI | Workflow is reviewed at PR time; reviewer must block any change that introduces `--apply` to `.github/workflows/post-merge-cleanup.yml` | n/a |

## Gate 2 compares against `origin/<default>`, not the local default branch (GOV-536 / F2)

> **GOV-536 — stale-local-default-ref landmine + authoritative-ref fix.** Gate 2
> originally compared candidate branches against the **local** default branch
> (`git branch --merged main`). An operational clone's local `main` can lag
> `origin/main` by many commits — and may even be checked out on an unrelated
> feature branch (observed 2026-06-24: `/Users/IA/Code/Government-watchdog` was
> ~30 commits behind on branch `GOV-367-…`). A stale local ref makes
> `--merged` blind to anything merged upstream after the clone last synced, so
> the `--apply` lane silently reclaims nothing — a **permanent no-op**, the same
> failure shape as the GOV-503/F1 dead-tunnel default.
>
> Fix: `resolve_merge_ref()` does a best-effort `git fetch origin <default>` and
> compares against the remote-tracking ref `origin/<default>` (falling back to
> the local branch only when there is no remote / it is unreachable, so
> local-only repos and tests still work). The fetch only updates remote-tracking
> refs in `.git` — it never touches the working tree or any local branch, so it
> is safe under dry-run. RED-proof in `tests/test_cleanup_merged_worktrees.py`
> (`TestCheckGate2OriginMain`): on a clone whose local refs are rewound behind a
> genuinely-merged branch, gate 2 fails with `do_fetch=False` and passes with
> `do_fetch=True`.

## Guarded squash-merge detector (GOV-537, CTO decision A / GOV-536)

Every GOV PR merges via **squash** (#84–#90). A squash commit is not an ancestor
of the merged branch, and the branch name is absent from the `GOV-NN: … (#N)`
squash subject, so the older gate-2 signals (ancestor / branch-name grep /
no-commits-ahead) never fire for this repo's merge style. Net effect before
GOV-537: even with the GOV-536/F2 `origin/<default>` fix and a fully-synced
clone, gate 2 preserved ~every branch forever — the `--apply` reclaim lane was
permanently inert.

GOV-537 adds a **fourth, additive** positive signal inside `check_gate2`
(`_squash_subject_in_default` + `_branch_content_contained`). It never relaxes
gates 1/3/4 or the existing gate-2 checks. A branch is reclaim-eligible via this
path **only when ALL hold**:

1. The branch name yields a `GOV-NN` id (`extract_issue_id`).
2. `origin/<default>` has a commit whose **subject** matches the anchored
   pattern `^GOV-NN\b.*\(#\d+\)` — issue id at subject start **and** a PR ref.
   (Loose `git log --grep` narrows; the anchor is re-checked in Python against
   `%s` so a body-mention of a *different* `GOV-MM` cannot satisfy it — AC3.)
3. **Content-containment safeguard (load-bearing):** `git cherry origin/<default>
   <branch>` reports **zero `+`-prefixed commits** — every branch commit is
   already patch-equivalent upstream. Any `+` line → the branch carries content
   the squash did not absorb → **PRESERVE**.
4. Gate 1 (issue `done`/`cancelled`) still passes independently (enforced at the
   `Candidate` level, not inside gate 2).

### Why the containment guard is the load-bearing part

The risk this must not regress is **two-branches-one-issue**: an unmerged WIP
branch sharing a `GOV-NN` id with an already-merged PR must never be deleted.
Gate 1 cannot protect it — the issue can be `done` while a second branch for it
is unmerged. Criterion 3 is what protects it: the WIP branch has `+` commits in
`git cherry` and is preserved. RED-proof
(`test_ac7_red_proof_neuter_containment_guard` + a physical on-disk neuter):
forcing `_branch_content_contained` to `True` makes the WIP branch falsely
eligible → the AC2 test goes RED. The subject match alone is **not** sufficient.

### Conservative by design (known, accepted limitations)

- **Group-squashed multi-commit branches stay preserved.** `git cherry` compares
  per-commit patch-ids; a branch whose *several* commits were squashed into one
  upstream commit has individual patch-ids that don't match the combined squash,
  so it reports `+` and is preserved. The detector reclaims only when containment
  is provable commit-by-commit. This is a deliberate safety bias, not a bug.
- **Squash subjects without a `(#N)` PR ref are declined** (criterion 2 requires
  the anchor `(#\d+)`). Observed on `gov93-allowlist-gate` below.
- A failed/odd `git cherry` (non-zero exit) → `False` → preserve (fail-closed).

### CTO-reviewable `--apply` dry-run on the real clone (2026-06-24)

Dry-run (default mode, **no `--apply`**) of the GOV-537 script against
`/Users/IA/Code/Government-watchdog` after `git fetch origin main`:

| Branch | issue gate1 | `git cherry origin/main` | anchored `(#N)` subject | gate 2 | disposition |
|---|---|---|---|---|---|
| GOV-362-stage3-03-source-inventory-contract | done | `+0 / -1` (contained) | `GOV-362: … (#64)` ✓ | **PASS** | **eligible** |
| GOV-363-stage3-04-raw-preservation-contract | done | `+0 / -1` (contained) | `GOV-363: … (#65)` ✓ | **PASS** | **eligible** |
| GOV-67-stage-1-15-…-escalation-contract | done | `+0 / -1` (contained) | `GOV-67: … (#5)` ✓ | **PASS** | **eligible** |
| GOV-367-stage3-04-raw-preservation-auditor | done | **`+1`** (uncontained) | `GOV-367: … (#68)` ✓ | FAIL | **preserved** — divergent local tip; guard catch |
| gov215-post-merge-cleanup-trigger | — | **`+2`** (uncontained) | `GOV-215: … (#44)` ✓ | FAIL | **preserved** — divergent local tip; guard catch |
| gov93-allowlist-gate | — | `+0 / -1` (contained) | `GOV-93: …` (no `(#N)`) ✗ | FAIL | **preserved** — subject lacks PR ref |

Result: **3 of the previously-stuck branches become eligible; no uncontained
branch is listed as eligible.** GOV-367 (`+1`) and gov215 (`+2`) have a matching
squash subject but a divergent local tip — exactly the two-branches/divergent
case the guard exists for. They are correctly preserved on live data, not by a
synthetic test. **`--apply` does not run until CTO reviews this transcript.**

## Issue-creation threshold

Create a Paperclip issue when:
- 3+ consecutive runs show the same failed removal (stuck worktree lock)
- A branch extraction yields an issue ID that does not exist in Paperclip (orphan branch)
- A worktree path matches a protected segment (possible misconfiguration)

## Review cadence

- After each on-demand run: operator reviews stdout/log
- **After each Trigger A run (per merge):** AutomationOpsEngineer skims the GitHub Actions step summary; if `failed_count > 0` or `would_remove_branches > 0`, they note it for the next daily routine review. CI-only inspection of the artifact does not authorise `--apply`.
- **Daily (Trigger B):** AutomationOpsEngineer reviews dry-run output before optional `--apply`. Comments outcome on the routine's execution issue.
- Weekly during active development: check `Logs/cleanup-merged-worktrees.log` for accumulating preserved candidates
- **First `--apply` run (one-time gate):** CTO reviews dry-run output and confirms before AutomationOpsEngineer first issues `--apply` in the daily routine. After that one-time gate, daily reviewer-gated `--apply` is the steady-state.

## Named owner

| Role | Owner | Scope |
|---|---|---|
| Script + tests + tool maintenance | AutomationOpsEngineer (`b9611d2e-d5d0-438e-9081-99f94cd65f06`) | `scripts/cleanup_merged_worktrees.py`, `tests/test_cleanup_merged_worktrees.py`, this workflow doc |
| Daily routine (Trigger B, the apply lane) | AutomationOpsEngineer via Paperclip routine `804d7f7c-89c4-47a1-9146-32245c31ae6a` | Daily dry-run review → reviewer-gated `--apply` |
| CI workflows (Trigger A, dry-run only) | CTO (`24fddc65-edca-462b-8647-61b596c8a46f`) | `.github/workflows/post-merge-cleanup.yml` in both Backend and Website repos |
| First `--apply` one-time gate | CTO | One-time review before AutomationOpsEngineer first issues `--apply` |
| Owner-level scope/policy changes | CEO (`e618342a-fd40-46f9-918a-b562e8948b87`) | Anything that loosens the quad-gate or enables automated `--apply` outside Trigger B |

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
