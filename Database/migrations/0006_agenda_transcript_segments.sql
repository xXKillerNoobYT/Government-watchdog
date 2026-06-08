-- Stage 1 Slice 2, Issue B (GOV-81): agenda_items + transcript_segments.
-- Contract 1.07 §1 (meeting -> agenda_item -> transcript_segment).
-- Source: GOV-80 gap analysis §3.1, §4 (Docs/stage2-transcript-evidence-gap-analysis.md).
--
-- Additive + idempotent (CREATE ... IF NOT EXISTS; db.py ledger skips an
-- already-applied file, and IF NOT EXISTS makes a bare re-run safe regardless).
--
-- Extends — does NOT rebuild — the landed Slice-1 schema. `agenda_items` and
-- `transcript_segments` are NEW 1.07 nodes with no landed equivalent (gap
-- analysis §7 no-duplication). They link to the existing `meetings` /
-- `transcripts` tables (INTEGER PKs, untouched) and to the Slice-1 `sources`
-- registry (`source_id` TEXT FK from 0003). Per gap-analysis Decision D-1, new
-- record tables use TEXT slug PKs while existing tables keep their INTEGER PKs.
--
-- Scope lock: Alpine-only, local/vault-only, NO AI. The deterministic segmenter
-- (scripts/segment_transcript.py) only *slices* an already-preserved
-- transcript, so `transcript_segments.is_verbatim` defaults to 1 (faithful
-- verbatim span, never a paraphrase). `transcript_path` is a vault-only/private
-- provenance field (1.07 §7) — it must never be projected to a web-safe surface.
--
-- NOTE (gap-analysis D-5): the 6-value record `verificationStatus`, the
-- `uiStatus` / publication-control columns, and the orphan-claim validator land
-- with `statements` / `evidence_links` (a later Slice-2 issue), NOT here. A
-- transcript_segment is a verbatim span, not yet a publishable claim, so this
-- migration adds no publication-control columns and re-types no enum
-- (the §5 vocabulary is owned by scripts/publication.py and must not be redefined).

PRAGMA foreign_keys = ON;

-- agenda_item (1.07 §1.1): one agenda line within a meeting. Carries the
-- `contains_agenda_item` spine edge as a relational FK to `meetings` (D-3) and
-- the `references_source` edge as a nullable FK to the agenda packet/source in
-- the `sources` registry. TEXT slug PK (e.g. 'alpine:2026-05-08:item-7').
CREATE TABLE IF NOT EXISTS agenda_items (
    agenda_item_id        TEXT PRIMARY KEY,
    meeting_id            INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    item_order            INTEGER,
    title                 TEXT NOT NULL,
    agenda_doc_source_id  TEXT REFERENCES sources(source_id),
    created_utc           TEXT
);
CREATE INDEX IF NOT EXISTS idx_agenda_items_meeting_id ON agenda_items(meeting_id);
CREATE INDEX IF NOT EXISTS idx_agenda_items_source_id ON agenda_items(agenda_doc_source_id);

-- transcript_segment (1.07 §1.1, §2): an addressable timestamped span produced
-- deterministically from `transcripts.timestamped_text` by
-- scripts/segment_transcript.py. The container `transcripts` table is untouched;
-- this turns its opaque blob into addressable rows. Carries the
-- `statement_from_segment` anchor target plus the timestamp locator (§2). Links
-- to its transcript container, its meeting (when one is linked), and the
-- registry source (`source_id` FK from Slice 1).
CREATE TABLE IF NOT EXISTS transcript_segments (
    segment_id        TEXT PRIMARY KEY,
    transcript_id     INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
    meeting_id        INTEGER REFERENCES meetings(id),
    source_id         TEXT REFERENCES sources(source_id),
    segment_index     INTEGER NOT NULL,
    timestamp_seconds INTEGER NOT NULL,
    timestamp_human   TEXT NOT NULL,
    segment_text      TEXT NOT NULL,
    is_verbatim       INTEGER NOT NULL DEFAULT 1 CHECK (is_verbatim IN (0, 1)),
    confidence        TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    transcript_path   TEXT,
    created_utc       TEXT,
    UNIQUE (transcript_id, segment_index)
);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_transcript_id ON transcript_segments(transcript_id);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_meeting_id ON transcript_segments(meeting_id);
CREATE INDEX IF NOT EXISTS idx_transcript_segments_source_id ON transcript_segments(source_id);
