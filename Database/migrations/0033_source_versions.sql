-- GOV-1684 (Stage 5, R1/Slice 1): civic source-version preservation + typed
-- supersession lineage. The chain head of the 2026-reopen reconciliation
-- (Docs/company-os/stage5-2026-reopen-reconciliation.md): nothing can be diffed
-- until two versions of the SAME crawled civic source URL are preserved.
--
-- ONE new additive table. NO ALTER on any existing table, so the frozen serving
-- surfaces (read_api, ai_risk_gate, stage5_agenda_board, mcp_service/) and every
-- prior record model stay byte-for-byte unaffected. Additive + idempotent:
-- CREATE ... IF NOT EXISTS, so the db.py ledger fast-path and a bare re-run are
-- both safe (INV-2). One statement per semicolon, no semicolons inside literals,
-- full-line comments only (db._statements splitter contract, INV-7).
--
-- Migration slot: 0033. Highest on main at merge 64abeff4 is 0032; 0033 is the
-- next free slot (re-verified against open PRs with migration_slot_claims.py).
--
-- SHAPE, and where it comes from. This is the SAME two-versions-retained +
-- flag-on-supersede pattern proven for SUPPLIED files in 0030
-- (supplied_file_supersede_events), reapplied to CRAWLED CIVIC SOURCES. The
-- lineage vocabulary is the closed set the 5.05 corrections ledger already uses:
-- 'supersedes' / 'corrects'. Each retrieved version of a source URL is one row;
-- a changed retrieval (new content_hash for the same URL) is a NEW row that
-- points a typed lineage edge back at the prior version. A prior row is NEVER
-- updated or deleted (history is append-only — the monotonicity seed for D-5).
--
-- DETERMINISM (Directive 7 / slot .09): content_hash and the supersession edge
-- are computed IN CODE (scripts/source_version_store.py), never by a model. This
-- table only stores those facts; it never stores a model summary or label.
--
-- FK INDEXING (INV-8): source_id and supersedes_version_id are REFERENCES child
-- columns and are DELIBERATELY left unindexed. No shipped code issues a DELETE
-- against sources or source_versions, so a parent DELETE never full-scans this
-- table; adding indexes here would cost write amplification to solve a problem no
-- code has (test_no_shipped_delete_targets_a_parent_with_unindexed_children
-- guards the precondition). The two UNIQUE constraints below DO create
-- sqlite_autoindex_* covering every read path this feature needs.

PRAGMA foreign_keys = ON;

-- One row per retrieved version of a civic source URL.
--   * (source_url, content_hash) UNIQUE -- re-preserving identical content for the
--     same URL is a no-op (the writer refuses to insert a duplicate); this
--     autoindex also serves every "versions for this URL" lookup.
--   * (source_url, version_ordinal) UNIQUE -- ordinals are monotonic per URL
--     starting at 1; a DB-level backstop against two concurrent writers minting
--     the same ordinal (the writer also takes BEGIN IMMEDIATE before deciding).
--   * lineage CHECK -- lineage_type is a CLOSED set; an unknown type is rejected,
--     not stored (fail-closed). The paired CHECK ties the edge together: either
--     BOTH lineage columns are NULL (the first/original version, which supersedes
--     nothing) or BOTH are set (every later version).
CREATE TABLE IF NOT EXISTS source_versions (
    -- identity. Explicit NOT NULL closes SQLite's legacy TEXT-PK-accepts-NULL hole.
    version_id             TEXT PRIMARY KEY NOT NULL,
    -- optional link to the registry row this URL belongs to (nullable: a version
    -- may be preserved before/without a registry seed). No index -- see INV-8 note.
    source_id              TEXT REFERENCES sources(source_id),
    -- the canonical crawled civic source URL -- the version-group key.
    source_url             TEXT NOT NULL,
    -- when THIS version was retrieved (UTC ISO-8601), caller-supplied.
    retrieval_time         TEXT NOT NULL,
    -- sha256 of the retrieved content, computed in code (never by a model).
    content_hash           TEXT NOT NULL,
    -- deterministic provenance record (JSON text): how/where this version came
    -- from -- crawl run, fetch method, canonical URL, http status. NOT a model
    -- output. Mandatory: the writer refuses a version it cannot provenance.
    provenance             TEXT NOT NULL,
    -- optional repo-relative path to the preserved raw bytes of this version.
    -- Repo-relative by contract; containment is re-checked at the READ site
    -- (is_relative_to, raising) because Path(root)/value silently discards root
    -- when value is absolute (GOV-1693).
    snapshot_path          TEXT,
    -- 1-based monotonic position within the (source_url) version group.
    version_ordinal        INTEGER NOT NULL CHECK (version_ordinal >= 1),
    -- typed lineage edge to the prior version of the SAME url (NULL on the first).
    supersedes_version_id  TEXT REFERENCES source_versions(version_id),
    lineage_type           TEXT CHECK (lineage_type IN ('supersedes', 'corrects')),
    -- when this row was written (UTC ISO-8601). Distinct from retrieval_time.
    created_utc            TEXT NOT NULL,
    -- idempotency + fast per-URL lookup (autoindex).
    UNIQUE (source_url, content_hash),
    -- monotonic ordinal per URL (autoindex + concurrency backstop).
    UNIQUE (source_url, version_ordinal),
    -- the edge is present exactly when there is a prior version to point at.
    CHECK ((lineage_type IS NULL) = (supersedes_version_id IS NULL))
);
