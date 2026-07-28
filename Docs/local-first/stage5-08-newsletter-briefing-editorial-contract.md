# Stage 5.08 — Newsletter / Briefing / Editorial Behavior Contract

**Issue:** GOV-564 · **Stage:** 5.08 · **Owner:** CTO (impl chain) / CEO (staging)
**Status:** planning contract (versioned behavior contract, Alpine-only)
**Substrate inherited from Stage 4:** `src/source-registry.js`, `src/statement-verification.js`,
`src/digest-assembler.js`, `src/refresh-runner.js`
**Governing boundary contract:** `Docs/stage4-automation-ai-boundary.md` (GOV-471) — Stage 5.08
EXTENDS that contract; it does not replace it.

---

## 1. Purpose

Define how Government Watchdog produces and gates newsletter, briefing, and editorial content for
the Town of Alpine, incorporating Stage 5 capabilities: Wayback archive verification (F3), hot-topic
detection (F4), and the Stage 5 corrections system. This is a **planning/contract slice only** —
no product feature implementation occurs here. Downstream child issues (impl, VSR, security) derive
their acceptance criteria from this document.

**Stage 5 additions over Stage 4:**
- Correction-awareness: briefings must reflect `false_corrected` and `disputed` transitions and
  must not re-publish superseded claims.
- Hot-topic surface: changed or high-activity Alpine sources are surfaced before the weekly digest
  assembly (editorial triage, not auto-publication).
- Wayback confirmation: external archive availability is checked as a Stage 5 verification signal
  (gated external call — CEO/CTO authorization required before any automated network call).
- All Stage 4 guarantees (source-grounding, publication gate, fail-closed, human review gate,
  Isaac-gated public delivery) remain in force.

**Governing principle (inherited):** Deterministic automation owns collection, identity,
validation, exact-source linking, archive checks, gate enforcement, and digest assembly. AI may
only summarize evidence that deterministic automation has already collected and source-bound, under
a visible label with a citation trail. AI output is never primary evidence and can never earn a
`verified` status.

---

## 2. Scope and non-scope

| In scope (Alpine-only) | Out of scope |
|---|---|
| Weekly briefing digest for Alpine registered sources | Any Wyoming/US expansion |
| Hot-topic editorial triage (changed/high-activity sources) | Auto-publication to email or public web (Isaac-gated, GOV-420) |
| Correction-aware digest: reflects `false_corrected` / `disputed` transitions | Publishing disputed claims as facts |
| Wayback archive availability signal (gated) | Any unapproved external automated call |
| Reviewer-internal summary view | Any summary delivered to end-users without reviewer approval |
| AI-assisted prose under `ai_analysis` label | AI-generated factual claims without source trace |

---

## 3. Behavior definition: what the newsletter/briefing/editorial surface does

### 3.1 Inputs

The briefing pipeline consumes:

1. **Source registry** — Alpine-only sources in `current` lifecycle state (`buildSourceCapture`,
   `canonicalizeUrl`, `hashFileSha256`). Replaced or missing-after-capture sources are excluded from
   new claims.
2. **Statement records** — `publishable === true` statements produced by `evaluatePublicationGate`.
   Only `fact_claim` statements with at least one `sourceLink` + trace hash, passed publication gate,
   not flagged `do_not_publish`, not `ai_analysis_as_fact`, and with `evidenceLimits` present.
3. **Correction history** — `correctionHistory` entries from `applyVerificationTransition('correct_false')`.
   Any statement whose prior text appeared in a prior digest and is now `false_corrected` must
   surface a correction notice in the new briefing, not the stale claim.
4. **Disputed statements** — `disputed` status statements must surface only under a visible
   "Disputed" label if shown at all; they may not appear as settled claims.
5. **Hot-topic signal** (Stage 5 / F4) — sources whose `contentHash` changed since the last
   weekly pass, or whose change frequency exceeds the configured threshold. These sources are
   surfaced to the editorial reviewer for triage before the digest is finalized. Detection is
   deterministic (hash/diff/recency); AI summarizes only after reviewer triage.
6. **Wayback availability signal** (Stage 5 / F3) — external archive confirmation that an
   independent archived copy exists for each source backing a published claim. This step contacts
   an external service and MUST NOT run in automated mode without explicit CEO/CTO authorization.
   Until authorized, this field is recorded as `unchecked` (not as a failure).

### 3.2 Cadence

| Cadence type | Trigger | Who runs it |
|---|---|---|
| **Weekly deterministic refresh** | Scheduled (weekly); on-demand re-validation | `runWeeklyRefresh` (F2, `src/refresh-runner.js`) |
| **Hot-topic editorial triage** | After weekly refresh, before digest assembly, if changed sources exist | Reviewer-internal; reviewer reads triage list and marks items for inclusion/hold |
| **Correction notice assembly** | After every refresh; any `false_corrected` statement whose prior text appeared in a prior digest triggers a correction notice | Deterministic; reviewer confirms before briefing is treated as current |
| **Digest assembly** | After reviewer triage and correction confirmation | `assembleDigest` (F1, `src/digest-assembler.js`); publishable statements only |
| **Wayback check** (Stage 5 / F3) | Per-digest cycle, after digest assembly; gated | External call; requires CEO/CTO authorization before any automation |

