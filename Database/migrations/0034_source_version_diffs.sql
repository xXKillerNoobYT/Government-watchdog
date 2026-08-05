-- GOV-1685 (Stage 5, R1/Slice 2): late-change detection + structured before/after
-- diff over a preserved civic source-version pair. Slice 2 of the 2026-reopen
-- reconciliation (Docs/company-os/stage5-2026-reopen-reconciliation.md, Part A
-- §A2, Part C Slice 2, Part D §D-3). Consumes Slice 1's source_versions
-- (migration 0033): nothing can be reprocessed until the change between two
-- preserved versions is DETECTED and STRUCTURED. This is the artifact Slice 4's
-- six-lens rerun later consumes as its {old, new, diff} input.
--
-- TWO new additive tables. NO ALTER on any existing table, so the frozen serving
-- surfaces (read_api, ai_risk_gate, stage5_agenda_board, mcp_service/) and every
-- prior record model stay byte-for-byte unaffected. Additive + idempotent:
-- CREATE ... IF NOT EXISTS, so the db.py ledger fast-path and a bare re-run are
-- both safe (INV-2/INV-5). One statement per semicolon, no semicolons inside
-- literals, full-line comments only (db._statements splitter contract, INV-7).
--
-- Migration slot: 0034. Highest on origin/main is 0033 (Slice 1); 0034 is the
-- next free slot (re-verified against the migrations dir and open branches).
--
-- SHAPE, and where it comes from. A detected change is ONE header row per version
-- pair (source_version_changes), carrying the pair binding, the stable content
-- hash of the whole structured diff (the reproducibility anchor, AC-3), and the
-- deterministic lateness verdict (A2's "viewed/notified after retrieval" +
-- meeting-proximity trigger). The structured diff itself is one or more anchored
-- SEGMENT rows (source_version_diff_segments): each anchored to a civic locator
-- in the CLOSED set {page, section, agenda_item, meeting, attachment} with its
-- before/after detail and a code-derived materiality_reason. A raw text blob is
-- never stored; the diff is structured and anchored.
--
-- DETERMINISM (Directive 7 / slot .09): change detection, diffing, the change/
-- segment ids, the change_hash, materiality derivation, anchor resolution, and
-- the lateness verdict are ALL computed IN CODE (scripts/stage5_source_diff.py),
-- never by a model. These tables only store those facts.
--
-- FAIL-CLOSED: anchor_type is a CLOSED set enforced by CHECK; an unknown anchor
-- is rejected, not stored. A version never diffs against itself
-- (old_version_id <> new_version_id). The edge is exactly one change per pair
-- (UNIQUE), so a re-diff is a no-op, not a duplicate artifact (AC-1/AC-3).
--
-- FK INDEXING (INV-8): old_version_id, new_version_id and change_id are all
-- REFERENCES child columns and are DELIBERATELY left unindexed. No shipped code
-- issues a DELETE against source_versions or source_version_changes, so a parent
-- DELETE never full-scans these tables; adding indexes would cost write
-- amplification to solve a problem no code has
-- (test_no_shipped_delete_targets_a_parent_with_unindexed_children guards the
-- precondition). The UNIQUE constraints below DO create sqlite_autoindex_*
-- covering every read path this feature needs -- including the child-by-parent
-- segment lookup (UNIQUE (change_id, ...)).

PRAGMA foreign_keys = ON;

