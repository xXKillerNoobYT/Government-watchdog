# 2026 Accounts, Beta Cohorts, and Notifications — Plan v0.1

**Issue:** GOV-721  
**Parent:** GOV-715 (2026 commercial platform directive)  
**Hermes plan ref:** `2026-07-15_190000-commercial-scale-mcp-funding-persona-queue.md` §6  
**Depends on:** GOV-716 (REQ-2026-COMM v1.0), GOV-720 (area economics / areas spine)  
**Status:** PENDING CTO + CEO APPROVAL  
**Next migration slot:** 0025  
**Repos:** `xXKillerNoobYT/Government-watchdog` (backend) + `xXKillerNoobYT/Government-watchdog-website` (frontend — CTO local merge)

---

## Objective

Replace the placeholder auth scaffolding with a real, gate-enforced account system that:

1. Holds **all unapproved visitors at the waitlist boundary** — pending/revoked/paused accounts receive zero civic data.
2. Advances users through owner-gated cohort steps: **2 → 3 → 15** (not automatic, each step requires an explicit owner decision recorded in an `owner_decision_ref`).
3. Sends **no email without explicit consent + unsubscribe + deliverability + abuse controls**.
4. Delivers **in-app notifications** for access changes (approval, revocation, cohort change, consent confirmations).
5. Keeps all code **additive-only** — no mutations to frozen serving modules (`read_api`, `stage5_agenda_board`, `ai_risk_gate`) and no production email activation in this card.

This card does **not** activate public launch, mass messaging, or cohort sizes beyond 15.

---

## Scope

### Backend (`Government-watchdog`)

| Component | Description |
|---|---|
| Migration 0025 | 9 new tables: users, waitlist_requests, access_grants, cohort_state, cohort_transitions, consent_preferences, notification_events, email_outbox, email_delivery_log |
| `scripts/accounts/` | Account service: create, approve, revoke, tier-check, cohort gate |
| `scripts/notifications/` | In-app notification writer + query endpoint |
| `scripts/email/` | Provider-agnostic email abstraction + deliverability/abuse controls |
| `tests/test_gov721_*.py` | RED-proof neuter suite; contract parity with serving modules |

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

- **AC-1 Zero-leak gate.** A request to any civic-data endpoint with a `pending`, `revoked`, or `paused` account token returns HTTP 403 with no civic data in the body.
- **AC-2 Cohort cap enforcement.** `cohort_state` enforces a configurable max-size per cohort step; attempts to exceed cap are rejected with a clear error and no row written.
- **AC-3 Cohort transition audit.** Every 2→3 and 3→15 transition writes a `cohort_transitions` row with a non-null `owner_decision_ref`; automated transitions without `owner_decision_ref` are rejected at the service layer.
- **AC-4 Consent gate.** No row is written to `email_outbox` unless `consent_preferences.email_consent = true` AND `consent_preferences.unsubscribe_token` is populated.
- **AC-5 Email abstraction.** `scripts/email/` sends via a provider-agnostic adapter interface; the default adapter is `null` (no-op, logging only) — production SMTP/SES adapter requires a separate owner-gated activation flag.
- **AC-6 Notification delivery.** In-app notifications for: account approved, account revoked, cohort advanced, consent recorded, unsubscribe confirmed.
- **AC-7 UI coverage.** All 6 access states render correctly at 1440 / 768 / 390 with no civic data visible to non-approved states. ARIA labels on all interactive elements.
- **AC-8 Additive-only.** `git diff origin/main -- scripts/read_api.py scripts/stage5_agenda_board.py scripts/ai_risk_gate.py` is empty after migration 0025 lands.
- **AC-9 No mass send.** No test or CI step sends real email to external addresses. All email tests use the null adapter or an isolated test harness address.
- **AC-10 VSR + SecPriv sign-off.** VerificationSafetyReviewer and SecurityPrivacyAgent both pass before merge.

---

## Migration 0025 — table design sketch