**Cadence is deterministic; AI does not run on a schedule.** AI summarization is invoked only on
the already-collected, already-verified evidence set, on demand, after the deterministic refresh
and reviewer triage complete.

### 3.3 Editorial output types

| Output type | Who sees it | Publication gate |
|---|---|---|
| **Reviewer-internal weekly digest** | Reviewer only | `publishable === true` + reviewer sign-off |
| **Hot-topic triage list** | Reviewer only | Reviewer action required before changed sources enter the digest |
| **Correction notice** | Reviewer first; then inherits digest gate | Correction history + correcting-source link required |
| **AI-assisted prose summary** | Reviewer only; visible AI label required | Source-grounded, `ai_analysis` kind, citation trail required |
| **Public/email delivery** | **Isaac-gated (GOV-420)** | Out of scope until owner approves |

---

## 4. Automation vs AI boundary (Stage 5.08 extension of Stage 4.09)

The Stage 4.09 boundary table (§2 of `stage4-automation-ai-boundary.md`) governs; Stage 5.08
adds the following rows:

| Step | Owner | Substrate symbol | Notes |
|---|---|---|---|
| **Hot-topic detection** (F4) | **Deterministic** | `hashFileSha256` + stored `contentHash` comparison; recency/frequency thresholds | Detection only; AI does not decide which sources are "hot" |
| **Correction notice generation** | **Deterministic** | `correctionHistory` from `applyVerificationTransition('correct_false')` | Correction text + correcting-source link assembled by code; AI may rephrase only under `ai_analysis` label |
| **Wayback availability check** (F3) | **Deterministic gate + CEO/CTO authorization** | External HTTP call (Wayback API); result stored per-source as `waybackStatus` field | Must NOT run without explicit CEO/CTO authorization; until authorized, field = `unchecked` |
| **Editorial triage review** | **Human (reviewer)** | Reviewer reads triage list; marks changed sources include/hold before assembly | No AI auto-approval |
| **AI briefing prose** | **AI, gated** | `createStatement({ kind: "ai_analysis" })` + citation trail | Inherits all Stage 4.09 AI constraints: labeled, source-grounded, no new claims, evidence limits required |

**Unchanged from Stage 4.09:** Lanes 1–6 of the AI Gateway processing workflow remain in force.
No new AI capabilities are introduced. AI summarization constraints (§4 of Stage 4.09 contract)
are not relaxed.

---

## 5. Correction-awareness requirements

These rules prevent superseded claims from being re-published.

1. **Correction history scan.** After each weekly refresh and before digest assembly, the pipeline
   scans all statements for `status === "false_corrected"`. If any such statement's prior text
   appeared in a prior digest, a correction notice is generated.
2. **Correction notice content.** The notice must include: the original claim (as `priorText`),
   the corrected text (as `newText`), the `reason` for correction, and the `correctingSourceLink`.
   It must not repeat the prior incorrect text as if it were settled fact.
3. **No re-publication of superseded claims.** A statement with `status === "false_corrected"` is
   excluded from the `publishable === true` pool. It may only appear as historical context, under a
   "Corrected" label, with the correction notice present.
4. **Disputed statements.** A statement with `status === "disputed"` may appear only under a
   visible "Disputed" label and the `disputeReason`. It may not appear in the main briefing body
   as a settled claim.
5. **Stage 5 correction pipeline.** Corrections generated by the Stage 5 corrections system
   (verification hot-topics, Wayback-identified changes) flow through the same
   `applyVerificationTransition` path. No separate correction format.

---

## 6. Publication-safety and gated-beta access

- **Gated-beta rule.** All briefing output is reviewer-internal (gated-beta access boundary,
  `GATED_BETA_ACCESS_WORKFLOW`). No briefing or editorial content is delivered to end-users in
  the gated-beta period without reviewer approval.
- **GOV-420 Isaac gate.** Public delivery (email, public web) remains Isaac-gated. No automated
  public dispatch may be implemented without owner approval. Editorial output is review-gated,
  not auto-published.
- **AI label visibility.** AI-assisted prose must render visually separate from source-backed
  facts, with the AI label and source/audit links visible. Visual polish must never imply
  verification.
- **No-overclaim.** AI prose may compress/paraphrase already-cited evidence. It may not introduce
  a fact, motive, intent, legal conclusion, allegation, or entity not present in the cited source.
- **Privacy/safety hard stops.** No private identity/address/voter-registry data in any briefing
  output. No public accusations or legal conclusions without owner approval. `do_not_publish` flags
  are hard exclusions regardless of verification status.

---

## 7. Source-grounding rule

Every editorial claim in a briefing must trace to a registered source/statement-verification
record. Concretely:

- Every `fact_claim` statement in the digest must have ≥1 `sourceLink` with a stable
  `computeSourceTraceHash` value.
