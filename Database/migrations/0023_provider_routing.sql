-- PLAN-2026-AI v1.0 (GOV-718 plan §3 / GOV-736 impl leg 1/2): the four additive
-- mcp_* tables that turn the GOV-717/731 seam into call-time enforcement —
-- policy-driven routing, fail-closed budgets, per-provider health. NEW tables
-- only; NO ALTER on any existing table, so every landed lane (statements,
-- evidence_links, mcp_provider_registry, mcp_audit_events, paperclip_outbox,
-- promotion/anchoring write-once) is byte-for-byte unaffected (INV-1/INV-3).
--
-- Migration slot: 0023 is the first free slot on origin/main (latest =
-- 0022_mcp_service.sql). If another 0023 lands first, the merge gate renumbers
-- this file — not the author leg (recorded second-lander-renumbers rule).
--
-- Additive + idempotent: every statement is CREATE ... IF NOT EXISTS, so the
-- db.py ledger fast-path and a bare re-run are both safe. One statement per ';',
-- no semicolons embedded in literals, full-line comments only (db.py splitter
-- contract).

-- §3.1 BUD-1 budget objects. A budget caps spend (in metered units) for a
-- provider over a window. basis is OWNER-SET: a budget only ever exists because
-- an owner created it (BUD-5 default is no budget => un-callable). paused_at is
-- set fail-closed on a breach (D3) and cleared only by an owner/CTO resume that
-- records an audit ref (BUD-4). window_kind bounds the spend query: 'total'
-- (all-time), 'day' (UTC date prefix), or 'month' (UTC year-month prefix).
CREATE TABLE IF NOT EXISTS mcp_budgets (
    budget_id              TEXT PRIMARY KEY,
    provider_id            TEXT NOT NULL,
    area_id                TEXT,
    window_kind            TEXT NOT NULL DEFAULT 'total'
        CHECK (window_kind IN ('total', 'day', 'month')),
    cap_units              INTEGER NOT NULL DEFAULT 0,
    basis                  TEXT NOT NULL DEFAULT 'OWNER-SET',
    paused_at              TEXT,
    created_utc            TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_budgets_provider ON mcp_budgets(provider_id);

-- §3.2 budget ledger (D3/BUD-4). One row per lifecycle event: a fail-closed
-- 'breach' (with the window spend that tripped it), an owner 'pause'/'resume',
-- or an 'owner-change' to the cap. audit_ref ties an owner action back to the
-- audit/interaction that authorized it (BUD-4) — no silent cap changes.
CREATE TABLE IF NOT EXISTS mcp_budget_events (
    event_id               TEXT PRIMARY KEY,
    budget_id              TEXT NOT NULL REFERENCES mcp_budgets(budget_id),
    event_kind             TEXT NOT NULL
        CHECK (event_kind IN ('breach', 'pause', 'resume', 'owner-change')),
    window_start           TEXT,
    spent_units            INTEGER NOT NULL DEFAULT 0,
    cap_units              INTEGER NOT NULL DEFAULT 0,
    audit_ref              TEXT,
    note                   TEXT,
    created_utc            TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_budget_events_budget ON mcp_budget_events(budget_id);

-- §3.3 routing policy rows (D1). Routing is DATA, not code branches. A policy
-- names an ordered provider_preference (JSON array, evaluated left-to-right,
-- deterministic — no randomness), a model, and a per-call output ceiling, keyed
-- by (job_kind, context_class). context_class='local_only' pins a job to local
-- providers (D7). Write-once per (policy_id, version): version pinning is
-- caller-visible, no 'latest' resolution.
CREATE TABLE IF NOT EXISTS mcp_routing_policies (
    policy_id              TEXT NOT NULL,
    version                TEXT NOT NULL,
    job_kind               TEXT NOT NULL,
    context_class          TEXT NOT NULL DEFAULT 'local_only',
    provider_preference    TEXT NOT NULL DEFAULT '[]',
    model                  TEXT NOT NULL DEFAULT '',
    max_output_units       INTEGER NOT NULL DEFAULT 0,
    created_utc            TEXT,
    PRIMARY KEY (policy_id, version)
);
CREATE INDEX IF NOT EXISTS idx_mcp_routing_kind ON mcp_routing_policies(job_kind, context_class);

-- §3.5 per-call provider health. Append-only: one row per adapter call recording
-- outcome ('ok'/'error'), locally-measured latency, and the deny/error code on a
-- failure. A provider whose most recent N calls are all 'error' is degraded and
-- the router skips it (health.is_degraded, threshold default 3) — graceful
-- degradation without a fallback to a non-local provider (D7 fail-closed).
CREATE TABLE IF NOT EXISTS mcp_provider_health (
    health_id              TEXT PRIMARY KEY,
    provider_id            TEXT NOT NULL,
    outcome                TEXT NOT NULL CHECK (outcome IN ('ok', 'error')),
    latency_ms             INTEGER,
    error_code             TEXT,
    created_utc            TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_provider_health_provider ON mcp_provider_health(provider_id);
