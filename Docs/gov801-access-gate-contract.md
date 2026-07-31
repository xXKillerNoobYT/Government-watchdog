# GOV-801 Access Gate — as-built contract (`scripts/beta/`)

**Status:** **AS-BUILT**, not a forward plan. Every statement below describes code that is
already merged on `main` and is written so a reader can check it against the file cited.
**Ticket lineage:** GOV-801 (gate + front door), GOV-1538 (numeric-code fallback),
GOV-1576 (intake upload API), GOV-1544 (email-gateway wiring).
**Migrations:** `0026_beta_gate.sql`, `0027_beta_magic_code.sql`. No slot consumed by this
document.
**Written:** 2026-07-30 by the AUTO GO backend routine, AUTO GO check C1.

---

## Why this document exists

`scripts/beta/` is 1,450 lines of production access-control code guarding a public-repo
civic system, and until this file it had **no contract in `Docs/`**. Its design intent
lived entirely in module docstrings — which are good, and which this document leans on —
but a docstring cannot be diffed against a plan, cannot be reviewed as a unit, and is not
where a reviewer looks.

The gap was found by AUTO GO check C1 (*plan is complete*). C1 had been reading
`Docs/2026-accounts-cohorts-notifications-plan-v0.2.md` as this area's contract. That
document is complete and CTO-approved, but its Backend scope table
(`2026-accounts-cohorts-notifications-plan-v0.2.md:41-51`) names `scripts/accounts/`,
`scripts/notifications/` and `scripts/email/` — **it does not scope `scripts/beta/` at
all.** It is the contract for the *accounts* lane described in §7 below, not this one.

---

## 1. What this does

The access gate is the **front door**: it decides who may hold a beta session at all. It
answers exactly one question — *is this email owner-approved, and can it prove control of
that address?* — and nothing else. It serves **zero civic data** by construction: every
route in `beta/http_api.py:102-162` returns a fixed status body (`{"status": "ok"}`,
`{"error": ...}`) or a redirect. There is no route through which a civic record can leave
this package.

Five surfaces:

| Surface | Route | Module |
|---|---|---|
| Request a magic link | `POST /api/beta/magic-link/request` | `beta/service.py:41` |
| Redeem the link | `GET /api/beta/magic-link/verify` | `beta/service.py:69` |
| Redeem the 6-digit code | `POST /api/beta/magic-link/consume` | `beta/service.py:93` |
| Join the waitlist | `POST /api/beta/waitlist` | `beta/service.py:121` |
| Sign out | `DELETE /api/beta/sessions/current` | `beta/service.py:142` |

Plus two non-front-door surfaces in the same package: the owner CLI (`beta/admin.py`) and
the authenticated supplied-file intake upload (`beta/intake_api.py`, GOV-1576).

## 2. Why it is shaped this way

**Owner-gated, not self-serve.** No code path admits anyone. `allowlist.add` requires a
non-empty `owner_decision_ref` and raises `OwnerlessAllowlistChange` without one
(`beta/allowlist.py:30-31`), and the column is `NOT NULL` in-schema as well — defence in
depth, the same posture `access_grants` and `feature_flags` take.

**Enumeration-neutral.** An attacker must not be able to use the front door to learn who is
on the allowlist. `request_magic_link` returns `None` on every path — invalid address,
rate-limited, not allowlisted, or success — and the HTTP layer answers a constant `200`
(`beta/http_api.py:117-123`). The code-redemption route collapses every failure into one
`401` (`beta/service.py:96-102`).

**Fail-closed at the flag.** `process_request` checks `beta_gate_enabled` *before* method
or route are even examined (`beta/http_api.py:113-114`), so the entire `/api/beta/*`
surface is a constant `404` until an owner appends the flag row. Absent flag = disabled.

**Nothing leaves the machine yet.** `mailer` renders only the two registered templates and
hands to `email_gateway.adapters.resolve_adapter`, which is fail-closed to the null adapter
while `email_adapter_enabled` is off — the shipped state (`beta/mailer.py:8-12`).

## 3. Data model (as built)

`0026_beta_gate.sql` creates five tables; `0027_beta_magic_code.sql` adds two columns to
one of them.

| Table | Holds | Notes |
|---|---|---|
| `beta_allowlist` | email, status (`active`/`revoked`), `owner_decision_ref`, note | one row per email; `ON CONFLICT(email) DO UPDATE` re-activates |
| `beta_magic_tokens` | `token_hash`, `code_hash`, `expires_utc`, `consumed_utc`, `code_attempts` | 0027 adds `code_hash` + `code_attempts` |
| `beta_sessions` | `token_hash`, email, `expires_utc`, `revoked_utc` | 7-day TTL (`beta/sessions.py:19`) |
| `beta_waitlist` | email, `area_interest`, `ip_hint`, `submitted_utc` | not an admission path — informational |
| `beta_audit_log` | event enum, `email_hash`, `ip_hint`, detail, `at_utc` | append-only; `CHECK` enum of 10 events |