- Every AI-assisted summary (`ai_analysis`) must carry ≥1 `sourceLink` and non-empty
  `evidenceLimits`.
- A digest line that cannot be traced back to a `publishable` statement with a source link and
  trace hash is a defect and must be excluded (logged with reason).
- Correction notices must cite their `correctingSourceLink`.

This rule inherits from `BACKEND_FRONTEND_EVIDENCE_WORKFLOW` and `AI_GATEWAY_PROCESSING_WORKFLOW`
and is enforced by `evaluatePublicationGate` and `assertHasSourceTrace`.

---

## 8. Exit gate and downstream impl chain

This contract is the chain head. Downstream child issues must be spawned by CTO before any
implementation work begins.

### Downstream child issues (CTO to create)

| Child | Title pattern | Owner | Blocked by |
|---|---|---|---|
| **Impl** | [Stage 5.08-impl] Backend: implement hot-topic detection + correction-aware digest | AutomationOpsEngineer | This contract (GOV-564) |
| **VSR** | [Stage 5.08-vsr] Verification: validate correction-aware briefing behavior | VerificationSafetyReviewer | Impl child |
| **Security** | [Stage 5.08-sec] Security/privacy gate review for briefing output | SecurityReviewer | VSR child |

### Acceptance criteria for each child

**Impl child:**
- `hashFileSha256`-based hot-topic detection flags sources with changed `contentHash`; produces
  a triage list (not a digest entry) for reviewer action.
- Correction-aware assembly: any `false_corrected` statement with prior digest presence generates
  a correction notice; statement is excluded from `publishable` pool.
- Disputed statements excluded from main digest body.
- All new logic covered by deterministic tests (no AI in tests).
- `npm test` green; no existing tests broken.
- Log output per established format (`[YYYY-MM-DD HH:MM:SS] [LEVEL] msg`).
- Wayback check (F3) NOT implemented; field recorded as `unchecked` pending CEO/CTO
  authorization.

**VSR child:**
- Reviewer verifies that `false_corrected` statements do not appear as facts in digest output.
- Reviewer verifies that disputed statements appear only under "Disputed" label.
- Reviewer verifies that hot-topic triage list surfaces correctly and requires reviewer action
  before inclusion.
- Reviewer verifies AI prose carries `ai_analysis` label and source links.
- Evidence: test run output, log sample, and a reviewer sign-off comment.

**Security child:**
- Confirms no private identity/address/voter-registry data in briefing output.
- Confirms no public accusations or legal conclusions without owner approval.
- Confirms `do_not_publish` and `sensitive_flag` hard-exclude from all output paths.
- Confirms AI label is visible and not suppressible by visual polish.
- Evidence: checklist against `RISK_ASSESSMENT_WORKFLOW` and `GATED_BETA_ACCESS_WORKFLOW`.

---

## 9. Contract operations

| Field | Value |
|---|---|
| **Run trigger** | Weekly deterministic refresh (F2); on-demand re-validation; hot-topic triage after refresh |
| **Input contract** | Registered Alpine sources (current lifecycle); statement records with source links; correction history; (Stage 5) hot-topic detection signal; (Stage 5 / F3 gated) Wayback status |
| **Output contract** | Reviewer-internal weekly digest (publishable statements only); hot-topic triage list; correction notices; AI-assisted prose (labeled); run log. No public/email output (gated, GOV-420). |
| **Log path** | `Logs/stage5-newsletter-briefing.log` (gitignored, local/vault-only) |
| **Failure handling** | Fail-closed (inherited from Stage 4.09 §5). Excluded statements logged with reasons. |
| **Issue threshold** | 3+ consecutive run failures; missing-after-capture on a published source; scope leak (non-Alpine); any digest line failing trace-back; any correction notice missing `correctingSourceLink`; any AI item lacking source link/evidence limits |
| **Review cadence** | Human review of each weekly run log and triage list before digest is treated as current. No auto-promotion. |
| **Owner** | AutomationOpsEngineer (impl); VerificationSafetyReviewer (VSR); SecurityReviewer (security gate); CTO (sequencing); CEO (Stage 5 unlock, Wayback authorization); Isaac (public publication, GOV-420) |
| **Escalation / hard stops** | External calls (F3 Wayback) require CEO/CTO authorization. Scope beyond Alpine, public/email delivery (GOV-420), any unapproved automated external call → escalate before proceeding |

---

## 10. Verification evidence (this contract)

This is a planning/contract document. It produces no new code. Verification evidence is:

- File path: `Docs/stage5-08-newsletter-briefing-editorial-contract.md` (this document).
- Downstream child issues: spawned by CTO as the impl chain head; issue IDs recorded in GOV-564
  evidence comment before close.
- Alignment: inherits Stage 4.09 19-test substrate (`npm test` → 19 passing); no tests added by
  this contract alone.
- Stage 4.09 boundary contract (`Docs/stage4-automation-ai-boundary.md`) remains the governing
  substrate contract; this document extends it for Stage 5.08.
