# Stage 5.15 — Agent Handoff & Owner-Escalation Contract

**Issue:** GOV-584 (plan + sequence) · **Stage:** 5.15 · **Goal:** `8ae9a6ae-cbbf-4277-9614-b19989cba78c` (Agent handoff and owner escalation)
**Owner:** CTO (impl chain) / CEO (staging) · **Status:** planning contract (versioned, Alpine-only)
**Substrate covered (Stage 4/5):** `src/source-registry.js`, `src/statement-verification.js`,
`src/digest-assembler.js`, `src/refresh-runner.js`, `src/briefing.js`, `src/doc-continuity.js`
(and the Python-substrate back-gap analyzer, 5.13)
**Governing boundary contract:** `Docs/stage4-automation-ai-boundary.md` (GOV-471) — Stage 5.15
EXTENDS that contract; it does not replace it. The buildable-envelope / no-public-output rule
(GOV-436, GOV-420) remains in force. This is the **final non-deferred Stage 5 subgoal**; closing it
lets the Stage 5 parent (`9d3d7fbd`) flip to `achieved`.

---

## 1. Purpose

Define, as a **deterministic, source-grounded** contract, **how work moves between agents** and
**when Isaac must decide**, for the Stage 5 trust mechanics (corrections, hot-topic, source-change /
Wayback, future-fact verification of past AI assumptions).

A handoff is the moment a deliverable leaves one role and becomes another role's responsibility
(e.g. impl → VSR → security → CTO merge → CEO ledger flip). Each handoff must carry a **complete,
trust-separated evidence bundle** and **preserve review status**, so the receiving role can act with
no out-of-band knowledge and no possibility of an AI assumption being mistaken for a verified fact.

Two distinct concerns, plus a stage-exit concern:

1. **Agent handoff model** — which role hands off to which, on what trigger, with what evidence
   contract carried across the handoff, and what review status is preserved.
2. **Owner-escalation triggers** — the explicit, enumerated conditions that require Isaac (the owner)
   to decide before work continues: publication, scope expansion beyond Alpine, AI-label /
   verification-policy change, legal/privacy on individuals, budget.
3. **Next-stage handoff** — what Stage 5 hands to Stage 6 and the exit-gate condition that must hold
   before that handoff is permitted.

This is a **planning/contract slice only** — no module/test implementation occurs in GOV-584.
Downstream child issues (impl, VSR, security) derive their acceptance criteria from this document.

**Governing principle (inherited from GOV-471):** Deterministic logic owns the routing, validation,
trust-separation, and escalation decision. The evaluator reads an explicit handoff request and the
declared evidence bundle and produces a reviewer-internal verdict. It **never** mutates issues,
**never** auto-publishes, **never** contacts an owner channel, and **never** uses AI to decide whether
a handoff is permitted or an escalation is required. AI output, if surfaced at all, is confined to an
optional human-readable summary under an `ai_analysis` label over a verdict the deterministic engine
already produced — it can never be primary evidence and never flips a `deny`/`escalate` to `allow`.

---

## 2. Scope and non-scope

| In scope (Alpine-only) | Out of scope |
|---|---|
| A declarative **role-transition model** (who may hand to whom, on what trigger) | Any Wyoming/US expansion |
| Deterministic **handoff validator** (transition legal? evidence bundle complete + trust-separated?) | Auto-publication / sending any owner notification (email/Slack/web — Isaac-gated, GOV-420) |
| A declarative **owner-escalation trigger set** (the five enumerated owner gates) | AI-decided handoff permission or escalation classification |
| Deterministic **escalation evaluator** (action/condition → `required` / `not_required`, fail-closed) | Mutating source / statement / verification records or issue state |
| **Evidence-bundle trust separation** (facts / summaries / ai_assumptions / later_verification buckets) | Editorial/newsletter handoffs (deferred x.08 lane — GOV-572 HOLD) |
| **Review-status preservation** invariant across every handoff | Advancing deferred 5.08 / 5.09 / 5.11 chains (Isaac owner-gate) |
| **Stage-5 → Stage-6 exit-gate** predicate over subgoal + ledger state | Filing/triaging findings as new issues automatically |
| Reviewer-internal verdict (object + deterministic text render) | Any unapproved external automated network call |
| Deterministic, idempotent, read-only analysis | |

---

## 3. Inputs

The evaluator is a pure function of explicit inputs — no hidden clock, no network, no live board read.

