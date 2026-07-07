# GOV-634 — Automation & credit-efficiency implementation (GOV-631 T1–T6)

**Plan of record:** `Docs/gov-631-automation-credit-efficiency-plan.md` @ `4b0c47c`
(Node substrate repo, branch `GOV-585-handoff-escalation`). This doc is the
backend implementation contract + the WORKFLOW_GOVERNANCE "automation/log rule"
record (trigger, IO, log location, failure handling, thresholds, cadence, owner)
for the pieces landed by GOV-634.

## What landed where

| Target | Artifact | Credits |
|---|---|---|
| T1 refresh runner | `scripts/refresh_runner.py` (dry-run default, `--apply` gated, `--emit-cron`) | 0 |
| T2 hash-skip | `scripts/ingest_local_corpus.py` (`skipped:hash` in summary/notes + `crawl_runs.skipped_hash`); dry-run plans vs an existing DB read-only | 0 |
| T3 lane-2 batch queue | `scripts/lane2_batch_queue.py` (hash-gated pending set, floor-model batches, `escalate` refuses without a logged low-confidence record) | 0 until lane-2 unlocks |
| T4 credit metering | `Database/migrations/0019_credit_metering.sql` + `scripts/credit_metering.py`; metering block embedded in every runner log | 0 (logging) |
| T5 failure→issue | `scripts/failure_issue_filer.py` (defined patterns → Paperclip issues; dry-run default, dedupe keys) | 0 |
| T6 CI + graphify | `.github/workflows/backend-tests.yml` (pytest on PR + conditional `graphify update .`, AST-only) | 0 |

## Run contract (governance rule 9)

- **Trigger:** cron (dry-run only; `refresh_runner.py --emit-cron` prints the
  line) or manual. An `--apply` run happens only after CTO review of the latest
  dry-run log (GOV-631 §2 dry-run gate).
- **Input/output:** signed GOV-133 selection narrowed to the pilot window
  (`--only-date 2026-06-23`) → `documents`/raw-store/`crawl_runs` rows.
  `--scope full` exits 2 naming owner card `confirmation:GOV-612:full-ingest:v1`
  (GOV-625); no bypass flag exists.
- **Log location:** `Logs/refresh-runner/refresh-<utc>-<mode>.json` (gitignored,
  local/vault-only). Each log embeds the T4 metering block (run window +
  all-time) and `ai_run_delta`.
- **Normal success:** `ok: true`, `failures: []`, `ai_run_delta: 0`, and on a
  no-change pilot run `skipped:hash == selected` with `new_documents == 0`.
- **Failure examples:** per-file copy/hash errors land in `failures[]` (run
  status `partial`); an `ai_extraction_runs` row appearing during a
  deterministic run flags `credit_anomaly: true` and the run exits non-zero.
- **Retry policy:** no auto-retry; failed runs re-run manually after triage
  (idempotent — completed work re-skips by hash).
- **Issue-creation thresholds (T5):** `failure_issue_filer.py` files a Paperclip
  issue (assignee CTO, backend project) for exactly three patterns:
  `lane1_run_failed` (any `failed`/`partial` `crawl_runs` row),
  `runner_failures` (runner log `ok: false`), and `credit_anomaly`
  (non-zero `ai_run_delta`, or a tier escalation whose floor run has no
  `low_confidence_items` record). Dedupe key in the title (`[auto:T5 …]`)
  makes re-scans flood-proof. Dry-run default; `--apply` files.
- **Review cadence:** CTO reviews runner logs on each merge touching the
  pipeline and at least weekly while scheduled (GOV-631 §5).
- **Metrics (improvement proof):** documents/run, AI calls/run, tokens and
  estimated cost per document, skip ratio — all from `credit_metering.meter`.
  Success = cost-per-document trends down while reviewed throughput holds.
- **Owner:** CTO (triage + log review). Escalation: publication/beta/scope/
  data-boundary impact → Isaac via CEO (GOV-631 §6); credit anomalies → CTO
  issue, CEO informed on next heartbeat.

## Credit-spend gate wiring (GOV-631 §2 → code)

- **Hash gate:** `ingest_local_corpus` skips unchanged sources
  (`skipped:hash`); `lane2_batch_queue.pending_items` never re-queues a segment
  a successful lane-2 run already covered.
- **Batch gate:** `plan_batches` is the only lane-2 planning path (no per-doc
  one-offs); execution runs through the unchanged `ai_extraction` adapter.
- **Model floor:** batches default to `FLOOR_MODEL` (Haiku-class);
  `escalate()` raises `EscalationWithoutReason` unless the floor run logged
  `low_confidence_items` — the gateway log justifies every tier bump.
- **Dry-run gate:** runner, filer, and queue CLI all default to
  dry-run/read-only; mutation requires `--apply` (F2/GOV-479 pattern).
- **Scope gate:** pilot window hard-coded; full scope refuses at exit 2.

## Unchanged gates (explicitly)

Reviewer gate (lane 5), AI-never-primary-evidence, `not_publishable` defaults,
PII guard, attribution safety, publication allowlists, and the Isaac-gated full
ingest/publication/expansion decisions are all untouched — T1–T6 only add
skip-accounting, metering, queue planning, and log/issue plumbing around them.
