# Stage 1.10 Alpine QA & Workflow Testing Plan Contract

Issue: GOV-50
Owner role: VerificationSafetyReviewer
Stage: Stage 1.10, planning/specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only
Created: 2026-06-08

## Gate Decision

GOV-50 passes when this document gives CTO, VerificationSafetyReviewer,
SecurityPrivacyAgent, BackendCrawlerEngineer, SourceArchivist,
TranscriptEvidenceEngineer, NewsletterEditor, FrontendTimelineEngineer, and
AutomationOpsEngineer a single authoritative answer to one question: **before any
Stage 1 implementation is accepted, exactly which tests must exist, which
adversarial civic cases they must defeat, and which gates a piece of work must
pass to move draft → reviewed → website-ready.**

This pass does **not** authorize writing or running test code, running any
pipeline/crawler/AI step against real Alpine targets, producing any public
output, contacting any official or subscriber, or expanding beyond Alpine.
Stage 1 implementation stays locked (§11). The only downstream unlock is the next
sequential Stage 1 planning gate; any implementation issue created later must
explicitly consume this plan and name its own narrow Alpine step, the specific
tests it adds from §1, and its reviewer lane.

## Inputs Read (predecessor evidence — daisy chain)

- Required agent instructions: `AGENTS.md`, `COMPANY.md`, `SOUL.md`, `TOOLS.md`,
  `HEARTBEAT.md`, `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`,
  `VERIFICATION_SAFETY_WORKFLOWS.md`, `STAGE0_EXECUTION_WORKFLOW.md`,
  `RISK_ASSESSMENT_WORKFLOW.md`, `GATED_BETA_ACCESS_WORKFLOW.md`,
  `AI_GATEWAY_PROCESSING_WORKFLOW.md`, `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`.
- Staged master plan:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`
- Stage 0.10 base QA plan: **GOV-19** (backend project, goal `4b71b9ff`).
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
- Authoritative status code: `scripts/validate_concept_map_export.py` —
  `SCHEMA_VERSION = "gov-watchdog-card-map.v1"`, `ALLOWED_VERIFICATION_STATUSES`,
  `ALLOWED_UI_STATUSES`, `REVIEWED_VERIFICATION_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`,
  `_VERIFICATION_STATUS_ROLES` parity assertion,
  `ALLOWED_NODE/EDGE/CARD/LINK_TYPES`. Existing contract test:
  `tests/test_validate_concept_map_export.py`.
- Premium template:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`

### Predecessor-evidence note (read before relying on this plan)

The predecessor contracts (1.05/1.06/1.07/1.08/1.09) and the
`validate_concept_map_export.py` validator currently live on **unmerged task
branches** (`gov-17-newsletter-briefing-contract`, `GOV-40-…-transcript…`,
`GOV-43-…-newsletter…`, `GOV-46-…-automation…`), not on `main`. This plan is
spec-only and cites those artifacts by stable path + constant name. A future
implementation gate must run its tests against the **merged** versions of those
files; if a constant named here has drifted at merge time, the merge — not this
plan — is the authority, and this plan is patched to match. This plan never
redefines the status vocabulary (owned by GOV-36/37/38/39); it only defines how
that vocabulary is *tested*.

---

## 0. Status vocabulary under test (reference, not redefinition)

QA validates the vocabulary exactly as the validator defines it. Reproduced here
read-only so tests can be specified against concrete values:

**`ALLOWED_VERIFICATION_STATUSES` (6 values):** `source_recorded`,
`machine_extracted_unreviewed`, `reviewed_source_linked`, `human_verified`,
`disputed`, `do_not_publish`.

**`REVIEWED_VERIFICATION_STATUSES` (derived):** `reviewed_source_linked`,
`human_verified`.

**`ALLOWED_UI_STATUSES` (10 values):** `do-not-publish`, `disputed`,
`source-missing`, `source-changed`, `corrected`, `needs-clarification`,
`unverified`, `pending-review`, `archived-source-backed`, `source-backed`.

**`PUBLICATION_ELIGIBLE_UI_STATUSES` (3 values — the only publishable set):**
`source-backed`, `archived-source-backed`, `corrected`.

**`compute_ui_status` rules #1–#12:** first-match-wins, top-down, fail-closed
default `pending-review` (rule 12). Rules 5/10/11 (the only paths to a
publication-eligible state) are guarded by `reviewed = status ∈
REVIEWED_VERIFICATION_STATUSES`. Absent boolean signals
(`sourcePresent`/`archivePresent`/`rawPreserved`/`sourceChanged`) are treated as
`False` — the conservative direction.

The QA invariant that everything else serves: **for every input combination, a
record reaches a `PUBLICATION_ELIGIBLE_UI_STATUSES` value only through a human
review promotion to a `REVIEWED_VERIFICATION_STATUSES` value; no other path
exists, and any unknown/ambiguous input lands in a gated state.**

---

## 1. Test Taxonomy