**Identity is the email address**, normalized (`common.normalize_email`) at every entry
point. There is no user row and no user id in this lane.

**No secret is stored in the clear.** Magic tokens, numeric codes and session tokens are
stored only as sha256 digests (`common.token_hash`); the raw value is returned to the
caller exactly once and never persisted. The audit log has **no raw email column by
design** — `audit.record` hashes the address itself so no caller can push plaintext in even
by mistake (`beta/audit.py:2-8, 32-34`), and `ip_hint` is a truncated digest, never an IP.

## 4. Data flow

**Sign-in (link or code).** `request` → normalize → validate → audit `magic_link_requested`
→ rate-limit check (5/hour/email) → allowlist check → `tokens.issue_with_code` mints **one
row bearing both** a link-token hash and a code hash → mailer → audit `magic_link_sent`.
Redemption (`consume` or `consume_code`) atomically stamps `consumed_utc`, then
**re-checks the allowlist** before issuing a session, so an invite revoked between request
and redemption cannot still be redeemed (`beta/service.py:82-85` and `:110-113`).

**Single-use is enforced in SQL, not in Python.** Both consume paths issue
`UPDATE ... SET consumed_utc = ? WHERE token_id = ? AND consumed_utc IS NULL` and reject on
`rowcount != 1` (`beta/tokens.py:93-98`, `:135-140`). Two concurrent redemptions of the same
credential can never both win. Because the link token and the numeric code share one row,
redeeming either invalidates the other.

**Brute force on the 6-digit code.** A wrong guess increments `code_attempts`; past
`MAX_CODE_ATTEMPTS = 5` the code is dead even if later guessed correctly
(`beta/tokens.py:127-134`). Five guesses against a 10^6 space inside a 15-minute TTL.

**Revocation is one lever.** `allowlist.revoke` flips the row to `revoked` **and**
cascade-revokes every live session for that email in the same call
(`beta/allowlist.py:72`). An owner locks someone out with one command; no separate
token-invalidation step exists or is needed.

## 5. Invariants (each one checkable)

- **INV-1** No `/api/beta/*` response body ever contains civic data. Bodies are fixed
  constants or redirects (`beta/http_api.py:102-163`).
- **INV-2** With `beta_gate_enabled` absent or off, every request to the surface is `404`,
  regardless of method, route or credential.
- **INV-3** An allowlist mutation without an `owner_decision_ref` is impossible — rejected
  in code *and* by the schema.
- **INV-4** A magic token or numeric code redeems at most once, enforced by conditional
  UPDATE, safe under concurrency.
- **INV-5** Revoking an allowlist entry revokes that email's live sessions in the same
  call.
- **INV-6** The audit log stores no raw email and no raw IP, and has no update or delete
  path.
- **INV-7** The front door reveals no allowlist membership through status code or body.
  *(Scope note: this is a statement about the response, not about latency — see §8.)*
- **INV-8** No email leaves the machine while `email_adapter_enabled` is off.

## 6. Test plan / current coverage

Three suites, 1,211 lines: `tests/test_gov801_beta_gate.py` (424),
`tests/test_gov1538_magic_code.py` (314), `tests/test_gov1576_intake_api.py` (473).

C4 for this area was closed across PRs #183, #186 and #187 after a five-gap audit; the
single-use race guard (INV-4) is barrier-gated on the `common.iso` seam so the contended
window is forced on every run, and was proved against 8/8 mutations. Full-suite green is
the standing bar — 2,012 tests as of PR #187.

