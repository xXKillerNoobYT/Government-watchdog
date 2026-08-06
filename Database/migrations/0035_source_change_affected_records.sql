-- GOV-1688 (Stage 5, R1/Slice 3): affected-set resolver + selective invalidation
-- + statement/evidence<->diff binding. Slice 3 of the 2026-reopen reconciliation
-- (Docs/company-os/stage5-2026-reopen-reconciliation.md, Part A §A3, Part C
-- Slice 3, Part D §D-1). Consumes Slice 2's structured diff (migration 0034:
-- source_version_changes + source_version_diff_segments) and records WHAT that
-- change invalidates: nothing downstream can be reprocessed until we know which
-- canonical records a diff affects.
--
-- ONE new additive table. NO ALTER on any existing table, so the frozen serving
-- surfaces (read_api, ai_risk_gate, stage5_agenda_board, mcp_service/) and every
-- prior record model stay byte-for-byte unaffected. This is deliberate: Slice 3
-- NEVER overwrites canonical content (that is monotonicity, Slice 6) and never
-- runs a rerun (Slice 4). The affected set is expressed ENTIRELY as marker rows
-- in this ledger; the statements/evidence/normalization/review rows it points at
-- are left untouched. Additive + idempotent: CREATE ... IF NOT EXISTS, so the
-- db.py ledger fast-path and a bare re-run are both safe (INV-2/INV-5). One
-- statement per semicolon, no semicolons inside literals, full-line comments only
-- (db._statements splitter contract, INV-7).
--
-- Migration slot: 0035. Highest on origin/main is 0034 (Slice 2); 0035 is the
-- next free slot (re-verified against the migrations dir and open branches).
-- tests/test_migration_slots.py fails and names both files on a collision.
--
-- SHAPE, and where it comes from. This is the invalidation LEDGER + the
-- statement/evidence<->diff BINDING in one table (systems shrink: one ledger, not
-- two). One row per (detected change, diff segment, affected canonical record).
-- The row's EXISTENCE is the invalidation marker -- "this record is affected by
-- this change and must be reprocessed." Because the row carries BOTH change_id
-- and segment_id, it is simultaneously the D-1 binding: a single join reaches
-- source -> source_versions -> source_version_changes -> source_version_diff_segments
-- -> this ledger -> the affected statement/evidence, with no dangling hop.
--
-- record_class is the CLOSED A3 affected-class vocabulary plus the fail-closed
-- 'unresolved' sentinel. Slice 3 resolves the classes that carry a deterministic
-- civic locator today (statement, evidence_link, review, normalization); the
-- classes with no landed locator table (tag, source_grounded_summary,
-- lens_output) are in the vocabulary so later slices add a resolver WITHOUT a
-- constraint rebuild (INV-4), and any segment that resolves to zero concrete
-- records is FLAGGED via the 'unresolved' sentinel -- never silently dropped.
--
-- DETERMINISM (Directive 7 / slot .09): anchor->record matching, the affected
-- set, the affected_id, and the invalidation markers are ALL computed IN CODE
-- (scripts/stage5_affected_set.py), never by a model. This table only stores
-- those facts.
--
-- FAIL-CLOSED: anchor_type is the CLOSED civic-locator set enforced by CHECK; an
-- unknown anchor is rejected, not stored. record_class is CLOSED by CHECK. The
-- 'unresolved' sentinel pairs EXACTLY with resolution 'unresolved_flagged' (a
-- paired CHECK): a segment whose anchor localizes no concrete record widens the
-- affected set with a flagged sentinel row -- it never shrinks it.
--
-- FK INDEXING (INV-8): change_id and segment_id are REFERENCES child columns and
-- are DELIBERATELY left unindexed. No shipped code issues a DELETE against
-- source_version_changes or source_version_diff_segments, so a parent DELETE
-- never full-scans this table; adding indexes would cost write amplification to
-- solve a problem no code has
-- (test_no_shipped_delete_targets_a_parent_with_unindexed_children guards the
-- precondition). The UNIQUE constraint below DOES create a sqlite_autoindex_*
-- whose leading change_id column serves the "affected records for this change"
-- read this feature needs.

PRAGMA foreign_keys = ON;

-- One row per (change, segment, affected record) -- the invalidation ledger AND
-- the statement/evidence<->diff binding.
--   * (change_id, segment_id, record_class, record_id) UNIQUE -- idempotency:
--     re-running resolve+invalidate on the same change marks nothing twice and
--     inserts no duplicate. This autoindex (leading change_id) also serves the
--     child-by-parent "affected records for this change" read.
--   * anchor_type CHECK -- the CLOSED civic-locator set, fail-closed. Mirrors
--     source_version_diff_segments.anchor_type exactly.
--   * record_class CHECK -- the CLOSED A3 affected-class vocabulary + the
--     'unresolved' fail-closed sentinel.
--   * resolution CHECK + the paired CHECK -- 'unresolved' record_class pairs
--     exactly with 'unresolved_flagged'; every concrete class pairs with
--     'resolved'. A flagged segment is never silently dropped.
CREATE TABLE IF NOT EXISTS source_change_affected_records (
    -- identity. Content-addressed in code from
    -- (change_id, segment_id, record_class, record_id) -- deterministic and
    -- byte-stable (AC-5). Explicit NOT NULL closes SQLite's legacy
    -- TEXT-PK-accepts-NULL hole.
    affected_id     TEXT PRIMARY KEY NOT NULL,
    -- the detected change (diff header) this marker belongs to.
    -- REFERENCES -- no index (INV-8); the UNIQUE autoindex covers the by-change read.
    change_id       TEXT NOT NULL REFERENCES source_version_changes(change_id),
    -- the specific anchored diff segment that made this record affected.
    -- REFERENCES -- no index (INV-8).
    segment_id      TEXT NOT NULL REFERENCES source_version_diff_segments(segment_id),
    -- the civic-locator kind of the segment -- CLOSED set, fail-closed CHECK.
    anchor_type     TEXT NOT NULL CHECK (
        anchor_type IN ('page', 'section', 'agenda_item', 'meeting', 'attachment')
    ),
    -- the specific locator the segment anchored to (copied from the segment so a
    -- reviewer reads the ledger without a second join).
    anchor_ref      TEXT NOT NULL,
    -- the affected canonical record class -- CLOSED A3 vocabulary + 'unresolved'.
    record_class    TEXT NOT NULL CHECK (
        record_class IN (
            'normalization', 'statement', 'evidence_link', 'tag',
            'source_grounded_summary', 'lens_output', 'review', 'unresolved'
        )
    ),
    -- the affected record's id (statement_id / evidence_link_id / decision_id /
    -- alias_id). For the 'unresolved' sentinel this is 'anchor_type:anchor_ref'
    -- so the UNIQUE key is stable and re-running is a no-op.
    record_id       TEXT NOT NULL,
    -- whether the anchor localized a concrete record ('resolved') or nothing
    -- ('unresolved_flagged', the fail-closed withhold).
    resolution      TEXT NOT NULL CHECK (resolution IN ('resolved', 'unresolved_flagged')),
    -- when this marker was written (UTC ISO-8601). NOT part of affected_id, so
    -- re-derivation is byte-stable regardless of wall-clock (AC-5).
    marked_utc      TEXT NOT NULL,
    -- idempotency + child-by-parent autoindex.
    UNIQUE (change_id, segment_id, record_class, record_id),
    -- the sentinel and the flag are one fact: neither exists without the other.
    CHECK ((record_class = 'unresolved') = (resolution = 'unresolved_flagged'))
);
