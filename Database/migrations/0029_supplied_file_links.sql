-- GOV-1577 (GOV-1566 B4): Linkage of a supplied file to an area / meeting /
-- agenda item, plus the structural anchor the deterministic gap-detection update
-- keys off. Parent chain: B1 stores raw bytes, B2 (0028) records the canonical
-- supplied_files ledger row, and THIS migration lets an operator attach that
-- recorded file to the subject it is a source for, so a supplied primary source
-- can close a known `no_primary_source` completeness gap (0015).
--
-- ONE NEW additive table (supplied_file_links). NO ALTER on any existing table,
-- so the four frozen serving surfaces (read_api, ai_risk_gate,
-- stage5_agenda_board, mcp_service/) stay byte-for-byte unaffected, and the
-- existing completeness_gaps (0015) / supplied_files (0028) schemas are untouched.
-- Additive + idempotent: CREATE ... IF NOT EXISTS, so the db.py ledger fast-path
-- and a bare re-run are both safe. One statement per ';', no semicolons in
-- literals, full-line comments only (db.py splitter contract).
--
-- Migration slot: 0029 is the first free slot on origin/main (latest =
-- 0028_supplied_file_records.sql). If another 0029 lands first, the merge gate
-- renumbers this file (second-lander-renumbers rule).
--
-- NO AI OUTPUT AS FACT (GOV-1566 §9, hard gate): every column here is operator/
-- content-supplied provenance. `is_primary_source` is an OPERATOR classification
-- (does this file serve as the source-of-record for the subject?), NOT a model
-- label. The gap-detection update reads these rows deterministically; no AI value
-- is ever written to or read from this table.
--
-- DELIBERATELY NO FK on subject_node_id: mirroring completeness_gaps (0015), a
-- link may point at a meeting FOLDER/date that has no node row yet (exactly the
-- `no_primary_source` case a supplied file exists to close), so the subject
-- cannot be constrained to an existing row. `subject_node_type` is CHECK-bounded
-- instead. file_id IS a real FK into supplied_files (0028): you cannot link a
-- file that was never recorded.

PRAGMA foreign_keys = ON;

-- §1 supplied_file_links. One row per (file, subject) attachment. The subject a
-- supplied file is a source for is addressed by (subject_node_type,
-- subject_node_id) — the same addressing completeness_gaps uses — so a link and
-- the gap it may close share a key space (e.g. type 'meeting', id = folder date).
CREATE TABLE IF NOT EXISTS supplied_file_links (
    -- deterministic identity: derived from (subject, file) by the writer so a
    -- re-link of the same pair is idempotent (upsert), never a duplicate row.
    link_id            TEXT PRIMARY KEY NOT NULL,
    -- the recorded supplied file being attached (real FK: no orphan links)
    file_id            TEXT NOT NULL REFERENCES supplied_files(file_id),
    -- what the file is a source for. Free TEXT id (no FK, see header); the type is
    -- CHECK-bounded to the three subjects B4 links against.
    subject_node_type  TEXT NOT NULL
        CHECK (subject_node_type IN ('area', 'meeting', 'agenda_item')),
    subject_node_id    TEXT NOT NULL,
    -- OPERATOR classification (NOT AI): is this file the source-of-record (primary
    -- source) for the subject? Only a primary-source link can close a
    -- `no_primary_source` gap. 0 = supporting/secondary, 1 = primary source.
    is_primary_source  INTEGER NOT NULL DEFAULT 0
        CHECK (is_primary_source IN (0, 1)),
    -- provenance: WHO attached it, WHEN (both mandatory; a link is an audited act)
    linked_by          TEXT NOT NULL,
    linked_at          TEXT NOT NULL,
    -- one link per (file, subject): re-linking updates in place (writer upsert)
    UNIQUE (file_id, subject_node_type, subject_node_id)
);
CREATE INDEX IF NOT EXISTS idx_supplied_file_links_subject
    ON supplied_file_links(subject_node_type, subject_node_id);
CREATE INDEX IF NOT EXISTS idx_supplied_file_links_file
    ON supplied_file_links(file_id);
CREATE INDEX IF NOT EXISTS idx_supplied_file_links_primary
    ON supplied_file_links(subject_node_type, subject_node_id, is_primary_source);