Four categories. Every Stage 1 implementation issue must add tests in the
categories marked **required** for the contract(s) it touches, and a closeout may
not claim "tested" without naming the category and the file.

### 1.1 Categories

| Category | Question it answers | Form (Alpine, pytest unless noted) | Runs against |
|---|---|---|---|
| **Unit** | Does one function compute the right value? | `tests/test_*.py`, pure-function, no I/O | Synthetic in-memory inputs |
| **Integration** | Do two+ steps hand off correctly? | pytest with local fixture files; no network, no real targets | Local/vault fixtures only |
| **Contract** | Does an emitted payload satisfy the agreed schema + invariants? | Schema/validator assertion (e.g. `validate_concept_map_export.py`) over a fixture payload | Fixture export payloads |
| **Adversarial-civic** | Does a *hostile or degraded* civic input fail **closed**? | pytest cases built from the §2 catalog; asserts a *gated* outcome, not a happy one | Synthetic adversarial fixtures |

The adversarial-civic category is the one unique to this product and the one a
generic test suite omits. It is **required** for every contract that can emit a
card, statement, label, summary, or publication decision.

### 1.2 Per-contract conformance checks

For each Stage 1 contract, the kinds of checks that prove conformance. "✓R" =
required for any implementation issue touching that contract.

| Contract | Source-trail completeness | No-orphan-claims | Label/gate correctness | Fail-closed publication |
|---|---|---|---|---|
| **1.05 backend/tooling** (registry, raw, hash, dedup, archive) | ✓R — every registry row has all required 1.05 fields; raw artifact has `source_id`/`original_url`/`captured_at_utc`/`content_type`/`byte_size`; `sha256` matches bytes; inventory⇄registry⇄manifest agree | ✓R — a row with no resolving source is quarantined, not stored | `scope == alpine` enforced; `archive_status` recorded; dedup preserves changed content (`sourceChanged`) | n/a at this layer (no publish) |
| **1.06 frontend/product surface** | ✓R — card renders source-drawer pointer, archive link, timestamp/page | ✓R — no card without a source drawer | ✓R — `uiStatus` badge rendered verbatim, never re-derived from `card.type`; provenance chip from `produced_by`+`ai_label` | ✓R — gated `uiStatus` values render visible labels; polish never implies verification |
| **1.07 transcript/evidence/statement** | ✓R — each `statement` carries an exact-source `pointer`; `transcript_segment` has `timestamp_seconds`, `is_verbatim`, `confidence` | ✓R — statement with no resolving pointer is **hard-rejected at extraction** (1.07 §2.3) | ✓R — machine transcript labeled `machine_extracted_unreviewed`; AI paraphrase `is_verbatim == false`; `attribution_state` set only from official records | n/a (terminates non-reviewed) |
| **1.08 newsletter/briefing** | ✓R — every cited record resolves to a reviewed-eligible source | ✓R — draft cannot cite a record lacking a pointer | ✓R — AI label present; cites only reviewed-eligible records; `known_then` not mutated | ✓R — draft held at `private_review`; no claim asserted from non-reviewed records |
| **1.09 automation-vs-AI boundary** | ✓R — DET steps own provenance; AI steps carry source anchors | ✓R — AI orphan claim rejected (matrix step 8) | ✓R — AI may not write `verificationStatus`/`uiStatus`/`correctionStatus`/`publicExportApproved`/`attribution_state`/hashes/archive | ✓R — only DET step 15 sets `publicExportApproved`, only when `uiStatus ∈ PUBLICATION_ELIGIBLE_UI_STATUSES` |

### 1.3 The status engine itself (cross-cutting, ✓R always)

The `compute_ui_status` / publication-allowlist engine is tested directly and is
the spine of §4:

- **Unit:** each of rules #1–#12 has at least one case that uniquely triggers it
  (first-match-wins means later rules need inputs that pass earlier guards).
- **Contract:** `validate_concept_map_export.py` today computes `compute_ui_status`
  only to enforce the publication allowlist on any `publicExportApproved` card; it
  does **not** yet read or validate a stored card-level `uiStatus` (the concept-map
  export carries no stored `uiStatus`; that field first appears on the §7.1
  reviewed public-subset fixture). The implementation issue that introduces the
  stored-`uiStatus` public-subset export **must extend the validator** (or add a
  paired public-subset validator) to (a) reject a stored `uiStatus` outside
  `ALLOWED_UI_STATUSES` and (b) reject any card whose stored `uiStatus` disagrees
  with `compute_ui_status` over its inputs — shipped as a new contract test, not
  assumed to already exist.
- **Drift guard:** the module-load parity assertion
  `set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES` holds; a
  test deliberately imports the module to assert it does not raise, and a
  companion test documents that adding/removing a `verificationStatus` value
  without updating the map fails at import (GOV-36 CTO Blocker 6).
- **Reproducibility:** running `compute_ui_status` / export validation twice over
  identical inputs yields byte-identical results (no model judgment in the path).

---

## 2. Adversarial Civic Cases (the standard hard cases)

