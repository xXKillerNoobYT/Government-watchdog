# Goal-ledger ⇄ board auto-sync — run contract (GOV-396)

**Owner:** AutomationOpsEngineer.
**Operative workflow:** `AUTOMATION_OPS_WORKFLOWS.md` → "Goal-ledger ⇄ board auto-sync run"
(this repo doc is the PR-visible mirror; the agent instruction file is authoritative).
**Lane spec:** `CTO_WORKFLOWS.md` → "Goal-ledger ⇄ board auto-sync lane" (GOV-325, GOV-395).
**Script:** `scripts/governance/sync_goal_ledger.py` · **Tests:** `tests/test_gov396_sync_goal_ledger.py`.

## Why this lane exists (GOV-394)

The goal ledger (`goal.status`) drifts from the board (`issue.status`) because
nothing links an issue transition back to its `goalId`. Isaac caught the
active-goals view showing Stage 2 active while work had reached Stage 3.10; the
CEO reconciled by hand. This deterministic control-plane script makes the *safe*
flips automatic and *proposes* (never auto-applies) the owner-gated stage unlock.

It is a Paperclip control-plane tool: it reads/PATCHes **goals** and reads
**issues**. It touches no Government-Watchdog crawler data, no public surface,
no DB, no AI. It reasons over goal/issue `status` + `title` only — no PII, no
record payload.

## The three rules

| # | Transition | Mode | Trigger |
|---|---|---|---|
| 1 | subgoal `planned` → `active` | **AUTO** | ≥1 issue with that `goalId` is `in_progress`/`in_review` |
| 2 | numbered-stage parent `active` → `achieved` | **AUTO** | every *non-deferred* child subgoal is `achieved` |
| 3 | next numbered-stage parent `planned` → `active` | **PROPOSE-ONLY** | zero numbered stages active after projected rules 1+2 → recommend lowest-numbered planned stage to CEO |

A child is **deferred** iff its description matches
`DEFERRED to Stage \d+ \(Isaac-gated\)` (e.g. Stage 2.08/2.09/2.11). Deferred
children are not-required and must NOT block a rule-2 parent flip.

## Guardrails (CTO-owned, non-negotiable)

- **Always-active allowlist** — never flipped to `achieved` by rule 2: HEAD
  `5e8b8006`, governance `59fd6f5e`, premium `6834f0dd`, obsidian/backup
  `a44b4936`, security-testing `31744b17`, security-control `527b9486`.
- **Dry-run is the default.** Mutation requires `--apply`. `--apply` is a
  CEO-approved-plan gate (CTO hard stop) — running it on the live ledger requires
  CEO approval routed by the CTO. **Rule 3 is never executed under `--apply`** (it
  is structurally excluded from the apply set, not merely runtime-guarded).
- **One numbered stage active at a time.** >1 active numbered stage is reported as
  an anomaly, never auto-resolved.
- **Idempotent.** A re-run over a synced ledger yields zero PATCHes.

## Run contract

| Field | Value |
|---|---|
| **Trigger** | Invoked on the CEO heartbeat ledger-vs-board audit (read-only). On-demand by AutomationOps/CTO. `--apply` only after CEO approval. |
| **Run command (dry-run / audit)** | `python scripts/governance/sync_goal_ledger.py` |
| **Audit gate (CI/heartbeat)** | `python scripts/governance/sync_goal_ledger.py --fail-on-drift` → exit 1 iff rule-1/rule-2 drift exists (rule-3 recs do not count) |
| **Apply (gated)** | `python scripts/governance/sync_goal_ledger.py --apply` — **CEO-approved-plan only; CTO hard stop** |
| **Offline scan** | `--goals-file g.json --issues-file i.json` (no network) |
| **Input contract** | `GET /api/companies/{companyId}/goals` (235) + `/issues` (396); join via `issue.goalId`. `companyId bcac096e-…`. Base URL `http://127.0.0.1:3100` (override `--base-url`). |
| **Output contract** | stdout drift report (or `--json`); under `--apply`, `PATCH /api/goals/{id} {"status":...}` for rule-1/rule-2 flips only. |
| **Log path** | `Logs/governance/sync_goal_ledger.log` (gitignored; control-plane audit log, local/vault-only). `--log-path ''` disables file logging. Format `[YYYY-MM-DD HH:MM:SS] [LEVEL] message`. |
| **Exit codes** | `0` success/clean · `1` drift (only with `--fail-on-drift`) · `2` control-plane fetch failure. |
| **Retry policy** | Idempotent → safe to re-run. Transient fetch failure → exit 2, retry the whole run (no partial state; `--apply` PATCHes one goal at a time and a re-run skips already-synced goals). |
| **Issue-creation threshold** | Create a Paperclip issue when: (a) `--fail-on-drift` reports drift the CEO heartbeat cannot explain; (b) any anomaly row (>1 active numbered stage, or an active stage with no non-deferred children); (c) ≥3 consecutive exit-2 fetch failures; (d) a rule-3 recommendation surfaces (route to CEO for the unlock decision). |
| **Review cadence** | Every CEO heartbeat ledger-vs-board audit; AutomationOps reviews the log after any `--apply`. |
| **Owner** | AutomationOpsEngineer (monitoring + maintenance); CTO owns lane sequencing + the `--apply`/rule-3 gates; CEO owns stage-unlock approval. |
| **Improvement metric** | Drift rows surfaced per heartbeat trending to 0 with zero manual PATCHes; GOV-394-class hand reconciliations eliminated. |

## Normal output (live baseline, GOV-396 / GOV-395)

```
=== Goal-ledger <-> board drift report ===
rule-1 (subgoal planned->active)   : 0
rule-2 (parent active->achieved)   : 0
rule-3 (propose next stage unlock) : 0  [PROPOSE-ONLY]
--> 0 AUTO flip(s) (ledger in sync); 0 CEO recommendation(s).
```

Baseline: **0/0 drift across 396 issues / 235 goals.**

## Failure examples

- **Fetch failure:** `[ERROR] failed to load control-plane data: <urlopen error ...>` → exit 2. Retry; if ≥3 consecutive, open an issue (control plane down).
- **Drift detected (audit gate):** with `--fail-on-drift`, a non-empty rule-1/rule-2 list → `[ERROR] drift detected (rule-1/rule-2); ledger out of sync.` → exit 1.
- **Anomaly:** `[!] more than one numbered stage would be active: …` — invariant violated; no auto-resolution, route to CTO/CEO.

## Acceptance (lane is healthy)

A read-only drift scan returns 0 rule-1 and 0 rule-2 rows, and any rule-3
recommendation is surfaced to the CEO, not applied.
