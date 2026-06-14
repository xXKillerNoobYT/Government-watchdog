-- Stage 1, GOV-149: add a `char_span` locator to node_label_aliases so a concept
-- (topic / agenda_thread) alias can ground in a REAL untimed char-span AI row.
--
-- Why this exists (verified against the real vault DB): the GOV-146 reviewer-
-- internal corpus is 6 promoted AI rows whose evidence_links all carry
-- locator_kind='char_span' (the GOV-137 untimed-extraction contract — the 28
-- real Alpine transcripts are untimed ASR, so char offsets are the only honest
-- anchor; see migration 0016). The node-label-alias provenance model (migration
-- 0013) predated 0016 and only modeled timed/page/section/paragraph locators, so
-- a topic alias could not ground in a real char-span row — the GOV-149 seed
-- refused fail-closed ("no web-safe locator"). This migration adds the same
-- char-span anchor to alias sourceRefs that 0016 added to evidence_links:
-- half-open [char_start, char_end) offsets into the preserved source text.
--
-- WEB-SAFE: char_start / char_end are integer offsets (positionally analogous to
-- `page`), not paths — they ARE web-safe and read_api projects them as the
-- alias locator. The `file://` vault provenance URI stays in
-- source_ref_local_ref (reviewer-internal, never projected; GOV-34 sweep).
--
-- Additive + idempotent: db.py's migration ledger skips an already-applied file.
-- node_label_aliases has no CHECK on the locator, so a plain ADD COLUMN suffices
-- (no table rebuild). Strictly additive: existing alias rows default the two new
-- columns to NULL (no historic alias is a char-span anchor).
--
-- SCOPE: Alpine-only, local/vault-only. These are NEW alias pointer fields; the
-- vault path remains in source_ref_local_ref and stays reviewer/vault-only by the
-- fail-closed web-safe allowlist.

PRAGMA foreign_keys = ON;

ALTER TABLE node_label_aliases ADD COLUMN source_ref_char_start INTEGER;
ALTER TABLE node_label_aliases ADD COLUMN source_ref_char_end   INTEGER;