Every workflow that produces a card, statement, label, summary, or publication
decision **must** be tested against this catalog. Each case names the input, the
**required gated outcome** (what "pass" means is that the system refused to
over-claim), and the contract that owns the behavior. Default-deny on ambiguity
is the through-line: the correct result is almost always a *gated state plus a
routed review*, never a silent pass and never a hard crash that drops the record.

| # | Adversarial case | Input fixture | Required outcome (PASS = fails closed) | Owning contract |
|---|---|---|---|---|
| A1 | **Missing page** (live source 404, no archive, no raw) | card with `sourcePresent=false`, `archivePresent=false`, `rawPreserved=false` | `uiStatus == source-missing` (rule 3); downstream use blocked; not publishable | 1.05 / 1.09 step 6 |
| A2 | **Changed page** (source moved since review) | reviewed card with `sourceChanged=true` | `uiStatus == source-changed` (rule 4); prior review invalidated; re-review required; not publishable | 1.05 / 1.09 step 5 |
| A3 | **Duplicate source** (same bytes) | two rows, identical `sha256` | exact dupes collapse deterministically; no AI "merge"; one record retained | 1.05 / 1.09 step 5 |
| A4 | **Ambiguous speaker — no name beats wrong name** | transcript segment, no official-record identity | `attribution_state ∈ {uncertain, unattributed}`; renders generic label; candidate name reviewer-only; **never** renders a guessed name | 1.07 §3 / 1.09 step 9 |
| A5 | **Broken archive** (Wayback miss) | card, `archive_status=miss`, live source present | warning only by default; **but** if live source also gone → A1 (`source-missing`) | 1.05 / 1.09 step 6 |
| A6 | **Private data in a fixture** | record containing address/personal identifier/voter-reg field | export validation **fails**; field dropped from any public payload; record not handed off; routed to SecurityPrivacyAgent | §8 / 1.09 step 15 |
| A7 | **Unsupported claim (orphan)** | AI `statement` with no resolving `pointer` | **hard-rejected at extraction** (1.07 §2.3); never stored, never surfaced | 1.07 / 1.09 step 8 |
| A8 | **Stale / corrected output** | card with a later `corrected_later` layer | correction links **forward** with `correction_date`+source trail; `known_then` intact; `uiStatus == corrected` only if `reviewed && correctionStatus==corrected` (rule 5) | 1.07 §4 / 1.08 §4 / 1.09 step 14 |
| A9 | **Low-confidence AI output** | `machine_extracted_unreviewed`, low `confidence` | stays `unverified` (rule 7); never auto-promoted by confidence; reviewer-only | 1.09 §2 / step 11 |
| A10 | **Wrong/unknown status value** | card with `verificationStatus` outside the 6-value enum, or `uiStatus` outside the 10-value enum | a `verificationStatus` outside the 6-value enum is a validator **error** today (nodes/edges/cards/sources) and `compute_ui_status` falls to `pending-review` (rule 12); a stored `uiStatus` outside the 10-value enum becomes a validator error **only once the §1.3 stored-`uiStatus` check is added** with the public-subset export. Never fails open. | §4 / validator |
| A11 | **Conflicting sources** | two statements with `contradicts` edge | record stays gated; neither asserted as fact; routed to VSR (G3) | 1.07 / 1.09 §5 |
| A12 | **Scope leak** (non-Alpine row in an Alpine run) | registry row `scope != alpine` | rejected with explicit out-of-scope logging; not processed | 1.05 / 1.09 step 1 |
| A13 | **AI tries to write a gating field** | AI step attempts to set `verificationStatus=human_verified` / `publicExportApproved=true` | rejected; AI may only write labeled non-authoritative drafts; gating fields are DET/HUM-only | 1.09 §2.3 |
| A14 | **Hash mismatch** | artifact bytes ≠ recorded `sha256` | hard validation failure; artifact quarantined; not handed off | 1.05 / 1.09 step 4 |
| A15 | **`needs-clarification`** | card with `correctionStatus == needs_clarification` | `uiStatus == needs-clarification` (rule 6); not publishable | §4 / validator |

**Catalog completeness rule:** A1, A2, A3, A4, A5, A6, A7, A8, A9 are the nine
named in the GOV-50 acceptance criteria; A10–A15 extend them to cover the status
engine, scope, AI-field-write, integrity, and clarification paths surfaced by the
1.09 matrix. An implementation issue may add cases but may not drop a case that
applies to its layer without an explicit reviewer-approved exception.

---

## 3. Acceptance Gates (draft → reviewed → website-ready)

Three promotion gates. Each names the required evidence; on any ambiguity the
default is **deny** — the record stays at the lower state and a Paperclip
issue/comment routes it to the gate owner. No gate is satisfied by an AI
confidence score, and no gate is satisfied by "the code ran."

