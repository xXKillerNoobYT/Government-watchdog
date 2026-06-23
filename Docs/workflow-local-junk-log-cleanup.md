# Workflow: Daily GOV Local Junk/Log/Data Cleanup Review

**Owner:** AutomationOpsEngineer (`b9611d2e-d5d0-438e-9081-99f94cd65f06`)
**Script:** `scripts/cleanup_junk.py`
**Tests:** `tests/test_cleanup_junk.py`
**Paperclip routine:** `804d7f7c-89c4-47a1-9146-32245c31ae6a` (*Daily GOV local data cleanup review*)
**Routine issue (source of truth):** GOV-501
**Post-merge lane:** see `Docs/workflow-cleanup-merged-worktrees.md`

---

> **Provenance note (GOV-503 / F2).** This file is a faithful, versioned mirror
> of the routine that previously lived only in the GOV-501 issue body and a now
> -missing Obsidian note
> (`.../Docs/2026-06-06-Local-Junk-Log-Cleanup-Workflow.md`, no longer in the
> vault or trash). The Paperclip routine `804d7f7c…` and the GOV-501 issue body
> remain the operative source of truth; this repo doc is the durable, checked-in
> reference so the routine no longer depends on a vault path that can disappear.
> No dated history was reconstructed — only the routine steps as they exist
> today were copied here.

## Target

Paperclip-managed daily routine for Government Watchdog local junk/log/data
cleanup. Isaac intent: Paperclip should manage and maintain the Government
Watchdog data intelligently — this is a daily Paperclip routine, not just an
outside Hermes cron.

## Trigger

Daily, via Paperclip routine `804d7f7c-89c4-47a1-9146-32245c31ae6a`, assignee
AutomationOpsEngineer. Dry-run → human review → optional reviewer-gated
`--apply`.

## Daily routine steps

1. **Read the workflow note:** this file (`Docs/workflow-local-junk-log-cleanup.md`).
   *(Previously pointed at a now-missing Obsidian vault note; corrected under
   GOV-503/F2 to this versioned repo doc.)*
2. **Run dry-run first:**
   `python3 /Users/IA/Code/Government-watchdog/scripts/cleanup_junk.py --retention-days 3 --json`
3. **Review candidates intelligently:**
   - ordinary old junk/log/cache/temp files can be cleaned;
   - git-tracked files are report-only unless reviewed;
   - databases and markdown log notes are review-only by default;
   - never delete source evidence, website-ready data, or owner-approved retained artifacts.
4. **If safe, run normal apply:**
   `python3 /Users/IA/Code/Government-watchdog/scripts/cleanup_junk.py --retention-days 3 --apply --json`
5. If review-only candidates remain, create or update a child issue with exact paths/reasons.
6. Comment results on GOV-33 or the routine execution issue: deleted count,
   skipped/review count, bytes, and blockers.
7. **Post-merge cleanup lane (GOV-215):**
   a. Dry-run:
      `python3 /Users/IA/Code/Government-watchdog/scripts/cleanup_merged_worktrees.py --api-url http://127.0.0.1:3100 --json`
   b. Review the JSON: removable candidates (all 4 gates pass) and preserved candidates.
   c. If review confirms safety AND the CTO has signed off the one-time first
      `--apply` gate, run apply (otherwise dry-run only):
      `python3 /Users/IA/Code/Government-watchdog/scripts/cleanup_merged_worktrees.py --api-url http://127.0.0.1:3100 --apply --json`
   d. Comment outcome on the routine execution issue: removable count, preserved
      count, failed count, and any orphan-branch / protected-path / stuck-worktree
      concerns that hit the issue-creation threshold in
      `Docs/workflow-cleanup-merged-worktrees.md`.

   > **GOV-503 / F1.** Step 7a/7c now pin `--api-url http://127.0.0.1:3100`.
   > The host's `PAPERCLIP_API_URL` env var points at a dead Cloudflare tunnel;
   > without an explicit `--api-url` the merged-worktrees script inherited that
   > dead default, gate 1 returned `None`, and every candidate was preserved
   > (a permanent no-op apply lane). The script also self-heals to localhost
   > now (see that script's `query_issue_status`), but pinning the URL keeps
   > the documented command unambiguous.

## Input contract

| Input | Source | Required |
|---|---|---|
| Retention window | `--retention-days N` (routine uses 3) | Yes |
| Mode | Default dry-run; `--apply` to delete | Yes |
| Output format | `--json` for machine-readable | Optional |

## Output contract

| Output | Location |
|---|---|
| Human/JSON summary | stdout |
| Routine evidence captures | `Logs/gov501-routine/` (gitignored, local/vault-only) |

## Log + evidence path

- Raw routine run captures: `Logs/gov501-routine/` — **gitignored** (GOV-503/F4),
  operational/local-only per the data-publication boundary, never committed.

## Issue-creation threshold

- Review-only candidate set requires owner decision (databases, markdown log notes).
- Out-of-scope path appears as a deletion candidate (scope leak).
- Post-merge lane thresholds: see `Docs/workflow-cleanup-merged-worktrees.md`.

## Review cadence

Daily, by AutomationOpsEngineer, before any `--apply`.

## Publication / data boundary

GitHub gets tools/code/specs/tests/sanitized fixtures. Raw gathered data,
crawler outputs, databases, run logs, and unreviewed research stay
local/vault-only. Only processed/reviewed/selected website-ready data may go
public or feed the website.

## Named owner

| Role | Owner |
|---|---|
| Script + tests + this workflow doc | AutomationOpsEngineer (`b9611d2e-d5d0-438e-9081-99f94cd65f06`) |
| Daily routine `804d7f7c…` apply lane | AutomationOpsEngineer |
| Scope/policy changes (loosening any safety default) | CEO (`e618342a-fd40-46f9-918a-b562e8948b87`) |
