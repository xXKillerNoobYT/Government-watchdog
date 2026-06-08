# Stage 1.11 Alpine Security, Privacy, and Publication Gates Contract

Issue: GOV-55
Owner role: SecurityPrivacyAgent (`72d0eccf`)
Stage: Stage 1.11, planning/specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only
Created: 2026-06-08

## Gate Decision

GOV-55 passes when this document gives CTO, VerificationSafetyReviewer,
SecurityPrivacyAgent, BackendCrawlerEngineer, SourceArchivist,
TranscriptEvidenceEngineer, NewsletterEditor, FrontendTimelineEngineer, and
AutomationOpsEngineer a single authoritative answer to one question: **before any
Government Watchdog record, summary, label, page, export, newsletter, or API
response can move from local/vault material toward any human's eyes — internal
reviewer, gated-beta user, or the public — exactly which security, privacy, and
publication gates must ALL hold, who owns each gate, and what the system does on
ambiguity.**

The answer this contract commits to: **default-deny.** A record surfaces only
through an explicit allow; every unknown, missing, ambiguous, AI-asserted,
disputed, private, or unreviewed input lands in a gated state and stays local.
The gates are rules with named escalation points, not decisions this contract
makes. This pass does **not** publish anything, contact any official or
subscriber, make a privacy/defamation judgment about a specific real individual,
change any AI-label policy, or expand beyond Alpine (§12). Stage 1 implementation
stays locked.

The only downstream unlock is the next sequential Stage 1 planning gate
(Stage 1.12 traceability). Any implementation issue created later must explicitly
consume this contract, name its own narrow Alpine step, and name the gate(s) it
exercises plus its reviewer lane.

## Inputs Read (predecessor evidence — daisy chain)

- Required agent instructions: `AGENTS.md`, `COMPANY.md`, `SOUL.md`, `TOOLS.md`,
  `HEARTBEAT.md`, `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`,
  `SECURITY_PRIVACY_WORKFLOWS.md`, `STAGE0_EXECUTION_WORKFLOW.md`,
  `RISK_ASSESSMENT_WORKFLOW.md`, `GATED_BETA_ACCESS_WORKFLOW.md`,
  `AI_GATEWAY_PROCESSING_WORKFLOW.md`, `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`.
- Staged master plan:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`
- Stage 0.11 base security/privacy/publication gates: **GOV-21** (backend
  project, goal `b4c1ecc4`) and its tracked-log-removal / boundary-CI follow-up
  **GOV-22**.
- Stage 1.05 backend/tooling contract `Docs/stage1-backend-tooling-implementation-contract.md`
  (incl. GOV-36/37/38/39 Card Status Vocabulary — authoritative
  `verificationStatus`, `uiStatus-map.v1`, fail-closed publication allowlist).
- Stage 1.06 frontend/product-surface contract.
- Stage 1.07 transcript/evidence/statement contract
  `Docs/stage1-transcript-evidence-statement-contract.md` (GOV-40 done).
- Stage 1.08 newsletter/briefing/editorial contract
  `Docs/stage1-newsletter-briefing-editorial-contract.md` (GOV-43 done).
- Stage 1.09 automation-vs-AI boundary matrix contract
  `Docs/stage1-automation-ai-boundary-matrix-contract.md` (GOV-46 done;
  GOV-47 VSR APPROVE, GOV-48 CTO APPROVE).
- Stage 1.10 QA & workflow testing plan contract
  `Docs/stage1-qa-workflow-testing-plan-contract.md` (GOV-50 done; GOV-51 CTO
  APPROVE, GOV-52 SecurityPrivacyAgent privacy consult).
- Authoritative status code: `scripts/validate_concept_map_export.py` —
  `SCHEMA_VERSION = "gov-watchdog-card-map.v1"`, `ALLOWED_VERIFICATION_STATUSES`,
  `ALLOWED_UI_STATUSES`, `REVIEWED_VERIFICATION_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`,
  `_VERIFICATION_STATUS_ROLES` import-time parity assertion. Existing contract
  test: `tests/test_validate_concept_map_export.py`.
- Secret-path posture: `Docs/stage0-github-paperclip-secret-path.md`.
- Premium template:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`

### Predecessor-evidence note (read before relying on this contract)

The predecessor contracts (1.05–1.10) and the `validate_concept_map_export.py`
validator currently live on **unmerged task branches**
(`gov-17-newsletter-briefing-contract`, `GOV-40-…-transcript…`,
`GOV-43-…-newsletter…`, `GOV-46-…-automation…`,
`GOV-50-…-qa…`), not on `main`. This contract is spec-only and cites those
artifacts by stable path + constant name. A future implementation gate must run
its boundary checks against the **merged** versions of those files; if a constant
named here has drifted at merge time, the merge — not this contract — is the
authority, and this contract is patched to match. This contract never redefines
the status vocabulary, the `uiStatus` mapping, the publication allowlist, or any
type enum — those are owned by GOV-36/37/38/39. It defines how the security,
privacy, and publication **gates** consume that vocabulary and what controls wrap
it.