| Gate | Transition | Owner | Evidence required to promote |
|---|---|---|---|
| **QG-1 Draft accepted** | new work → *draft tested* | Implementing specialist | Unit + integration + contract tests for the touched contract pass locally; adversarial-civic cases from §2 that apply to the layer are present and **assert gated outcomes**; test files + pass output named in the closeout |
| **QG-2 Reviewed** | draft → *reviewed* | VerificationSafetyReviewer (evidence/labels/privacy) + CTO (technical) | Source-trail review PASSED (no orphan claims); AI-label audit PASSED; privacy boundary review PASSED; status-engine tests (§4) green; review verdict recorded as a Paperclip comment + status (mirror GOV-44/47 lane) |
| **QG-3 Website-ready** | reviewed → *publishable* | **CEO / Isaac (owner)** — never an agent or script | All of QG-2, plus: every card `uiStatus ∈ PUBLICATION_ELIGIBLE_UI_STATUSES`; export validation passes with **zero** private fields and **zero** orphan claims; Alpine-scope only; owner publication decision recorded |

**Default-deny encodings under test:**
- A record with no resolving source/pointer never reaches QG-1 (A7).
- A record at any non-reviewed `verificationStatus` cannot reach QG-3 (the
  `reviewed` guard on rules 5/10/11).
- An unknown status, missing signal, or unclassifiable input resolves to
  `pending-review` (rule 12), which is **not** in
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, so it cannot pass QG-3.

A failing check at any gate **blocks promotion** and returns the work to the
prior state with the failing case named. "Tests skipped" or "covered manually" is
not evidence and does not satisfy a gate.

---

## 4. verificationStatus / uiStatus Integration Testing

How QA proves the 6-value vocab and the fail-closed allowlist behave as the
publication safety net.

### 4.1 Vocabulary conformance
- **Enum membership:** any `verificationStatus` not in the 6-value set and any
  `uiStatus` not in the 10-value set is a validator **error** (not a warning).
- **Parity assertion:** the module-load guard
  `set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES` is asserted
  by an explicit test; a documented negative test shows that a drifted mapping
  raises at import (fail-fast, not fail-open).

### 4.2 Mapping correctness (rule-by-rule)
A table-driven test asserts `compute_ui_status(card) == expected` for at least
one card per rule #1–#12, with cases ordered so each later rule's inputs pass the
earlier guards (first-match-wins). Minimum required rows:

| Rule | Trigger (minimal) | Expected `uiStatus` |
|---|---|---|
| 1 | `verificationStatus=do_not_publish` | `do-not-publish` |
| 2 | `disputed` | `disputed` |
| 3 | no source/archive/raw | `source-missing` |
| 4 | `sourceChanged=true` | `source-changed` |
| 5 | reviewed **and** `correctionStatus=corrected` | `corrected` |
| 6 | `correctionStatus=needs_clarification` | `needs-clarification` |
| 7 | `machine_extracted_unreviewed` | `unverified` |
| 8 | `source_recorded` | `pending-review` |
| 9 | `verificationStatus=None` | `pending-review` |
| 10 | reviewed, no source, archive/raw present | `archived-source-backed` |
| 11 | reviewed, source present | `source-backed` |
| 12 | anything else / unknown | `pending-review` (fail closed) |

### 4.3 Fail-closed publication allowlist
- **Positive:** a card may carry `publicExportApproved == true` **only** when its
  computed `uiStatus ∈ {source-backed, archived-source-backed, corrected}`.
- **Negative (the safety property):** for **every** other `uiStatus` value, a card
  asserting `publicExportApproved == true` is a validator **error** and export is
  blocked. This negative set is enumerated explicitly so a newly added `uiStatus`
  is publication-gated by default until someone deliberately allowlists it.
- **Reviewed-guard property:** no non-reviewed `verificationStatus` can produce a
  publication-eligible `uiStatus` (rules 5/10/11 all require `reviewed`). A
  property/parametrized test sweeps the 4 non-reviewed statuses × signal
  combinations and asserts the result is never in the eligible set.

### 4.4 Reproducibility
Export validation and `compute_ui_status` run twice over identical inputs must
produce byte-identical output — proving no AI/non-deterministic judgment sits in
the gating path (1.09 §3).

---

## 5. Regression & Back-Gap Coverage

How corrections/outcomes and contract changes get re-tested without rewriting
known-then context. Ties forward to **Stage 1.13 back-gap analysis**.

### 5.1 Known-then immutability (golden fixtures)
- Each adversarial and mapping fixture is a **golden file**: the `known_then`
  layer is frozen. A regression test asserts that applying a later
  `corrected_later` or `actual_later` layer **adds** a forward record and leaves
  the original bytes of `known_then` unchanged (diff against the golden).
- A correction is modeled as a forward-only append with `correction_date` +
  source trail and an `outcome_updates` / `evidence_link.relation: corrects`
  edge; a test asserts no in-place edit of the corrected record occurred.

