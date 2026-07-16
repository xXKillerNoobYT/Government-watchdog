-- LEDGER-2026 v0.1 (GOV-720 plan §1 / GOV-743 impl leg 1/2): the nine additive
-- tables for empirical area-cost, capacity, funding, and entitlement accounting
-- (REQ-2026-COMM v1.0 §2/§6/§9). This is an AGGREGATION build over the cost
-- substrate that 0021 (event_jobs), 0022 (mcp_audit_events), 0023 (mcp_budget_*),
-- 0019 (ai_extraction_runs, crawl_runs) already landed — it does NOT re-instrument
-- any worker. NEW tables only; NO ALTER on any existing table, so every landed
-- lane and the frozen serving surfaces (read_api.reviewer_internal_records,
-- ai_risk_gate, stage5_agenda_board) stay byte-for-byte unaffected (INV-1/INV-3).
--
-- Migration slot: 0024 is the first free slot on origin/main (latest =
-- 0023_provider_routing.sql). If another 0024 lands first, the merge gate (GOV-744)
-- renumbers this file — not the author leg (recorded second-lander-renumbers rule).
--
-- Additive + idempotent: every statement is CREATE ... IF NOT EXISTS, so the
-- db.py ledger fast-path and a bare re-run are both safe. One statement per ';',
-- no semicolons embedded in literals, full-line comments only (db.py splitter
-- contract).

-- §1.1 AREA-1..3 rollup spine. The canonical table today's loose area_id TEXT
-- tags point at: town -> county -> state via parent_area_id. area_id IS NULL is
-- the disclosed shared-cost pool (AREA-2) and is never smeared silently onto a
-- named area; it is aggregated separately.
CREATE TABLE IF NOT EXISTS areas (
    area_id        TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('town', 'county', 'state')),
    name           TEXT NOT NULL,
    parent_area_id TEXT REFERENCES areas(area_id),
    created_utc    TEXT
);
CREATE INDEX IF NOT EXISTS idx_areas_parent ON areas(parent_area_id);

-- §1.2 AREA-4 current state. Default 'locked': an area is un-served until an
-- owner decision moves it (define-not-activate). The legal-transition table is
-- enforced in code (economics/areas.py), not by trigger, so AM-1 unit tests can
-- assert every illegal edge is rejected BEFORE any write.
CREATE TABLE IF NOT EXISTS area_state (
    area_id     TEXT PRIMARY KEY REFERENCES areas(area_id),
    state       TEXT NOT NULL DEFAULT 'locked'
        CHECK (state IN ('locked', 'free_home', 'free_beta', 'funded', 'paid', 'limited')),
    updated_utc TEXT
);

-- §1.2 AREA-5 append-only transition audit. One row per applied transition.
-- owner_decision_ref is NOT NULL: there is no ownerless transition (AREA-5).
-- rule_evaluated snapshots the rule id + inputs that justified the move.
CREATE TABLE IF NOT EXISTS area_transitions (
    transition_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id            TEXT NOT NULL REFERENCES areas(area_id),
    from_state         TEXT,
    to_state           TEXT NOT NULL,
    owner_decision_ref TEXT NOT NULL,
    rule_evaluated     TEXT,
    at_utc             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_area_transitions_area ON area_transitions(area_id);

-- §1.3 F-ELIG funding inputs (designed, not activated). Append-only donations /
-- grants / allocations in OWNER-SET units. amount_units is NEVER auto-derived;
-- basis defaults to OWNER-SET so a report can never mistake it for a measured
-- figure.
CREATE TABLE IF NOT EXISTS area_funding_entries (
    entry_id     TEXT PRIMARY KEY,
    area_id      TEXT NOT NULL REFERENCES areas(area_id),
    period       TEXT NOT NULL,
    amount_units INTEGER NOT NULL,
    basis        TEXT NOT NULL DEFAULT 'OWNER-SET',
    note         TEXT,
    created_utc  TEXT
);
CREATE INDEX IF NOT EXISTS idx_area_funding_entries_area ON area_funding_entries(area_id, period);

-- §1.3 AREA-6 per-area OWNER-SET safety_factor for the F-ELIG rule
-- (monthly_measured_cost <= monthly_funding_balance * safety_factor). One row per
-- area; unset areas fall back to the OWNER-SET default 1.0 in code.
CREATE TABLE IF NOT EXISTS area_funding_policy (
    area_id       TEXT PRIMARY KEY REFERENCES areas(area_id),
    safety_factor REAL NOT NULL DEFAULT 1.0,
    updated_utc   TEXT
);

-- §1.4 GATE-P paid entitlement DESIGN (schema + evaluator only). No dollar/price
-- column: pricing lives in the business-plan lane, never here. state='designed'
-- is the ONLY state this build ever writes; leaving it requires an
-- owner_decision_ref and is GATE-P downstream (GOV-721+).
CREATE TABLE IF NOT EXISTS area_entitlements (
    entitlement_id     TEXT PRIMARY KEY,
    area_id            TEXT NOT NULL REFERENCES areas(area_id),
    tier               TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'designed'
        CHECK (state IN ('designed', 'offered', 'active', 'revoked')),
    owner_decision_ref TEXT,
    created_utc        TEXT
);
CREATE INDEX IF NOT EXISTS idx_area_entitlements_area ON area_entitlements(area_id);

-- §1.5 LED-4 / F2 / F7 monthly fixed-cost allocation. fixed_total_units is the
-- OWNER-SET monthly infra allocation; weight_basis is the F7 assumption
-- (document_share default) disclosed on every rollup; basis OWNER-SET.
CREATE TABLE IF NOT EXISTS ledger_fixed_costs (
    period            TEXT PRIMARY KEY,
    fixed_total_units INTEGER NOT NULL,
    weight_basis      TEXT NOT NULL DEFAULT 'document_share',
    basis             TEXT NOT NULL DEFAULT 'OWNER-SET',
    created_utc       TEXT
);

-- §1.6 LED-2 reviewer-work meter. reviewer_minutes MEASURED when captured, else
-- proxy = decision_count * per_decision_units (OWNER-SET constant) with basis
-- DERIVED. correction / rejection / source-coverage rates are MEASURED.
CREATE TABLE IF NOT EXISTS ledger_reviewer_work (
    batch_id             TEXT PRIMARY KEY,
    area_id              TEXT REFERENCES areas(area_id),
    period               TEXT NOT NULL,
    reviewer_minutes     REAL,
    decision_count       INTEGER,
    per_decision_units   REAL,
    correction_rate      REAL,
    rejection_rate       REAL,
    source_coverage_rate REAL,
    basis                TEXT NOT NULL,
    created_utc          TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_reviewer_work_area ON ledger_reviewer_work(area_id, period);

-- §1.7 report reproducibility ledger. content_sha256 is the canonical hash of the
-- emitted pack; verify-hash recomputes and asserts equality (reproducibility
-- proof). area_id NULL = a multi-area rollup pack.
CREATE TABLE IF NOT EXISTS ledger_report_runs (
    report_id      TEXT PRIMARY KEY,
    area_id        TEXT,
    period         TEXT NOT NULL,
    scope          TEXT NOT NULL
        CHECK (scope IN ('area', 'county_rollup', 'state_rollup')),
    content_sha256 TEXT NOT NULL,
    generated_utc  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_report_runs_area ON ledger_report_runs(area_id, period);
