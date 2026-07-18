-- GOV-801 (GOV-799 leg): magic-link auth + allowlist + waitlist backend for the
-- gated-beta front door. Five NEW additive tables — NO ALTER on any existing
-- table, so the four frozen serving surfaces (read_api, ai_risk_gate,
-- stage5_agenda_board, mcp_service/) stay byte-for-byte unaffected.
--
-- Migration slot: 0026 is the first free slot on origin/main (latest =
-- 0025_accounts_cohorts_notifications.sql). If another 0026 lands first, the
-- merge gate renumbers this file (second-lander-renumbers rule).
--
-- Additive + idempotent: every statement is CREATE ... IF NOT EXISTS, so the
-- db.py ledger fast-path and a bare re-run are both safe. One statement per ';',
-- no semicolons embedded in literals, full-line comments only (db.py splitter
-- contract).
--
-- This migration is schema-only and INERT: applying it sends no email, admits
-- no user, opens no route. The HTTP surface answers 404 until the owner-gated
-- feature flag `beta_gate_enabled` is enabled, and no email leaves the machine
-- until a real email adapter is owner-registered (fail-closed, D1/INV-5).
--
-- Privacy (GOV-801): operational tables hold the address they must (to match
-- the allowlist and to address mail); the audit log NEVER does — it carries
-- only email_hash (sha256) and ip_hint (a truncated sha256), never a raw email
-- or raw IP.

-- §1 beta_allowlist. Only owner-approved emails may ever receive a magic link.
-- owner_decision_ref is NOT NULL: adding an email is an owner decision (a real
-- Isaac board card), same posture as access_grants/feature_flags. status flips
-- to 'revoked' on owner revocation, which also revokes that email's sessions.
CREATE TABLE IF NOT EXISTS beta_allowlist (
    email               TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    owner_decision_ref  TEXT NOT NULL,
    added_utc           TEXT NOT NULL,
    revoked_utc         TEXT,
    note                TEXT
);

-- §2 beta_magic_tokens. One-time-use, 15-minute TTL. token_hash = sha256 of the
-- raw token (the raw value is emailed once and never stored). consumed_utc goes
-- non-null on the first successful verify; a second verify of the same token
-- finds it consumed and is rejected.
CREATE TABLE IF NOT EXISTS beta_magic_tokens (
    token_id        TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    created_utc     TEXT NOT NULL,
    expires_utc     TEXT NOT NULL,
    consumed_utc    TEXT,
    ip_hint         TEXT
);
CREATE INDEX IF NOT EXISTS idx_beta_magic_email ON beta_magic_tokens(email, created_utc);

-- §3 beta_sessions. 7-day sessions delivered as an HttpOnly cookie. token_hash =
-- sha256 of the raw cookie value (raw never stored). revoked_utc non-null =
-- signed out or allowlist-revoked; verify checks hash + not-revoked + unexpired.
CREATE TABLE IF NOT EXISTS beta_sessions (
    session_id      TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    issued_utc      TEXT NOT NULL,
    expires_utc     TEXT NOT NULL,
    revoked_utc     TEXT
);
CREATE INDEX IF NOT EXISTS idx_beta_sessions_email ON beta_sessions(email);

-- §4 beta_waitlist. Public intake; no allowlist required. area_interest optional.
CREATE TABLE IF NOT EXISTS beta_waitlist (
    request_id      TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    area_interest   TEXT,
    submitted_utc   TEXT NOT NULL,
    ip_hint         TEXT
);
CREATE INDEX IF NOT EXISTS idx_beta_waitlist_email ON beta_waitlist(email, submitted_utc);

-- §5 beta_audit_log. Append-only (code writes only INSERTs). No raw email column
-- exists by design: email_hash correlates a subject's events without ever
-- storing the address; ip_hint is a truncated sha256, never a raw IP.
CREATE TABLE IF NOT EXISTS beta_audit_log (
    audit_id        TEXT PRIMARY KEY,
    event           TEXT NOT NULL
        CHECK (event IN ('magic_link_requested', 'magic_link_sent',
                         'magic_link_verified', 'magic_link_rejected',
                         'session_issued', 'session_revoked', 'waitlist_joined',
                         'allowlist_added', 'allowlist_revoked', 'rate_limited')),
    email_hash      TEXT,
    ip_hint         TEXT,
    detail          TEXT,
    at_utc          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beta_audit_at ON beta_audit_log(at_utc);