### 5.2 Contract-change regression
- When a Stage 1 contract field/enum changes (G7), the change must ship with: (a)
  updated fixtures, (b) a migration note, and (c) a regression test proving old
  golden payloads either still validate or are explicitly versioned. The validator
  `SCHEMA_VERSION` (`gov-watchdog-card-map.v1`) is the version key; a bump
  requires CTO sign-off and a paired test update on both sides of the handoff.

### 5.3 Back-gap hooks (forward to 1.13)
- This plan reserves a **back-gap suite**: as later sources fill earlier gaps,
  re-running the suite must show the gap card transitioning from `source-missing`
  → a reviewed state **only** through the normal review gate, never by silently
  overwriting the earlier "unavailable" label. 1.13 will define the gap inventory;
  1.10 defines that the transition is test-gated, forward-only, and re-review-bound.

---

## 6. QA Sign-off Workflow

Who runs/reviews QA, how a failing check blocks promotion, and how sign-off is
recorded as evidence. Mirrors the GOV-44/47 reviewer-lane pattern.

| Step | Actor | Action | Recorded evidence |
|---|---|---|---|
| 1 | Implementing specialist | Runs the §1 test categories for the touched contract; includes the §2 adversarial cases for the layer | pytest output + named test files in the issue closeout (QG-1) |
| 2 | CTO | Technical contract sign-off: schema/contract tests, determinism, handoff field names, status-engine parity | Comment verdict (APPROVE / CHANGES) + status (mirrors GOV-48) |
| 3 | VerificationSafetyReviewer | Evidence-quality, label, and privacy review: source-trail completeness, no-orphan-claims, AI-label audit, privacy boundary | Comment verdict (APPROVE / BLOCKED) + status (mirrors GOV-47) |
| 4 | SecurityPrivacyAgent | Consult on test-data privacy boundary (§8) when fixtures or new source classes are involved | Comment ruling: allow / flag / block |
| 5 | CEO / Isaac (owner) | QG-3 publication decision only | Owner decision recorded on the issue |

**Failing-check rule:** any ❌ from steps 1–4 sets the issue back to
`in_progress` for the originating agent with the exact failing case/label named;
promotion is blocked until re-verified. A reviewer who **owns** the deliverable
cannot self-review (VSR owns GOV-50, so the CTO lane is primary and the
SecurityPrivacy lane is consult). Sign-off is recorded as a first-class Paperclip
comment + status transition, never as an inert note on a closed issue.

### 6.1 Test-environment security caveats — no silent clean pass

Owner/board directive (recorded on GOV-50, 2026-06-10): when a test run or browser
automation reports an **unsupported or security-reducing command-line flag** — the
canonical example is `--no-sandbox` on a headless Chromium/Playwright run, but the
rule applies to any flag/warning that disables a sandbox, certificate check, or
isolation protection — that warning is a **testing caveat / security finding, not
harmless noise.** It means browser (or harness) stability and security protections
may be reduced for that run, so the result is **not** a clean pass on its own.

A QG-1/QG-2 closeout for any run that emitted such a flag/warning **must** record,
explicitly:

1. **Where it appeared** — the exact command, tool (e.g. Playwright/headless
   Chrome), and the verbatim flag/warning text.
2. **Whether it was required only for the local test harness** — i.e. a CI/sandbox
   accommodation — or whether it leaks into any non-test path.
3. **Whether it affects production/runtime configuration** — does any shipped
   config, container, or runtime invoke the same flag? If yes, that is a security
   finding routed to SecurityPrivacyAgent, not a test caveat.
4. **Follow-up disposition** — whether work is needed to remove or isolate the
   flag (e.g. a properly-sandboxed CI image), and who owns it; or an explicit,
   reviewer-acknowledged exception if it is unavoidable for the local harness.

A run that emitted such a flag may not be reported as a clean/green pass without
the four points above and an explicit note of the **reduced stability/security
posture**. Silence is treated as a failed closeout, the same way a skipped test is
not evidence (§3). This is most likely to surface in the §7.3 browser-automation
viewport runs; it is not limited to them.

---

## 7. Backend ↔ Frontend Test Handoff

What backend test fixtures/contracts the frontend relies on. Field names align
with the 1.06 frontend, 1.07 statement, 1.08 editorial, and 1.09 boundary
contracts.

### 7.1 The shared fixture contract
Backend publishes a **reviewed public-subset fixture** — the exact card +
source-drawer payload shape the frontend consumes — as the single source of test
truth. Per card it carries: `id`, `type`, `verificationStatus`,
`correctionStatus`, `uiStatus`, `statusLabel`, `sourceCount`, `links[]`,
`publicExportApproved`, plus the provenance fields `produced_by`
(`{automation, ai, human}`), `review_state` (`{not_reviewed, reviewed,
blocked}`), `confidence`, `ai_label` (`{none, AI-generated, AI-paraphrased,
AI-summarized}`), `layer` (`{known_then, presented_then, ai_thought_then,
corrected_later, actual_later}`), the source-drawer `pointer`, the generic-or-
approved `speaker_label`, and `is_verbatim`.

