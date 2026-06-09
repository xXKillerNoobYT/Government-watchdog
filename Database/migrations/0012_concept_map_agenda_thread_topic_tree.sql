-- Stage 1 Slice 4 Prereq-0 (GOV-98): concept-map registry additions —
-- agenda_thread node + topic nodes + a generic forward-linking concept_edges
-- table for the new typed edges. Contract: GOV-97 plan Part A.1/A.2.
-- Source: Docs/stage1-slice4-prereq0-read-api-concept-map.md.
--
-- Additive + idempotent (CREATE ... IF NOT EXISTS; db.py ledger skips an
-- already-applied file, and IF NOT EXISTS makes a bare re-run safe regardless).
--
-- Extends — does NOT rebuild — the landed 1.07 schema. The existing relational-FK
-- spine edges (contains_agenda_item, statement_from_segment, references_source,
-- ...) stay as relational FKs on agenda_items/statements/evidence_links; this
-- migration adds ONLY the new GOV-98 forward-linking edges + their endpoint
-- nodes. Per gap-analysis Decision D-1, new record tables use TEXT slug PKs.
--
-- SCOPE LOCK: Alpine-only, reviewer-internal/local, NO public exposure. These
-- are graph-structure tables; they carry NO publication-control columns and
-- re-type NO status enum (the §5 vocabulary is owned by scripts/publication.py;
-- the node/edge type vocabulary is owned by scripts/concept_map.py — neither is
-- redefined here). Eligibility/web-safe gating happens at the read-API boundary,
-- not in these tables.

PRAGMA foreign_keys = ON;

-- agenda_thread (GOV-97 A.1): a durable civic subject that recurs across
-- meetings. Stable identity that groups per-meeting `agenda_item` instances via
-- the `agenda_item_in_thread` edge. Alpine-locked via jurisdiction_id. status is
-- a lifecycle label (open/decided/dormant), NOT a publication-control field.
CREATE TABLE IF NOT EXISTS agenda_threads (
    agenda_thread_id  TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    jurisdiction_id   TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'decided', 'dormant')),
    first_seen_date   TEXT,
    last_seen_date    TEXT,
    created_utc       TEXT
);

-- topic (GOV-97 A.1 "reused as-is"): a flat issue/theme node. No landed topic
-- table existed; this minimal table is storage-required so a topic_rollup chain
-- can be stored and served. The TREE is carried by concept_edges(topic_rollup),
-- never by a parent_id column here (keeps grouping vs. tree concerns separate,
-- GOV-36).
CREATE TABLE IF NOT EXISTS topics (
    topic_id         TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    jurisdiction_id  TEXT,
    created_utc      TEXT
);

-- concept_edges (GOV-97 A.2): a generic, append-only, forward-linking typed-edge
-- table for the NEW GOV-98 edge types only. edge_type is CHECK-constrained to the
-- additions (the existing spine edges remain relational FKs elsewhere and are NOT
-- stored here). from_node_type/to_node_type record the endpoint node types so the
-- read-API can validate endpoints against scripts/concept_map.py without a JOIN.
--
-- created_by/note are forward-compatible audit/movement-provenance columns
-- (BEH-TOPICTREE-1). They are reviewer-internal and are NEVER projected to the
-- web-safe boundary (publication.WEB_SAFE_FIELD_ALLOWLIST excludes them). The
-- category-move audit LEDGER itself is a frontend-D behavior, not built here.
--
-- UNIQUE(edge_type, from_node_id, to_node_id) makes a duplicate edge insert a
-- no-op identity (idempotent re-link). Acyclicity for topic_rollup is a
-- cross-row invariant a single-row CHECK cannot express; it is enforced by
-- scripts/concept_map.insert_edge() and re-validated at serve time by
-- scripts/read_api.topic_tree().
CREATE TABLE IF NOT EXISTS concept_edges (
    edge_id         TEXT PRIMARY KEY,
    edge_type       TEXT NOT NULL CHECK (edge_type IN (
        'agenda_item_in_thread',
        'agenda_item_supersedes',
        'agenda_item_amends',
        'agenda_item_revisits',
        'topic_rollup',
        'topic_groups')),
    from_node_id    TEXT NOT NULL,
    from_node_type  TEXT NOT NULL,
    to_node_id      TEXT NOT NULL,
    to_node_type    TEXT NOT NULL,
    created_by      TEXT,
    created_utc     TEXT,
    note            TEXT,
    UNIQUE (edge_type, from_node_id, to_node_id)
);
CREATE INDEX IF NOT EXISTS idx_concept_edges_type ON concept_edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_concept_edges_from ON concept_edges(from_node_id, edge_type);
CREATE INDEX IF NOT EXISTS idx_concept_edges_to ON concept_edges(to_node_id, edge_type);
