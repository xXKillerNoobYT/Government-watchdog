# Coaching proposal — AutomationOpsEngineer (`b9611d2e-d5d0-438e-9081-99f94cd65f06`)

Window 2026-07-09→16. Volume: 6 issues (GOV-680/692/694/703/705/768 — all "Daily GOV local data cleanup
review", all `done`), runs 6 ok / 0 failed. Perfectly clean lane. No behavioral correction; one routine-scope
extension proposed because this agent is the natural owner of the fix for a cross-agent failure cluster.

## Proposal — extend the daily cleanup routine with execution-workspace hygiene

**Evidence for the gap:** three failure modes in the window trace to stale/drifted git state in the shared
Paperclip execution workspaces, none covered by the current cleanup scope:
- `setup_failed: git worktree add …/projects/bcac096e…/0a1832c4…` — CTO Jul 15, VSR Jul 10,
  BackendCrawlerEngineer Jul 10 (stale worktree registrations block new `git worktree add`).
- `workspace_validation_failed` storm Jul 13 — 10 failed runs across CTO/BCE/VSR because the shared
  worktree sat on `GOV-612-owner-decision-…` instead of each issue's expected branch.

**Smallest durable change (routine spec / workflow doc, not AGENTS.md):** add one step to the daily
cleanup checklist:
```
+ Execution-workspace hygiene: for each project execution workspace, run `git worktree prune`; list
+ worktrees and report (do not delete) any registered worktree whose branch does not match a live issue;
+ report the currently checked-out branch of the shared workspace if it differs from its expected branch.
```
Report-only (no destructive deletes) keeps it inside existing cleanup guardrails; acting on drift stays a
CTO decision.

**Replay case:** given a stale worktree registration from a crashed Jul-10-style run → expected: next daily
cleanup report lists it with its path+branch, and the subsequent `git worktree add` failure never happens
(prune cleared it) or is escalated with the exact stale path.

**Next-step owner:** board accepts → CEO patches the "Daily GOV local data cleanup" routine description
(resource `routine:804d7f7c-89c4-47a1-9146-32245c31ae6a`) via an accepted confirmation in a follow-up run.