---

## 0. Authoritative vocabulary the gates consume (reference, not redefinition)

Reproduced read-only so the gates below can be specified against concrete values.
The validator is the source of truth.

**`ALLOWED_VERIFICATION_STATUSES` (6 values):** `source_recorded`,
`machine_extracted_unreviewed`, `reviewed_source_linked`, `human_verified`,
`disputed`, `do_not_publish`.

**`REVIEWED_VERIFICATION_STATUSES` (derived — the only "review complete" set):**
`reviewed_source_linked`, `human_verified`.

**`ALLOWED_UI_STATUSES` (10 values, kebab-case wire form):** `do-not-publish`,
`disputed`, `source-missing`, `source-changed`, `corrected`,
`needs-clarification`, `unverified`, `pending-review`, `archived-source-backed`,
`source-backed`.

**`PUBLICATION_ELIGIBLE_UI_STATUSES` (3 values — the entire publishable set):**
`source-backed`, `archived-source-backed`, `corrected`.

**`compute_ui_status` rules #1–#12:** evaluated top-down, first match wins,
publication-gating states outrank reassuring ones. Rules **5, 10, 11** are the
*only* branches that yield a publication-eligible value, and each is guarded by
`reviewed = verificationStatus ∈ REVIEWED_VERIFICATION_STATUSES`. Absent boolean
signals (`sourcePresent` / `archivePresent` / `rawPreserved` / `sourceChanged`)
are treated as `False` (conservative direction). Rule **12** is the fail-closed
default: any unhandled or unknown input → `pending-review`, which is **not**
publishable.

**Import-time drift guard:** `validate_concept_map_export.py` asserts
`set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES`. A 7th
verificationStatus added without a mapping role makes the module fail to import,
rather than silently fail-opening through rule 12. The publication gate (§1)
depends on this assertion holding.

The single invariant every gate in this contract serves: **a record reaches a
`PUBLICATION_ELIGIBLE_UI_STATUSES` value, and therefore can carry
`publicExportApproved == true`, only through a human-review promotion to a
`REVIEWED_VERIFICATION_STATUSES` value; no other path exists, and any
unknown/ambiguous input lands in a gated state.**

---

## 1. Publication Gate

**Rule:** No record becomes public-ish until **ALL** of the following hold. Any
single failure or ambiguity → **deny**, the record stays at its current tier, and
a Paperclip issue/comment routes it to the named owner. There is no "publish on
balance" path.

| # | Condition | Mechanically checked by | Fails closed when |
|---|---|---|---|
| P1 | `verificationStatus ∈ REVIEWED_VERIFICATION_STATUSES` (`reviewed_source_linked` or `human_verified`) | validator `reviewed` flag | any other of the 6 values, or missing |
| P2 | Computed `uiStatus ∈ PUBLICATION_ELIGIBLE_UI_STATUSES` (`source-backed`, `archived-source-backed`, `corrected`) | `compute_ui_status` rules 5/10/11 | rule 12 default, or any gating state |
| P3 | Complete source trail present — every claim resolves to a `source_record` (`source_id`, `original_url`, `scan_date`, `source_class`, `source_type`, `jurisdiction`, archive/hash where available, exact pointer); **no orphan claims** | export validation: every `evidence_link.to_source_id` and statement `source_id` resolves | any unresolved/orphan pointer |
| P4 | Zero private/PII fields in the export payload (§2) | export validation: private-field deny-list returns **fail** if present | any private field present |
| P5 | AI/unverified/disputed/corrected content carries its visible label and is not asserted as fact (§5) | label audit (VerificationSafetyReviewer) | missing/contradictory label |
| P6 | Defamation/legal gate cleared — no accusation, motive claim, legal conclusion, or campaign framing about a named individual without explicit owner approval (§4) | reviewer + owner sign-off | any such content without owner record |
| P7 | Scope is **Alpine-only** | reviewer check | any non-Alpine record |
| P8 | Owner (CEO / Isaac) publication decision recorded for the surface | Paperclip owner decision artifact | no owner decision on record |

**Default-deny on ambiguity is the design, not an add-on.** P1+P2 are already
fail-closed in `compute_ui_status` (rule 12). P3 is enforced by the no-orphan-
claims validator check. P4 is enforced by the private-field deny-list (§2,
§6). P5/P6 are human gates whose *absence* of a positive record is itself a
denial. P8 — the owner decision — is never made by an agent or script (§3, §7).

This gate maps one-to-one onto the QA contract's **QG-3 Website-ready** gate
(Stage 1.10 §3); QG-3 is the test-side expression of P1–P8, and a future
implementation issue satisfies the publication gate by passing QG-3 with the
evidence named there.

