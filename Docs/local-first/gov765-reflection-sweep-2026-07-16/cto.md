# Coaching proposal — CTO (`24fddc65-edca-462b-8647-61b596c8a46f`)

Window 2026-07-09→16. Volume: 29 issues touched, runs 36 ok / 9 failed / 6 cancelled. Highest-throughput
agent; failures are environmental+process, not judgment. Two small durable rules proposed.

## Cluster 1 — chain links staged `backlog`/unassigned trigger harness escalation spam

**Evidence:** GOV-723's dependency chain had GOV-743 unassigned (`a5657b0c` on GOV-745), then GOV-744 in
`backlog` with no assignee (`d492cc49` on GOV-748). The harness fired 12 near-identical "Unblock liveness
incident for GOV-723" escalations in ~30s intervals (GOV-724…730 at 19:46–19:49Z, GOV-745…748 at
22:07–22:14Z, same incident key `harness_liveness:…:9092aa17`). CTO and BackendCrawlerEngineer resolved
overlapping duplicates in parallel.

**Smallest durable change (AGENTS.md diff, +3 lines):**
```
+ ## Chain staging liveness rule
+ When staging a blocker chain, every link must be `todo` + assigned (or explicitly parked with a named
+ owner). Never leave a chain link `backlog`/unassigned. When the harness fires multiple escalations with
+ the same incident key, resolve ONE canonical escalation and close the rest as duplicates of it.
```

**Replay case:** given a new 4-leg chain where leg 3 is created in `backlog` unassigned → expected: CTO
activates leg 3 as `todo`+assignee at staging time; if escalations still spawn, exactly one is worked and
the others closed referencing it.

## Cluster 2 — shared execution workspace left on another branch fails other agents' runs

**Evidence:** 4 CTO runs failed Jul 13 `workspace_validation_failed` (expected `GOV-698-agenda-anchoring-cli`
etc., found `GOV-612-owner-decision-…`); same-day BackendCrawlerEngineer x5 and VSR x1 failed identically.
One stale checkout in the shared worktree (`…/projects/bcac096e…/0a1832c4…`) cascades across agents.
Plus 3 `setup_failed: git worktree add` (Jul 10/15/16) from stale worktree leftovers.

**Smallest durable change (AGENTS.md diff, +2 lines):**
```
+ Before ending any run that used the shared execution workspace, restore it to the branch the harness
+ expects for the checked-out issue (or at minimum do not leave it on an unrelated issue's branch).
```
(Complementary automation: AutomationOpsEngineer proposal adds `git worktree prune` + drift check to the
daily cleanup routine.)

**Replay case:** given a run that checked out branch B in the shared workspace and finished → expected: the
workspace is back on B (not left on some C) and the next agent's spawn validates cleanly.

## Reinforce (no change)

- Decisive duplicate-lane collapse: GOV-773/774 collapse decision `d0c3502d` + `20fa4afd` (single owner
  named, surviving lane explicit, loser cancelled with reference). Keep doing exactly this.
- Non-author merge-gate discipline across GOV-711/732/734/737/739/744/762/766 — consistently evidence-first.

## Not proposed

- The GOV-682/683/684 critical triplicate (identical requirements issues, all cancelled Jul 10) was a
  creation-side race; creation attribution needs board review of who staged them — parked as a summary
  note rather than a CTO rule, since CTO was the assignee, not necessarily the creator.

Size check: proposed AGENTS.md delta ≈ 5 lines, well under +20%.
