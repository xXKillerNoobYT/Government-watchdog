# 2026 Accounts, Beta Cohorts, and Notifications — Plan v0.2

**Issue:** GOV-721  
**Parent:** GOV-715 (2026 commercial platform directive)  
**Hermes plan ref:** `2026-07-15_190000-commercial-scale-mcp-funding-persona-queue.md` §6  
**Depends on:** GOV-716 (REQ-2026-COMM v1.0), GOV-720 (area economics / areas spine)  
**Status:** **APPROVED** — CEO product/staging + CTO technical, 2026-07-16, both recorded on routing issue GOV-751 (CTO verdict comment `57cb400e`). v0.2 applies the 11 CTO amendments + decisions D1–D4; per the CTO verdict there is **no v0.2 re-review round** — the next checkpoint is the Leg-1 CTO migration review gate.  
**Supersedes:** v0.1 (same file renamed; v0.1 text at commit `3f59cfe`)  
**Next migration slot:** 0025  
**Repos:** `xXKillerNoobYT/Government-watchdog` (backend) + `xXKillerNoobYT/Government-watchdog-website` (frontend — CTO local merge)

---

## Objective

Replace the placeholder auth scaffolding with a real, gate-enforced account system that:

1. Holds **all unapproved visitors at the waitlist boundary** — pending/revoked/paused accounts receive zero civic data.
2. Advances users through owner-gated cohort steps: **2 → 3 → 15** (not automatic, each step requires an explicit owner decision recorded in an `owner_decision_ref`).
3. Sends **no email without explicit consent + unsubscribe + deliverability + abuse controls**.
4. Delivers **in-app notifications** for access changes (approval, revocation, cohort change, consent confirmations).
5. Keeps all code **additive-only** — no mutations to the FOUR frozen serving surfaces (`scripts/read_api.py`, `scripts/stage5_agenda_board.py`, `scripts/ai_risk_gate.py`, `scripts/mcp_service/`) and no production email activation in this card.

This card does **not** activate public launch, mass messaging, or cohort sizes beyond 15.

---

## Resolved decisions (D1–D4, recorded on GOV-751 2026-07-16)

| # | Decision | Resolution | Decider |
|---|---|---|---|
| D1 | Email activation flag | **DB `feature_flags` row, fail-closed** — NOT the env var. Activation/deactivation each append a row carrying its own `owner_decision_ref` (an explicit Isaac board-card decision, exactly like a cohort transition). No row or latest row disabled → null adapter. The `ENABLE_EMAIL_ADAPTER` env var is **dropped entirely** (one source of truth; an env var cannot carry who/when/which-card). | CTO (CEO constraint honored) |
| D2 | Password scheme | **argon2id** via `argon2-cffi>=23.1` (prebuilt wheels, CI is py3.12). Store the PHC-encoded string (self-describing: algorithm, params, salt) in `users.password_hash`; library default parameters acceptable at beta-15 scale; run `check_needs_rehash` on login so future parameter bumps need no schema change. | CTO (CEO no objection) |
| D3 | Frontend framework | **No React / no new framework.** Extend existing website-repo patterns (Vite/TS, existing `stage*.js` idioms). | CEO (CTO agrees — no technical case for React on this slice) |
| D4 | Cohort definition | **Additive** — beta-2 users remain members through beta-3/beta-15; revocation is a separate lane; every transition still requires its own `owner_decision_ref`, so Isaac keeps per-step control. The append-only `cohort_transitions` model supports cumulative membership cleanly. | CEO (CTO agrees) |

---

## Scope

### Backend (`Government-watchdog`)

