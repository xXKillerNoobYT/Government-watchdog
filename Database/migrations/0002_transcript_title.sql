-- Phase 1.5 (WEI-70): add transcripts.title so the metadata extractor can
-- carry the YouTube-provided meeting title (e.g.
-- "Town of Alpine Town Council Meeting 03/03/2026") into the corpus.

ALTER TABLE transcripts ADD COLUMN title TEXT;
CREATE INDEX IF NOT EXISTS idx_transcripts_meeting_date ON transcripts(meeting_date);