1. **Role-transition model** — an explicit, version-controlled descriptor (committed in the repo, e.g.
   `Docs/handoff-escalation-model.json` or an exported constant) declaring the legal handoffs. Each
   entry: `{ from, to, trigger, requiredEvidenceKinds[], preservesReviewStatus }`. Supplied as data;
   the evaluator does not invent transitions. `from`/`to` ∈ the Stage 5 role set
   (`impl`/Backend/Automation, `VSR`, `Security`, `CTO`, `CEO`, `Owner`). `trigger` ∈
   `{ impl_complete, vsr_pass, sec_pass, cto_merge, correction_filed, hot_topic_flag,
   source_change_detected, future_fact_verified, ledger_flip_ready, owner_decision }`.
2. **Handoff request** — the proposed handoff under evaluation:
   `{ from, to, trigger, evidenceBundle, reviewStatus }`. The evaluator decides whether this request
   is permitted by the model and whether its bundle is complete + trust-separated.
3. **Evidence bundle** — a structured object with **trust-separated buckets** that must stay disjoint:
   `{ facts[], summaries[], aiAssumptions[], laterVerification[] }`. `facts` are source-grounded and
   carry a `sourceRef`; `summaries` are human/AI prose *about* facts; `aiAssumptions` are
   unverified AI inferences (e.g. an assumption made about a then-unknown future fact);
   `laterVerification` records the real-world outcome that later confirmed/refuted an assumption,
   without rewriting the known-then record (GOV-36 typed-link discipline).
4. **Escalation-trigger set** — the declarative owner-gate descriptor: each gate
   `{ id, conditionKind, severity, requiresOwner: true }` over `conditionKind ∈
   { publication, scope_expansion_beyond_alpine, ai_label_or_verification_policy_change,
   legal_privacy_on_individual, budget }`. Supplied as data.
5. **Proposed action/condition** — for the escalation evaluator: `{ conditionKind?, descriptorFlags }`
   describing what the slice wants to do; the evaluator maps it to `required` / `not_required`.
6. **Stage-state snapshot** *(for the exit-gate predicate)* — `{ subgoals: [{ id, status, deferred }],
   handoffLedger: [HandoffRecord] }`, passed in by the caller (no live goals API read).
7. **`now` / window bounds** — passed in by the caller (determinism; mirrors `refresh-runner`'s
   injected-clock pattern). The evaluator must not call `Date.now()` directly.

---

## 4. Verdict model (output)

The evaluator returns a structured, JSON-serializable verdict — never mutates inputs:

```
{
  generatedAt,                 // echoes injected `now`
  modelVersion,
  handoff: {
    decision,                  // "allow" | "deny"
    violations: [ Violation ], // empty iff decision === "allow"
    preservedReviewStatus,     // the review status carried to the receiving role
    evidenceContract: {        // the trust-separated bundle, validated
      facts, summaries, aiAssumptions, laterVerification, separationOk
    }
  },
  escalation: {
    decision,                  // "required" | "not_required"
    gatesTriggered: [ id ],    // owner gates that fired
    reason                     // deterministic one-line string
  },
  stageExit: {                 // present only when a stage-state snapshot is supplied
    decision,                  // "ready" | "blocked"
    blockingSubgoals: [ id ],  // non-deferred subgoals not yet `achieved`
    reason
  },
  summary                      // deterministic one-line text
}
```

Each `Violation`:

```
{
  type,        // enumerated below
  severity,    // "high" | "medium" | "low"
  subjectId,   // transition key / evidence bucket / gate id
  detail,      // deterministic human-readable string, no AI
  evidence     // { from?, to?, trigger?, missingEvidenceKinds?, crossContaminatedBucket?, ... }
}
```

### 4.1 Handoff-validation violation types

| `type` | Meaning | Severity |
|---|---|---|
| `illegal_transition` | `(from, to, trigger)` is not present in the role-transition model | high |
| `missing_required_evidence` | The bundle omits an evidence kind the transition requires | high |
| `ai_assumption_as_fact` | An `aiAssumptions` item appears in (or is labeled as) `facts` | high |
| `fact_without_source` | A `facts` item lacks a `sourceRef` | high |
| `bucket_cross_contamination` | The four trust buckets are not disjoint (same item in two buckets) | high |
| `review_status_dropped` | A transition declared `preservesReviewStatus` but the request drops/empties it | medium |
| `unknown_trigger` | `trigger` is outside the enumerated trigger set | medium |
| `unrecognized_role` | `from` or `to` is outside the Stage 5 role set | medium |