### 7.2 Handoff tests (consumer/producer)
- **Producer (backend) test:** the emitted fixture validates against
  `validate_concept_map_export.py`; no record is "frontend-ready" without a
  resolving `pointer` and an access/publication state.
- **Consumer (frontend) test:** the frontend renders the `uiStatus` badge
  **verbatim** and derives the provenance chip from `produced_by` + `ai_label`;
  a consumer test asserts the UI **never** re-derives trust from `card.type` or
  from visual styling, and that gated `uiStatus` values render a visible label.
- **Mismatch reopens the gate:** any divergence between the backend evidence
  state and the frontend display must reopen the relevant Paperclip goal/gate
  (per `BACKEND_FRONTEND_EVIDENCE_WORKFLOW`). A field rename requires a
  coordinated dual-side patch (1.09 G7); a test asserts both sides reference the
  same field name.

### 7.3 Viewport floor (UI handoff)
Per the company UI viewport floor: frontend verification of any card/source-drawer
surface must name **desktop 1440×900, tablet 768×1024, and mobile 390×844**. A
`Pass` verdict for responsive UI requires evidence at all three classes or an
explicit issue-level exception naming the missing class, the reason, and the next
owner. Browser-automation runs (headless Chromium/Playwright) that emit a
security-reducing flag such as `--no-sandbox` are subject to the §6.1
test-environment security-caveat rule: they cannot be reported as a clean pass
without recording the flag, its scope, any production impact, and the follow-up
disposition.

---

## 8. Privacy / Data Boundary in Testing

Test data must not contain real private identity/address/voter-registry data;
raw/unreviewed fixtures stay local/vault-only.

- **Synthetic only:** all committed fixtures use **fabricated** Alpine-shaped data
  (placeholder names, fake addresses clearly marked, invented identifiers). No
  real resident PII, no real voter-registry rows, no real personal contact info
  enters the repo or any public surface.
- **A6 is a required test, not just a rule:** a fixture deliberately seeded with a
  private-data field must cause export validation to **fail** and the field to be
  dropped — proving the boundary is enforced by code, not by reviewer vigilance.
- **Raw stays vault-only:** raw crawler output, machine transcripts
  (`machine_extracted_unreviewed`), AI scratch/candidate names, and run logs are
  local/vault-only (`Docs/Source-Data/`, vault paths) and are **never** committed
  as test fixtures. Only sanitized, reviewed, website-ready subsets may be
  fixtures, and only with the data-publication boundary respected
  (`WORKFLOW_GOVERNANCE` §Data publication boundary).
- **Minors / private individuals:** no fixture names a minor or a private
  individual, even synthetically-plausibly, in a way that could be mistaken for a
  real person. Public-official fixtures use clearly-fictional placeholder names.
- **Secrets:** no API keys, tokens, or reviewer-only notes in fixtures or test
  output. CI logs must not echo secret material.
- **Consult lane:** any new source class or new person-mention class introduced by
  a fixture triggers a SecurityPrivacyAgent consult (§6 step 4) before merge.

---

## 9. Similar-Product Research (QA/test-plan patterns)

Four QA/test-plan patterns for evidence/provenance or human-in-the-loop systems,
with pros/cons/tradeoffs and Alpine fit (per the premium framework, §4/§5).

### 9.1 Schema/contract validation as a CI gate — JSON Schema + pytest
- **Pattern:** define the data contract as a machine-checkable schema and fail CI
  when any payload violates it. Source: https://json-schema.org/ ,
  https://docs.pytest.org/ . GOV already does a hand-rolled version in
  `validate_concept_map_export.py` + `tests/test_validate_concept_map_export.py`.
- **Pros:** the contract is executable, not prose; drift fails fast; reviewers
  read one schema instead of re-checking every payload.
- **Cons:** schemas check *shape*, not *truth* — a well-formed card can still be a
  wrong claim; schema-only testing gives false confidence.
- **GOV use:** keep the validator as the QG-2 contract gate, but never let
  schema-pass substitute for the source-trail/label review. **Fits Alpine** — the
  card map is small and stable.
- **GOV avoid:** treating "validates" as "verified."

### 9.2 Data-quality "expectations" over a pipeline — Great Expectations style
- **Pattern:** attach declarative expectations (non-null, allowed-set, referential
  integrity) to each pipeline stage and produce a validation report.
  Source: https://greatexpectations.io/ .
- **Pros:** matches GOV's DET ingest lanes (registry → raw → hash → dedup →
  archive); each step gets explicit pass/fail expectations and a report artifact.
- **Cons:** a heavyweight dependency for an Alpine-scale pipeline; can encourage
  "expectation sprawl" that nobody reads.
- **GOV use:** **borrow the idea, not the dependency** — express the §1.2
  per-stage checks as plain pytest assertions over fixtures with a small report,
  matching the 1.05 manifest/log shapes. **Partial Alpine fit.**
- **GOV avoid:** adopting a large framework before the pipeline exists.