---

## 2. Privacy / PII Boundary

**Rule:** Some data is **never** published and, for the most sensitive classes,
**never collected**. Reviewed website-ready content is physically and logically
separated from raw/unreviewed material.

### 2.1 Data that is NEVER published

| Class | Storage rule | Publication rule |
|---|---|---|
| Private individual PII (home address, personal phone/email, government IDs) | **Never collect** | **Never publish** |
| Voter-registration / voter-roll data | Never collect | Never publish |
| Minors' identifying information | Never collect | Never publish |
| Raw crawler output (HTML/PDF) | Local/vault only | Never publish raw |
| Source registry (JSON/DB) | Local/vault only | Never publish registry |
| Machine transcripts (`machine_extracted_unreviewed`) | Local/vault only | Website gets reviewed segments only, labelled |
| Run logs | Local only | Summary counts only in Paperclip comments |
| Reviewer-only notes / internal validation data | Local only | Never surfaced to gated-beta or public UI |
| Account-validation details (beta intake) | Backend only, access-controlled | Never published; kept separate from civic claims |

### 2.2 What MAY surface (with attribution)

| Class | Rule |
|---|---|
| Named public officials acting on-record | Allowed in source notes; allowed on website **with correct attribution** to the source pointer |
| Public meeting/document content | Allowed after the §1 publication gate |
| Reviewed, source-linked statements | Allowed at `source-backed`/`archived-source-backed`/`corrected` |

### 2.3 Redaction & separation rules

- **Redact-before-store for never-collect classes:** if a private identifier is
  encountered during ingest (e.g. a home address inside a meeting packet), it is
  redacted at the ingest boundary; the redaction is logged (count only), and the
  unredacted original is **not** committed anywhere git-tracked.
- **Raw/reviewed separation:** raw and unreviewed material lives in
  `Docs/Source-Data/` and vault paths only. Reviewed, website-ready content is a
  **separate, sanitized subset** produced only after §1. The website/API may read
  only the reviewed subset; it has no path to the raw store.
- **No private field in the export payload:** the export validator carries a
  private-field deny-list; an export containing any deny-listed field **fails**
  validation (this is the P4 enforcement, and is a *required test*, not a
  reviewer courtesy — mirrors QA contract §8 case A6).
- **Synthetic-only fixtures:** any fixture committed for tests uses fabricated
  Alpine-shaped data — placeholder names, clearly-fake addresses, invented IDs —
  never real resident PII or real voter-registry rows.

**Escalation:** any *new* source class or new person-mention class, or any case
where it is unclear whether a field is private, triggers a SecurityPrivacyAgent
consult **before** the data is stored or surfaced. When unclear: keep local,
label the blocker, route to CEO/owner. Never guess toward disclosure.

---

## 3. Access Tiers

Three tiers. Content is promoted between tiers only by the named gate; nothing
self-promotes, and demotion (revocation) is always available.

| Tier | Who/what sees it | What it may contain | Promotion INTO this tier |
|---|---|---|---|
| **T0 — Internal / reviewer** | Isaac, named agents, reviewers (authenticated, role-scoped) | Raw + reviewed + reviewer notes + AI drafts + risk flags | (origin tier — all ingest lands here) |
| **T1 — Gated beta** | Authenticated, **approved** beta accounts only | Reviewed, source-linked, publication-eligible content **only**; gated-access states; **no** reviewer notes, **no** raw, **no** registry | Passes §1 publication gate **and** owner enables the beta surface for it |
| **T2 — Public** | Anyone (post-owner-approved launch) | Strict subset of T1 the owner has explicitly approved for fully-public release | All of T1 **plus** an explicit owner public-launch decision (currently **not** granted; out of scope here) |

**Tier rules:**

- **No anonymous access to civic content.** Per the Gated Beta Access Workflow,
  the initial release is gated: every viewer of civic evidence must have an
  account. Public visitors see only minimal landing/waitlist information.
- **Account validation ≠ civic-claim verification.** Approving a beta account
  says nothing about any civic claim; failing validation says nothing about a
  person's civic standing. The two are separate subsystems (§2.1, Gated Beta
  Workflow).
- **Reviewer-only material never leaves T0.** Internal notes, raw, registry, and
  AI scratch are T0-only by construction (the T1/T2 surfaces read only the
  sanitized reviewed subset, §2.3).
- **Revocation/pause is first-class.** Beta access can be revoked, paused, or
  role-limited; access changes are logged without leaking secrets/PII.
- **The promotion T1→T2 is an owner decision**, not authorized here.

---

## 4. Defamation / Legal-Risk Gate