### 4.2 Escalation-evaluation outcomes

| Outcome | Meaning |
|---|---|
| `required` | `conditionKind` matches an owner gate, **or** the action is ambiguous/unknown (**fail-closed**) |
| `not_required` | The action maps to no owner gate and is unambiguously inside the Alpine buildable envelope |

> **Fail-closed rule:** an action whose `conditionKind` cannot be resolved, or whose
> `descriptorFlags` are unrecognized, evaluates to `required` (escalate), never `not_required`.
> The evaluator never silently lets a possibly-owner-gated action through.

### 4.3 Stage-exit predicate

`stageExit.decision === "ready"` **iff** every non-deferred Stage 5 subgoal is `achieved` **and** the
handoff ledger contains no `deny`/`required` handoff still open. A subgoal is **deferred** iff its
descriptor carries the `DEFERRED ... (Isaac-gated)` banner (5.08 / 5.09 / 5.11) — deferred subgoals
do not block the exit gate (mirrors the goal-ledger rule-2 deferred-child exclusion in
`CTO_WORKFLOWS.md`). Otherwise `blocked`, listing the blocking subgoal ids.

> **Note on overlap with siblings:** 5.13 (`backgap-regression`) audits *verification records*;
> 5.14 (`doc-continuity`) audits *documentation/module state*; 5.15 (`handoff-escalation`) audits
> *role transitions, evidence custody, and owner gates*. All three share the read-only / deterministic
> / fail-closed posture and the `{ ..., violations/findings, summary }` report shape, but operate on
> disjoint inputs and must not be merged.

---

## 5. Behavioral guarantees (acceptance-relevant invariants)

1. **Read-only.** The evaluator must not mutate any input, issue, record, or file. Same inputs in ⇒
   identical verdict out (deep-equal); verified by running twice on a frozen fixture.
2. **Deterministic & idempotent.** No `Date.now()`, no `Math.random()`, stable sort on
   `(severity desc, type, subjectId)`. Re-running on an unchanged request yields an identical verdict.
3. **Fail-closed on ambiguity.** Unknown transitions deny; unknown/ambiguous escalation conditions
   escalate; missing stage-state degrades to an explicit absent `stageExit` block — never a silent
   `allow` / `not_required` / `ready`.
4. **Trust separation is hard.** `facts`, `summaries`, `aiAssumptions`, `laterVerification` are kept
   disjoint and individually labeled at every handoff; no AI assumption is ever promotable to a fact
   inside the evaluator. Known-then context is never rewritten by a later verification (GOV-36).
5. **No network, no owner contact.** The evaluator performs no external calls and never sends an owner
   notification — escalation `required` is a *verdict*, surfaced to CEO/CTO, not an automated message.
6. **No public output.** The verdict is a reviewer-internal artifact. No email, no public web, no
   editorial surface (deferred x.08 lane — GOV-572 HOLD). Buildable-envelope only (GOV-436, GOV-420).
7. **AI boundary.** No AI in the decision path. Optional summary prose only over an already-produced
   deterministic verdict, labeled `ai_analysis`, never primary evidence, never flips a decision.

---

## 6. Implementation shape (for the impl child)

The impl child has **two deliverables**:

**(A) The deterministic handoff/escalation evaluator**
- New module `src/handoff-escalation.js`, ESM `export` style consistent with the existing substrate.
- Pure exported functions:
  - `evaluateHandoff({ model, request, now })` → the §4 `handoff` block.
  - `evaluateEscalation({ triggerSet, action, now })` → the §4 `escalation` block.
  - `evaluateStageExit({ snapshot, now })` → the §4 `stageExit` block.
  - `evaluateHandoffSlice({ model, request, triggerSet, action, snapshot, now })` → the full §4 verdict
    (composes the three above).
  - `renderHandoffVerdict(verdict)` — deterministic text render (mirrors
    `refresh-runner.renderRunLog` / `doc-continuity.renderContinuityReport` style).
  - Frozen `HANDOFF_VIOLATION_TYPES` `{ type: severity }` map and `OWNER_GATES` constant.
- Reuse existing helpers where applicable (canonicalization, stable-sort, severity ordering); do not
  re-derive predicates already shipped in the substrate.