### 9.3 Consumer-driven contract testing — Pact style
- **Pattern:** the consumer (frontend) declares what it expects from the producer
  (backend); a broker verifies both sides agree before deploy.
  Source: https://docs.pact.io/ .
- **Pros:** directly models the §7 backend↔frontend handoff; catches a field
  rename (G7) before it breaks the UI; encodes "frontend reads `uiStatus`
  verbatim" as a verifiable expectation.
- **Cons:** broker infrastructure is overkill for one backend + one website at
  Alpine scale.
- **GOV use:** **adopt the discipline, not the tooling** — a single shared
  reviewed-public-subset fixture (§7.1) plus a producer test and a consumer test
  gives the same guarantee without a broker. **Fits Alpine.**
- **GOV avoid:** letting backend and frontend keep independent, drifting copies of
  the card shape.

### 9.4 Human-in-the-loop labeling QA — golden sets + review-queue acceptance
- **Pattern:** for systems where humans verify machine output, QA uses a
  frozen golden set, measures where the machine draft disagrees with the human
  verdict, and treats the human review queue itself as a tested workflow (no
  auto-promotion on model confidence). General HITL/annotation-QA practice.
- **Pros:** exactly GOV's AI-draft → human-verify model (1.09 steps 7/8/11);
  golden sets give regression coverage (§5) and prove the no-auto-promote
  invariant (A9, A13).
- **Cons:** golden sets need maintenance as sources change; small Alpine volume
  means few examples per case.
- **GOV use:** **core pattern** — the §2 adversarial fixtures *are* the golden
  set; the §6 sign-off workflow *is* the tested review queue. **Fits Alpine.**
- **GOV avoid:** any metric that would let "models agree" or "high confidence"
  stand in for a human verification gate.

**Synthesis / chosen approach:** combine 9.1 (keep the existing validator as the
contract gate), 9.3's *discipline* via a single shared fixture (§7), and 9.4 as
the spine (adversarial golden set + tested review queue). Reject heavyweight
frameworks (9.2 tooling, 9.3 broker) as premature at Alpine scale. **Tradeoff
chosen:** source-completeness and human-gate integrity over speed/automation —
consistent with the company non-negotiables and the fail-closed posture.

---

## 10. GOV Premium Success Criteria

Stage: Stage 1.10 (Alpine QA & workflow testing plan contract — planning/spec only)
Scope: Town of Alpine only; defines tests, does not write or run them
Project/repo: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Owner role: VerificationSafetyReviewer (this issue, GOV-50)
Reviewer path: CTO technical sign-off (primary) + SecurityPrivacyAgent privacy consult; VSR owns the issue and cannot self-review
Blockers / unlock rule: builds on 1.05–1.09 (done); unlocks only the next sequential Stage 1 planning gate; no implementation unlocked

### Success Definition
- Success means: every future Stage 1 implementation issue can read this plan and
  know exactly which test categories (§1), which adversarial civic cases (§2), and
  which acceptance gates (§3) it must satisfy before any work is accepted, with
  the status-engine and fail-closed allowlist tested as the publication safety net
  (§4).
- Evidence proving success: this committed contract document; CTO APPROVE on the
  sign-off lane; SecurityPrivacyAgent consult on §8; the plan demonstrably maps to
  the real validator constants and predecessor field names.

### Failure Definition
- Failure looks like: a QA plan that tests only happy paths; that lets schema-pass
  stand in for source/label/privacy review; that omits the no-orphan-claim or
  fail-closed-publication checks; that permits an AI step to set a gating field; or
  that allows real private data into fixtures.
- Stop/escalation trigger: any request to run tests against real Alpine targets,
  publish output, contact officials, change AI-label/verification/publication
  policy, make a privacy judgment on a specific individual, or expand beyond
  Alpine → stop and route to CEO.

### Workability
- Real user/operator workflow: an implementing specialist opens an Alpine Stage 1
  implementation issue, picks the §1 categories + §2 cases for the contract they
  touch, writes those tests, and presents pass output at QG-1; reviewers gate at
  QG-2; owner gates at QG-3.
- Inputs: a Stage 1 contract under implementation, its fixtures, the validator.
- Outputs: a passing test set, a reviewer verdict, a gated promotion decision.
- Missing/stale/disputed source behavior: §2 A1/A2/A5/A8/A11 define the required
  gated outcomes; default-deny.
- Resume/retry behavior: golden fixtures (§5) make re-runs deterministic; an
  interrupted crawl/backfill resumes against the same fixtures without rewriting
  known-then.

### Ease of Use
- Resident/Isaac comprehension target: a non-coder can read §2 and §3 and
  understand "bad civic input must end up labeled and gated, and only a human can
  promote it to public."
- Labels/statuses/gaps visible: the plan keys everything to the visible
  `uiStatus` badge and provenance chip (§7).
- Required screenshot/prototype/wireframe/review note: n/a for a spec-only plan;
  UI verification floor (desktop/tablet/mobile) is specified in §7.3 for the
  implementation issues this plan governs.

