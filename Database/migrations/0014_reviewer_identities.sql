-- Stage 1 (GOV-131): reviewer-identity registry — the source of truth the Lane-5
-- reviewer-gate allowlists against.
--
-- Builds subtask A (mechanism) of the GOV-130 ADR
-- (issue GOV-130 #document-plan §2 — schema; §3 — lookup contract; §5 — data
-- boundary). The Lane-5 gate (scripts/ai_risk_gate.py) is today a DENYLIST:
-- promote_statement / resolve_flag reject only the FORBIDDEN_REVIEWER_IDS
-- automation/AI sentinels and accept any other non-empty reviewer_id. GOV-93
-- hardens that to an ALLOWLIST (default-deny): a reviewer_id may promote only if
-- it resolves to a REGISTERED, ACTIVE human reviewer. That flip needs a source of
-- truth of registered identities — this table is it.
--
-- DESIGN — same fail-closed, append-mostly shape as the 0010/0011 side tables:
-- this migration creates a NEW table only. It mutates no existing table, widens
-- no CHECK, adds no column to `statements` / `evidence_links`. Identities are
-- REVOKED, never hard-deleted (status flips to 'revoked'), so the registry is its
-- own audit trail of who could review and when that was withdrawn.
--
-- Additive + idempotent: db.py's schema_migrations ledger skips an already-applied
-- file, and the table/index use CREATE ... IF NOT EXISTS, so a bare re-run on a
-- fresh OR already-migrated DB is a no-op. NO table rebuild.
--
-- DATA BOUNDARY (ADR §5; 1.11 §2.1; AI_GATEWAY §7.1): reviewer identities are
-- OPERATIONAL metadata, local/vault-only. This table and EVERY one of its columns
-- (display_name / note / registered_by / revoked_* included) are deliberately NOT
-- on publication.WEB_SAFE_FIELD_ALLOWLIST and are never returned by read_api /
-- to_web_safe. A column-name guard test (mirroring the 0011 guard) asserts no
-- reviewer_identities column can ever be web-projected.
--
-- NO SEEDING HERE: this migration creates the EMPTY, fail-closed registry — the
-- safe default (nobody passes the allowlist). WHICH humans are authorized Stage-1
-- reviewers is an owner decision (GOV-130 subtask B, escalated to CEO -> Isaac);
-- seeding lands there, not in this migration.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- reviewer_identities — the allowlist of authenticated human reviewer identities
-- the Lane-5 gate consumes via ai_risk_gate.is_registered_reviewer(). One row per
-- reviewer id. `status` gates membership: 'active' => may promote; 'revoked' =>
-- immediately excluded (revoke, never DELETE — keeps the audit trail). All
-- contents are reviewer/vault-only evidence (ADR §5) — never web-projected.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviewer_identities (
    reviewer_id     TEXT PRIMARY KEY,            -- stable id the gate allowlists against
    display_name    TEXT NOT NULL,               -- reviewer-facing label (vault-only)
    status          TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')), -- revoke, never DELETE (audit trail)
    registered_utc  TEXT NOT NULL,
    registered_by   TEXT NOT NULL,               -- who seeded this identity (owner-approved)
    revoked_utc     TEXT,
    revoked_by      TEXT,
    revoked_reason  TEXT,
    note            TEXT                          -- vault-only
);
CREATE INDEX IF NOT EXISTS idx_reviewer_identities_status ON reviewer_identities(status);