- Test file `test/handoff-escalation.test.js` (node:test) covering **every** §4.1 violation type, both
  §4.2 escalation outcomes (including the fail-closed unknown-condition case), the §4.3 exit predicate
  (ready + blocked + deferred-exclusion), and the §5 read-only/idempotent/no-AI-promotion invariants.

**(B) The declarative model the evaluator reads**
- A committed role-transition model + owner-gate set (`Docs/handoff-escalation-model.json`, or an
  exported constant the test imports) encoding the real Stage 5 chain:
  `impl --impl_complete--> VSR --vsr_pass--> Security --sec_pass--> CTO --cto_merge--> CEO`
  plus the trust-mechanic handoffs (`correction_filed`, `hot_topic_flag`, `source_change_detected`,
  `future_fact_verified`) and the five owner gates.
- A continuity doc `Docs/modules/handoff-escalation.md` (mirrors the §B module-doc pattern) covering
  purpose, public surface, I/O, invariants, and the `node --test` entry — and a matching entry added
  to `Docs/doc-maintenance-manifest.json` so the 5.14 `doc-continuity` detector reports zero
  `missing_module_continuity_doc` / `orphan_doc` for the new module after this slice.

**Definition of impl done:** `node --test` full suite green (including the new test, and the 5.14
`doc-continuity` detector still returning `handoffReady: true` with the new module documented), and a
recorded run of `evaluateHandoffSlice` over the real Stage 5 chain returning `allow` /
`not_required` for the legitimate chain handoffs and the correct `deny` / `required` for the negative
fixtures, with the verdict text pasted into the impl issue comment.

---

## 7. Stage gate & sequencing

Mirrors the Stage 5.13/5.14 slices (plan → impl → VSR → security). Chain built with the
`status:blocked` + blocker-GOV-id-in-description mechanism, because `blockedByIssueIds` does not
persist in this build (verified: null on every GOV-580→583 node).

| Step | Issue | Owner | Blocked by | Initial status |
|---|---|---|---|---|
| Plan + sequence | **GOV-584** (this) | CTO | — | this slice |
| Impl | 5.15-impl | AutomationOpsEngineer | — | **todo (chain head)** |
| VSR | 5.15-vsr | VerificationSafetyReviewer | impl | blocked |
| Security | 5.15-sec | SecurityPrivacyAgent | vsr | blocked |

Only the impl child is unblocked. VSR and Security carry the blocker in their description
(`status:blocked` + blocker GOV-id). No scope beyond Alpine; no public/email/editorial output; no
owner notification is automated. The deferred Stage 5.08/5.09/5.11 chains are NOT advanced (Isaac
owner-gate). 5.15 is the **only remaining non-deferred subgoal** — its closeout flips the Stage 5
parent (`9d3d7fbd`) to `achieved` and arms the Stage-5 → Stage-6 exit gate (§4.3), which remains a
CEO/owner proposal, never an auto-unlock.

---

## 8. Verification evidence required at each step

- **Impl:** `node --test` full suite green; new `src/handoff-escalation.js` +
  `test/handoff-escalation.test.js` + `Docs/handoff-escalation-model.json` +
  `Docs/modules/handoff-escalation.md` + manifest entry committed; commit SHA + file paths + the
  evaluator's verdict text (positive `allow`/`not_required` chain run **and** negative
  `deny`/`required` fixtures) in the issue comment; `doc-continuity` still `handoffReady: true`.
- **VSR:** independent confirmation of the §5 invariants (read-only, deterministic/idempotent,
  fail-closed on illegal transition + ambiguous escalation, trust-separation hardness with a planted
  `ai_assumption_as_fact`, no-network/no-owner-contact, no-public-output, AI-boundary) with the exact
  commands run and their output; explicit pass/fail per invariant.
- **Security:** privacy/publication-gate review — confirm the verdict and model docs contain no
  publishable-as-fact unreviewed content, no PII beyond what the substrate already holds, that the
  escalation path never auto-contacts an owner or publishes, and that the buildable-envelope (no
  public output) holds; pass/fail with evidence.

**Closeout:** at impl merge, flip goal `8ae9a6ae-cbbf-4277-9614-b19989cba78c` → `achieved` with a CTO
closeout comment naming the merge commit and the dependents transitioned, then surface to CEO that the
Stage 5 parent (`9d3d7fbd`) is ready to flip `achieved` and the Stage-5 → Stage-6 exit gate is armed
for owner review.