**Known coverage gap, filed not fixed:** `audit.EVENTS` (`beta/audit.py:17-21`) and the
`CHECK` enum on `beta_audit_log` (`0026_beta_gate.sql`) are two hand-maintained copies of
the same 10-event list. They agree today. Nothing pins them together, and divergence fails
at runtime inside a security-audit write path — tracked as
[#193](https://github.com/xXKillerNoobYT/Government-watchdog/issues/193).

## 7. Lane boundary — this gate is *not* the civic-data gate

This is the single most important thing a reader needs, and it is currently recorded
nowhere outside one module docstring.

Two access lanes exist in this repo and **both are live**:

| | **beta lane** (this document) | **accounts lane** |
|---|---|---|
| Migrations | 0026, 0027 | 0025 |
| Code | `scripts/beta/` | `scripts/accounts/` |
| Identity | email address | `user_id` (uuid) |
| Session table | `beta_sessions`, 7-day TTL | `auth_sessions`, 24-hour TTL |
| Credential transport | cookie `gw_beta_session` | `Authorization: Bearer` |
| Admission | `beta_allowlist` status | `access_grants` latest tier == `approved` |
| Guards civic data? | **no** | **yes** — `accounts/gate.py:53` |
| Contract | this file | `Docs/2026-accounts-cohorts-notifications-plan-v0.2.md` |

The separation is deliberate and is stated in `beta/mailer.py:14-16`: the beta flow has no
`users` row and no `consent_preferences` row, so it deliberately bypasses the
consent-gated `email_gateway.outbox`.

**UPDATED 2026-07-30 (GOV-1663) — the AUTHORIZATION half of this is now built; the
TRANSPORT half is not.** Owner decision, same day: the beta front door **provisions** an
accounts row on first verified sign-in (`scripts/beta/provision.py`), carrying the beta
allowlist's own `owner_decision_ref` onto an `access_grants` row, so `accounts.gate`
remains the single civic gate. Provisioned users are **passwordless** by owner direction —
`password_hash` stays NULL and `accounts.service.login` refuses such a row with its
constant `LoginFailed`, so the magic link is the only credential. Provisioning may only
ever open a door that was never opened: a `revoked` or `paused` accounts tier is left
untouched, so a stale-but-active allowlist row cannot resurrect an account an owner shut.

**What remains, stated precisely.** `accounts.gate` resolves a `Bearer` token against
`auth_sessions` (`scripts/export_web_artifact.py:314-325`); a beta session lives in
`beta_sessions` and nothing writes `auth_sessions` from this lane. So a beta cookie still
does not *transport* an identity the civic gate accepts, even though the identity now
exists and is approved. **A signed-in user still cannot read civic data** — fail-closed,
leaking nothing, and the safe direction to be wrong in.

**Do not fix the transport half here.** Open PR
[#125](https://github.com/xXKillerNoobYT/Government-watchdog/pull/125) ("Accept beta cookie
at reviewer export gate") owns exactly this seam, with
[#132](https://github.com/xXKillerNoobYT/Government-watchdog/pull/132) (server-authoritative
decision core) and [#133](https://github.com/xXKillerNoobYT/Government-watchdog/pull/133)
(serialize cohort capacity) adjacent to it. All three have been open since 2026-07-24.

## 8. Known limitations

1. **INV-7 covers the response, not the clock.** A non-allowlisted request returns after
   two SELECTs; an allowlisted one additionally inserts a token row and calls the mail
   adapter. Under the shipped null adapter the difference is small, but promoting a real
   SMTP/SES adapter puts a network round-trip inside the request and widens it into a
   usable membership oracle. Mitigating it means moving the send off the request path.
2. **The waitlist is not an admission path.** `beta_waitlist` rows grant nothing; only
   `allowlist.add` admits. Two separate waitlists exist across the lanes
   (`beta_waitlist` and `waitlist_requests`) with no reconciliation between them.
3. ~~**`allowlist.add` overwrites `note` on re-add**~~ — **RESOLVED 2026-07-31 (GOV-1666,
   [#185](https://github.com/xXKillerNoobYT/Government-watchdog/issues/185)).** `note` is now
   three-state: `None` (the CLI default when `--note` is omitted) leaves the stored note
   unchanged, a string replaces it, and `""` clears it deliberately. The failure modes are
   not symmetric — a note surviving when the owner meant to drop it is visible and fixable;
   a note silently erased on re-invite is unrecoverable and unaudited, since
   `allowlist_added` records `owner_decision_ref` and never the note.
4. **Duplicate cookie names resolve last-wins** on the beta and intake surfaces — tracked
   as [#182](https://github.com/xXKillerNoobYT/Government-watchdog/issues/182).

## 9. Out of scope for this contract

Cohort sizing and transitions, consent preferences, in-app notifications, email outbox and
deliverability, and the `users`/`access_grants` model — all of these belong to the accounts
lane and are contracted in `Docs/2026-accounts-cohorts-notifications-plan-v0.2.md`. The
`/v1` vs `/api/beta/*` namespace question
([#143](https://github.com/xXKillerNoobYT/Government-watchdog/issues/143)) is an owner/CTO
decision and is deliberately not answered here; this document describes the routes as
built, under the names they currently have.

## 10. Open decision for the owner

**ANSWERED 2026-07-30 by the owner: option 1 — the beta lane becomes a front end that
provisions accounts rows, and `accounts.gate` stays the single civic gate.** Chosen over
"accounts absorbs the beta tables" (which needs a data migration) and "two lanes with a
documented bridge" (which keeps two revocation levers forever). One owner decision now
admits a person in both lanes.

Implemented for the authorization half in GOV-1663 (§7). The remaining open sub-question is
**transport**: how a beta cookie comes to carry an identity `accounts.gate` accepts. That is
where PRs #125 and #181 collide, and it is the part still to be reconciled —
[#192](https://github.com/xXKillerNoobYT/Government-watchdog/issues/192) stays open for it.