```sql
-- ACCT-2026 v0.1 (GOV-721): secure accounts, cohorts, consent, notifications.
-- Slot 0025 (0024 = area_economics). Additive + idempotent: CREATE IF NOT EXISTS.
-- No ALTER on any existing table; frozen surfaces byte-0 unaffected.

-- §1 Users
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,           -- uuid, assigned at creation
    email           TEXT NOT NULL UNIQUE,
    email_verified  INTEGER NOT NULL DEFAULT 0, -- 0/1
    password_hash   TEXT,                       -- bcrypt; NULL = pending email-verify
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

-- §3 Access grants (append-only; current state = latest row per user)
CREATE TABLE IF NOT EXISTS access_grants (
    grant_id            TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    tier                TEXT NOT NULL
        CHECK (tier IN ('none', 'waitlisted', 'pending', 'approved', 'revoked', 'paused')),
    owner_decision_ref  TEXT,                   -- NOT NULL for approved/revoked/paused
    reviewer_id         TEXT,
    granted_utc         TEXT NOT NULL,
    note                TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_grants_user ON access_grants(user_id, granted_utc);

-- §4 Cohort state
CREATE TABLE IF NOT EXISTS cohort_state (
    cohort_id       TEXT PRIMARY KEY,           -- e.g. 'beta-2', 'beta-3', 'beta-15'
    max_size        INTEGER NOT NULL,           -- 2 / 3 / 15
    current_size    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'closed'
        CHECK (status IN ('closed', 'open', 'full')),
    opened_utc      TEXT,
    owner_decision_ref TEXT                     -- required to open
);

-- §5 Cohort membership + transitions (append-only)
CREATE TABLE IF NOT EXISTS cohort_transitions (
    transition_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL REFERENCES users(user_id),
    from_cohort         TEXT,
    to_cohort           TEXT NOT NULL,
    owner_decision_ref  TEXT NOT NULL,          -- enforced in service layer
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

-- §8 Email outbox (provider-agnostic; null adapter by default)
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
```

---

## Leg breakdown

| Leg | Owner | Blocked by | Deliverable | Gate |
|---|---|---|---|---|
| **Leg 1 — Schema + migration 0025** | FoundingEngineer | — | `0025_accounts_cohorts_notifications.sql` + RED-proof neuter tests | CTO review of migration before merge |
| **Leg 2 — Backend service layer** | FoundingEngineer | Leg 1 merged | `scripts/accounts/`, `scripts/notifications/`, `scripts/email/`; integration tests | SecurityPrivacyAgent review |
| **Leg 3 — Frontend gated flows** | FrontendTimelineEngineer (child GOV-7xx) | Leg 1 merged | All 6 access states; notification panel; ARIA + responsive | UXProductDesigner review |
| **Leg 4 — VSR + SecPriv sign-off** | VerificationSafetyReviewer + SecurityPrivacyAgent (child issues) | Legs 2+3 | Pass reports | Both must PASS |
| **Leg 5 — CTO merge gate** | CTO | Leg 4 passed | Squash-merge to main; CEO goal-flip | CTO non-author merge |

### Child issues to create after plan approval

1. **GOV-7xx** (FrontendTimelineEngineer) — Frontend gated access states, notification panel, ARIA/responsive (blocked on Leg 1 merge)
2. **GOV-7xx** (UXProductDesigner) — UX review of gated flows and notification panel (blocked on GOV-7xx frontend leg)
3. **GOV-7xx** (SecurityPrivacyAgent) — Security/privacy review: account creation, password storage, email consent controls, unsubscribe token entropy (blocked on Leg 2)
4. **GOV-7xx** (VerificationSafetyReviewer) — VSR verification: zero-leak gate (AC-1), cohort cap (AC-2), consent gate (AC-4), UI state correctness (AC-7) (blocked on Legs 2+3)

---

## Invariants

| Code | Rule |
|---|---|
| INV-1 | `git diff origin/main -- scripts/read_api.py` is empty after 0025 |
| INV-2 | No `email_outbox` row without `consent_preferences.email_consent = 1` |
| INV-3 | No `cohort_transitions` row without `owner_decision_ref IS NOT NULL` |
| INV-4 | `access_grants` is append-only; current state is always the latest row |
| INV-5 | Email adapter default is `null` (no-op); production adapter requires `ENABLE_EMAIL_ADAPTER=true` env var, owner-set |
| INV-6 | No `cohort_state.current_size > cohort_state.max_size` is ever written |
| INV-7 | `password_hash` is bcrypt; raw passwords are never logged or stored |
| INV-8 | `unsubscribe_token` is a cryptographically random token, stored on first consent, never reused |

---

## Not in scope for this card

- Public launch or mass email activation
- Cohort sizes beyond 15
- Paid-area entitlement gating (GOV-723)
- OAuth/SSO integration
- Self-service account deletion (deferred to a follow-up)
- Wyoming/US expansion

---

## Open decisions (need owner/CTO input before Leg 1)

1. **Email activation flag:** confirm `ENABLE_EMAIL_ADAPTER` as the name for the owner-gated env var that promotes from null→real adapter (or prefer a DB `feature_flags` table row).
2. **Password scheme:** confirm bcrypt is acceptable vs. argon2id. argon2id is stronger; bcrypt is simpler to deploy without a native library. Recommendation: argon2id if Python 3.12 + `argon2-cffi` is acceptable (CI already on py3.12).
3. **Frontend framework:** confirm whether the website repo (currently JS with `stage*.js` scripts) expects a React-based account UI or a server-rendered approach. This determines whether FrontendTimelineEngineer needs a framework install or can extend existing patterns.
4. **Cohort definition:** confirm whether cohort steps are additive (beta-2 users + 1 more = beta-3) or replacement (entirely new set). Recommendation: additive — existing approved users stay approved.
