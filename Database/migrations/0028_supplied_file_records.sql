-- GOV-1575 (GOV-1566 B2): File record + full-provenance data model for supplied
-- files. Parent chain: a person hands us a file, B1 (0-migration, filesystem)
-- stores the raw bytes content-addressed + encrypted; THIS record is the
-- canonical, queryable ledger row describing that file with mandatory provenance.
--
-- ONE NEW additive table (supplied_files). NO ALTER on any existing table, so
-- the four frozen serving surfaces (read_api, ai_risk_gate, stage5_agenda_board,
-- mcp_service/) stay byte-for-byte unaffected. Additive + idempotent: CREATE ...
-- IF NOT EXISTS, so the db.py ledger fast-path and a bare re-run are both safe.
-- One statement per ';', no semicolons in literals, full-line comments only
-- (db.py splitter contract).
--
-- Migration slot: 0028 is the first free slot on origin/main (latest =
-- 0027_beta_magic_code.sql). If another 0028 lands first, the merge gate
-- renumbers this file (second-lander-renumbers rule).
--
-- FAIL-CLOSED / PRIVATE-BY-DEFAULT (GOV-1566 hard gates):
--   * review_state DEFAULT 'pending' — a supplied file is NEVER web-safe until a
--     reviewer moves it. review-before-AI and review-before-display both key off
--     this column; nothing downstream may treat a 'pending' file as displayable.
--   * All provenance columns are NOT NULL: a record cannot exist without knowing
--     WHAT the file is (sha256, mime, byte_size), WHERE it came from (area,
--     source_type, original_filename, supplied_by, captured_at), and WHEN it was
--     recorded (created_at). origin_url is the only optional locator (a hand-
--     supplied file often has no URL).
--
-- NO AI OUTPUT AS FACT (GOV-1566 §9, hard gate): this table has NO column that
-- stores an AI interpretation, summary, classification-by-model, extracted claim,
-- or any model-derived value. Every column is human/operator-supplied provenance
-- or a content-derived integrity value (sha256/byte_size). source_type is an
-- operator classification, NOT a model label. AI lanes (ai_extraction_runs, etc.)
-- reference files by id from their own tables; they never write back here.
--
-- VERSIONING (B5): version_group_id ties every revision of one logical file into
-- a group; supersedes_id points a new version at the row it replaces (self-FK).
-- B5 (GOV-15xx) owns the supersede/red-flag WORKFLOW; this migration only
-- provides the structural anchors it needs.

PRAGMA foreign_keys = ON;

-- §1 supplied_files. One row per supplied raw file (a version is its own row).
-- sha256 is the B1 content address (sha256 of the plaintext bytes); it is NOT
-- unique here because the same bytes may be supplied more than once under
-- different provenance (different supplied_by / original_filename), which the
-- record must preserve distinctly even though B1 dedupes the physical object.
CREATE TABLE IF NOT EXISTS supplied_files (
    -- identity. Explicit NOT NULL closes SQLite's legacy hole where a non-INTEGER
    -- PRIMARY KEY still accepts NULL (only INTEGER PRIMARY KEY implies NOT NULL).
    file_id            TEXT PRIMARY KEY NOT NULL,
    -- provenance: WHERE it belongs / came from (all mandatory)
    area               TEXT NOT NULL,
    source_type        TEXT NOT NULL,
    original_filename  TEXT NOT NULL,
    supplied_by        TEXT NOT NULL,
    captured_at        TEXT NOT NULL,
    -- optional locator: a hand-supplied file often has no origin URL
    origin_url         TEXT,
    -- content-derived integrity (NOT AI): sha256 = B1 plaintext content address
    sha256             TEXT NOT NULL CHECK (length(sha256) = 64),
    mime               TEXT NOT NULL,
    byte_size          INTEGER NOT NULL CHECK (byte_size >= 0),
    -- review lifecycle: fail-closed default; only a reviewer advances it
    review_state       TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_state IN ('pending', 'reviewing', 'web_safe', 'held', 'rejected')),
    -- versioning anchors for B5 (self-referential supersede chain)
    version_group_id   TEXT NOT NULL,
    supersedes_id      TEXT REFERENCES supplied_files(file_id),
    -- audit
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supplied_files_area ON supplied_files(area);
CREATE INDEX IF NOT EXISTS idx_supplied_files_sha256 ON supplied_files(sha256);
CREATE INDEX IF NOT EXISTS idx_supplied_files_review_state ON supplied_files(review_state);
CREATE INDEX IF NOT EXISTS idx_supplied_files_version_group ON supplied_files(version_group_id);