**Rule:** Government Watchdog presents **sourced records and clearly-labelled
interpretation**, never accusations or legal conclusions. No accusation, statement
of guilt, imputation of motive/intent, legal conclusion, or campaign messaging
about a **named individual** surfaces at any tier without an **explicit owner
approval record**. This contract defines the gate; it makes **no** judgment about
any specific real person (§12).

### 4.1 What is blocked by default

- Unsupported allegations or imputations of wrongdoing.
- Legal conclusions ("violated the law", "is liable", "committed fraud").
- Motive/intent claims about a named person not directly sourced to that person's
  own on-record statement.
- Campaign messaging, endorsement, or "vote against X" framing.
- Any of the above even when phrased as an AI summary or a question.

### 4.2 How the gate operates

- A record that contains a candidate accusation/legal-conclusion/campaign frame
  about a named individual is held at T0 and routed to **VerificationSafety
  Reviewer** (label/claim correctness) and then to **CEO → Isaac (owner)** for the
  publication decision. No agent or script may approve it.
- The safe transformation is to present the **underlying sourced fact** with its
  pointer and let the reader see the record — not to assert the conclusion. "The
  minutes record that the council voted 3–2 to …" (sourced) is publishable;
  "The mayor corruptly forced the vote" (accusation) is not.
- **No name is better than a wrong name.** A statement whose speaker attribution
  is uncertain has its speaker label gated even when the statement text and
  `verificationStatus` are otherwise fine (mirrors transcript contract 1.07 §
  speaker gating).

### 4.3 Known-then / corrected-later / actual-later separation

Disputed and corrected content is gated and **labelled by time-of-knowledge**, not
silently rewritten:

- `disputed` → `uiStatus = disputed` (rule 2), never publishable.
- A correction applies **forward from the correction date** and only reaches the
  publishable `corrected` state through the reviewed guard (rule 5: `reviewed and
  correction == "corrected"`). The original known-then context is preserved, not
  overwritten.
- "What was known then" / "what was presented then" / "what AI thought then" /
  "what was corrected later" / "what actually happened later" remain separable
  layers; the gate never collapses them into a single edited claim.

---

## 5. AI-Content Gating

**Rule:** AI output is **never primary evidence** and **nothing AI-asserted
publishes by default.** AI/unverified/disputed/corrected content is visibly
labelled and gated at every tier.

- Every AI-generated item must point back to a `source_id` + exact citation
  target or be labelled `Unverified` / `Do not publish` (AI Gateway Processing
  Workflow). An AI item with no source anchor cannot leave T0.
- AI extractions enter as `machine_extracted_unreviewed` → `uiStatus = unverified`
  (rule 7), which is **not** in the publishable set. Promotion to a reviewed
  status is a **human** action; the AI cannot promote itself.
- The frontend renders `uiStatus` verbatim and must **not** re-derive trust from
  `card.type` (including `card.type == "ai_analysis"`) or from label text. Visual
  polish must never imply verification (Backend/Frontend Evidence Workflow).
- AI analysis is presented in a **separate lane** from source-backed fact
  (concept separation; transcript & newsletter contracts). An AI summary may
  accompany a fact but is labelled as AI and carries its own status.
- **AI-label policy changes are owner-gated** (§12): this contract enforces the
  existing labels; it does not invent, weaken, or rename them.

---

## 6. Security Controls

**Rule:** Secrets, logs, repos, CI, and provenance are controlled so private data
and credentials never leak into public docs, UI, exports, or the repo.

### 6.1 Secrets handling

- GOV agents/runners use the host-local `gh` keyring; tokens are **never** copied
  into repo files, Obsidian docs, Paperclip comments, runner logs, workflow
  output, or instructions (`stage0-github-paperclip-secret-path.md`).
- If a Paperclip secret is required, it is a **GOV-owned** secret
  (`GOV_GITHUB_TOKEN`), bound via `secret_ref`; values are entered only through
  the board secrets UI/API. **WPR2 / other-company secrets are never reused in
  GOV** (`GOV-26`).
- Minimum scope only: `repo` + `workflow`. Runner-registration tokens are
  short-lived and never reused/shared (least-privilege workflow).
- On any suspected exposure → **Incident Response** (Security/Privacy Workflows):
  stop work on the affected repo, revoke at source, never print the secret value,
  open a CTO issue for history rewrite/rotation, report on the CEO issue.

### 6.2 No tracked local-only logs (ref GOV-22)

- Run logs, crawler output, raw caches, local databases, and scratch exports are
  **local/vault-only** and must **not** be git-tracked (Data Publication
  Boundary). `Logs/` and raw paths stay `.gitignore`-covered.
- **Boundary CI (GOV-22 lineage):** a CI check on both repos fails the build if a
  commit adds a tracked file under a local-only path (logs, raw source data,
  registry dumps) or matching a private-data/secret pattern. This is the
  repo-side expression of P4/§2 and the "no tracked logs" rule. *(Spec: this
  contract defines the requirement; the CI implementation is a future
  implementation issue, not authorized here.)*

