-- GOV-1625 (GOV-1566 B3 schema evolution): split the supplied-file durable
-- record into a VALIDATED LOCATOR and a FREE-TEXT PROVENANCE NOTE. This executes
-- the accepted GOV-1624 contract (BCE answer to UXD GOV-1609 §5): origin_url is a
-- clean, validated http(s) locator; anything a supplier types that is NOT a URL
-- ("handed to me at the June council meeting", "emailed by the clerk") belongs in
-- its own field so it is never mistaken for a link and never auto-linkified.
--
-- ONE additive, forward-only ALTER on the B2 table (supplied_files). NOT a touch
-- of 0028 (F4: existing migrations are immutable; the merge gate rejects any diff
-- to a landed migration). Migration slot: 0031 is the first free slot on
-- origin/main (latest = 0030_supplied_file_versioning.sql). If another 0031 lands
-- first, the merge gate renumbers this file (second-lander-renumbers rule) and
-- the MIGRATION_ALLOWLIST entry follows.
--
-- RE-RUN SAFE two ways: db.apply_migrations records each file in the
-- schema_migrations ledger (runs once ever), AND db._apply_statement special-
-- cases ADD COLUMN via a PRAGMA table_info existence check, so a bare re-run on
-- an already-migrated DB is a no-op rather than a "duplicate column" error.
--
-- NULLABLE, NO DEFAULT (deliberate): a supplied file usually has no free-text
-- note, exactly as origin_url is the only other optional locator column. NULL
-- means "no note", distinct from an empty string. This column is NOT mandatory
-- provenance (unlike area/source_type/supplied_by/captured_at) and does not
-- appear in file_records._MANDATORY_TEXT_FIELDS.
--
-- NOT AN AI COLUMN (GOV-1566 §9, hard gate): provenance_note stores ONLY the
-- supplier's / operator's own free text — never a model summary, classification,
-- extracted claim, or any interpretation. It carries no banned AI token
-- (test_file_records.TestNoAiAsFact pins the whole column set against a denylist).
--
-- One statement per ';', full-line '--' comments only (db.py splitter contract).

PRAGMA foreign_keys = ON;

-- §1 free-text provenance note. Nullable, no default. Sits alongside origin_url
-- (the validated locator); the intake API routes any non-URL prose a supplier
-- puts in origin_url into THIS column instead of storing prose as a "link".
ALTER TABLE supplied_files ADD COLUMN provenance_note TEXT;
