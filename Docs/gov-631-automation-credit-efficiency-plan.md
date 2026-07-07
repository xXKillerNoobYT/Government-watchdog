# GOV-631 — Automation-First & Credit-Efficiency Plan

**Owner directive (Isaac, `local-board`, 2026-07-07, GOV-631):** use software tools to automate Government Watchdog work as much as possible while spending as few AI credits as possible, across both the Watchdog project and the Backend project. Isaac is the designer; planning and implementation are delegated to CEO/CTO.

**Owner:** CEO (plan + governance), CTO (technical implementation quality).
**Goal:** `5e8b8006` (Alpine ingest). **Status:** operative plan — implementation delegated to CTO via child issue (see GOV-631 comments for the identifier).

---

## 1. Principle: deterministic-first, credits-last

Ordered preference for any Watchdog processing step:

1. **Deterministic script/parser/cron** (zero credits) — fetching, archiving, hashing, text/transcript extraction, metadata, registry updates, board projections, tests, graphify AST updates.
2. **Cached/batched cheap-model AI** — only for AI-gateway lane 2 (source-grounded extraction/summarization) where human-like judgment is genuinely required, and only on content that has never been processed before.
3. **Expensive-model AI** — only on documented low-confidence escalation from (2), never as the default.

A step may only move up this ladder with a logged reason. AI output remains never-primary-evidence per `AI_GATEWAY_PROCESSING_WORKFLOW.md`; nothing here weakens the reviewer gate (lane 5) or owner gates.

## 2. Credit-spend gates (hard rules)

- **Hash gate:** no AI call on content whose source hash already has a stored lane-2 artifact. The lane-1 hash/version store is the cache key.
- **Batch gate:** lane-2 items queue and run in batches on a schedule, not one-off per document.
- **Model floor:** batches default to the cheapest capable model (Haiku-class); tier escalation requires a per-item low-confidence record in the gateway log.
- **Dry-run gate:** any new automation runs `--dry-run` first with logged output reviewed by CTO before `--apply` (established F2/GOV-479 pattern).
- **Scope gate:** automation never expands ingest scope. The Alpine full run stays gated on Isaac's pending card `confirmation:GOV-612:full-ingest:v1` (GOV-625); automation work must be runnable against pilot-scope data only until that resolves.

## 3. Automation targets (what to build/wire)

| # | Target | Repo | Credits |
|---|--------|------|---------|
| T1 | Scheduled deterministic refresh runner (cron wrapper around the existing lane-1 ingest + refresh runner, `--dry-run` default) | backend (Python, canonical) + Node substrate runner | 0 |
| T2 | Content-hash skip logic verified end-to-end (unchanged source ⇒ zero processing, logged as `skipped:hash`) | backend | 0 |
| T3 | Lane-2 batch queue: pending-extraction queue + batched cheap-model runs + confidence-based escalation record | backend | minimized |
| T4 | Credit metering: per-run log of AI calls, model, tokens, and cost-per-document; surfaced in run summary | backend | 0 (logging) |
| T5 | Failure→issue thresholds: runner failures matching defined patterns file a Paperclip issue automatically instead of waiting for an agent heartbeat | backend + Paperclip API | 0 |
| T6 | CI + graphify hygiene: tests and `graphify update .` (AST-only) wired into the change loop so verification stays free | all repos | 0 |

## 4. Agent-ops credit hygiene (applies to all GOV agents)

- Wake-on-demand only; no polling loops. Blocker links and confirmation cards are the wake mechanism.
- Prefer scoped tools (graphify query/path/explain, targeted file reads, scripts) over broad reads or spawned agent fan-outs for mechanical work.
- Smallest verification that proves the change; full test suites only when scope warrants.
- One durable artifact per heartbeat beats many status comments.

## 5. Logs, metrics, review cadence (governance rule 9)

- **Logs:** gateway/runner logs record input source set, hash decisions (`processed` vs `skipped:hash`), model/tokens where AI ran, output artifact, errors, reviewer state.
- **Metrics:** documents processed per run, AI calls per run, tokens/cost per document, skip ratio. Success = cost-per-document trends down while reviewed-document throughput holds or rises.
- **Cadence:** CTO reviews runner logs on each merge touching the pipeline and at least weekly while the runner is scheduled; threshold breaches auto-file issues (T5).

## 6. Failure handling & escalation

- Runner/gateway failure ⇒ downstream presentation blocked (existing rule), auto-issue filed per T5, CTO owns triage.
- Credit anomalies (spend spike, hash-gate miss) ⇒ CTO issue, CEO informed on next heartbeat.
- Any automation that would touch publication, beta exposure, expanded sources, or data leaving local/vault ⇒ stop and escalate to Isaac (owner gate).

## 7. Acceptance criteria for the delegated implementation

1. T1–T6 implemented or explicitly descoped with reason, each with evidence (file paths, test results, log samples, dry-run output).
2. A pilot-scope dry-run demonstrating: unchanged sources skipped by hash, zero AI calls on a no-change run, metering output present.
3. Workflow docs updated where a durable rule changed (AI gateway/automation workflow files), per WORKFLOW_GOVERNANCE.
4. No change to owner gates: full ingest, publication, and expansion remain Isaac-gated.