### 6.3 Repo privacy

- Both repos (`Government-watchdog`, `Government-watchdog-website`) remain
  **private** until an explicit owner publication/launch decision.
- Only scripts, workflows, code, and **approved** sanitized fixtures may be
  committed. No crawler output, raw source, registry, or run logs.
- Minimum one required review before merge; CI boundary check (§6.2) is a required
  status.

### 6.4 CI / runner security (per new workflow file)

Every new `.github/workflows/` file is reviewed against the CI checklist
(Security/Privacy Workflows): no `pull_request_target` checking out PR code with
write perms; secrets only via `${{ secrets.NAME }}`; job-level least-privilege
permissions; external actions pinned to commit SHA; `write` scope intentional;
external API calls in-scope/approved; no output to a public surface unless the
§1 gate passes.

### 6.5 Provenance / audit hooks (ties to Stage 1.12 traceability)

- Every promotion between tiers and every gate decision (allow/deny, who, when,
  reason category) is **auditable**: recorded as a Paperclip artifact/comment or a
  backend audit record, without leaking secrets/PII.
- Every published claim retains its full source trail (P3). The audit chain — who
  reviewed, when, against which source version — is the input Stage 1.12
  traceability builds on. This contract reserves the hooks; Stage 1.12 specifies
  the traceability model.

---

## 7. Official-Contact Gate

**Rule:** Government Watchdog performs **no automated contact** with any official,
agency, or subscriber. Any such action is **owner-gated** and out of scope here.

- No crawler, AI step, scheduler, newsletter job, or agent may email, call, form-
  submit to, or otherwise contact a government official, agency, or public body.
- Subscriber/newsletter sending to real recipients is likewise **not** authorized
  by this contract; newsletter work stays at draft/review tiers (newsletter
  contract 1.08) until an owner decision.
- Contacting officials on sensitive/controversial topics specifically requires a
  human gate (Risk Assessment Workflow no-go rule).
- The gate is a **rule + escalation point**: a request to contact anyone →
  **stop, route to CEO → Isaac.** This contract defines the prohibition; it does
  not authorize any contact.

---

## 8. verificationStatus / uiStatus Integration

How the gates consume the 6-value vocab and the fail-closed allowlist so nothing
publishes unless explicitly allowed. This contract **consumes**, never redefines
(owned by GOV-36/37/38/39).

- **The publication gate's P1+P2 are the `reviewed` guard + the allowlist.** A
  record is publishable **iff** `compute_ui_status` returns a value in
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, which is reachable only via rules 5/10/11,
  each gated by `reviewed = verificationStatus ∈ {reviewed_source_linked,
  human_verified}`. No other branch yields a publishable state.
- **`publicExportApproved` is dependent, not independent.** A card may carry
  `publicExportApproved == true` **only** when its computed `uiStatus` is in the
  allowlist (GOV-37 Blocker 2 / GOV-39). Setting the flag on a non-eligible card
  is a validation failure, not a publish.
- **Unknown → gated.** Any unrecognized status, missing signal, or
  unclassifiable input resolves to `pending-review` (rule 12) — outside the
  allowlist — so it cannot pass §1. Absent boolean signals are `False`
  (conservative).
- **Drift fails the build, not the boundary.** The import-time assertion
  `set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES` means a new
  status added without a mapping role breaks import rather than fail-opening. The
  publication gate explicitly depends on this guard remaining green.
- **Test side:** the integration behaviour above is exercised by QA contract §4
  (vocabulary conformance, rule-by-rule mapping, fail-closed allowlist,
  reproducibility). This contract's gates and that test plan are two views of the
  same invariant.

---

## 9. Backend ↔ Frontend Handoff

What public/private/gated state the backend emits and how the frontend enforces
it. Field names align with the 1.05–1.10 contracts and the validator.

### 9.1 What the backend emits (per record)

| Field | Meaning | Gate role |
|---|---|---|
| `verificationStatus` | one of the 6 `ALLOWED_VERIFICATION_STATUSES` | drives P1, `reviewed` guard |
| `correctionStatus` | correction lifecycle (`not_applicable`, `needs_clarification`, `corrected`, …) | rules 5/6 |
| `uiStatus` | computed, one of 10 `ALLOWED_UI_STATUSES` (kebab-case) | the wire trust signal; drives P2 |
| `sourcePresent` / `archivePresent` / `rawPreserved` / `sourceChanged` | source-trail booleans | rules 3/4/10/11; absent ⇒ `False` |
| `publicExportApproved` | publish flag | true only when `uiStatus` ∈ allowlist |
| `source_id` + `evidence_link.pointer` | source trail | P3 no-orphan-claims |
| `accessTier` (T0/T1/T2 — see §3) | which tier may read this record | tier enforcement |

