-- GOV-1538 (GOV-1523 P4c-2): 6-digit code fallback on the gated-beta magic
-- token. Universal-link sign-in needs the not-yet-existing Phase-3 domain's
-- AASA file, so v1 sign-in also delivers a short numeric code in the SAME
-- magic-link email; consuming the code redeems the same one-time token row.
--
-- Two NEW additive columns on the existing beta_magic_tokens table (GOV-801,
-- 0026 §2). NO new table, NO ALTER on any of the four frozen serving surfaces
-- (read_api, ai_risk_gate, stage5_agenda_board, mcp_service/) — they stay
-- byte-for-byte unaffected. Additive + idempotent: db.py guards each ADD COLUMN
-- with a PRAGMA table_info check, so a bare re-run is safe.
--
-- Migration slot: 0027 is the first free slot on origin/main (latest =
-- 0026_beta_gate.sql). If another 0027 lands first, the merge gate renumbers
-- this file (second-lander-renumbers rule).
--
-- INERT like 0026: applying this sends no email, admits no user, opens no
-- route. The whole /api/beta/* surface answers 404 until the owner-gated
-- `beta_gate_enabled` flag is enabled, and no email leaves the machine until an
-- owner registers a real email adapter (fail-closed, D1/INV-5).
--
-- Privacy: code_hash = sha256 of the raw code (the raw code is emailed once and
-- never stored), exactly the token_hash shape §2 already uses. code_attempts is
-- a per-token brute-force counter (a wrong code increments it; the code is
-- refused once it reaches the cap), so the small 6-digit space cannot be
-- ground down inside the 15-minute TTL.

-- One-time numeric code, hashed. NULL for token rows minted before this leg or
-- via the token-only issue() path (link-only); a code consume ignores NULLs.
ALTER TABLE beta_magic_tokens ADD COLUMN code_hash TEXT;

-- Failed-code attempt counter for this token row. NOT NULL DEFAULT 0 so every
-- existing row reads 0 without a backfill.
ALTER TABLE beta_magic_tokens ADD COLUMN code_attempts INTEGER NOT NULL DEFAULT 0;
