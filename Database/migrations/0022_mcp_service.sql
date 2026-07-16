-- CONTRACT-2026-MCP v1.0 (GOV-717 plan §4 / GOV-731 impl leg): the six additive
-- mcp_* tables for the self-contained MCP service layer. NEW tables only; NO
-- ALTER on any existing table, so every landed lane (statements, evidence_links,
-- promotion/anchoring write-once) is byte-for-byte unaffected (INV-1/INV-3).
--
-- Migration-number note (GOV-731 collision warning): the accepted GOV-719
-- control-plane plan also reserved 0021. The GOV-733 control-plane leg landed
-- on main first (PR #105, 0021_control_plane.sql), so per the recorded
-- second-lander-renumbers rule this file takes 0022 (renumbered at the GOV-732
-- merge gate).
--
-- Additive + idempotent: every statement is CREATE ... IF NOT EXISTS, so the
-- db.py ledger fast-path and a bare re-run are both safe. One statement per ';',
-- no semicolons embedded in literals, full-line comments only (db.py splitter
-- contract).

-- §3.1 job.spec backing. A job is the unit of capability scoping: only the
-- statement/segment ids enumerated for a job resolve for that job's grants
-- (context minimization, D4). input_selector is an opaque JSON id list; the MCP
-- boundary never exposes selector internals (allowlist drops it).
CREATE TABLE IF NOT EXISTS mcp_jobs (
    job_id                 TEXT PRIMARY KEY,
    area_id                TEXT,
    job_kind               TEXT NOT NULL,
    input_selector         TEXT NOT NULL DEFAULT '{}',
    policy_pack_id         TEXT,
    policy_pack_version    TEXT,
    status                 TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'closed', 'cancelled')),
    created_utc            TEXT
);

-- §3.3 capability grants. Only the token HMAC (not the token) is stored; a grant
-- carries an exact-match scope allowlist and a budget envelope, and can be
-- revoked. Expired/revoked/out-of-scope tokens fail closed (deny + audit).
CREATE TABLE IF NOT EXISTS mcp_capability_grants (
    grant_id               TEXT PRIMARY KEY,
    job_id                 TEXT NOT NULL REFERENCES mcp_jobs(job_id),
    token_hash             TEXT NOT NULL,
    scopes                 TEXT NOT NULL DEFAULT '[]',
    max_calls              INTEGER NOT NULL DEFAULT 0,
    max_input_units        INTEGER NOT NULL DEFAULT 0,
    max_output_units       INTEGER NOT NULL DEFAULT 0,
    calls_used             INTEGER NOT NULL DEFAULT 0,
    expires_utc            TEXT,
    revoked                INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    created_utc            TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_grants_job ON mcp_capability_grants(job_id);

-- §3.6 policy/lens packs. Data, not code: a pack can never mutate a canonical
-- record (INV-1, structurally guaranteed by D5). Write-once per (pack_id,
-- version): version pinning is caller-visible, no 'latest' resolution.
CREATE TABLE IF NOT EXISTS mcp_policy_packs (
    pack_id                TEXT NOT NULL,
    version                TEXT NOT NULL,
    kind                   TEXT NOT NULL CHECK (kind IN ('lens', 'processing')),
    disclosure             TEXT NOT NULL DEFAULT '{}',
    rules_template         TEXT NOT NULL DEFAULT '',
    required_output_schema_id TEXT NOT NULL,
    content_hash           TEXT NOT NULL,
    created_utc            TEXT,
    PRIMARY KEY (pack_id, version)
);

-- §3.2 submit_output staging sink (D5). The ONLY write surface reachable from an
-- MCP tool. Canonical tables are never written here; promotion of an output back
-- into the graph stays with the existing reviewer-gated lanes.
CREATE TABLE IF NOT EXISTS mcp_job_outputs (
    output_id              TEXT PRIMARY KEY,
    job_id                 TEXT NOT NULL REFERENCES mcp_jobs(job_id),
    grant_id               TEXT,
    output_kind            TEXT NOT NULL,
    body                   TEXT NOT NULL,
    claims                 TEXT NOT NULL DEFAULT '[]',
    policy_pack_id         TEXT NOT NULL,
    policy_pack_version    TEXT NOT NULL,
    output_schema_id       TEXT,
    review_state           TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (review_state IN ('unreviewed', 'approved', 'rejected')),
    created_utc            TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_job_outputs_job ON mcp_job_outputs(job_id);

-- §3.4 audit envelope (LED-1 cost subset). Exactly one row per resource read /
-- tool call / denial. area_id NULL => unattributable shared pool (AREA-2).
CREATE TABLE IF NOT EXISTS mcp_audit_events (
    audit_id               TEXT PRIMARY KEY,
    grant_id               TEXT,
    seq                    INTEGER,
    job_id                 TEXT,
    area_id                TEXT,
    kind                   TEXT NOT NULL,
    name                   TEXT NOT NULL,
    schema_id              TEXT,
    schema_version         TEXT,
    request_hash           TEXT,
    response_hash          TEXT,
    outcome                TEXT NOT NULL CHECK (outcome IN ('allow', 'deny')),
    error_code             TEXT,
    latency_ms             INTEGER,
    queue_wait_s           REAL,
    provider               TEXT,
    model                  TEXT,
    input_units            INTEGER NOT NULL DEFAULT 0,
    output_units           INTEGER NOT NULL DEFAULT 0,
    direct_cost_units      INTEGER NOT NULL DEFAULT 0,
    cache_hit              INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0, 1)),
    retry_count            INTEGER NOT NULL DEFAULT 0,
    policy_version         TEXT,
    lens_version           TEXT,
    created_at             TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_grant ON mcp_audit_events(grant_id);
CREATE INDEX IF NOT EXISTS idx_mcp_audit_job ON mcp_audit_events(job_id);

-- §3.5 provider registry (PORT-3, BUD-5). budget_cap_units DEFAULT 0: a newly
-- registered provider is un-callable until an owner sets a budget. GOV-717 wires
-- the zero-default and the swappable protocol only; enforcement is GOV-718.
CREATE TABLE IF NOT EXISTS mcp_provider_registry (
    provider_id            TEXT PRIMARY KEY,
    kind                   TEXT NOT NULL,
    enabled                INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    budget_cap_units       INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT
);
