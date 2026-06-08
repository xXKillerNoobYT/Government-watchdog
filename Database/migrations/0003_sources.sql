-- Stage 1 Slice 1, Issue B (GOV-74): source registry.
-- Contracts 1.02 (registry schema) / 1.03 (archive+crawl evidence).
-- Source: GOV-72 gap analysis §3.1, §4 (Docs/stage1-backend-gap-analysis.md).
--
-- Additive + idempotent. The `sources` table is the single inventory of what
-- Government Watchdog crawls/ingests. `documents`/`transcripts` gain a nullable
-- `source_id` FK so every crawled artifact resolves to one registered source
-- (back-filled by scripts/source_inventory.py reconciliation).
--
-- Scope lock: scope is CHECK-constrained to 'alpine' (COMPANY non-negotiable
-- Alpine-first; 1.05-a input contract). Every publication/verification field
-- defaults to a NOT-publishable / unreviewed state.
--
-- NOTE: `verification_status` / `correction_status` are deliberately plain TEXT
-- here (no enum CHECK). The authoritative 6-value `verificationStatus` enum and
-- its CHECK enforcement land in Issue D (GOV-72 §5.1 — that enum decision is in
-- the CTO/VerificationSafetyReviewer lane). This migration must not pre-decide it.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    -- identity (1.02-c: stable, slug-like, not title-derived)
    source_id               TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    scope                   TEXT NOT NULL DEFAULT 'alpine' CHECK (scope = 'alpine'),
    -- locators (1.02-b)
    url                     TEXT,
    original_url            TEXT,
    -- classification (1.02-b)
    source_type             TEXT,
    source_class            TEXT,
    source_authority_level  TEXT,
    jurisdiction            TEXT,
    expected_artifacts      TEXT,
    robots_policy           TEXT,
    owner_agent             TEXT,
    -- scan / validation timing (1.02-i: scan_date immutable, last_validated_utc updates)
    scan_date               TEXT,
    last_validated_utc      TEXT,
    -- archive (1.03-a; populated by the Wayback helper in Issue C)
    archive_url             TEXT,
    archive_status          TEXT NOT NULL DEFAULT 'not_checked',
    -- raw preservation (1.02-d/h; seed rows are seed_only until Issue C preserves them)
    raw_local_path          TEXT,
    raw_sha256              TEXT,
    raw_preservation_status TEXT NOT NULL DEFAULT 'seed_only',
    local_note_path         TEXT,
    -- status (enum CHECK deferred to Issue D — see header note)
    verification_status     TEXT NOT NULL DEFAULT 'source_recorded',
    correction_status       TEXT NOT NULL DEFAULT 'none',
    -- misc
    topic_tags              TEXT,
    notes                   TEXT,
    registered_utc          TEXT
);
CREATE INDEX IF NOT EXISTS idx_sources_source_class ON sources(source_class);
CREATE INDEX IF NOT EXISTS idx_sources_jurisdiction ON sources(jurisdiction);

-- Additive FK columns: every crawled artifact resolves to a registered source
-- (1.02-c). Nullable; back-filled by reconciliation. db.py guards these ADD
-- COLUMNs with PRAGMA table_info so re-running the migration is safe.
ALTER TABLE documents ADD COLUMN source_id TEXT REFERENCES sources(source_id);
ALTER TABLE transcripts ADD COLUMN source_id TEXT REFERENCES sources(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_source_id ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_source_id ON transcripts(source_id);
