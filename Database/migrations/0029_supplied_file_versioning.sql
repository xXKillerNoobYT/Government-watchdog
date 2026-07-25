-- GOV-1578 (GOV-1566 B5): Versioning + red-flag on supersede.
-- Parent chain: B1 (0-migration filesystem) stores raw bytes; B2 (0028) is the
-- canonical `supplied_files` record with mandatory provenance + the versioning
-- anchors (version_group_id, supersedes_id). THIS migration adds the two tables
-- B5's supersede/red-flag WORKFLOW needs on top of those anchors.
--
-- TWO NEW additive tables. NO ALTER on any existing table (supplied_files
-- included), so the four frozen serving surfaces (read_api, ai_risk_gate,
-- stage5_agenda_board, mcp_service/) AND the B2 record model stay byte-for-byte
-- unaffected. Additive + idempotent: CREATE ... IF NOT EXISTS, so the db.py
-- ledger fast-path and a bare re-run are both safe. One statement per ';', no
-- semicolons in literals, full-line comments only (db.py splitter contract).
--
-- Migration slot: 0029 is the first free slot on origin/main (latest =
-- 0028_supplied_file_records.sql). If another 0029 lands first, the merge gate
-- renumbers this file (second-lander-renumbers rule).
--
-- WHY THESE TABLES (plan §5): "Replacing/superseding a file is a red-flag event:
-- keep both versions, compute before/after, mark affected records, and require
-- re-review of affected work." B2 already keeps both versions (a supersede is a
-- new row, never an UPDATE/DELETE of the prior). B5 adds:
--   * supplied_file_dependencies -- which downstream records were built from a
--     specific file VERSION, plus a mutable re-review flag. When that version is
--     superseded, every dependency on it flips to 'needs_re_review' (fail-closed:
--     affected work is NOT trusted until a human re-reviews it). Generic
--     (record_kind, record_ref) so any downstream lane -- B4 linkage, agenda
--     anchoring, AI extraction, newsletter items -- registers without B5 needing
--     to know their schemas (B5 blocks only on B2, not B4).
--   * supplied_file_supersede_events -- one immutable audit row per supersede,
--     capturing the prior + replacement file ids and the deterministic
--     before/after diff. Backs the SEC review leg's "audit trail of every
--     intake/version/review action" requirement.
--
-- NO AI OUTPUT AS FACT (GOV-1566 §9, hard gate): neither table stores a model
-- summary, classification, or extracted claim. record_kind is an operator/lane
-- classification (not a model label); diff_json is a DETERMINISTIC comparison of
-- content-integrity + provenance fields (sha256, byte_size, filename, mime, ...),
-- computed by code, not an AI interpretation. review_flag is fail-closed: it only
-- ever gains meaning from a real supersede event or a human re-review resolve.

PRAGMA foreign_keys = ON;

-- §1 supplied_file_dependencies. One row per (downstream record, file version)
-- it was built from. review_flag starts 'current'; a supersede of that exact
-- version flips it to 'needs_re_review' (fail-closed) until a human resolves it.
-- record_kind/record_ref are an opaque pointer to the downstream record so this
-- table stays decoupled from any specific downstream schema.
CREATE TABLE IF NOT EXISTS supplied_file_dependencies (
    -- identity. Explicit NOT NULL closes SQLite's legacy TEXT-PK-accepts-NULL hole.
    dependency_id       TEXT PRIMARY KEY NOT NULL,
    -- the EXACT supplied-file version the downstream record was built from
    file_id             TEXT NOT NULL REFERENCES supplied_files(file_id),
    -- denormalized group id, so a caller can ask "any re-review open in group X".
    version_group_id    TEXT NOT NULL,
    -- opaque classification + id of the downstream record (NOT a model label)
    record_kind         TEXT NOT NULL,
    record_ref          TEXT NOT NULL,
    -- fail-closed re-review flag: only a supersede raises it, only a human clears
    review_flag         TEXT NOT NULL DEFAULT 'current'
        CHECK (review_flag IN ('current', 'needs_re_review')),
    -- which NEW version triggered the flag (nullable until flagged)
    flagged_by_file_id  TEXT REFERENCES supplied_files(file_id),
    -- audit timestamps
    created_at          TEXT NOT NULL,
    flagged_at          TEXT,
    resolved_at         TEXT,
    -- one dependency row per (version, downstream record)
    UNIQUE (file_id, record_kind, record_ref)
);
CREATE INDEX IF NOT EXISTS idx_sfdep_file ON supplied_file_dependencies(file_id);
CREATE INDEX IF NOT EXISTS idx_sfdep_group ON supplied_file_dependencies(version_group_id);
CREATE INDEX IF NOT EXISTS idx_sfdep_flag ON supplied_file_dependencies(review_flag);

-- §2 supplied_file_supersede_events. Immutable audit log: one row per supersede.
-- diff_json is the deterministic before/after comparison (fact, not AI). No
-- UPDATE path exists in code; rows are append-only.
CREATE TABLE IF NOT EXISTS supplied_file_supersede_events (
    event_id            TEXT PRIMARY KEY NOT NULL,
    version_group_id    TEXT NOT NULL,
    -- the prior/old version being replaced (preserved, never deleted)
    superseded_file_id  TEXT NOT NULL REFERENCES supplied_files(file_id),
    -- the new/replacement version
    new_file_id         TEXT NOT NULL REFERENCES supplied_files(file_id),
    -- deterministic before/after field diff, JSON text (NOT AI output)
    diff_json           TEXT NOT NULL,
    -- how many downstream dependencies this supersede flipped to needs_re_review
    affected_count      INTEGER NOT NULL CHECK (affected_count >= 0),
    -- provenance: who performed the supersede (mandatory)
    superseded_by       TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sfevent_group ON supplied_file_supersede_events(version_group_id);
CREATE INDEX IF NOT EXISTS idx_sfevent_superseded ON supplied_file_supersede_events(superseded_file_id);
