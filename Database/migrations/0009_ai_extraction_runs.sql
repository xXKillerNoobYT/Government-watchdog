-- Stage 1 Slice 3 B (GOV-89): Lane 2 AI extraction — gateway-run ledger +
-- per-record provenance + widen statements.produced_by to permit 'ai'.
--
-- Consumes the GOV-88 (Slice 3 A) gap analysis & interface design
-- (Docs/stage3-ai-gateway-gap-analysis.md) §3 (ledger schema), §5 D-1/D-2/D-4
-- (the additive migration plan + CTO ruling), against contracts 1.09
-- (automation-vs-AI boundary) + 1.11 (publication gates) and
-- AI_GATEWAY_PROCESSING_WORKFLOW.md §17 (run-log requirements).
--
-- Additive + idempotent: db.py's ledger skips an already-applied file, and the
-- new-table/index statements use CREATE ... IF NOT EXISTS. The one statement
-- that is not naturally re-runnable in SQLite is the `statements` CHECK rebuild
-- (SQLite cannot ALTER a CHECK); the ledger is its idempotency guarantee, and a
-- bare re-run still reproduces an equivalent widened schema while preserving every
-- row (see the rebuild block below). Per-row data is never altered, only the
-- value space of produced_by grows (strictly additive in effect).
--
-- SCOPE: Alpine-only, local/vault-only. This migration permits the AI lane at the
-- schema level; the fail-closed defaults (machine_extracted_unreviewed /
-- not_publishable / unreviewed) are unchanged, so permitting 'ai' does not make
-- any AI row publishable. The ledger and its error_detail are reviewer/vault-only
-- and deliberately NOT on publication.WEB_SAFE_FIELD_ALLOWLIST.

PRAGMA foreign_keys = ON;

