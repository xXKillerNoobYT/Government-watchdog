-- 0020_agenda_item_provenance.sql
-- GOV-698 (agenda-anchoring leg 2 of 6). Additive-only.
--
-- Problem (GOV-697 spec §1): agenda_items.agenda_doc_source_id points only at the
-- single shared corpus `sources` row (`alpine_local_corpus`) and therefore cannot
-- identify the exact agenda document a row was extracted from, nor the exact line
-- span within it. Anchoring the pilot would leave agenda items with no verifiable
-- provenance.
--
-- Fix: two additive columns recording the exact source document and the exact
-- line span in that document's raw text (e.g. `lines:8-10`). Every agenda_items
-- row inserted by scripts/agenda_anchor_batch.py carries both, so no agenda item
-- is an orphan and every card resolves to a citable source span.
--
-- Safety: agenda_items has 0 rows today; these are nullable ADD COLUMNs with no
-- default rewrite; no serving/gate module (read_api, ai_risk_gate,
-- stage5_agenda_board) is touched. Re-run safe: db.apply_migrations guards each
-- ADD COLUMN with a PRAGMA table_info existence check.

ALTER TABLE agenda_items ADD COLUMN source_document_id INTEGER REFERENCES documents(id);
ALTER TABLE agenda_items ADD COLUMN citation_target TEXT;

CREATE INDEX IF NOT EXISTS idx_agenda_items_source_document_id
    ON agenda_items(source_document_id);