### Comparable Research
- Comparable tools reviewed: JSON Schema + pytest contract gating; Great
  Expectations data-quality expectations; Pact consumer-driven contracts; HITL
  golden-set + review-queue QA (§9).
- Lessons GOV should use: executable contracts; per-stage expectations as plain
  assertions; a single shared fixture for the handoff; adversarial golden set as
  the spine; never auto-promote on confidence.
- Patterns GOV should avoid: schema-pass as verification; heavyweight frameworks
  before the pipeline exists; brokers at Alpine scale; drifting card-shape copies.
- Source links: https://json-schema.org/ , https://docs.pytest.org/ ,
  https://greatexpectations.io/ , https://docs.pact.io/ .

### Tradeoffs
- Main tradeoffs: speed/automation vs source-completeness and human-gate
  integrity; lightweight pytest vs heavyweight frameworks; broad coverage vs
  Alpine-scale fixture maintenance.
- Chosen approach and reason: lightweight pytest + the existing validator +
  adversarial golden set; prioritize fail-closed correctness over throughput,
  matching the company non-negotiables.

### Plan Before Implementation
- Concept/data model: the `gov-watchdog-card-map.v1` card + 1.07 statement/
  transcript + 1.08 editorial + 1.09 provenance fields (§7.1).
- UI/operator behavior: verbatim `uiStatus` badge; provenance chip; gated states
  visibly labeled; viewport floor (§7.3).
- Verification commands or review steps: pytest categories §1; validator contract
  check §4; reviewer lanes §6.
- Artifact paths: tests under `tests/`; fixtures synthetic + vault-only raw (§8);
  this contract under `Docs/`.
- Failure handling: default-deny gates (§3); failing check returns work to
  `in_progress` with the case named (§6).

### Source and Auditability
- Required source fields: original public URL, scan/crawl date, source type,
  source authority level, jurisdiction, local raw path, archive/Wayback link,
  page/timestamp/section, verification status, correction status (§1.2 / 1.05).
- Local source-data paths: `Docs/Source-Data/` and vault paths; raw never
  committed (§8).
- Archive/Wayback/timestamp/page requirements: tested via A1/A5 and the
  source-drawer pointer (§7).
- Verification/correction status handling: §4 + §5; forward-only corrections.

### Timeline and Concept Integrity
- Known-then vs later-outcome handling: §5.1 golden-file immutability; corrections
  append forward (A8).
- Correction handling: forward-only with `correction_date` + source trail; no
  in-place edit (§5.1).
- Concept records kept separate: tests assert node/edge/card/link types validate
  against `ALLOWED_NODE/EDGE/CARD/LINK_TYPES`; cards are presentation nodes, not
  source of truth.
- Required typed relationships: `corrects`, `outcome_updates`, `contradicts`,
  `source supports card`, tested in §2/§5.

### Acceptance Evidence
- Required artifacts: this contract document (committed); CTO sign-off issue
  (mirror GOV-48); SecurityPrivacyAgent consult.
- Required tests/checks: none run at Stage 1.10 (spec-only); the plan itself maps
  to real validator constants verified by reading
  `scripts/validate_concept_map_export.py`.
- Required issue/PR/screenshot/API/source evidence: file path + line count in the
  GOV-50 closeout comment; child sign-off issue identifiers.

---

## 11. Stage Boundary — Locked Scope

**Stage 1.10 authorizes only this planning/specification document.** It does
**not** authorize:

- writing or running any test code, fixture generator, validator, crawler,
  transcriber, AI step, exporter, scheduler, or UI;
- running any pipeline, automation, or AI step against real Alpine targets;
- fetching, transcribing, or processing any new source material;
- producing any public output, newsletter, export, screenshot-as-approved, or API
  surface;
- contacting any official, subscriber, or government system;
- writing any accusation, motive claim, legal conclusion, or campaign wording;
- redefining `verificationStatus`, `uiStatus-map.v1`, the publication allowlist,
  or any type enum (owned by GOV-36/37/38/39);
- making a privacy/legal judgment about a specific real individual;
- changing any AI-label, verification, or publication policy;
- expanding beyond the Town of Alpine.

The only downstream unlock is the next sequential Stage 1 planning gate. Stage 1
implementation stays locked until its own gates pass. Any of the owner-escalation
triggers above → **stop and route to CEO**; none are authorized here.

## Next Action

1. Commit this contract on branch
   `GOV-50-stage-1-10-qa-define-alpine-qa-and-workflow-testing-plan-contract`.
2. Create the **CTO technical sign-off** child issue (mirror GOV-48), assigned to
   CTO `24fddc65`, review target = this file.
3. Create the **SecurityPrivacyAgent privacy consult** child issue for §8
   (test-data privacy boundary), assigned to `72d0eccf`.
4. Comment the disposition on GOV-50 with file path + line count and mark it
   `done` (the sign-off child issues carry the live next action; this avoids the
   GOV-49 `in_review_without_action_path` liveness incident).
