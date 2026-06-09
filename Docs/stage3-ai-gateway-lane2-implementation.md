# Stage 1 Impl — Slice 3 B: Lane 2 AI Extraction Adapter + Run Ledger (GOV-89)

Issue: GOV-89 · Owner: BackendCrawlerEngineer (`f26f530c`) · TranscriptEvidenceEngineer consult
Stage: Stage 1 implementation — Slice 3 B (Lane 2). Alpine-only, local/vault-only.
Blocked by: GOV-88 (Slice 3 A interface + migration plan) — **resolved/merged** (PR #18).
Implements: `Docs/stage3-ai-gateway-gap-analysis.md` §2/§3/§5 against 1.09 (automation-vs-AI
boundary), 1.11 (publication gates), `AI_GATEWAY_PROCESSING_WORKFLOW.md` (lanes + run-log).

## What shipped

| Artifact | Role |
|---|---|
| `Database/migrations/0009_ai_extraction_runs.sql` | `ai_extraction_runs` ledger; widens `statements.produced_by` CHECK to permit `'ai'` (guarded SQLite rebuild); adds nullable `ai_extraction_run_id` provenance FK to `statements` + `evidence_links`. |
| `scripts/ai_extraction.py` | Lane-2 adapter: opens a run, proposes statements/evidence_links from **already-preserved** segments/sources only, writes `produced_by='ai'` rows via the existing `insert_statement` path, routes speakers through the attribution-safety gate, finalizes the ledger. |
| `scripts/statements.py` | Scope lock lifted: app-layer `produced_by` set widened to `{automation, ai, human}` in lockstep with the DB CHECK (CTO D-1); `ai_extraction_run_id` threaded into both INSERTs. |
| `scripts/slice3_smoke.py` | End-to-end offline Lane-2 smoke over the sanitized Alpine fixture. |
| `tests/test_ai_extraction.py`, `tests/test_slice3_integration_smoke.py` | Unit + integration coverage for the AI-gateway invariants. |

## Migration 0009 — how the CHECK rebuild stays safe

SQLite cannot `ALTER` a CHECK, so widening `statements.produced_by` to include
`'ai'` (CTO D-1 ruling, GOV-88 §5) uses the guarded table-rebuild. Because the
project's migration runner wraps every migration in **one transaction**,
`PRAGMA foreign_keys=OFF` is a no-op; the rebuild instead uses:

- `PRAGMA defer_foreign_keys = ON` — defers FK enforcement to the runner's final
  COMMIT, so dropping `statements` (referenced by `speaker_attributions`,
  `made_statement`, and a self-FK) is safe; integrity is verified once, at commit,
  with the rebuilt table + identical rows in place.
- `PRAGMA legacy_alter_table = ON` around the RENAME — stops SQLite rewriting the
  child tables' FK references.

The rebuild copies columns **explicitly** (not `SELECT *`), mirrors the landed
`0005_ssot_publication.sql` widened literal, touches no row value, and only grows
the value space (strictly additive). A test applies `0001→0008`, populates, then
applies `0009` and asserts: row count + content digest identical pre/post,
`PRAGMA foreign_key_check` empty, an `'ai'` INSERT now lands, and the migration is
idempotent.

## Invariants enforced (GOV-89 done-bar 7–11)

1. **AI provenance + fail-closed defaults (7).** Every AI row is forced to
   `produced_by='ai'`, `verification_status='machine_extracted_unreviewed'`,
   `review_state='unreviewed'`, `publication_state='not_publishable'`,
   `layer='ai_thought_then'`, `is_verbatim=0`, with its `ai_extraction_run_id`.
   Gating fields are **overridden** by the adapter regardless of proposer output.
2. **No orphan claims (8).** Lane 2 reuses `insert_statement` unchanged; an AI
   claim with no resolving `evidence_link`/segment is rejected (1.07 §2.3) and
   counted in `orphan_rejected_count` — never written.
3. **Attribution safety (9).** A proposed speaker is routed through
   `speakers.attribute_speaker` with `person_confirmed=False` (AI can never
   confirm an identity) → resolves to `uncertain`: no bound `person_id`, no
   `made_statement` edge, a name-free label; the guess survives only as the
   vault-only `candidate_person_id`. **No name is better than a wrong name.**
4. **Gateway run-log (10).** `ai_extraction_runs` records input source/segment
   set, model/tool/prompt version, output artifact ids, `error_status`,
   `reviewer_state`, and the forward-only retry chain.
5. **Fail-closed downstream (11).** `outputs_publication_blocked()` returns True
   unless the run finished `error_status='ok'` **and** a human set
   `reviewer_state='approved'`; a failed/partial/unreviewed run is blocked. The
   produced rows are also `not_publishable` at the DB layer regardless.

## Provider / offline posture

The model call is injected as a `proposer` callable; the real provider is loaded
from a gitignored local config (`Database/ai_provider.local.json` or
`GOV_AI_*` env). With no proposer and an offline config, `run_extraction` records
a **failed** run (`ProviderNotConfigured`) — it never makes a live call. Tests/CI
inject a deterministic offline proposer, so the whole lane is reproducible with no
network and no AI dependency.

## Data-publication boundary (done-bar 12)

Code/schema/migrations/tests/sanitized fixtures → GitHub. The
`ai_extraction_runs` ledger, its `error_detail`, the `ai_extraction_run_id`
provenance columns, the local provider config, AI output bytes, and the SQLite DB
→ **local/vault-only**. The ledger/provenance fields are deliberately NOT on
`publication.WEB_SAFE_FIELD_ALLOWLIST`; `to_web_safe()` drops them fail-closed
(asserted by a test). `produced_by='ai'` IS publicly labelable (the only AI fact
that crosses), satisfying the AI-label requirement without exposing run internals.

## Verification

- `python -m pytest -q` → all green (incl. `test_ai_extraction.py`,
  `test_slice3_integration_smoke.py`, the updated `test_statements_evidence.py`
  scope tests, and `test_smoke.py` table set).
- `python scripts/slice3_smoke.py` → `RESULT: OK` (5/5 invariants).
- CI: `local-runner-smoke.yml` (workflow_dispatch) now runs the slice-3 smoke;
  green run URL on `IA-Mac-GOV-Backend` recorded in the GOV-89 closeout comment.

## Scope lock (what this issue does NOT do)

No live model call; no Lane-3 deterministic compare tooling; no Lane-5 reviewer
CLI; no publication of any AI output; no scope beyond the Town of Alpine; no new
`verificationStatus`/`uiStatus`/publication-allowlist vocabulary. Those remain
later, gated implementation issues.
