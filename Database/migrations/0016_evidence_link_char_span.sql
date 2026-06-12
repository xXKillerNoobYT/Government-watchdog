-- Stage 1, GOV-137 (GOV-126 Phase 1): untimed-extraction contract — add the
-- `char_span` locator_kind so a statement can anchor to a SOURCE + an exact
-- quoted character span, with NO timed-segment requirement.
--
-- Why this exists (verified on main): GOV-125 produced 0 statements because all
-- 28 real Alpine transcripts are untimed ASR (0/28 carry MM:SS). The 1.07
-- exact-source pointer (migration 0007) only modeled timed/page/section/
-- paragraph locators, so untimed prose had no honest anchor. This migration adds
-- the char-span anchor: `to_source_id` (already required) + `char_start` +
-- `char_end` (half-open [start, end) offsets into the preserved source text) +
-- `quoted_text` (the verbatim span those offsets select). The
-- scripts/statements.py validator requires all three when locator_kind =
-- 'char_span', and the GOV-137 proposer DERIVES the offsets by locating the
-- model's verbatim quote in the source (never trusts model arithmetic), so the
-- span is a reproducible, source-grounded anchor — a hallucinated quote that
-- isn't literally in the source has no offsets and is dropped fail-closed.
--
-- Additive + idempotent: db.py's ledger skips an already-applied file. The one
-- statement that is not naturally re-runnable in SQLite is the `evidence_links`
-- CHECK rebuild (SQLite cannot ALTER a CHECK); the ledger is its idempotency
-- guarantee, and a bare re-run still reproduces an equivalent widened schema
-- while preserving every row. This mirrors landed precedent exactly: migration
-- 0009 widened statements.produced_by the same way. Per-row data is never
-- altered, only the value space of locator_kind grows (strictly additive).
--
-- SCOPE: Alpine-only, local/vault-only. char_start/char_end/quoted_text are
-- NEW evidence_link pointer fields and are deliberately NOT added to
-- publication.WEB_SAFE_FIELD_ALLOWLIST — quoted_text is a verbatim raw-source
-- span and stays reviewer/vault-only by the fail-closed allowlist (the AI
-- paraphrase carried on statements.statement_text is the web-safe surface, and
-- it stays not_publishable until a human promotes it).

PRAGMA foreign_keys = ON;

-- Defer FK enforcement to the runner's final COMMIT. PRAGMA foreign_keys=OFF is a
-- no-op inside the runner's open transaction; defer_foreign_keys IS settable mid-
-- transaction and makes the drop/rename of `evidence_links` safe — integrity is
-- verified once, at commit, with the rebuilt table in place carrying identical
-- rows. (evidence_links carries FKs to sources/agenda_items/ai_extraction_runs;
-- legacy_alter_table=ON below keeps those references naming the same tables.)
PRAGMA defer_foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Rebuild evidence_links with the widened locator_kind CHECK + the three
-- char-span pointer columns. evidence_links_new is byte-identical to the
-- current evidence_links (0007 columns + the 0009 ai_extraction_run_id column)
-- EXCEPT the locator_kind CHECK now includes 'char_span' and three nullable
-- char-span columns are appended.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_links_new (
    evidence_link_id    TEXT PRIMARY KEY,
    from_node_id        TEXT NOT NULL,
    from_node_type      TEXT NOT NULL DEFAULT 'statement',
    to_source_id        TEXT NOT NULL REFERENCES sources(source_id),
    relation            TEXT NOT NULL
        CHECK (relation IN ('references', 'supports', 'contradicts', 'corrects', 'substantiates')),
    layer               TEXT NOT NULL DEFAULT 'known_then'
        CHECK (layer IN ('known_then', 'presented_then', 'ai_thought_then', 'corrected_later', 'actual_later')),
    -- pointer object (§2.1) — locator_kind widened to add 'char_span'.
    locator_kind        TEXT NOT NULL
        CHECK (locator_kind IN ('timestamp', 'page', 'section', 'paragraph', 'char_span')),
    timestamp_seconds   INTEGER,
    timestamp_human     TEXT,
    page                INTEGER,
    section             TEXT,
    paragraph           INTEGER,
    original_url        TEXT,
    final_url           TEXT,
    archive_url         TEXT,
    archive_status      TEXT NOT NULL DEFAULT 'not_checked'
        CHECK (archive_status IN ('available', 'unavailable', 'not_checked')),
    scan_date           TEXT,
    captured_at_utc     TEXT,
    agenda_item_id      TEXT REFERENCES agenda_items(agenda_item_id),
    is_verbatim         INTEGER NOT NULL DEFAULT 1 CHECK (is_verbatim IN (0, 1)),
    verification_status TEXT NOT NULL DEFAULT 'machine_extracted_unreviewed'
        CHECK (verification_status IN ('source_recorded', 'machine_extracted_unreviewed', 'reviewed_source_linked', 'human_verified', 'disputed', 'do_not_publish')),
    correction_status   TEXT NOT NULL DEFAULT 'none',
    confidence          TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    transcript_path     TEXT,
    deep_link           TEXT,
    created_utc         TEXT,
    ai_extraction_run_id TEXT REFERENCES ai_extraction_runs(run_id),
    -- NEW: char-span pointer (GOV-137). Half-open [char_start, char_end) offsets
    -- into the preserved source text; quoted_text is the verbatim span they
    -- select. Required-together when locator_kind='char_span' (enforced in
    -- scripts/statements.py, not by a single-row CHECK). Vault-only.
    char_start          INTEGER,
    char_end            INTEGER,
    quoted_text         TEXT
);

-- Copy every landed row. Columns are listed explicitly (NOT SELECT *) so the
-- copy is stable; the three new char-span columns default to NULL on existing
-- rows (no historic evidence_link is a char-span anchor).
INSERT INTO evidence_links_new (
    evidence_link_id, from_node_id, from_node_type, to_source_id, relation,
    layer, locator_kind, timestamp_seconds, timestamp_human, page, section,
    paragraph, original_url, final_url, archive_url, archive_status, scan_date,
    captured_at_utc, agenda_item_id, is_verbatim, verification_status,
    correction_status, confidence, transcript_path, deep_link,
    ai_extraction_run_id, created_utc
)
SELECT
    evidence_link_id, from_node_id, from_node_type, to_source_id, relation,
    layer, locator_kind, timestamp_seconds, timestamp_human, page, section,
    paragraph, original_url, final_url, archive_url, archive_status, scan_date,
    captured_at_utc, agenda_item_id, is_verbatim, verification_status,
    correction_status, confidence, transcript_path, deep_link,
    ai_extraction_run_id, created_utc
FROM evidence_links;

-- legacy_alter_table=ON keeps the RENAME from rewriting FK references elsewhere
-- and preserves this table's own outbound FKs naming their target tables.
PRAGMA legacy_alter_table = ON;
DROP TABLE evidence_links;
ALTER TABLE evidence_links_new RENAME TO evidence_links;
PRAGMA legacy_alter_table = OFF;

-- Recreate the 0007 indexes on the rebuilt table.
CREATE INDEX IF NOT EXISTS idx_evidence_links_from_node ON evidence_links(from_node_id, from_node_type);
CREATE INDEX IF NOT EXISTS idx_evidence_links_to_source ON evidence_links(to_source_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_relation ON evidence_links(relation);