### 9.2 Handoff rules

- **Backend may not call a record frontend-ready** without source traceability
  (P3) and a resolved publication/access state. The API exposes only records
  whose `accessTier` matches the caller's authenticated/approved access state.
- **Frontend may not manufacture trust.** It renders `uiStatus` verbatim, shows
  source drawers, verification status, correction history, and gated-beta states
  (not-signed-in, waitlisted, pending-review, approved, denied/needs-info,
  revoked). It must **not** re-derive trust from `card.type` or label text, and
  must **not** create a public claim from a field that is AI-only, unverified,
  disputed, private, or pending review.
- **Mismatch reopens the gate.** Any disagreement between backend evidence state
  and frontend display **reopens the relevant Paperclip goal/gate** (Backend/
  Frontend Evidence Workflow handoff contract).
- **Source drawers** expose the source trail (original URL, scan date, source
  type, archive link, pointer) for every surfaced claim — the user-facing proof
  of P3.

### 9.3 UI viewport floor (for any future UI verification)

Per COMPANY.md and the Evidence Workflow: user-facing verification of these gated
states must cover **desktop 1440×900, tablet 768×1024, and mobile 390×844**.
Mobile/tablet evidence alone does not pass; a missing viewport class must be named
with its reason and next owner. *(Stated here so the future implementation issue
inherits the floor; no UI is built in this pass.)*

---

## 10. Similar-Product Research (publication / moderation gate patterns)

Per the premium framework. Each entry: how it gates publication, what GOV should
adopt, what GOV should avoid, and fit for local Alpine civic records.

### 10.1 Wikipedia / MediaWiki — pending-changes & revision/oversight model

- **Gate pattern:** edits to protected pages enter a *pending* state until a
  reviewer accepts; "oversight/suppression" hard-hides defamatory or private
  material from all but a tiny role.
- **Adopt:** the pending-by-default posture (our rule 12 → `pending-review`) and a
  hard-suppression class for private/defamatory content (our §2 never-publish +
  §4 gate).
- **Avoid:** open anonymous editing and crowd-driven acceptance — GOV reviewers
  are named and accountable, not an open crowd; we never let community volume
  promote a civic claim.
- **Alpine fit:** strong for the gate concept; the *governance* (who reviews) must
  be GOV's named reviewer lane, not open community.
- Source: https://en.wikipedia.org/wiki/Wikipedia:Pending_changes

### 10.2 DocumentCloud — primary-source publishing with controlled access

- **Gate pattern:** documents are private by default to the uploading
  org/project, with explicit per-document publish; annotations are layered over
  the source, not edits to it.
- **Adopt:** private-by-default, explicit-publish (our §1/§3), and the
  annotation-over-source separation (our raw/reviewed split, §2.3; AI lane, §5).
- **Avoid:** treating "uploaded" as "verified" — upload ≠ review; GOV requires the
  `reviewed` guard before publish.
- **Alpine fit:** very strong — Alpine records *are* primary-source documents
  (agendas, minutes, packets); private-by-default matches our gated-beta posture.
- Source: https://www.documentcloud.org/

### 10.3 Trust & Safety content-moderation pipelines (queue → human review → action)

- **Gate pattern:** automated classifiers flag/route content into review queues;
  irreversible/public actions require a human decision; appeals and corrections
  re-enter the queue.
- **Adopt:** AI proposes / human disposes (our §5, AI Gateway Workflow), default-
  deny on the high-harm classes (our §2/§4), and an auditable decision log (our
  §6.5).
- **Avoid:** auto-actioning on classifier confidence alone, and opaque decisions —
  GOV records a reason category and keeps the source trail; "AI was confident" is
  never a gate pass.
- **Alpine fit:** the *pipeline shape* fits (ingest → AI extract → human gate →
  publish); the harm taxonomy is narrowed to GOV's privacy/defamation classes.
- Source: industry pattern; cf. AI Gateway Processing Workflow lanes 1–6.

### 10.4 GovTrack / Open States — civic-data publishing with explicit sourcing

- **Gate pattern:** publish structured civic activity (bills, votes, officials)
  each tied to an official source, with corrections issued forward and clearly
  marked; analysis is labelled distinctly from the record.
- **Adopt:** every published item carries its official source pointer (our P3),
  corrections move forward (our §4.3), analysis is a labelled separate layer
  (our §5).
- **Avoid:** scaling breadth before source depth — these tools cover huge
  jurisdictions; GOV intentionally stays Alpine-deep first (tradeoff §11).
- **Alpine fit:** the *sourcing discipline* fits directly; the legislative-scale
  data model does not — Alpine is meeting/document-centric, not bill-centric.
- Sources: https://www.govtrack.us/about , https://docs.openstates.org/api-v3/

