-- Stage 1 Slice 1, Issue C (GOV-75): formalize `crawl_runs` as the AI-gateway
-- Lane 1 (deterministic ingest) run log. Contract 1.04-f; source: GOV-72 gap
-- analysis §3.3 / §4 (Issue C).
--
-- `crawl_runs` already records started/finished/status/targets/new_documents/
-- new_transcripts/notes (0001_init.sql). This migration adds the Lane 1 fields
-- the contract requires and the run log lacked: an explicit lane label, the
-- input source set, and a retry count.
--
-- Additive + idempotent: db.py guards each ADD COLUMN with a PRAGMA table_info
-- check, so re-running is safe (no SQLite "duplicate column" on a second run).
-- This is an ALTER-only migration — no new table (tests/test_smoke.py asserts
-- the exact table set).
--
-- NOTE (db.py splitter constraint): comments must be on their own full lines;
-- the migration splitter only strips lines that start with `--`, so trailing
-- inline comments after a statement would leak into the next statement.

PRAGMA foreign_keys = ON;

-- AI-gateway lane label (AI_GATEWAY_PROCESSING_WORKFLOW lane 1: deterministic
-- ingest = fetch/archive/hash/version/extract-text/store-metadata/log-run).
ALTER TABLE crawl_runs ADD COLUMN lane TEXT NOT NULL DEFAULT 'lane1_deterministic_ingest';

-- Input source set for the run: JSON array of registry source_ids (or crawler
-- target keys when the run pre-dates registry reconciliation).
ALTER TABLE crawl_runs ADD COLUMN source_set TEXT;

-- Number of retries performed during the run (transient fetch failures, etc.).
ALTER TABLE crawl_runs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