| Component | Description |
|---|---|
| Migration 0025 | **11 new tables**: users, waitlist_requests, access_grants, cohort_state, cohort_transitions, consent_preferences, notification_events, email_outbox, email_delivery_log, **feature_flags, auth_sessions** |
| `scripts/accounts/` | Account service: create, approve, revoke, tier-check, cohort gate, session issue/verify/revoke |
| `scripts/notifications/` | In-app notification writer + query endpoint |
| `scripts/email/` | Provider-agnostic email abstraction + deliverability/abuse controls; adapter resolution via `feature_flags` (fail-closed) |
| `tests/test_gov721_*.py` | RED-proof neuter suite; contract parity with serving modules |
| `tests/test_deploy_frozen_surface.py` | **Deliberate Leg-1 update**: `test_leg2_added_no_new_migration` rewritten to pin an explicit migration allowlist (`0025_accounts_cohorts_notifications.sql`) rather than asserting zero migration diff — the guard stays, the allowlist grows |

### Frontend (`Government-watchdog-website`)

| Component | Description |
|---|---|
| Gated access states | not-signed-in / waitlisted / pending-review / approved / denied / revoked — all 6 states rendered |
| Waitlist form | Minimal intake; no PII beyond email + area interest |
| Notification panel | In-app notification bell / drawer |
| ARIA + responsive | Desktop 1440 / tablet 768 / mobile 390 coverage |

**Frontend lane owner:** FrontendTimelineEngineer (child issue, blocked on Leg 1 schema).  
**UX review:** UXProductDesigner (child issue, blocked on frontend leg).

---

## Acceptance criteria

- **AC-1 Zero-leak gate.** A request to any civic-data endpoint with a `pending`, `revoked`, or `paused` account token returns HTTP 403 with no civic data in the body. The gate resolves token → `auth_sessions` → user → latest `access_grants` tier on **every request**, so revocation propagates immediately. **Zero-leak extends to mail bodies:** no civic data may appear in `email_outbox.body_text`/`body_html` addressed to non-approved users.
- **AC-2 Cohort cap enforcement.** `cohort_state` enforces a configurable max-size per cohort step; attempts to exceed cap are rejected with a clear error and no row written. Enforcement recomputes size from `cohort_transitions` in-transaction (see INV-6) — the `current_size` counter is a cache, never the authority.
- **AC-3 Cohort transition audit.** Every 2→3 and 3→15 transition writes a `cohort_transitions` row with a non-null `owner_decision_ref`; automated transitions without `owner_decision_ref` are rejected at the service layer.
- **AC-4 Consent gate.** No row is written to `email_outbox` unless `consent_preferences.email_consent = true` AND `consent_preferences.unsubscribe_token` is populated.
- **AC-5 Email abstraction.** `scripts/email/` sends via a provider-agnostic adapter interface; the default adapter is `null` (no-op, logging only) — promoting to a production SMTP/SES adapter requires the owner-gated `email_adapter_enabled` `feature_flags` row (D1/INV-5), fail-closed.
- **AC-6 Notification delivery.** In-app notifications for: account approved, account revoked, cohort advanced, consent recorded, unsubscribe confirmed.
- **AC-7 UI coverage.** All 6 access states render correctly at 1440 / 768 / 390 with no civic data visible to non-approved states. ARIA labels on all interactive elements.
- **AC-8 Additive-only.** `git diff origin/main -- scripts/read_api.py scripts/stage5_agenda_board.py scripts/ai_risk_gate.py scripts/mcp_service/` is empty after migration 0025 lands (the FOUR frozen surfaces, matching the live `tests/test_deploy_frozen_surface.py` FROZEN list).
- **AC-9 No mass send.** No test or CI step sends real email to external addresses. All email tests use the null adapter or an isolated test harness address. With D1 fail-closed defaulting, tests cannot reach a real adapter without a flag row.
- **AC-10 VSR + SecPriv sign-off.** VerificationSafetyReviewer and SecurityPrivacyAgent both pass before merge.

---

## Migration 0025 — table design sketch

