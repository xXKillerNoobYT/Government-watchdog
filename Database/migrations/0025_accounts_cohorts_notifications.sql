-- ACCT-2026 v0.2 (GOV-721 plan / GOV-753 impl leg 1/5): secure accounts, beta
-- cohorts, consent, and notifications. Eleven additive tables per the approved
-- plan Docs/2026-accounts-cohorts-notifications-plan-v0.2.md (CEO+CTO approved
-- 2026-07-16 on GOV-751; v0.1's 9 tables + feature_flags + auth_sessions per
-- CTO amendments #3/#4). NEW tables only; NO ALTER on any existing table, so
-- the FOUR frozen serving surfaces (read_api, ai_risk_gate, stage5_agenda_board,
-- mcp_service/) stay byte-for-byte unaffected (AC-8/INV-1).
--
-- Migration slot: 0025 is the first free slot on origin/main (latest =
-- 0024_area_economics.sql). If another 0025 lands first, the merge gate
-- renumbers this file — not the author leg (second-lander-renumbers rule).
--
-- Additive + idempotent: every statement is CREATE ... IF NOT EXISTS, so the
-- db.py ledger fast-path and a bare re-run are both safe. One statement per ';',
-- no semicolons embedded in literals, full-line comments only (db.py splitter
-- contract).
--
-- This migration is schema-only and inert: no email is sent, no cohort opens,
-- no account is created by applying it. Activation stays owner-gated
-- (feature_flags fail-closed, cohort transitions require owner_decision_ref).

-- §1 Users. password_hash is an argon2id PHC-encoded string (D2, INV-7);
-- NULL = pending email-verify. Service layer lowercases+trims email before
-- the UNIQUE check (INV-9).
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    password_hash   TEXT,
    created_utc     TEXT NOT NULL,
    last_login_utc  TEXT
);

-- §2 Waitlist. Minimal intake: email lives on users; area_interest optional.
CREATE TABLE IF NOT EXISTS waitlist_requests (
    request_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    area_interest   TEXT,
    submitted_utc   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'revoked'))
);
CREATE INDEX IF NOT EXISTS idx_waitlist_user ON waitlist_requests(user_id);

-- §3 Access grants (append-only; current state = latest row per user, ordered
-- (granted_utc, rowid) — rowid tie-break because ISO-8601 TEXT timestamps can
-- collide within a second). DB-level enforcement (0024 AREA-5 precedent):
-- ownerless approve/revoke/pause is rejected in-schema by the final CHECK.
CREATE TABLE IF NOT EXISTS access_grants (
    grant_id            TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    tier                TEXT NOT NULL
        CHECK (tier IN ('none', 'waitlisted', 'pending', 'approved', 'revoked', 'paused')),
    owner_decision_ref  TEXT,
    reviewer_id         TEXT,
    granted_utc         TEXT NOT NULL,
    note                TEXT,
    CHECK (tier NOT IN ('approved', 'revoked', 'paused') OR owner_decision_ref IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_access_grants_user ON access_grants(user_id, granted_utc);

-- §4 Cohort state. current_size is a CACHE only — never the enforcement
-- authority; cap enforcement recomputes from cohort_transitions in-transaction
-- (INV-6, service layer Leg 2). owner_decision_ref required to open (service
-- layer; opening is an owner decision like any transition).
CREATE TABLE IF NOT EXISTS cohort_state (
    cohort_id       TEXT PRIMARY KEY,
    max_size        INTEGER NOT NULL,
    current_size    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'closed'
        CHECK (status IN ('closed', 'open', 'full')),
    opened_utc      TEXT,
    owner_decision_ref TEXT
);

-- §5 Cohort membership + transitions (append-only; membership is ADDITIVE per
-- D4 — beta-2 users remain members through beta-3/beta-15). owner_decision_ref
-- is NOT NULL in-schema (INV-3) and re-checked at the service layer.
CREATE TABLE IF NOT EXISTS cohort_transitions (
    transition_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    from_cohort         TEXT,
    to_cohort           TEXT NOT NULL,
    owner_decision_ref  TEXT NOT NULL,
    at_utc              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cohort_transitions_user ON cohort_transitions(user_id);

-- §6 Consent preferences. unsubscribe_token is NULL until first consent is
-- recorded, then a secrets.token_urlsafe(32) value (INV-8), never reused.
CREATE TABLE IF NOT EXISTS consent_preferences (
    user_id             TEXT PRIMARY KEY REFERENCES users(user_id),
    email_consent       INTEGER NOT NULL DEFAULT 0,
    notification_consent INTEGER NOT NULL DEFAULT 1,
    unsubscribe_token   TEXT UNIQUE,
    consented_utc       TEXT,
    updated_utc         TEXT
);

-- §7 In-app notification events. read_utc NULL = unread.
CREATE TABLE IF NOT EXISTS notification_events (
    notif_id        TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    kind            TEXT NOT NULL
        CHECK (kind IN ('access_approved', 'access_revoked', 'cohort_advanced',
                        'consent_recorded', 'unsubscribe_confirmed', 'system')),
    body_text       TEXT NOT NULL,
    read_utc        TEXT,
    created_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notification_events(user_id, created_utc);

-- §8 Email outbox (provider-agnostic; null adapter by default, AC-5).
-- body_text/body_html are a ZERO-LEAK surface (AC-1): no civic data mailed to
-- non-approved users — in SecPriv Leg-4 review scope.
CREATE TABLE IF NOT EXISTS email_outbox (
    outbox_id       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    template_id     TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body_text       TEXT NOT NULL,
    body_html       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed', 'suppressed')),
    adapter_used    TEXT NOT NULL DEFAULT 'null',
    queued_utc      TEXT NOT NULL,
    sent_utc        TEXT
);

-- §9 Email delivery log (append-only audit).
CREATE TABLE IF NOT EXISTS email_delivery_log (
    log_id          TEXT PRIMARY KEY,
    outbox_id       TEXT NOT NULL REFERENCES email_outbox(outbox_id),
    event_kind      TEXT NOT NULL
        CHECK (event_kind IN ('sent', 'delivered', 'bounced', 'complaint',
                              'unsubscribed', 'suppressed', 'failed')),
    provider_ref    TEXT,
    recorded_utc    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_email_log_outbox ON email_delivery_log(outbox_id);

-- §10 Feature flags (append-only, D1; mirrors access_grants / 0024
-- area_transitions AREA-5 precedent). Current state = latest row per
-- flag_name, ordered (at_utc, rowid) tie-break. FAIL-CLOSED: no row => flag
-- off. owner_decision_ref is NOT NULL — activation AND deactivation each
-- carry an explicit Isaac board-card decision (INV-5).
CREATE TABLE IF NOT EXISTS feature_flags (
    flag_seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_name           TEXT NOT NULL,
    enabled             INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    owner_decision_ref  TEXT NOT NULL,
    actor               TEXT,
    at_utc              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feature_flags_name ON feature_flags(flag_name, flag_seq);

-- §11 Auth sessions (session-storage gap fix, CTO amendment #4). token_hash =
-- sha256 of the bearer token; the RAW token is never stored (INV-10). Raw
-- tokens generated with secrets.token_urlsafe(32) (same rule as INV-8
-- unsubscribe tokens). The zero-leak gate resolves token -> user -> latest
-- access_grants tier on EVERY request, so revocation propagates immediately
-- without token-invalidation machinery.
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    token_hash      TEXT NOT NULL UNIQUE,
    issued_utc      TEXT NOT NULL,
    expires_utc     TEXT NOT NULL,
    revoked_utc     TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