-- One row per DETECTED change on a version pair -- the diff header.
--   * (old_version_id, new_version_id) UNIQUE -- exactly one detected change per
--     pair; a re-diff of the same pair is a no-op, never a duplicate artifact.
--   * old <> new -- a version never supersedes/diffs against itself.
--   * change_hash -- sha256 over the canonical ordered segments; identical inputs
--     reproduce it byte-for-byte (AC-3). Stored once here, not per segment.
--   * late_change / lateness_basis -- the deterministic lateness verdict. Paired
--     CHECK: a basis is present exactly when late_change is 1 (a late change must
--     say WHY; a non-late change carries no basis).
CREATE TABLE IF NOT EXISTS source_version_changes (
    -- identity. Explicit NOT NULL closes SQLite's legacy TEXT-PK-accepts-NULL hole.
    change_id          TEXT PRIMARY KEY NOT NULL,
    -- the canonical crawled civic source URL both versions belong to.
    source_url         TEXT NOT NULL,
    -- the prior version (the "before"). REFERENCES source_versions -- no index
    -- (INV-8).
    old_version_id     TEXT NOT NULL REFERENCES source_versions(version_id),
    -- the newer version (the "after"). REFERENCES source_versions -- no index.
    new_version_id     TEXT NOT NULL REFERENCES source_versions(version_id),
    -- sha256 over the canonical ordered structured diff -- the reproducibility
    -- anchor (AC-3), computed in code, never by a model.
    change_hash        TEXT NOT NULL,
    -- deterministic lateness verdict (0/1) -- did this change land late relative
    -- to the meeting / after users were notified of the prior version.
    late_change        INTEGER NOT NULL DEFAULT 0 CHECK (late_change IN (0, 1)),
    -- the code-derived reason a change is late (a closed vocabulary in code),
    -- NULL when the change is not late.
    lateness_basis     TEXT,
    -- when the change was detected (UTC ISO-8601).
    detected_utc       TEXT NOT NULL,
    -- exactly one detected change per version pair (autoindex + idempotency).
    UNIQUE (old_version_id, new_version_id),
    -- a version never diffs against itself.
    CHECK (old_version_id <> new_version_id),
    -- a late change must carry a basis; a non-late change carries none.
    CHECK ((lateness_basis IS NULL) = (late_change = 0))
);

-- One row per anchored segment of a detected change -- the STRUCTURED diff.
--   * anchor_type CHECK -- the CLOSED civic-locator set. An unknown anchor is
--     rejected, not stored (fail-closed).
--   * (change_id, anchor_type, anchor_ref) UNIQUE -- idempotency: re-diffing the
--     same pair reproduces the same segments, never duplicates them. This
--     autoindex also serves the child-by-parent "segments for this change"
--     lookup, so change_id needs no separate index (INV-8).
--   * (change_id, segment_ordinal) UNIQUE -- 1-based deterministic ordering
--     within a change, so the canonical serialization (hence change_hash) is
--     stable.
CREATE TABLE IF NOT EXISTS source_version_diff_segments (
    -- identity. Explicit NOT NULL closes the legacy TEXT-PK-accepts-NULL hole.
    segment_id         TEXT PRIMARY KEY NOT NULL,
    -- the detected change this segment belongs to. REFERENCES -- no index (INV-8);
    -- the UNIQUE (change_id, ...) autoindex covers the by-parent read.
    change_id          TEXT NOT NULL REFERENCES source_version_changes(change_id),
    -- the civic locator kind -- CLOSED set, fail-closed CHECK.
    anchor_type        TEXT NOT NULL CHECK (
        anchor_type IN ('page', 'section', 'agenda_item', 'meeting', 'attachment')
    ),
    -- the specific locator within the anchor kind (e.g. an agenda item ref, a
    -- page number, the attachment URL). Deterministic, caller/parse-supplied.
    anchor_ref         TEXT NOT NULL,
    -- canonical JSON of the before field-set at this anchor (the JSON string
    -- 'null'/'{}' for an added anchor -- never SQL NULL: the column is present).
    before_detail      TEXT NOT NULL,
    -- canonical JSON of the after field-set at this anchor.
    after_detail       TEXT NOT NULL,
    -- the code-derived materiality reason token (a closed vocabulary in code).
    -- Never a model label. Present on every segment (fail-closed: an ambiguous
    -- change is flagged, never silently dropped).
    materiality_reason TEXT NOT NULL,
    -- 1-based deterministic position within the change (stable serialization).
    segment_ordinal    INTEGER NOT NULL CHECK (segment_ordinal >= 1),
    -- idempotency + child-by-parent autoindex.
    UNIQUE (change_id, anchor_type, anchor_ref),
    -- deterministic ordering within a change.
    UNIQUE (change_id, segment_ordinal)
);