```sql
-- ACCT-2026 v0.2 (GOV-721): secure accounts, cohorts, consent, notifications.
-- Slot 0025 (0024 = area_economics). Additive + idempotent: CREATE IF NOT EXISTS.
-- No ALTER on any existing table; frozen surfaces byte-0 unaffected.
-- 11 tables (v0.1's 9 + feature_flags + auth_sessions per CTO review GOV-751).

-- §1 Users
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,           -- uuid, assigned at creation
    email           TEXT NOT NULL UNIQUE,       -- service layer lowercases+trims before the UNIQUE check
    email_verified  INTEGER NOT NULL DEFAULT 0, -- 0/1
    password_hash   TEXT,                       -- argon2id PHC string; NULL = pending email-verify
    created_utc     TEXT NOT NULL,
    last_login_utc  TEXT
);

-- §2 Waitlist
CREATE TABLE IF NOT EXISTS waitlist_requests (
    request_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    area_interest   TEXT,                       -- optional
    submitted_utc   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'denied', 'revoked'))
);
CREATE INDEX IF NOT EXISTS idx_waitlist_user ON waitlist_requests(user_id);

-- §3 Access grants (append-only; current state = latest row per user,
--    ordered (granted_utc, rowid) — rowid tie-break because ISO-8601 TEXT
--    timestamps can collide within a second)
CREATE TABLE IF NOT EXISTS access_grants (
    grant_id            TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    tier                TEXT NOT NULL
        CHECK (tier IN ('none', 'waitlisted', 'pending', 'approved', 'revoked', 'paused')),
    owner_decision_ref  TEXT,
    reviewer_id         TEXT,
    granted_utc         TEXT NOT NULL,
    note                TEXT,
    -- DB-level enforcement (AREA-5 precedent): ownerless approve/revoke/pause rejected in-schema
    CHECK (tier NOT IN ('approved', 'revoked', 'paused') OR owner_decision_ref IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_access_grants_user ON access_grants(user_id, granted_utc);

-- §4 Cohort state (current_size is a CACHE only — never the enforcement authority;
--    see INV-6: cap enforcement recomputes from cohort_transitions in-transaction)
CREATE TABLE IF NOT EXISTS cohort_state (
    cohort_id       TEXT PRIMARY KEY,           -- e.g. 'beta-2', 'beta-3', 'beta-15'
    max_size        INTEGER NOT NULL,           -- 2 / 3 / 15
    current_size    INTEGER NOT NULL DEFAULT 0, -- cache of latest recompute
    status          TEXT NOT NULL DEFAULT 'closed'
        CHECK (status IN ('closed', 'open', 'full')),
    opened_utc      TEXT,
    owner_decision_ref TEXT                     -- required to open
);

-- §5 Cohort membership + transitions (append-only; membership is ADDITIVE per D4)
CREATE TABLE IF NOT EXISTS cohort_transitions (
    transition_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    from_cohort         TEXT,
    to_cohort           TEXT NOT NULL,
    owner_decision_ref  TEXT NOT NULL,          -- also enforced in service layer
    at_utc              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cohort_transitions_user ON cohort_transitions(user_id);

-- §6 Consent preferences
CREATE TABLE IF NOT EXISTS consent_preferences (
    user_id             TEXT PRIMARY KEY REFERENCES users(user_id),
    email_consent       INTEGER NOT NULL DEFAULT 0,  -- 0/1
    notification_consent INTEGER NOT NULL DEFAULT 1, -- in-app always-on default
    unsubscribe_token   TEXT UNIQUE,            -- NULL until first consent recorded
    consented_utc       TEXT,
    updated_utc         TEXT
);

-- §7 In-app notification events
CREATE TABLE IF NOT EXISTS notification_events (
    notif_id        TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    kind            TEXT NOT NULL
        CHECK (kind IN ('access_approved', 'access_revoked', 'cohort_advanced',
                        'consent_recorded', 'unsubscribe_confirmed', 'system')),
    body_text       TEXT NOT NULL,
    read_utc        TEXT,                       -- NULL = unread
    created_utc     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notification_events(user_id, created_utc);

-- §8 Email outbox (provider-agnostic; null adapter by default).
--    body_text/body_html are a ZERO-LEAK surface (AC-1): no civic data mailed
--    to non-approved users — in SecPriv Leg-2 review scope.
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

-- §9 Email delivery log (append-only audit)
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

-- §10 Feature flags (append-only, D1; mirrors access_grants/area_transitions
--     0024 AREA-5 precedent. Current state = latest row per flag_name,
--     ordered (at_utc, rowid) tie-break. FAIL-CLOSED: no row => flag off.)
CREATE TABLE IF NOT EXISTS feature_flags (
    flag_seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_name           TEXT NOT NULL,          -- e.g. 'email_adapter_enabled'
    enabled             INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    owner_decision_ref  TEXT NOT NULL,          -- explicit Isaac board-card decision, activation AND deactivation
    actor               TEXT,
    at_utc              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feature_flags_name ON feature_flags(flag_name, flag_seq);

-- §11 Auth sessions (session-storage gap fix, CTO amendment #4).
--     token_hash = sha256 of the bearer token; the RAW token is never stored.
--     Raw tokens generated with secrets.token_urlsafe(32) (same rule as INV-8
--     unsubscribe tokens). Zero-leak gate resolves token -> user -> latest
--     access_grants tier on every request, so revocation propagates
--     immediately without token-invalidation machinery.
CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(user_id),
    token_hash      TEXT NOT NULL UNIQUE,
    issued_utc      TEXT NOT NULL,
    expires_utc     TEXT NOT NULL,
    revoked_utc     TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
```

