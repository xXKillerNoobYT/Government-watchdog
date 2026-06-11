-- Stage 1 Impl (GOV-125): first-class completeness-gap layer.
-- Plan §3 (GOV-125 plan rev c50ce2ad, author CTO). Builder: TranscriptEvidenceEngineer.
--
-- WHY a new table: no completeness/gap schema exists today. The closest existing
-- columns — `verification_status` (record trust) and `correction_status`
-- (forward-only fixes) — describe a row that DOES exist; they cannot represent a
-- meeting that has NO primary source, a transcript that has NO timestamps, or an
-- agenda thread left unresolved. Per GOV-124/133 ground-truth only 34/124 meeting
-- folders have a primary source, so ~90 meetings MUST carry a surfaced
-- `no_primary_source`/`missing_transcript` gap. Surfacing the backfill gap is the
-- headline acceptance criterion (plan §5.5) — it must be queryable, never silently
-- dropped, and it must NEVER gate or flip publication.
--
-- Additive + idempotent (CREATE ... IF NOT EXISTS; db.py ledger + IF NOT EXISTS).
-- Scope lock: Alpine-only, local/vault-only, NO AI. Deterministic detector only.
--
-- SSOT PARITY: the `gap_type` / `severity` / `resolved_status` / `produced_by`
-- CHECK literals below mirror the frozensets in scripts/completeness.py. A parity
-- test (tests/test_gov125_realdata_structuring.py) asserts the two cannot drift,
-- the same pattern concept_map.py uses for ALLOWED_EDGE_TYPES vs the 0012 CHECK.

PRAGMA foreign_keys = ON;

-- completeness_gap (plan §3): a first-class, surfaced statement that some expected
-- evidence is absent or incomplete for a subject node/meeting. `subject_node_id`
-- is intentionally free TEXT (not an FK): a gap can describe a meeting FOLDER/date
-- that has no node yet (the no_primary_source case), so it cannot be constrained
-- to an existing node row. `source_id` is a nullable FK to the registry for gaps
-- that DO resolve to a known source. `detected_run_id` ties the gap to the
-- crawl/structuring run that emitted it (provenance + re-run audit).
CREATE TABLE IF NOT EXISTS completeness_gaps (
    gap_id            TEXT PRIMARY KEY,
    subject_node_id   TEXT NOT NULL,
    subject_node_type TEXT NOT NULL,
    gap_type          TEXT NOT NULL CHECK (gap_type IN (
                          'missing_transcript',
                          'missing_timestamps',
                          'partial_agenda',
                          'unresolved_thread',
                          'no_primary_source',
                          'pdf_text_unextracted',
                          'untimed_segment',
                          'speaker_unattributable')),
    severity          TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('info', 'warn', 'blocking')),
    detail            TEXT,
    source_id         TEXT REFERENCES sources(source_id),
    detected_run_id   INTEGER REFERENCES crawl_runs(id),
    detected_utc      TEXT NOT NULL,
    resolved_status   TEXT NOT NULL DEFAULT 'open' CHECK (resolved_status IN ('open', 'acknowledged', 'resolved', 'wontfix')),
    produced_by       TEXT NOT NULL DEFAULT 'deterministic' CHECK (produced_by IN ('deterministic', 'ai', 'human')),
    UNIQUE (subject_node_id, subject_node_type, gap_type)
);
CREATE INDEX IF NOT EXISTS idx_completeness_gaps_gap_type ON completeness_gaps(gap_type);
CREATE INDEX IF NOT EXISTS idx_completeness_gaps_subject ON completeness_gaps(subject_node_type, subject_node_id);
CREATE INDEX IF NOT EXISTS idx_completeness_gaps_resolved ON completeness_gaps(resolved_status);
CREATE INDEX IF NOT EXISTS idx_completeness_gaps_source_id ON completeness_gaps(source_id);