-- Defer FK enforcement to the runner's final COMMIT. PRAGMA foreign_keys=OFF is a
-- no-op inside the runner's open transaction; defer_foreign_keys IS settable mid-
-- transaction and makes the drop/rename of `statements` (referenced by
-- speaker_attributions + made_statement + a self-FK) safe — integrity is verified
-- once, at commit, when the rebuilt table is in place with identical rows.
PRAGMA defer_foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- §3.1 ai_extraction_runs — the gateway-run ledger (AI_GATEWAY §17; 1.09 step 17;
-- 1.11 §6.5). One row per gateway run; records the input source/segment set, the
-- model/tool version, the produced artifact ids, errors, reviewer state, and the
-- forward-only retry chain. Vault-only — never web-projected.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_extraction_runs (
    run_id                   TEXT PRIMARY KEY,
    lane                     TEXT NOT NULL DEFAULT '2_extraction'
        CHECK (lane IN ('2_extraction', '3_verification', '4_risk')),
    input_source_ids         TEXT NOT NULL DEFAULT '[]',
    input_segment_ids        TEXT,
    model_name               TEXT,
    model_version            TEXT,
    tool_version             TEXT,
    prompt_id                TEXT,
    output_statement_ids     TEXT NOT NULL DEFAULT '[]',
    output_evidence_link_ids TEXT NOT NULL DEFAULT '[]',
    output_count             INTEGER NOT NULL DEFAULT 0,
    orphan_rejected_count    INTEGER NOT NULL DEFAULT 0,
    error_status             TEXT NOT NULL DEFAULT 'ok'
        CHECK (error_status IN ('ok', 'partial', 'failed')),
    error_detail             TEXT,
    reviewer_state           TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (reviewer_state IN ('unreviewed', 'in_review', 'approved', 'rejected')),
    retry_of_run_id          TEXT REFERENCES ai_extraction_runs(run_id),
    retry_count              INTEGER NOT NULL DEFAULT 0,
    dry_run                  INTEGER NOT NULL DEFAULT 1 CHECK (dry_run IN (0, 1)),
    started_utc              TEXT,
    finished_utc             TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_runs_lane ON ai_extraction_runs(lane);
CREATE INDEX IF NOT EXISTS idx_ai_runs_retry_of ON ai_extraction_runs(retry_of_run_id);

-- ---------------------------------------------------------------------------
-- §5 D-1 (CTO ruling) — widen statements.produced_by to ('automation','ai','human')
-- via the guarded SQLite table rebuild. This mirrors landed precedent:
-- 0005_ssot_publication.sql already carries the identical widened literal on
-- sources.produced_by. The rebuild reproduces an existing in-schema constraint,
-- touches no row value, and only grows the value space.
--
-- statements_new is byte-identical to the 0007 `statements` table EXCEPT the
-- produced_by CHECK now includes 'ai'. Self-FK references the FINAL name.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS statements_new (
    statement_id           TEXT PRIMARY KEY,
    segment_id             TEXT REFERENCES transcript_segments(segment_id),
    agenda_item_id         TEXT REFERENCES agenda_items(agenda_item_id),
    speaker_attribution_id TEXT,
    statement_text         TEXT NOT NULL,
    is_verbatim            INTEGER NOT NULL DEFAULT 1 CHECK (is_verbatim IN (0, 1)),
    layer                  TEXT NOT NULL DEFAULT 'known_then'
        CHECK (layer IN ('known_then', 'presented_then', 'ai_thought_then', 'corrected_later', 'actual_later')),
    produced_by            TEXT NOT NULL DEFAULT 'automation' CHECK (produced_by IN ('automation', 'ai', 'human')),
    verification_status    TEXT NOT NULL DEFAULT 'machine_extracted_unreviewed'
        CHECK (verification_status IN ('source_recorded', 'machine_extracted_unreviewed', 'reviewed_source_linked', 'human_verified', 'disputed', 'do_not_publish')),
    correction_status      TEXT NOT NULL DEFAULT 'none',
    review_state           TEXT NOT NULL DEFAULT 'unreviewed',
    publication_state      TEXT NOT NULL DEFAULT 'not_publishable' CHECK (publication_state IN ('not_publishable', 'publishable')),
    source_changed         INTEGER NOT NULL DEFAULT 0 CHECK (source_changed IN (0, 1)),
    ui_status              TEXT,
    confidence             TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    updates_statement_id   TEXT REFERENCES statements(statement_id),
    created_utc            TEXT
);

-- Copy every landed row. Columns are listed explicitly (NOT SELECT *) so the
-- copy is stable across the later ai_extraction_run_id ADD COLUMN and across a
-- bare (ledger-less) re-run.
INSERT INTO statements_new (
    statement_id, segment_id, agenda_item_id, speaker_attribution_id,
    statement_text, is_verbatim, layer, produced_by, verification_status,
    correction_status, review_state, publication_state, source_changed,
    ui_status, confidence, updates_statement_id, created_utc
)
SELECT
    statement_id, segment_id, agenda_item_id, speaker_attribution_id,
    statement_text, is_verbatim, layer, produced_by, verification_status,
    correction_status, review_state, publication_state, source_changed,
    ui_status, confidence, updates_statement_id, created_utc
FROM statements;

-- legacy_alter_table=ON keeps the RENAME from rewriting the FK references in
-- speaker_attributions/made_statement (they already name "statements" and must
-- keep doing so) and the self-FK in the rebuilt table.
PRAGMA legacy_alter_table = ON;
DROP TABLE statements;
ALTER TABLE statements_new RENAME TO statements;
PRAGMA legacy_alter_table = OFF;

-- Recreate the 0007 indexes on the rebuilt table.
CREATE INDEX IF NOT EXISTS idx_statements_segment_id ON statements(segment_id);
CREATE INDEX IF NOT EXISTS idx_statements_agenda_item_id ON statements(agenda_item_id);
CREATE INDEX IF NOT EXISTS idx_statements_updates ON statements(updates_statement_id);

-- ---------------------------------------------------------------------------
-- §3.3 D-2 — per-record provenance: each AI-produced row names its run. Nullable
-- (every existing automation/human row stays untouched and valid). db.py guards
-- each ADD COLUMN with PRAGMA table_info, so these are idempotent. Reviewer/
-- provenance state — NOT on the web-safe allowlist.
-- ---------------------------------------------------------------------------
ALTER TABLE statements ADD COLUMN ai_extraction_run_id TEXT REFERENCES ai_extraction_runs(run_id);
ALTER TABLE evidence_links ADD COLUMN ai_extraction_run_id TEXT REFERENCES ai_extraction_runs(run_id);