**Cross-cutting lesson:** every mature civic/moderation system converges on
*private-or-pending by default, explicit human gate for public/irreversible
action, source-or-suppress, and an audit trail.* GOV's §1–§9 already encode this;
the validator makes the "default" mechanical rather than procedural.

---

## 11. GOV Premium Success Criteria

Stage: **Stage 1.11** — Alpine security, privacy & publication gates contract
(planning/specification only).
Scope: **Town of Alpine only.** Defines gates; authorizes no publication/contact.
Project/repo: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`.
Owner role: SecurityPrivacyAgent (`72d0eccf`).
Reviewer path: CTO (`24fddc65`) technical sign-off; VerificationSafetyReviewer
(`3f95c8ce`) label/gate + no-orphan-claims correctness.
Blockers / unlock rule: builds on Stage 1.05–1.10 (done) and Stage 0.11
(GOV-21/22); unlocks only the next sequential Stage 1 planning gate (1.12
traceability). Implementation stays locked.

### Success Definition

- **Success means:** any agent or reviewer, before letting any record/summary/
  page/export/newsletter/API response reach a reviewer, a beta user, or the
  public, can read this contract and get one default-deny answer for *which gates
  must hold, who owns each, and what happens on ambiguity* — and that answer is
  consistent with the merged validator (`compute_ui_status`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, the import-time drift guard).
- **Evidence proving success:** this file (path + line count below); §1 publication
  gate maps 1:1 to QA QG-3; §8 cites the exact validator constants/rules; §2/§4/§5
  no-go classes match the Risk Assessment Workflow; two reviewer sign-off child
  issues created (CTO + VSR).

### Failure Definition

- **Failure looks like:** a gate stated as "use judgment" with no owner or no
  fail-closed default; any section that *redefines* `verificationStatus`/
  `uiStatus`/the allowlist instead of consuming it; a publish path that does not
  require the `reviewed` guard; any private-data class allowed to surface; this
  document making a privacy/defamation judgment about a real named individual; or
  authorizing publication/contact/expansion.
- **Stop/escalation trigger:** any owner-sensitive decision (publish, contact,
  accusation/legal conclusion about a named person, AI-label change, budget,
  beyond-Alpine) → **stop, route to CEO → Isaac.**

### Workability

- **Real user/operator workflow:** a specialist finishing a Stage 1
  implementation step, or a reviewer at QG-2/QG-3, checks their output against
  §1's P1–P8 before promoting a record.
- **Inputs:** a candidate record with `verificationStatus`, `correctionStatus`,
  source-trail booleans, `source_id`/pointers, proposed `accessTier`.
- **Outputs:** an allow (record may move to the named tier) or a deny (record
  stays, blocker routed to the named owner) — plus an audit entry (§6.5).
- **Missing/stale/disputed source behavior:** rule 3 → `source-missing`; rule 4 →
  `source-changed`; status `disputed` → rule 2; all non-publishable. Default-deny.
- **Resume/retry behavior:** a denied record re-enters at its current tier; fixing
  the failing condition and re-running the gate is the retry path. No silent
  promotion on retry.

### Ease of Use

- **Resident/Isaac comprehension target:** a resident sees a clear status label
  and a source drawer on every surfaced claim; an unreviewed/AI/disputed item is
  visibly marked and never looks like verified fact. Isaac, as designer, can read
  §1–§9 without code to see what will and won't ever go public.
- **Labels/statuses/gaps visible:** the 10 `uiStatus` values + source drawers +
  gated-beta states carry this; gaps/unavailable sources are labelled, never
  hidden.
- **Required screenshot/prototype/wireframe/review note:** none in this
  planning pass (spec-only, no UI built); the future UI implementation issue
  inherits the §9.3 viewport floor and must provide desktop+tablet+mobile
  evidence.

### Comparable Research

- **Comparable tools reviewed:** Wikipedia/MediaWiki pending-changes &
  oversight; DocumentCloud private-by-default publishing; Trust & Safety
  review-queue moderation; GovTrack/Open States sourced civic publishing (§10).
- **Lessons GOV should use:** private-or-pending by default; explicit human gate
  for public/irreversible action; source-or-suppress; auditable decisions.
- **Patterns GOV should avoid:** open/anonymous promotion; "uploaded/confident =
  verified"; scaling breadth before source depth.
- **Source links:** in §10.

### Tradeoffs

- **Main tradeoffs:** publication speed vs source completeness & legal safety;
  simple "just publish reviewed cards" vs strict default-deny that holds
  ambiguous records; AI throughput vs human-gate latency; private progress vs
  public-launch risk; raw-preservation richness vs the GitHub/public boundary;
  Alpine depth vs premature Wyoming/US generalization.
- **Chosen approach and reason:** **default-deny everywhere**, human gate for every
  public/irreversible step, Alpine-only. For a civic watchdog, a wrong or
  privacy-violating publication is far costlier than a delayed one; the validator
  makes the conservative path the *default* path, so safety does not depend on
  vigilance.

### Plan Before Implementation

- **Concept/data model:** consumes the existing concept map + status vocabulary;
  adds the `accessTier` (T0/T1/T2) concept and the gate/audit decision record
  (§3, §6.5). No new status vocabulary.
- **UI/operator behavior:** §9 handoff fields + gated-beta states + source
  drawers; operator runs the §1 checklist at promotion time.
- **Verification commands or review steps:** future implementation runs the export
  validator (no-orphan-claims + private-field deny-list) and QA QG-1→QG-3; the
  boundary CI (§6.2) gates the repo. *(Not run in this spec-only pass.)*
- **Artifact paths:** this contract; `scripts/validate_concept_map_export.py`;
  `tests/test_validate_concept_map_export.py`; future boundary-CI workflow.
- **Failure handling:** any gate failure → deny + route to named owner + audit
  entry; any secret/PII exposure → Incident Response.

### Source and Auditability

- **Required source fields:** `source_id`, `original_url`, `scan_date`,
  `source_class`, `source_type`, `jurisdiction`, archive/hash where available,
  exact pointer, `verificationStatus`, `correctionStatus` (P3).
- **Local source-data paths:** `Docs/Source-Data/` and vault paths; raw never
  git-tracked (§6.2).
- **Archive/Wayback/timestamp/page requirements:** archive link + pointer required
  for the `archived-source-backed` path (rule 10); exact citation target for
  every claim.
- **Verification/correction status handling:** per §8; corrections forward-dated
  (§4.3).

### Timeline and Concept Integrity

- **Known-then vs later-outcome handling:** §4.3 — layers stay separable;
  corrections apply forward; known-then context is never overwritten.
- **Correction handling:** rule 5 (`reviewed and correction == "corrected"`) is the
  only path to the publishable `corrected` state.
- **Concept records kept separate:** raw/reviewed/AI/reviewer-note separation
  (§2.3, §5); AI analysis is its own labelled lane.
- **Required typed relationships:** `source_supports`, `outcome_updates`,
  `card_presents`, document chain edges — consumed from the validator's
  `ALLOWED_EDGE_TYPES`, not redefined here.

### Acceptance Evidence

- **Required artifacts:** this contract committed on the GOV-55 branch.
- **Required tests/checks:** none executed in this spec-only pass; no code
  changed. Future implementation must pass the export validator + QA QG-1→QG-3 +
  boundary CI.
- **Required issue/PR/screenshot/API/source evidence:** file path + line count in
  the GOV-55 disposition comment; CTO sign-off child issue; VerificationSafety
  Reviewer sign-off child issue.

---

## 12. Stage Boundary — Locked Scope

**Stage 1.11 authorizes only this planning/specification document.** It does
**not** authorize:

- publishing any record, page, export, newsletter, screenshot-as-approved, or API
  surface;
- contacting any official, agency, subscriber, or government system (§7);
- writing any accusation, motive claim, legal conclusion, or campaign framing
  about a named individual (§4);
- making a privacy or defamation judgment about a specific real person;
- changing any AI-label, verification, or publication policy (§5);
- redefining `verificationStatus`, `uiStatus-map.v1`, the publication allowlist,
  or any type enum (owned by GOV-36/37/38/39);
- writing or running any crawler, transcriber, AI step, exporter, validator, CI
  workflow, scheduler, or UI;
- running any pipeline/automation/AI step against real Alpine targets;
- granting any access tier, beta approval, or public launch;
- budget/donation decisions;
- expanding beyond the Town of Alpine.

Each of these is an **owner-escalation trigger**: defining the gate is in scope;
exercising or deciding it is **not** — **stop and route to CEO → Isaac.** The
gates in this contract are rules with named escalation points, never unilateral
decisions.

The only downstream unlock is the next sequential Stage 1 planning gate
(Stage 1.12 traceability), which consumes the §6.5 provenance/audit hooks. Stage 1
implementation stays locked until its own gates pass.

## Next Action

1. Commit this contract on branch
   `GOV-55-stage-1-11-security-define-alpine-security-privacy-and-publication-gates-contract`.
2. Create the **CTO technical sign-off** child issue (mirror GOV-48/51), assigned
   to CTO `24fddc65`, review target = this file.
3. Create the **VerificationSafetyReviewer** sign-off child issue (mirror
   GOV-47), assigned to `3f95c8ce`, for label/gate + no-orphan-claims
   correctness, review target = this file.
4. Comment the disposition on GOV-55 with file path + line count and mark it
   `done` (the sign-off child issues carry the live next action; this avoids the
   GOV-49 `in_review_without_action_path` liveness incident).