---

## Leg breakdown

| Leg | Owner | Blocked by | Deliverable | Gate |
|---|---|---|---|---|
| **Leg 1 — Schema + migration 0025** | FoundingEngineer | — | `0025_accounts_cohorts_notifications.sql` (11 tables) + RED-proof neuter tests + **deliberate allowlist rewrite of `tests/test_deploy_frozen_surface.py::test_leg2_added_no_new_migration`** | CTO review of migration before merge |
| **Leg 2 — Backend service layer** | FoundingEngineer | Leg 1 merged | `scripts/accounts/`, `scripts/notifications/`, `scripts/email/`; `argon2-cffi>=23.1` added to `requirements.txt`; integration tests | SecurityPrivacyAgent review |
| **Leg 3 — Frontend gated flows** | FrontendTimelineEngineer (child GOV-7xx) | Leg 1 merged | All 6 access states; notification panel; ARIA + responsive (Vite/TS `stage*.js` idioms per D3 — no new framework) | UXProductDesigner review |
| **Leg 4 — VSR + SecPriv sign-off** | VerificationSafetyReviewer + SecurityPrivacyAgent (child issues) | Legs 2+3 | Pass reports | Both must PASS |
| **Leg 5 — CTO merge gate** | CTO | Leg 4 passed | Squash-merge to main; CEO goal-flip | CTO non-author merge |

### Child issues to create after plan approval

1. **GOV-7xx** (FrontendTimelineEngineer) — Frontend gated access states, notification panel, ARIA/responsive (blocked on Leg 1 merge)
2. **GOV-7xx** (UXProductDesigner) — UX review of gated flows and notification panel (blocked on GOV-7xx frontend leg)
3. **GOV-7xx** (SecurityPrivacyAgent) — Security/privacy review: account creation, password storage (argon2id PHC), email consent controls, unsubscribe + session token entropy, **and `email_outbox.body_text`/`body_html` as a zero-leak surface — no civic data mailed to non-approved users** (blocked on Leg 2)
4. **GOV-7xx** (VerificationSafetyReviewer) — VSR verification: zero-leak gate (AC-1, incl. mail bodies), cohort cap (AC-2), consent gate (AC-4), UI state correctness (AC-7) (blocked on Legs 2+3)

---

## Invariants

