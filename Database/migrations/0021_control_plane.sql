-- GOV-733 (implements GOV-719 plan CTRL-2026, rev c4d03918 §3.1): event
-- control plane — signed-webhook envelopes, micro-job orchestration, and the
-- Paperclip outbox. ADDITIVE ONLY: six new tables via CREATE TABLE IF NOT
-- EXISTS; NO ALTER on any existing table, NO new column on a landed row, so
-- every existing row stays untouched and valid. tests/test_smoke.py's exact
-- table-set assertion is extended in the same PR (additive).
--
-- SCOPE (plan §3.3 non-goals): Alpine/local only, registry gitignored per
-- INV-7. No crawler/model/provider changes, no public bind, no broker. These
-- are operational control-plane tables — nothing here touches any
-- publication/reviewer gate or WEB_SAFE_FIELD_ALLOWLIST.
--
-- Migration-number note (GOV-733 scope): GOV-717's accepted plan also reserved
-- 0021; on this branch main tops out at 0020, so 0021 is the next free number.
-- If MCP lands 0021 first, renumber this file to the next free sequential
-- number and keep the content.
--
-- Splitter constraint (db.py _statements): one statement per ';', no ';'
-- embedded in any string literal or trigger. Honoured below.

PRAGMA foreign_keys = ON;

-- Registered ingress principals. secret_ref is the NAME of a local env var /
-- keychain entry that holds the HMAC secret — NEVER the secret itself (INV-7;
-- committing a secret is a hard stop).
CREATE TABLE IF NOT EXISTS webhook_sources (
    source_key  TEXT PRIMARY KEY,
    description TEXT,
    secret_ref  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- Immutable, WRITE-ONCE event record (plain INSERT, no UPDATE path). Only
-- signature-verified requests ever create a row; rejected requests create
-- nothing (AC-2). dedupe_key is UNIQUE — a replay of the same signed content
-- collides here and produces a dedupe-hit instead of a second envelope (AC-1).
CREATE TABLE IF NOT EXISTS event_envelopes (
    envelope_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at      TEXT NOT NULL,
    source_key       TEXT NOT NULL REFERENCES webhook_sources(source_key),
    signature_state  TEXT NOT NULL DEFAULT 'verified',
    canonical_payload TEXT NOT NULL,
    payload_sha256   TEXT NOT NULL,
    source_hash      TEXT NOT NULL,
    area_id          TEXT,
    event_kind       TEXT NOT NULL,
    policy_version   TEXT NOT NULL,
    dedupe_key       TEXT NOT NULL UNIQUE
);

-- Append-only replay/duplicate ledger. One row per repeated delivery of an
-- already-seen dedupe_key; the dedupe-hit-rate metric derives from here while
-- envelope immutability is preserved.
CREATE TABLE IF NOT EXISTS event_dedupe_hits (
    hit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    seen_at    TEXT NOT NULL,
    source_key TEXT NOT NULL
);

-- One row per unit of work. Carries the LED-1 subset (plan §9) so every job is
-- traceable to source/policy/area/cost/quality/reviewer state (AC-5). state is
-- driven only through job_queue.transition().
CREATE TABLE IF NOT EXISTS event_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    envelope_id     INTEGER NOT NULL REFERENCES event_envelopes(envelope_id),
    lane            TEXT NOT NULL,
    area_id         TEXT,
    state           TEXT NOT NULL DEFAULT 'queued',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    not_before      TEXT,
    lease_owner     TEXT,
    lease_expires_at TEXT,
    policy_version  TEXT,
    lens_version    TEXT,
    enqueued_at     TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    queue_wait_s    REAL,
    cpu_s           REAL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    cache_hit       INTEGER,
    quality_outcome TEXT,
    reviewer_outcome TEXT,
    last_error      TEXT
);

-- Append-only state-machine audit. Every applied transition writes exactly one
-- row (AC-3); illegal transitions are refused before any write.
CREATE TABLE IF NOT EXISTS job_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL REFERENCES event_jobs(job_id),
    from_state    TEXT,
    to_state      TEXT NOT NULL,
    reason        TEXT,
    actor         TEXT,
    at            TEXT NOT NULL
);

-- Bounded, safe hand-off to Paperclip. dedupe_key is stable + UNIQUE so relay
-- re-runs are idempotent (AC-6); umbrella_key groups related rows into one
-- Paperclip issue/comment thread (flood-bounded). safe_summary is
-- whitelist-serialized JSON — counts/ids/hashes/states/lanes/areas only, never
-- payloads/source-text/PII/reviewer notes.
CREATE TABLE IF NOT EXISTS paperclip_outbox (
    outbox_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    dedupe_key   TEXT NOT NULL UNIQUE,
    umbrella_key TEXT NOT NULL,
    safe_summary TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    paperclip_ref TEXT
);

-- Hot-path indexes for the poller (queued/failed_retryable by not_before) and
-- lease reaping (leased by lease_expires_at).
CREATE INDEX IF NOT EXISTS idx_event_jobs_poll ON event_jobs (state, not_before);
CREATE INDEX IF NOT EXISTS idx_event_jobs_lease ON event_jobs (state, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_dedupe_hits_key ON event_dedupe_hits (dedupe_key);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON paperclip_outbox (state, umbrella_key);
