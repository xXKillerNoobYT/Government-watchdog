-- Stage 1 Slice 2, Issue C (GOV-82): statements + evidence_links.
-- Contract 1.07 §2 (exact-source pointer) + §1.4 (statement relationship
-- semantics / the evidence_link.relation enum). Sequenced in GOV-79 Part C-C;
-- consumes GOV-80 gap analysis §4 + decisions D-3/D-4/D-5
-- (Docs/stage2-transcript-evidence-gap-analysis.md).
--
-- Additive + idempotent (CREATE ... IF NOT EXISTS; db.py ledger skips an
-- already-applied file, and IF NOT EXISTS makes a bare re-run safe regardless).
--
-- Extends — does NOT rebuild — the landed schema. `statements` and
-- `evidence_links` are NEW 1.07 nodes with no landed equivalent (gap analysis §7
-- no-duplication). They link to the Slice-2-B `transcript_segments` /
-- `agenda_items` (0006, TEXT slug PKs) and to the Slice-1 `sources` registry
-- (`source_id` TEXT FK from 0003 — `sources` IS the 1.07 `source_record`). Per
-- gap-analysis Decision D-1 new record tables use TEXT slug PKs.
--
-- ENUM REUSE (gap analysis §5 D-5; 1.07 §5 "introduces no new status
-- vocabulary"): the record-level 6-value `verificationStatus`, the publication
-- allowlist, and `compute_ui_status` are owned by scripts/publication.py and are
-- IMPORTED by the validator — never re-typed. The CHECK literals below mirror
-- ALLOWED_VERIFICATION_STATUSES / ALLOWED_PUBLICATION_STATES; a parity test
-- (tests/test_statements_evidence.py) asserts the SQL constraint and the Python
-- enum cannot drift. Statements carry the 6-value RECORD enum directly (D-5) —
-- NOT the 11-value registry vocabulary on `sources.verification_status`; there
-- is no 11->6 mapping hop for a record-level row.
--
-- SCOPE LOCK (this issue): Alpine-only, local/vault-only, NO AI. `produced_by`
-- on `statements` is CHECK-constrained to `automation|human` ONLY (the §5.4 `ai`
-- value is deliberately excluded this slice — no AI extraction path lands here).
-- Default publication posture is fail-closed: every new statement defaults
-- `publication_state = not_publishable` and `verification_status =
-- machine_extracted_unreviewed` (gated `unverified` via uiStatus-map.v1). The
-- "no orphan claims" rule (1.07 §2.3) is a cross-row disjunction (segment edge
-- OR a complete evidence_link pointer) that a single-row CHECK cannot express;
-- it is enforced by scripts/statements.py insert_statement() and unit-tested.
--
-- DATA BOUNDARY (1.07 §7): `transcript_path` and `deep_link`-adjacent raw
-- locators are vault-only provenance; they must never reach a web-safe
-- projection (publication.WEB_SAFE_FIELD_ALLOWLIST stays the gate).

PRAGMA foreign_keys = ON;

-- statement (1.07 §1.1): a single claim/utterance asserted at a moment. The
-- load-bearing node. Anchored to a transcript_segment via `segment_id` (the
-- `statement_from_segment` spine edge as a relational FK, D-3) and optionally to
-- an agenda_item. `speaker_attribution_id` is a forward pointer ONLY — the
-- `speaker_attributions` table lands in a later slice (1.07 §3); no FK is
-- declared against a table that does not exist yet. `updates_statement_id` is
-- the forward-only correction self-reference (D-4): a `corrected_later` row
-- points back at the `known_then` row it updates and NEVER mutates it.
CREATE TABLE IF NOT EXISTS statements (
    statement_id           TEXT PRIMARY KEY,
    segment_id             TEXT REFERENCES transcript_segments(segment_id),
    agenda_item_id         TEXT REFERENCES agenda_items(agenda_item_id),
    speaker_attribution_id TEXT,
    statement_text         TEXT NOT NULL,
    is_verbatim            INTEGER NOT NULL DEFAULT 1 CHECK (is_verbatim IN (0, 1)),
    layer                  TEXT NOT NULL DEFAULT 'known_then'
        CHECK (layer IN ('known_then', 'presented_then', 'ai_thought_then', 'corrected_later', 'actual_later')),
    -- SSOT record-level columns (D-5): 6-value RECORD verificationStatus enum,
    -- carried directly. `produced_by` excludes 'ai' this slice (scope lock).
    produced_by            TEXT NOT NULL DEFAULT 'automation' CHECK (produced_by IN ('automation', 'human')),
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
CREATE INDEX IF NOT EXISTS idx_statements_segment_id ON statements(segment_id);
CREATE INDEX IF NOT EXISTS idx_statements_agenda_item_id ON statements(agenda_item_id);
CREATE INDEX IF NOT EXISTS idx_statements_updates ON statements(updates_statement_id);

-- evidence_link (1.07 §1.1, §1.4, §2): the typed join from a statement (or other
-- node) to its substantiating source pointer. Carries BOTH the analysis relation
-- (§1.4 `relation` enum) and the exact-source `pointer` object (§2) as flat
-- columns. `to_source_id` is REQUIRED and FK-resolves to the `sources` registry
-- (§2.2 "must resolve to a source_record") — a pointer whose source_id does not
-- resolve is rejected by the FK. `locator_kind` selects which locator field is
-- authoritative; insert_statement() validates that the matching locator is
-- present (§2.3). `transcript_path` and `deep_link` are convenience/private
-- provenance — the locator fields are authoritative (§2.2).
CREATE TABLE IF NOT EXISTS evidence_links (
    evidence_link_id    TEXT PRIMARY KEY,
    from_node_id        TEXT NOT NULL,
    from_node_type      TEXT NOT NULL DEFAULT 'statement',
    to_source_id        TEXT NOT NULL REFERENCES sources(source_id),
    relation            TEXT NOT NULL
        CHECK (relation IN ('references', 'supports', 'contradicts', 'corrects', 'substantiates')),
    layer               TEXT NOT NULL DEFAULT 'known_then'
        CHECK (layer IN ('known_then', 'presented_then', 'ai_thought_then', 'corrected_later', 'actual_later')),
    -- pointer object (§2.1) ---------------------------------------------------
    locator_kind        TEXT NOT NULL CHECK (locator_kind IN ('timestamp', 'page', 'section', 'paragraph')),
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
    -- record-level SSOT (D-5): 6-value RECORD enum, same as statements.
    verification_status TEXT NOT NULL DEFAULT 'machine_extracted_unreviewed'
        CHECK (verification_status IN ('source_recorded', 'machine_extracted_unreviewed', 'reviewed_source_linked', 'human_verified', 'disputed', 'do_not_publish')),
    correction_status   TEXT NOT NULL DEFAULT 'none',
    confidence          TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    transcript_path     TEXT,
    deep_link           TEXT,
    created_utc         TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_links_from_node ON evidence_links(from_node_id, from_node_type);
CREATE INDEX IF NOT EXISTS idx_evidence_links_to_source ON evidence_links(to_source_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_relation ON evidence_links(relation);