| Code | Rule |
|---|---|
| INV-1 | `git diff origin/main -- scripts/read_api.py scripts/stage5_agenda_board.py scripts/ai_risk_gate.py scripts/mcp_service/` is empty after 0025 (FOUR frozen surfaces, matching `tests/test_deploy_frozen_surface.py`) |
| INV-2 | No `email_outbox` row without `consent_preferences.email_consent = 1` |
| INV-3 | No `cohort_transitions` row without `owner_decision_ref IS NOT NULL` |
| INV-4 | `access_grants` is append-only; current state is always the latest row, ordered `(granted_utc, rowid)` |
| INV-5 | Email adapter resolution is **fail-closed from `feature_flags`**: real adapter only when the latest `email_adapter_enabled` row has `enabled = 1`; no row or latest row disabled → null adapter. Activation and deactivation each append a row with a non-null `owner_decision_ref` (Isaac board card). No env var is authoritative — `ENABLE_EMAIL_ADAPTER` is dropped. |
| INV-6 | Cohort cap enforcement recomputes membership from `cohort_transitions` (latest transition per user) **in-transaction before commit** and rejects if the recomputed size would exceed `max_size`. `cohort_state.current_size` is a cache only; no `current_size > max_size` is ever written. |
| INV-7 | `password_hash` is an argon2id PHC-encoded string; raw passwords are never logged or stored |
| INV-8 | `unsubscribe_token` is a cryptographically random token (`secrets.token_urlsafe(32)`), stored on first consent, never reused |
| INV-9 | Emails are normalized (lowercase + trim) at the service layer before any lookup or the `users.email` UNIQUE check |
| INV-10 | Raw session bearer tokens (`secrets.token_urlsafe(32)`) are never stored; only `sha256` hashes live in `auth_sessions.token_hash` |

### RED-proof requirements (Leg 1/2 tests)

Each invariant gate needs a neuter-goes-RED proof (GOV-738/743 pattern), specifically including:

- **INV-5:** a real-adapter send attempt with no `email_adapter_enabled` row (and separately with a disabled latest row) resolves to the null adapter; neutering the flag check goes RED.
- **INV-6:** cap rejection proven with `cohort_state.current_size` deliberately desynced low — the in-transaction recompute must still reject.
- **AC-1:** zero-leak proven for endpoint bodies AND for mail bodies.

---

## Not in scope for this card

- Public launch or mass email activation
- Cohort sizes beyond 15
- Paid-area entitlement gating (GOV-723)
- OAuth/SSO integration
- Self-service account deletion (deferred to a follow-up)
- Wyoming/US expansion

---

## Amendment log v0.1 → v0.2 (CTO review GOV-751, comment `57cb400e`)

1. bcrypt → argon2id reconciled (schema §1 comment + INV-7; D2).
2. INV-5 rewritten: `feature_flags` row w/ `owner_decision_ref NOT NULL`, fail-closed null default; env var dropped (D1).
3. `feature_flags` table added (§10).
4. `auth_sessions` table added (§11) — session-storage gap fix; token hashing + per-request tier resolution.
5. Frozen-surface list corrected to FOUR surfaces incl. `scripts/mcp_service/` (AC-8/INV-1).
6. Leg 1 deliberately rewrites `tests/test_deploy_frozen_surface.py::test_leg2_added_no_new_migration` to pin migration allowlist `0025_accounts_cohorts_notifications.sql` (guard preserved, not deleted).
7. `access_grants.owner_decision_ref` enforced in-schema via CHECK (§3).
8. `cohort_state.current_size` demoted to cache; INV-6 = in-transaction recompute; RED-proof w/ desynced counter.
9. SecPriv Leg-2 scope + AC-1 extended to `email_outbox.body_text/body_html` zero-leak (CEO fixup 2).
10. Email normalization (lowercase+trim) noted as service-layer rule (INV-9).
11. Latest-row ordering tie-break `(granted_utc/at_utc, rowid)` on `access_grants`/`feature_flags`.

Open-decisions section removed — all four decisions (D1–D4) are resolved above.
