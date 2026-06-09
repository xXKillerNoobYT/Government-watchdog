-- Stage 1 Slice 4 Prereq-0 — owner-direction addendum (GOV-98 / GOV-97 §A.7):
-- plain-language label layer on every topic + agenda_thread node.
-- Source: Docs/stage1-slice4-prereq0-read-api-concept-map.md §8.
--
-- Additive + idempotent (ALTER ... ADD COLUMN guarded by db.py's PRAGMA
-- table_info check; CREATE ... IF NOT EXISTS). Extends — does NOT rebuild — the
-- 0012 concept-map tables.
--
-- Owner rules (binding): every topic/agenda_thread carries a required
-- `canonical_human_label` (plain-English PRIMARY display); a government/source
-- term is NEVER primary and NEVER dropped — it lives as a `node_label_aliases`
-- row whose `source_ref_*` provenance is MANDATORY ("an alias may not exist
-- without a source trail"). Aliases are append/curate: there is no delete path,
-- so a reviewer can never strip an alias's sourceRef.
--
-- DATA BOUNDARY (GOV-34): `source_ref_local_ref` is a vault/local provenance
-- pointer — reviewer-internal ONLY. It is NEVER projected to the web-safe
-- boundary (publication.WEB_UNSAFE_FIELDS names it; read_api builds the web-safe
-- sourceRef from the public source id + original/archive URL + locator only, and
-- the transport sweep re-proves no local path leaks).

PRAGMA foreign_keys = ON;

-- canonical_human_label: the required plain-English PRIMARY label (e.g.
-- 'general safety', not the government 'public safety'). Nullable at the column
-- level (additive ALTER on existing rows); REQUIRED by concept_map.insert_topic /
-- insert_agenda_thread for any new node.
ALTER TABLE topics ADD COLUMN canonical_human_label TEXT;
ALTER TABLE agenda_threads ADD COLUMN canonical_human_label TEXT;

-- node_label_aliases: one row per source/government alias on a topic/agenda_thread.
-- aliasType ∈ government_term|legal_term|historical_term|agenda_label. The
-- sourceRef provenance is MANDATORY: source_ref_source_id is NOT NULL (DB-level
-- "no alias without a source trail"); concept_map.insert_label_alias() enforces
-- the fuller rule (a ref + a locator). UNIQUE(node, node_type, term, alias_type)
-- makes a re-curate idempotent (append, never duplicate).
CREATE TABLE IF NOT EXISTS node_label_aliases (
    alias_id                  TEXT PRIMARY KEY,
    node_id                   TEXT NOT NULL,
    node_type                 TEXT NOT NULL CHECK (node_type IN ('topic', 'agenda_thread')),
    term                      TEXT NOT NULL,
    alias_type                TEXT NOT NULL CHECK (alias_type IN (
        'government_term', 'legal_term', 'historical_term', 'agenda_label')),
    -- sourceRef (MANDATORY provenance): source/doc id + a ref + a locator.
    source_ref_source_id      TEXT NOT NULL,
    source_ref_original_url   TEXT,
    source_ref_archive_url    TEXT,
    source_ref_local_ref      TEXT,   -- vault/local: reviewer-internal, NEVER web-safe
    source_ref_locator_kind   TEXT,
    source_ref_timestamp_human TEXT,
    source_ref_page           INTEGER,
    source_ref_section        TEXT,
    source_ref_paragraph      INTEGER,
    first_seen_meeting_id     INTEGER REFERENCES meetings(id),
    first_seen_date           TEXT,
    created_by                TEXT,
    created_utc               TEXT,
    UNIQUE (node_id, node_type, term, alias_type)
);
CREATE INDEX IF NOT EXISTS idx_node_label_aliases_node ON node_label_aliases(node_id, node_type);
