-- Stage 2.05 impl (GOV-275): land the `transcript_class` column on `transcripts`.
-- First Stage 2.05 backend slice. Owner: BackendCrawlerEngineer.
--
-- WHY this column: the `transcript_class` deferred-delta was defined as a closed
-- enum + fail-closed default in the Stage 2.04 contract (GOV-230 goal 7e4434b1)
-- but its schema migration was deliberately deferred to Stage 2.05 (see
-- Docs/GOV-262-preservation-replay-evidence.md). The Alpine corpus is
-- PRESERVATION-VALID (GOV-262), so the extraction migration precondition is met.
-- This migration ADDS the column; a deterministic (no-AI) backfill populates it
-- (scripts/transcript_class.py). The AI-provenance half (`produced_by='ai'`
-- write-time binding) is intentionally split to a successor 2.05 slice.
--
-- Additive + idempotent: a single `ALTER TABLE ... ADD COLUMN`. db.py guards each
-- ADD COLUMN with a PRAGMA table_info check (SQLite has no ADD COLUMN IF NOT
-- EXISTS), so a re-run is a no-op. No table rebuild, no CHECK-widening on any
-- existing column, no Stage-1 column touched.
--
-- NULLABILITY: the column is intentionally NULLABLE with NO SQL default. NULL
-- means "not yet classified" and is kept distinct from the fail-closed default
-- value `auto_caption_untimed`, which the deterministic backfill writes
-- explicitly. A NOT NULL DEFAULT would write a value into every pre-existing row
-- at migration time (mutating existing rows), which the Stage 2.05 additive-only
-- rule forbids — classification is the backfill's job, not the column default's.
-- In SQLite a CHECK passes when its expression is NULL (it only fails on FALSE),
-- so existing rows legally sit at NULL until the backfill populates them.
--
-- SSOT PARITY: the CHECK literal below mirrors TRANSCRIPT_CLASSES in
-- scripts/transcript_class.py EXACTLY. A parity test
-- (tests/test_gov275_transcript_class.py) asserts the two cannot drift — the same
-- guard 0015/completeness.py uses for gap_type. The enum is FROZEN by GOV-230:
-- a Stage 2.x child MAY NOT add a value here without patching the GOV-230
-- contract first (inheritance-by-reference).

PRAGMA foreign_keys = ON;

-- transcript_class (GOV-230 closed enum). Locator/confidence semantics live in
-- the contract: official_transcript=timed/highest; auto_caption_timed=timed/medium;
-- auto_caption_untimed=untimed segment_id-only/lower (FAIL-CLOSED DEFAULT);
-- minutes_only=paraphrase/no quoted_text projection; derived_md_only=blocks
-- statement production; no_transcript=gap-only. Reviewer-internal: this column is
-- NOT on publication.WEB_SAFE_FIELD_ALLOWLIST and never projects to a card label.
ALTER TABLE transcripts ADD COLUMN transcript_class TEXT CHECK (transcript_class IN (
    'official_transcript',
    'auto_caption_timed',
    'auto_caption_untimed',
    'minutes_only',
    'derived_md_only',
    'no_transcript'));
CREATE INDEX IF NOT EXISTS idx_transcripts_transcript_class ON transcripts(transcript_class);
