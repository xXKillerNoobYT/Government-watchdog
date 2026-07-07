-- GOV-634 (implements GOV-631 T2 + T3 + T4): credit-metering + escalation
-- provenance on the EXISTING run ledgers. Plan of record:
-- Docs/gov-631-automation-credit-efficiency-plan.md @ 4b0c47c (Node substrate
-- repo) — hash gate, batch gate, model floor, metering surfaced in run summary.
--
-- ALTER-only, additive + idempotent: db.py guards each ADD COLUMN with a
-- PRAGMA table_info check, and NO new table is created (tests/test_smoke.py
-- asserts the exact table set). Every column is nullable / defaulted, so all
-- landed rows stay untouched and valid.
--
-- SCOPE: Alpine-only, local/vault-only. These are reviewer/operational fields —
-- NOT on publication.WEB_SAFE_FIELD_ALLOWLIST; nothing here changes any
-- publication/reviewer gate.

PRAGMA foreign_keys = ON;

-- T2: first-class hash-skip accounting on the Lane-1 run log. A skipped file is
-- an unchanged source (documents.sha256 match) that got ZERO processing.
ALTER TABLE crawl_runs ADD COLUMN skipped_hash INTEGER NOT NULL DEFAULT 0;

-- T4: per-run token/cost metering on the Lane-2/3/4 gateway ledger. Nullable —
-- a deterministic or refused run records nothing; a live model run records what
-- the provider reported. estimated_cost_usd is an ESTIMATE (provider list
-- price), for trend metering only, never billing truth.
ALTER TABLE ai_extraction_runs ADD COLUMN tokens_input INTEGER;
ALTER TABLE ai_extraction_runs ADD COLUMN tokens_output INTEGER;
ALTER TABLE ai_extraction_runs ADD COLUMN estimated_cost_usd REAL;

-- T3: model-floor + escalation provenance. model_tier is 'floor' (cheapest
-- capable, the default) or 'escalated'. An escalated run must name the floor
-- run it escalates from; the floor run must carry the per-item low-confidence
-- record (JSON list of {statement_id, confidence, reason}) that JUSTIFIES the
-- escalation — tier escalation without a logged reason is a defect (plan §2).
ALTER TABLE ai_extraction_runs ADD COLUMN model_tier TEXT;
ALTER TABLE ai_extraction_runs ADD COLUMN escalated_from_run_id TEXT REFERENCES ai_extraction_runs(run_id);
ALTER TABLE ai_extraction_runs ADD COLUMN low_confidence_items TEXT;
