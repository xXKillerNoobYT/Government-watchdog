# Stage 5.08 — Security / Privacy Gate Checklist (briefing output)

**Issue:** GOV-570 · **Stage:** 5.08-sec · **Owner:** SecurityPrivacyAgent
**Gates:** GOV-568 briefing assembler (`src/briefing.js`) under contract GOV-564 §8
**Blocked by:** GOV-569 (VSR — validate correction-aware briefing behavior) — **final sign-off
is withheld until VSR confirms behavior with verified evidence.**
**Governing workflows:** `RISK_ASSESSMENT_WORKFLOW.md`, `GATED_BETA_ACCESS_WORKFLOW.md`,
`SECURITY_PRIVACY_WORKFLOWS.md` (private-data boundary audit)

---

## Status of this document

This is a **pre-gate (provisional) checklist** assembled from a static read of `src/briefing.js`
and `src/statement-verification.js` while GOV-570 is dependency-blocked by GOV-569 (VSR). It exists
so the security gate is ready to close the moment VSR delivers verified behavior evidence. It is
**not** a final security sign-off. Each row marks what can be confirmed statically now vs. what
requires VSR's verified test/log evidence.

---

## Checklist vs GOV-564 §8 Security child acceptance criteria

| # | AC (GOV-564 §8) | Risk category (RISK_ASSESSMENT) | Static finding now | Final gate needs |
|---|---|---|---|---|
| AC1 | No private identity/address/voter-registry data in briefing output | 3 Privacy/account | **Provisional.** `renderBriefing` emits only `statement.text`, ids, `canonicalUrl`, content hashes, `evidenceLimits`, dispute/correction text. None are inherently PII — but the gate does **not** scan free text for PII. Guarantee rests on upstream source-grounding + sensitivity flags. | VSR confirmation that the actual test corpus produces **no** address/identity/voter-registry strings in any rendered path. |
| AC2 | No public accusations / legal conclusions without owner approval | 4 Defamation/legal | **Provisional pass (architecture).** Output is reviewer-internal (contract §6); public/email delivery is Isaac-gated (GOV-420). `disputed`/`corrected` buckets render under explicit non-settled labels, never as body facts. | VSR confirmation that no body line carries an accusation/legal conclusion; owner gate (GOV-420) unchanged. |
| AC3 | `do_not_publish` **and** `sensitive_flag` hard-exclude from **all** output paths | 3 Privacy + 6 Publication | **Pass (with one flagged gap — taxonomy now ruled by CTO).** Partition precedence (briefing.js:150-181) runs `evaluatePublicationGate` first → non-publishable statements land in `excluded` before reaching `disputed`/`corrected`/`aiProse`/body. Gate excludes `publication.doNotPublish` (stmt-verif.js:176) and `sensitivityFlags` containing `"do_not_publish"` (stmt-verif.js:180). **GAP:** a sensitivity flag whose value is *not* the literal `"do_not_publish"` (e.g. `["private_address"]`) is **not** excluded by the gate. | **CTO RULING (2026-06-26, contract decision — GOV-570):** `sensitivityFlags` is a *reserved exclusion-only* field. `evaluatePublicationGate` must hard-exclude on **any non-empty `sensitivityFlags`** (fail-closed), not only the literal `"do_not_publish"`. Privacy default: an unknown/new sensitivity value must exclude, never leak — this is what backs AC1 (address/identity/voter-registry data carried as a sensitivity flag must hard-exclude). Required impl change (lands in **GOV-568** scope when CEO authorizes 5.08 via **GOV-572** — *not* a new issue, to avoid compounding the held-chain drift): replace the literal check at stmt-verif.js:180 with `if (statement.sensitivityFlags?.length) failures.push("sensitive_flag");` and add a VSR test asserting `["private_address"]` → `excluded`. If a non-exclusionary informational tag is ever needed, it must use a **separate** field, never `sensitivityFlags`. |
| AC4 | AI label visible, not suppressible by polish | 2 AI-overclaim + 6 Publication | **Provisional pass (this slice).** `[AI ANALYSIS — NOT VERIFIED]` section header + per-line `[AI]` prefix are emitted by the deterministic assembler (renderBriefing), so they are structural, not a cosmetic/CSS layer — not suppressible *within this slice*. AI routing is deterministic by `kind === "ai_analysis"`. | Frontend-rendering suppressibility is a **separate surface** (not in GOV-568). If/when AI prose renders in the gated-beta UI, a follow-up frontend security check is required (BACKEND_FRONTEND_EVIDENCE / GATED_BETA). |

---

## Private-data boundary audit (SECURITY_PRIVACY_WORKFLOWS §"Private data boundary audit")

| Data type in briefing output | Boundary rule | Static finding |
|---|---|---|
| Reviewer-internal weekly digest | Local until gate passes; reviewer-only in gated-beta | ✅ Output is reviewer-internal; no auto public/email path in code (waybackStatus hard-coded `unchecked`, no network call). |
| Private individual PII | Never collect / never publish | ⚠️ Not enforced by a text scanner — relies on upstream + sensitivity flags. Carry to AC1/AC3 above. |
| Named public officials (on-record) | Allowed with attribution | ✅ Permitted; source-grounding (contract §7) requires ≥1 sourceLink + trace hash. |
| Run logs | Local only; summary counts only in comments | ✅ Contract §9 log path `Logs/stage5-newsletter-briefing.log` is gitignored/local-only. |

---

## Gated-beta access (GATED_BETA_ACCESS_WORKFLOW)

- ✅ No briefing/editorial content is rendered to a public/unauthenticated path in this slice
  (reviewer-internal only; GOV-420 Isaac gate for public/email is untouched).
- ✅ No reviewer-internal notes are emitted to a public surface by this code.
- N/A here: account/waitlist state UI (frontend slice; not in GOV-568).

---

## Disposition

- **AC3 architectural hard-exclude precedence: confirmed** by static read.
- **AC3 GAP — RESOLVED as a CTO contract ruling (2026-06-26):** the gate must fail-closed on **any**
  non-empty `sensitivityFlags`, not only the literal `"do_not_publish"`. Implementation folds into
  **GOV-568** scope (impl owner) once 5.08 is authorized — no new issue spawned, to avoid compounding
  the held-chain drift the chain is currently parked on. See AC3 row for the exact code change + VSR
  test requirement.
- **AC1, AC2, AC4 require VSR's verified behavior evidence** (test run output + log sample + reviewer
  sign-off, per GOV-564 §8 VSR child) before this security gate can close `done`.

## True blocker chain (corrected)

GOV-570 (this, SEC) → blockedBy GOV-569 (VSR) → blockedBy GOV-568 (impl) →
**blockedBy GOV-572 `[Stage 5.08][CTO→CEO] Authorize or hold deferred 5.08` (in_progress, CEO)**.

The entire 5.08 chain is held on **GOV-572**, a CTO→CEO sequencing/authorization gate: the 5.08 goal
is `planned` + DEFERRED and a live impl run drifted ahead of the staging frontier. **No level of this
chain (impl, VSR, or SEC) advances until CEO rules on GOV-572.**

**Unblock owner / action:** **CEO** authorizes (or holds) deferred 5.08 via **GOV-572** → GOV-568 impl
applies the fail-closed `sensitivityFlags` fix above → VerificationSafetyReviewer completes GOV-569
with verified evidence → SecurityPrivacyAgent runs the final gate against this checklist and closes
GOV-570.
