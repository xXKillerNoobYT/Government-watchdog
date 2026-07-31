-- GOV-1565 [GOV-1523 P4c-2 addendum]: user-initiated account-deletion requests.
--
-- GOV-1539 (iOS 4c-3) ships the account-deletion *request* screen; this table
-- is the auditable lifecycle record it routes into. A deletion request is the
-- account owner asking to be removed -- NOT an owner/reviewer access decision --
-- so it deliberately does not mint an `access_grants` tier (those require a
-- non-null `owner_decision_ref`, 0025 §3, and are the owner's lever, not the
-- user's). It is instead its own append-only request an owner later actions;
-- the gated beta queues the request and performs no hard delete.
--
-- No new `beta_audit_log` event: `audit.EVENTS` and that table's CHECK enum are
-- a matched pair (GOV-1664 / #193) a new event would desynchronize. This row --
-- carrying who asked (user_id) and when -- is the better, append-only trail for
-- the request, exactly as `access_grants` is for an authorization change
-- (mirrors the reasoning in `scripts/beta/provision.py`).
--
-- Idempotent by construction: `UNIQUE (user_id, status)` collapses repeat
-- 'requested' submissions onto one open row, so a double-tap in the app is a
-- no-op, never a second row or an error. Only the user_id (a uuid, not PII)
-- and timestamps are stored; no email/token/code ever reaches a column here.
--
-- Migration slot: 0032 is the first free slot on origin/main (latest =
-- 0031_supplied_file_provenance_note.sql). If another 0032 lands first, rebase
-- onto the next free slot; the MIGRATION_ALLOWLIST entry follows.
CREATE TABLE IF NOT EXISTS account_deletion_requests (
    request_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    status          TEXT NOT NULL DEFAULT 'requested'
                        CHECK (status IN ('requested', 'cancelled', 'completed')),
    requested_utc   TEXT NOT NULL,
    updated_utc     TEXT NOT NULL,
    UNIQUE (user_id, status)
);

CREATE INDEX IF NOT EXISTS idx_account_deletion_requests_user
    ON account_deletion_requests(user_id, requested_utc);
