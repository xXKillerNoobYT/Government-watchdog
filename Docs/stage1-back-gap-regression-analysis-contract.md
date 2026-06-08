# Stage 1.13 Alpine Back-Gap and Regression-Analysis Contract

Issue: GOV-61
Owner role: CTO (`24fddc65`)
Stage: Stage 1.13, planning/specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only
Created: 2026-06-08

## Gate Decision

GOV-61 passes when this document gives CTO, VerificationSafetyReviewer,
BackendCrawlerEngineer, SourceArchivist, TranscriptEvidenceEngineer,
SecurityPrivacyAgent, and FrontendTimelineEngineer a single authoritative answer
to two questions: **(a) how does Government Watchdog detect that its Alpine
coverage is missing history — and surface that gap honestly instead of hiding it?
and (b) how does it prove a contract/pipeline/model change did not silently
break, drop, or rewrite earlier verified output — telling an *intended
correction* apart from an *unintended regression* and routing each correctly?**

The answer this contract commits to: **missing coverage is recorded as an
explicit, append-only `gap_record` and surfaced through the existing
gating `uiStatus` values (never silently hidden); a change is checked against a
captured baseline — the prior verified export plus the §1.12 audit chain — so any
previously-verified claim that disappears, downgrades, loses its source trail, or
has its `known_then` rewritten is flagged; and a deterministic decision rule
separates an intended `corrected_later` correction (which appends forward) from
an unintended regression (which is blocked at the QA gate and fixed).** This
contract **defines** that model; it does **not** build any detector, run any
diff/pipeline, backfill any record, publish anything, or expand beyond Alpine
(§12). Stage 1 implementation stays locked.

The provenance, layer, status, audit, and as-of-replay vocabularies are **owned
upstream** (GOV-36/37/38/39, Stage 1.04/1.05/1.07, and Stage 1.12). This contract
**consumes** them and adds exactly two new concepts: the **`gap_record`** (§1)
and the **regression-baseline + comparison procedure** (§2). It never redefines
`verificationStatus`, `uiStatus-map.v1`, the publication allowlist, the `pointer`
object, the `layer` enum, the `audit_event` record, or any type enum.

The only downstream unlock is the next sequential Stage 1 planning gate. Any
implementation issue created later must explicitly consume this contract, name
its own narrow Alpine step, and name the gap/regression check it satisfies plus
its reviewer lane.

## Inputs Read (predecessor evidence — daisy chain)

- Required agent instructions: `AGENTS.md`, `COMPANY.md`, `SOUL.md`, `TOOLS.md`,
  `HEARTBEAT.md`, `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`,
  `CTO_WORKFLOWS.md`, `STAGE0_EXECUTION_WORKFLOW.md`, `RISK_ASSESSMENT_WORKFLOW.md`,
  `GATED_BETA_ACCESS_WORKFLOW.md`, `AI_GATEWAY_PROCESSING_WORKFLOW.md`,
  `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`.
- Staged master plan and product non-negotiables (no orphan claims; default-honest
  coverage; no retroactive rewrite of known-then):
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`
- Strict sourcing / auditability / as-of-date protocol:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/Strict-Sourcing-Auditability-and-Testing-Rules.md`
- Stage 0.13 base back-gap/regression contract: **GOV-24** (backend project, goal
  `9baefa7d`) — Alpine 1.13 extends the Stage 0 base into the live 6-value
  vocabulary, the layered concept map, and the Stage 1.12 audit baseline.
- Stage 1.04 raw preservation / reproducibility contract (the preserved raw
  artifact + `source_version_ref` hash/scan_date that a back-gap fill resolves to).
- Stage 1.05 backend/tooling contract `Docs/stage1-backend-tooling-implementation-contract.md`
  (Card Status Vocabulary, `uiStatus-map.v1`, fail-closed publication allowlist;
  GOV-36/37/38/39).
- Stage 1.06 newsletter/briefing contract (the surfaced field names a gap/regression
  marker must align with — `gov-17-newsletter-briefing-contract`).
- Stage 1.07 transcript/evidence/statement contract
  `Docs/stage1-transcript-evidence-statement-contract.md` (the `pointer` object
  §2.1, orphan-rejection §2.3, append-only `layer` enum §4).
- Stage 1.10 QA & workflow testing plan contract
  `Docs/stage1-qa-workflow-testing-plan-contract.md` — its **§5 "Regression &
  Back-Gap Coverage"** and **§5.3 "Back-gap hooks (forward to 1.13)"** explicitly
  reserve the gap inventory and back-gap suite for *this* contract to define. QG-1/
  QG-2/QG-3 gates (§3) are the gates a regression is blocked at.
- Stage 1.11 security/privacy/publication gates contract
  `Docs/stage1-security-privacy-publication-gates-contract.md` (P1–P8 publication
  gate, T0/T1/T2 access tiers, default-deny privacy boundary §2).
- Stage 1.12 traceability & audit-trail contract
  `Docs/stage1-traceability-audit-trail-contract.md` (the `audit_event` record §2,
  the append-only hash chain §2.5, temporal `layer` binding §3, the **as-of-T
  reconstruction** procedure §5 — this contract's regression baseline is built on
  the §5 replay). GOV-58 done; GOV-59 VSR APPROVE; GOV-60 BackendCrawlerEngineer
  feasibility APPROVE.
- Authoritative status code: `scripts/validate_concept_map_export.py`
  (`SCHEMA_VERSION`, `ALLOWED_VERIFICATION_STATUSES`, `ALLOWED_UI_STATUSES`,
  `REVIEWED_VERIFICATION_STATUSES`, `PUBLICATION_ELIGIBLE_UI_STATUSES`,
  `compute_ui_status`, `ALLOWED_NODE_TYPES`, `ALLOWED_EDGE_TYPES`,
  `ALLOWED_LINK_TYPES`). Contract test: `tests/test_validate_concept_map_export.py`.
- Tracked-log removal / boundary-CI precedent: **GOV-22**.
- Premium template:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`

### Predecessor-evidence note (read before relying on this contract)

The predecessor contracts (1.04–1.12) and the `validate_concept_map_export.py`
validator currently live on **unmerged task branches**
(`gov-17-newsletter-briefing-contract`, `GOV-40-…-transcript…`, `GOV-50-…-qa…`,
`GOV-55-…-security…`, `GOV-58-…-traceability…`), not on `main`. This contract is
spec-only and cites those artifacts by stable path + constant/field name. A future
implementation gate must run its gap/regression checks against the **merged**
versions of those files; if a name cited here has drifted at merge time, the merge
— not this contract — is the authority, and this contract is patched to match. This
contract never redefines an upstream-owned vocabulary; it defines how the back-gap
and regression layer consumes them.

---

## 0. Authoritative vocabulary this contract consumes (reference, not redefinition)

Reproduced read-only so the back-gap and regression layers below can be specified
against concrete values. Upstream is the source of truth.

- **`verificationStatus` (6 values, GOV-36/37/38/39):** `source_recorded`,
  `machine_extracted_unreviewed`, `reviewed_source_linked`, `human_verified`,
  `disputed`, `do_not_publish`. **Reviewed set:** `reviewed_source_linked`,
  `human_verified`.
- **`uiStatus` (10 kebab-case wire values)**, the **3-value
  `PUBLICATION_ELIGIBLE_UI_STATUSES`** (`source-backed`, `archived-source-backed`,
  `corrected`), and the gating values used to surface gaps honestly:
  **`source-missing`** (a known/expected source is absent), **`source-changed`**
  (the live source silently changed vs the captured version), **`pending-review`**
  (fail-closed default, rule 12), **`unverified`**. Computed by
  `compute_ui_status`; fail-closed.
- **`pointer` object (Stage 1.07 §2.1):** `source_id`, `original_url`, `final_url`,
  `wayback_url`, `archive_status`, `scan_date`, `captured_at_utc`, `source_type`,
  `source_class`, `source_authority_level`, `jurisdiction`, `locator_kind` +
  matching locator field, `agenda_item_id`, `transcript_path` (private),
  `is_verbatim`, `verificationStatus`, `correctionStatus`, `confidence`,
  and `hash`/`version` of the fetched artifact when captured.
- **`layer` enum (Stage 1.07 §4, append-only):** `known_then`, `presented_then`,
  `ai_thought_then`, `corrected_later`, `actual_later`.
- **`audit_event` record (Stage 1.12 §2):** append-only, hash-linked, who/what/when;
  `event_type` ∈ {`ingest`,`extract`,`status_transition`,`review_decision`,
  `gate_decision`,`publish`,`revoke`,`correction`}; `source_version_ref`
  (`source_id` + `hash` + `scan_date`). **As-of-T reconstruction** = filter chain to
  `occurred_at_utc <= T`, replay, resolve each pointer to the `source_version_ref`
  current at T (Stage 1.12 §5).

This contract adds exactly two new concepts: the **`gap_record`** (§1) and the
**regression baseline + comparison** (§2). Everything else is consumed.

---

## 1. Back-Gap Detection (find missing history; never hide it)

**Rule:** Missing historical coverage is a **first-class, recorded fact**, not an
empty space. Every identified gap becomes one **append-only `gap_record`**; it is
surfaced to the operator (and, where appropriate, the gated UI) through an existing
gating `uiStatus` value. A gap is **never silently dropped, back-filled invisibly,
or papered over** by an unlabeled empty result.

### 1.1 What counts as a gap

| `gap_kind` | What it means for Alpine |
|---|---|
| `missing_record` | A specific record we have positive reason to expect does not exist in the store (e.g. a council meeting on the published schedule with no captured minutes/video). |
| `unsourced_period` | A date range for a body where we hold **no** source of a required class (e.g. no Town Council minutes captured for 2024-Q3 at all). |
| `known_uncaptured_event` | A civic event referenced by an existing source but whose own primary source is not yet captured (e.g. an agenda item cites an ordinance we have not fetched). |
| `partial_extraction` | A source is captured but extraction is incomplete — pages/timestamps/agenda items present in the artifact have no derived nodes. |
| `dead_source` | A previously-cited `original_url` no longer resolves **and** no `wayback_url`/preserved raw artifact exists (link rot with no archive). |

### 1.2 The `gap_record`

```json
{
  "gap_id": "alpine:gap:2024-Q3:council:minutes",
  "gap_kind": "unsourced_period",
  "scope": {
    "jurisdiction": "town_of_alpine",
    "government_body": "alpine_town_council",
    "record_class": "meeting_minutes",
    "period_start": "2024-07-01",
    "period_end": "2024-09-30"
  },
  "expected_basis": "published_meeting_schedule",
  "expectation_evidence": [
    { "source_id": "alpine:schedule:2024", "locator_kind": "section", "section": "Q3 regular meetings" }
  ],
  "status": "open",
  "priority": "high",
  "surfaced_uiStatus": "source-missing",
  "detected_at_utc": "2026-06-08T18:00:00Z",
  "detector_kind": "automation",
  "audit_event_ref": "alpine:evt:2026-06-08T18:00:00Z:000931",
  "resolution_ref": null
}
```

### 1.3 Required `gap_record` fields (deny if missing)

`gap_id`, `gap_kind`, `scope` (with `jurisdiction` = `town_of_alpine`),
`expected_basis`, `expectation_evidence` (≥1 resolvable reference — **a gap is
itself an evidenced claim: "a source should exist here"**), `status`
(∈ `open`|`filling`|`filled`|`confirmed_absent`|`wontfix`), `surfaced_uiStatus`
(∈ the gating set — `source-missing`/`coverage-incomplete` framing, **never** a
publication-eligible value), `detected_at_utc`, `detector_kind`
(∈ `automation`|`ai`|`human`), `audit_event_ref` (its creation/transition is an
`audit_event`, §2 of 1.12).

### 1.4 How gaps are detected (spec, not built)

Three complementary detector classes — the future implementation builds these; this
contract only names them and what they emit:

1. **Expectation-vs-holdings diff.** Compare an **expectation set** (what *should*
   exist: published meeting schedules, agenda series, ordinance/resolution number
   sequences, document cross-references) against the **holdings set** (what the
   source registry actually has). A member of the expectation set with no matching
   holding → `missing_record`/`unsourced_period`/`known_uncaptured_event`.
2. **Extraction-completeness scan.** For each captured source, compare derivable
   units in the raw artifact (pages, timestamps, agenda items) against derived nodes
   → `partial_extraction`.
3. **Source-liveness scan.** Re-resolve previously-cited `original_url`s; a 404/gone
   with no `wayback_url`/preserved artifact → `dead_source` (ties to §2
   `source-changed` when content differs but resolves).

### 1.5 Gaps are recorded, prioritized, and surfaced — never hidden

- **Recorded:** every detected gap is one `gap_record`; closing it (fill or confirm
  absent) is an append-only status transition, itself an `audit_event`.
- **Prioritized:** `priority` is set by civic salience (a missing regular-council
  meeting outranks a missing minor notice) and by whether the gap blocks an already-
  surfaced claim. Prioritization is operator/owner-facing, not a publish decision.
- **Surfaced:** an open gap renders through the gating `uiStatus`
  (`source-missing` / a `coverage-incomplete` marker, §4/§7) so a resident sees
  "coverage incomplete for 2024-Q3" rather than an unexplained empty timeline.
  **Default-honest:** absence is shown as absence.

---

## 2. Regression Analysis (a change cannot silently break prior verified output)

**Rule:** Before any contract/pipeline/model change is promoted, its output is
compared against a **captured baseline** of prior verified output. Any
previously-verified claim that, after the change, **disappears, downgrades in
`verificationStatus`/`uiStatus`, loses its source trail, or has its `known_then`
rewritten** is a **regression candidate** and is blocked at the QA gate (§5) until
it is either confirmed as an intended correction (§3) or fixed.

### 2.1 What the baseline is

The baseline is **not** a fuzzy memory — it is concrete, consuming Stage 1.12:

1. **A captured verified-export snapshot** — the last export that passed QG-3 (or, in
   pre-publish development, the last QG-2-reviewed export), stored as a **golden
   fixture** (Stage 1.10 §5.1). It is the set of claims, their `uiStatus`,
   `verificationStatus`, source trails, and `known_then` text as of that snapshot.
2. **The §1.12 audit chain head** at the snapshot instant. Because the chain is
   append-only and hash-linked, the baseline state of any subject is reconstructable
   by **as-of-T replay** (Stage 1.12 §5) where T = the snapshot instant. The golden
   fixture is the convenience copy; the audit chain is the authority.

A regression check therefore = *compare new output against (golden fixture ∪ as-of-T
replay of the audit chain)*. It needs **no** new history store; it reads the one
1.12 defines.

### 2.2 The comparison (spec)

For each subject present in the baseline, after the change:

| Baseline → New | Classification |
|---|---|
| present, reviewed → **absent** | regression candidate (**dropped claim**) |
| `verificationStatus` reviewed → non-reviewed | regression candidate (**downgrade**) |
| `uiStatus` publication-eligible → gating | regression candidate (**de-publish**) unless an accompanying gate/correction event explains it |
| source trail resolvable → **orphaned** (pointer no longer resolves) | regression candidate (**trail break**) |
| `known_then` text changed (not a new appended layer) | **hard regression** (history rewrite — forbidden by 1.12 §3) |
| present → present, only a **new appended `corrected_later`/`actual_later` layer** added | **not** a regression → candidate correction (§3) |
| absent in baseline → present (new claim) | not a regression (forward coverage growth) |

A regression candidate that is **not** explained by an intended-correction signal
(§3) is a **confirmed regression** and fails the gate.

### 2.3 What triggers a regression check

- Any change to a Stage 1.0x contract, the validator, the status engine, an
  extraction prompt/model, or a pipeline stage, **before** its output is promoted
  (QG-1 → QG-2, Stage 1.10 §3).
- Any re-run that re-derives previously-surfaced claims (e.g. a re-extraction after a
  parser fix). The re-derived output is diffed against the baseline for those
  subjects.

This is the implementation of the **"contract-change regression"** (Stage 1.10 §5.2)
and the **back-gap suite** (Stage 1.10 §5.3) those sections reserved for 1.13.

---

## 3. Correction vs Regression Distinction (intended change vs unintended breakage)

**Rule:** A difference from the baseline is an **intended correction** *only* when
it carries the three positive signals below; otherwise it is an **unintended
regression**. The two are routed to opposite handlers — a correction **appends
forward**, a regression is **blocked and fixed**. Ambiguity defaults to *regression*
(fail-closed): an unexplained change is treated as breakage until proven a
correction.

### 3.1 Decision rule

A baseline difference is classified **CORRECTION** if **all** hold:

1. **Forward-only shape.** The change is a **new appended layer**
   (`corrected_later`/`actual_later`) or a status transition *toward* a more-reviewed
   state — the prior `known_then`/`presented_then` node is **untouched** (Stage 1.12
   §3.1). No prior node was mutated or deleted.
2. **Audited intent.** There is a `correction` or `review_decision` `audit_event`
   (Stage 1.12 §2.3) with `actor_kind: human` whose `reason_category` names the
   correction, linked to the changed subject. The intent is recorded *before* or
   *with* the change, not reconstructed after.
3. **Reviewed authority.** The change reaches a publishable state only through the
   reviewed guard (`compute_ui_status` rule 5: `reviewed and correctionStatus ==
   "corrected"`). Automation/AI alone can never mint a correction to a published
   surface (AI Gateway Workflow: AI proposes, human disposes).

If **any** of the three is absent, the difference is classified **REGRESSION**.

### 3.2 Routing table

| Classification | Route |
|---|---|
| **CORRECTION** | Temporal-lineage update: append the `corrected_later`/`actual_later` layer + its `audit_event` (Stage 1.12 §3); frontend renders original alongside the update; baseline is *re-snapshotted* to include the correction so the next diff is clean. |
| **REGRESSION** | **Block at QG** (§5): the change cannot promote. Open a blocker issue to the change's owner with the regressed subjects. Fix = restore the dropped/ downgraded claim or its trail, or supply the missing correction signal if the change was a genuine correction recorded incorrectly. The block/deny is itself an audited `gate_decision` event. |
| **AMBIGUOUS** | Treated as REGRESSION (fail-closed) and routed to VerificationSafetyReviewer for a human classification before promotion. |

### 3.3 Worked examples

- *"2026-05-08 vote tally corrected from 4–1 to 3–2 after the clerk's minutes were
  amended."* → CORRECTION: new `corrected_later` layer + human `correction` event;
  the original `4–1` `known_then` stays visible as "what was reported then."
- *"After a parser upgrade, the 2024-09 ordinance card vanished from the export."* →
  REGRESSION (dropped claim): no correction event, prior reviewed claim absent →
  block, restore.
- *"A model change rewrote the 2025-03 meeting summary `known_then` text in place."*
  → HARD REGRESSION (history rewrite): forbidden by 1.12 §3.1 → block regardless of
  intent; a real correction must *append*, not overwrite.

---

## 4. Coverage Definition (what "complete enough" means for Alpine; default-honest)

**Rule:** "Complete" is defined **per body × record-class × period**, against the
expectation set (§1.4), and is **always reported with its gaps visible**. There is
no global "done" flag; coverage is a measured, honest ratio, and "coverage
incomplete" is shown rather than faked.

### 4.1 The coverage unit and markers

- **Coverage unit:** `(government_body, record_class, period)` — e.g.
  `(alpine_town_council, meeting_minutes, 2024-Q3)`.
- **Coverage state per unit:** `complete` (every expected record captured **and**
  extraction-complete **and** no open `dead_source`), `partial` (some captured,
  open `gap_record`s remain), `absent` (no holdings, expectation exists),
  `unknown` (no expectation set yet built — itself surfaced as "coverage not yet
  assessed", never as "complete").
- **Metric:** `coverage_ratio = captured_and_complete_units / expected_units` for a
  scope, reported **alongside** the count and list of open gaps — never as a bare
  percentage that hides which periods are missing.

### 4.2 Default-honest rules

- An empty or partial timeline **must** carry its coverage marker; a resident sees
  "coverage incomplete — 2024-Q3 council minutes not yet sourced," not silence.
- Coverage **never** rounds up: a `partial` unit is never displayed as `complete`;
  an `unknown` unit is never displayed as covered.
- Filling a gap **raises** coverage forward from the fill date; it does **not**
  retroactively assert the period was "always covered" (§6).
- Coverage markers are **operator/owner-facing and gated-beta-facing**; they are not
  a civic claim about government conduct, only about *our* data completeness.

---

## 5. Integration with Traceability (1.12) + QA (1.10)

**Rule:** Back-gap and regression analysis is not a parallel system; it **reads the
1.12 audit trail to detect drift** and **runs inside the 1.10 QA gates to block
regressions before promotion**. `verificationStatus` integrity is preserved end to
end.

### 5.1 Uses the 1.12 audit trail to detect drift

- The baseline (§2.1) **is** the as-of-T replay of the 1.12 audit chain plus its
  golden-fixture snapshot. Drift is detected by replaying the chain and diffing.
- Because the chain is hash-linked and append-only, a **silent** rewrite is already
  detectable by 1.12 §2.5 chain-walk; this contract adds the *semantic* diff
  (dropped/downgraded/orphaned/rewritten claims) on top of that integrity check.
- Every gap detection, fill, and regression block emits an `audit_event` (gap →
  `extract`/`correction`; block → `gate_decision`), so the back-gap/regression
  activity is itself fully auditable.

### 5.2 Runs inside the 1.10 QA gates to block regressions

| QA gate (Stage 1.10 §3) | What back-gap/regression adds |
|---|---|
| **QG-1 (draft accepted)** | The regression check (§2.2) runs as part of the touched-contract conformance suite; the **back-gap suite** (Stage 1.10 §5.3) asserts gap cards transition correctly. A confirmed regression **fails QG-1**. |
| **QG-2 (reviewed)** | VerificationSafetyReviewer classifies any AMBIGUOUS baseline difference (§3.2) as correction or regression before sign-off; orphan/trail-break regressions are caught by the no-orphan-claims check already required here. |
| **QG-3 (website-ready)** | Coverage markers (§4) must be present and honest; a `partial`/`absent` unit shown as `complete` **fails QG-3**. No regressed subject may be in the export. |

### 5.3 verificationStatus integrity preserved

- The **only** path into a reviewed status is a human `review_decision`/
  `gate_decision` event (Stage 1.12 §4). A regression that downgrades a reviewed
  claim does **not** silently re-promote it; restoration goes back through the gate.
- A gap fill that produces a new candidate enters at `machine_extracted_unreviewed`
  and must pass review like any other claim — **filling a gap never auto-publishes**.

---

## 6. Temporal Integrity (back-gap fills preserve the layer separation)

**Rule:** Filling a back-gap **adds** history; it never **rewrites** it. A fill must
preserve the `known_then` / `presented_then` / `ai_thought_then` / `corrected_later`
/ `actual_later` separation (Stage 1.07 §4, Stage 1.12 §3). There is **no**
retroactive rewriting of known-then context.

- A newly-sourced historical record establishes its **own** `known_then` anchored to
  *that source's* date — it is the known-then **of the period it documents**, dated
  by `scan_date`/`captured_at_utc` for *when we learned it*. The two dates are kept
  distinct: the event date (in `known_then`) and the capture date (in the
  `audit_event`/`gap_record`).
- A fill that bears on an **already-surfaced** later claim links **forward** via a
  typed edge (`outcome_updates`, or `evidence_link.relation: corrects`) — it does
  **not** edit the existing claim's `known_then`. If a fill *contradicts* a prior
  claim, that is a **correction** (§3), handled by appending `corrected_later`, never
  by overwrite.
- **As-of replay stays honest:** because a fill is dated by when it entered the
  record (its `audit_event.occurred_at_utc`), an as-of-T reconstruction for a T
  *before* the fill correctly **excludes** it — the gap that existed then is shown as
  it was. Back-filling does not let later knowledge leak backward (Stage 1.12 §5.1).
- A gap's transition `source-missing → source-backed` after a fill is an append-only
  status transition; the prior "unavailable" state remains in the audit history (the
  back-gap suite, Stage 1.10 §5.3, asserts exactly this).

---

## 7. Backend ↔ Frontend Handoff (how gaps/regressions/incomplete-coverage surface)

Field names align with the 1.06/1.07/1.12 contracts and the validator. The backend
emits **sanitized gap/coverage markers** per surface; the frontend renders them as
honest "incomplete"/"corrected" affordances without manufacturing completeness.

### 7.1 What the backend emits (T1/T2 sanitized subset)

| Field | Meaning | Boundary |
|---|---|---|
| `coverageMarker` | per-surface `{ unit, coverageState (complete/partial/absent/unknown), openGapCount, periodLabel }` | public-safe; about *our* data, not a civic claim |
| `gapNotices[]` | open gaps touching the surface: `{ gapKind, scopeLabel, surfacedUiStatus, since }` — **no** internal `expectation_evidence` paths, **no** reviewer notes | public-safe subset of the `gap_record` |
| `uiStatus` + `statusLabel` | computed wire trust signal + plain label; a missing source surfaces as `source-missing`/`source-changed` | rendered verbatim |
| `correctionHistory[]` | append-only `{layer, occurred_at_utc, reason_category, supersedes_ref}` from `audit_event`s (consumed from 1.12 §7) | public-safe; reviewer notes excluded |
| `regressionHold` (T0-only) | whether a subject is currently blocked by a regression hold; **never** leaves T0 | internal — a held subject simply does not surface at T1/T2 |

### 7.2 Handoff rules

- **Backend may not call a surface frontend-ready** while it carries a confirmed,
  unresolved regression (the held subject does not surface) or while a coverage
  marker is missing for a surface that has known gaps.
- **Frontend may not manufacture completeness.** It renders `coverageMarker` and
  `gapNotices` verbatim; it must **not** hide an `incomplete` state, present a
  `partial` unit as full, or render a back-filled record as if it had always been
  present. It must keep `known_then` and any `corrected_later` visibly separate.
- **The gap notice = the user-facing proof of honesty.** A resident sees *where*
  coverage is missing and *since when*, the same way the source drawer proves
  provenance (1.12 §7).
- **Mismatch reopens the gate.** Any disagreement between backend gap/regression
  state and frontend display **reopens the relevant Paperclip goal/gate**
  (Backend/Frontend Evidence Workflow handoff contract).

### 7.3 UI viewport floor (for any future UI verification)

Per COMPANY.md and the Evidence Workflow: future verification of coverage markers /
gap notices / correction affordances must cover **desktop 1440×900, tablet
768×1024, and mobile 390×844**. Mobile/tablet evidence alone does not pass; a missing
viewport class must be named with its reason and next owner. *(Stated so the future
implementation issue inherits the floor; no UI is built in this pass.)*

---

## 8. Privacy Boundary

**Rule:** Gap and regression analysis operates **only** over reviewed
website-ready data or local/vault-only raw data (Stage 1.11 §2/§3; Risk Assessment
Workflow). Detecting a gap or a regression must never expose private data, and the
gap/regression records obey the same **default-deny** boundary as everything else.

- **No private PII in gap/regression records.** A `gap_record` references bodies,
  record classes, periods, and sources by **label/ID**, never by embedding a private
  address, personal phone/email, government ID, voter-registry datum, or minor's
  identifier. If such data is encountered while scanning, it is redacted at the
  boundary and only a redaction *count* is logged (Stage 1.11 §2.3).
- **Expectation evidence may be local.** `expectation_evidence` and
  `regressionHold` detail can point at local/vault raw artifacts; those paths stay
  **T0-only** and are stripped from every T1/T2 surface (§7.1).
- **No tracked local-only artifacts (GOV-22 lineage).** The gap inventory store, the
  baseline golden fixtures, regression diff reports, and run logs are
  **local/vault-only** and must **not** be git-tracked; they stay `.gitignore`-
  covered and the boundary CI fails the build if a commit adds them. Only summary
  counts surface in Paperclip comments.
- **Gaps are about data, not people.** A gap/coverage marker states *our* coverage is
  incomplete; it is never phrased as an accusation, a claim that an official withheld
  records, or a legal conclusion. Any such framing is a defamation/legal-risk item
  (Stage 1.11 §4) and routes to owner.
- **Escalation:** any case where it is unclear whether a gap/regression field is
  private or whether a missing record implies wrongdoing → SecurityPrivacyAgent /
  VerificationSafetyReviewer consult **before** it is stored or surfaced. When
  unclear: keep local, label the blocker, route to CEO/owner. Never guess toward
  disclosure.

---

## 9. Similar-Product Research (coverage-gap / regression-detection patterns)

Per the premium framework. Each entry: how it detects gaps/regressions, what GOV
should adopt, what GOV should avoid, and fit for local Alpine civic records.

### 9.1 Web archives — crawl-completeness & capture-gap reporting (Internet Archive / Wayback, Archive-It)

- **Pattern:** archiving services track *what was expected to be captured vs what was
  actually captured*, report capture gaps and crawl failures, and retain time-stamped
  snapshots so a later change to a live page is visible against the archived version.
- **Adopt:** the **expectation-vs-holdings** model (our §1.4) and snapshot-vs-live
  comparison for `source-changed`/`dead_source` detection; explicit capture-gap
  reporting rather than silent omission.
- **Avoid:** treating "crawled" as "complete/verified" — a captured page is not a
  reviewed civic claim; GOV keeps capture ≠ review.
- **Alpine fit:** strong — Alpine records are a bounded, schedulable set
  (meeting series, ordinance sequences), so an expectation set is tractable.
- Sources: https://web.archive.org/ , https://archive-it.org/

### 9.2 Data-pipeline regression / data-diff testing (dbt tests, Great Expectations, data-diff)

- **Pattern:** pipelines pin a **baseline** (golden tables / prior run) and run
  row/column **diffs** plus expectation suites on every change; a value that drops,
  changes type, or violates an expectation **fails the build** before promotion.
- **Adopt:** the **baseline-diff-as-a-gate** model — our §2 regression check is
  exactly this, run inside QG-1/QG-2 (§5.2); golden fixtures as the baseline
  (Stage 1.10 §5.1).
- **Avoid:** purely statistical/threshold diffs that tolerate "small" drift — for a
  civic record, a single silently-dropped verified claim is unacceptable; GOV's gate
  is zero-tolerance for unexplained loss of a reviewed claim.
- **Alpine fit:** strong for the gate mechanic; GOV adds the
  correction-vs-regression semantic layer (§3) a generic data-diff lacks.
- Sources: https://docs.getdbt.com/docs/build/data-tests , https://greatexpectations.io/

### 9.3 Change monitoring / silent-edit detection (page-change monitors, fact-trace)

- **Pattern:** monitors re-fetch a source on a cadence and alert when content changes,
  filtering noise (formatting/timestamps) from substantive change; fact-tracking
  tools flag when a previously-cited basis no longer supports a claim.
- **Adopt:** scheduled **source-liveness/`source-changed`** scanning (our §1.4.3) and
  noise-filtered substantive-change detection so a silent edit to an Alpine source is
  caught and audited rather than missed.
- **Avoid:** alert floods on cosmetic change — GOV must distinguish a substantive
  content change (→ `source-changed`, possible correction) from formatting noise, or
  reviewers drown.
- **Alpine fit:** good — a small Alpine source set is monitorable cheaply; the audit
  chain (1.12) gives the before/after to diff against.

### 9.4 Software regression suites / golden-master (snapshot) testing

- **Pattern:** a saved "golden" output is the contract; any code change that alters it
  fails until a human **explicitly re-blesses** the new golden — making *intended*
  changes deliberate and *unintended* changes loud.
- **Adopt:** the **explicit re-bless** step — our §3 re-snapshot of the baseline after
  a confirmed correction is a human-authorized re-bless; an un-blessed diff stays a
  regression (fail-closed).
- **Avoid:** auto-updating the golden on diff (the classic snapshot-test footgun) —
  that would let a regression silently become the new baseline; GOV re-snapshots
  **only** after the §3 correction signals are satisfied.
- **Alpine fit:** direct — golden fixtures already exist in the Stage 1.10 plan; this
  contract gives them civic semantics.

**Cross-cutting lesson:** durable gap/regression systems converge on *an explicit
expectation/baseline, a diff that fails closed on unexplained loss, a human-
authorized re-bless for intended change, and honest reporting of what is missing.*
GOV's §1–§6 encode this at Alpine scale, reusing the 1.12 audit chain as the
baseline and the 1.10 gates as the enforcement point so honesty is mechanical, not
procedural.

---

## 10. GOV Premium Success Criteria

Stage: **Stage 1.13** — Alpine back-gap & regression-analysis contract
(planning/specification only).
Scope: **Town of Alpine only.** Defines the contract; builds/runs/backfills/
publishes nothing.
Project/repo: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`.
Owner role: CTO (`24fddc65`).
Reviewer path: VerificationSafetyReviewer (`3f95c8ce`) — correctness / no-orphan /
correction-vs-regression handling; BackendCrawlerEngineer (`f26f530c`) — backend
feasibility of detection + baseline.
Blockers / unlock rule: builds on Stage 1.04–1.12 (done/approved) and Stage 0.13
(GOV-24); consumes the Stage 1.10 §5 back-gap hooks and the Stage 1.12 audit
baseline; unlocks only the next sequential Stage 1 planning gate. Implementation
stays locked.

### Success Definition

- **Success means:** an implementer or reviewer can take one Alpine coverage area
  and, using only this contract, know exactly (a) how a missing record is detected,
  recorded as a `gap_record`, prioritized, and surfaced honestly (§1, §4), (b) what
  baseline a change is diffed against and which differences are regressions (§2),
  (c) the deterministic rule that separates an intended correction from an
  unintended regression and where each is routed (§3), (d) how this runs inside the
  1.10 QA gates and reads the 1.12 audit trail without redefining either (§5), (e)
  how a back-gap fill preserves the temporal layers with no rewrite (§6), and (f)
  what the backend emits and the frontend renders for gaps/coverage (§7) — and every
  vocabulary used is consumed from upstream, not invented here.
- **Evidence proving success:** this file (path + line count below); §2/§3 give
  concrete diff classifications + a fail-closed decision rule; §5 maps each step to
  QG-1/QG-2/QG-3 and the 1.12 audit chain; §6 ties to 1.12 §3/§5 (no rewrite, honest
  as-of replay); §8 matches the Risk Assessment + GOV-22 boundary; two reviewer
  sign-off child issues created (VSR + BackendCrawlerEngineer).

### Failure Definition

- **Failure looks like:** a gap that is silently dropped instead of recorded/
  surfaced; a coverage marker that shows `partial`/`absent` as `complete`; a baseline
  difference auto-accepted as the new golden without the §3 correction signals; a
  back-gap fill that rewrites `known_then` or lets later knowledge leak into an
  as-of-T replay; a regression that downgrades a reviewed claim and silently
  re-publishes; a private field or local path in a surfaced gap notice or a tracked
  regression log; this contract *redefining* `verificationStatus`/`uiStatus`/the
  `pointer`/the `layer`/the `audit_event`; or authorizing building a detector,
  running a diff/pipeline, backfilling, publishing, or expanding beyond Alpine.
- **Stop/escalation trigger:** any owner-sensitive decision (build detector/backfill
  infra, run a pipeline against real targets, publish, official contact, a
  privacy/defamation judgment on a named individual — including any framing that a
  missing record implies wrongdoing — AI-label change, budget, beyond-Alpine) →
  **stop, route to CEO → Isaac.**

### Workability

- **Real user/operator workflow:** a specialist finishing a Stage 1 implementation
  step runs the regression check against the baseline before promoting at QG-1; a
  reviewer at QG-2 classifies any ambiguous difference; an operator reviews the open
  gap inventory and prioritizes the next fill; a resident sees an honest coverage
  marker.
- **Inputs:** the new output of a change; the captured baseline (golden fixture +
  as-of-T audit replay); the expectation set vs holdings for a scope.
- **Outputs:** a `gap_record` per detected gap; a regression classification per
  baseline difference; a coverage marker per surface; an `audit_event` for each.
- **Missing/stale/disputed source behavior:** missing → `gap_record` +
  `source-missing`; silently changed live source → `source-changed` + possible
  correction; disputed → stays gated; none publish.
- **Resume/retry behavior:** detection and diff are idempotent re-reads of the
  registry + audit chain (Stage 1.12 §5.3); an interrupted gap-fill resumes from the
  first open `gap_record` with no terminal event; the baseline is re-snapshotted only
  after a human-authorized §3 correction.

### Ease of Use

- **Resident/Isaac comprehension target:** a resident opening an incomplete timeline
  sees "coverage incomplete — 2024-Q3 council minutes not yet sourced" and, on a
  corrected claim, the original beside the correction. Isaac, as designer, can read
  §1–§7 without code to see how gaps are shown honestly and how a change is prevented
  from silently breaking prior coverage.
- **Labels/statuses/gaps visible:** the gating `uiStatus` values + `coverageMarker` +
  `gapNotices` + `correctionHistory` carry this; gaps/unavailable sources are
  labelled, never hidden.
- **Required screenshot/prototype/wireframe/review note:** none in this planning pass
  (spec-only, no UI built); the future UI implementation issue inherits the §7.3
  viewport floor and must provide desktop+tablet+mobile evidence.

### Comparable Research

- **Comparable tools reviewed:** web archives' capture-gap reporting (Wayback/
  Archive-It); data-pipeline regression/diff (dbt tests, Great Expectations,
  data-diff); change monitoring / silent-edit detection; software golden-master /
  snapshot testing (§9).
- **Lessons GOV should use:** explicit expectation/baseline; diff that fails closed on
  unexplained loss; human-authorized re-bless for intended change; honest reporting
  of what is missing.
- **Patterns GOV should avoid:** treating capture as verification; tolerating "small"
  drift on civic claims; cosmetic-change alert floods; auto-updating the golden on
  diff.
- **Source links:** in §9.

### Tradeoffs

- **Main tradeoffs:** coverage completeness vs speed-to-surface; zero-tolerance
  regression gating vs developer friction; eager gap detection vs alert/noise volume;
  rich local gap inventory vs the GitHub/public boundary; Alpine depth vs premature
  Wyoming/US generalization of the expectation set.
- **Chosen approach and reason:** **explicit `gap_record` inventory + baseline-diff
  regression check run inside the existing QA gates, reusing the 1.12 audit chain as
  the baseline**, all consuming upstream vocabulary. It makes missing coverage and
  silent breakage *mechanical to catch* without a new history store, keeps the
  records private-clean by referencing IDs/labels, fails closed on ambiguity, and
  stays Alpine-scoped. For a civic watchdog, honestly showing what is missing and
  never silently rewriting the past is the core trust asset.

### Plan Before Implementation

- **Concept/data model:** consumes the concept map + status/`pointer`/`layer`/
  `audit_event` vocab; adds the `gap_record` (§1) and the regression baseline +
  comparison procedure (§2). No new status vocabulary.
- **UI/operator behavior:** §7 handoff fields (coverage marker, gap notices,
  correction history); operator reviews the gap inventory and classifies diffs at the
  QA gates.
- **Verification commands or review steps:** future implementation runs the export
  validator (no-orphan-claims), the §2 regression diff against the golden fixture,
  the back-gap suite (Stage 1.10 §5.3), and QG-1→QG-3. *(Not run in this spec-only
  pass — no code changed.)*
- **Artifact paths:** this contract; `scripts/validate_concept_map_export.py`;
  `tests/test_validate_concept_map_export.py`; future gap-inventory store + baseline
  fixtures + regression-diff checker (implementation issue, not authorized here;
  local/vault-only per §8).
- **Failure handling:** any unexplained dropped/downgraded/orphaned/rewritten claim →
  block at QG + blocker issue to the change owner + (the deny is itself an audit
  event); any secret/PII exposure → Incident Response.

### Source and Auditability

- **Required source fields:** the §0 `pointer` set for any claim a gap/regression
  touches; for a `gap_record`, the §1.3 required fields including
  `expectation_evidence` (≥1 resolvable reference).
- **Local source-data paths:** `Docs/Source-Data/` and vault paths; gap inventory,
  baseline fixtures, and diff reports never git-tracked (§8, GOV-22).
- **Archive/Wayback/timestamp/page requirements:** `wayback_url`/`archive_status` for
  `dead_source` distinction; `source_version_ref` hash+date for the as-of-T baseline
  replay (Stage 1.12 §5).
- **Verification/correction status handling:** per §3 (correction-vs-regression) and
  §5 (gate integration); reviewed guard preserved.

### Timeline and Concept Integrity

- **Known-then vs later-outcome handling:** §6 — a fill establishes its own dated
  `known_then`, links forward via `outcome_updates`/`corrects`, never edits a prior
  node; as-of-T replay excludes later fills.
- **Correction handling:** §3 — `corrected_later` layer + human `correction` event +
  reviewed guard; otherwise the difference is a regression.
- **Concept records kept separate:** `gap_record` references subjects/sources by ID;
  raw / reviewed / AI / reviewer-note separation preserved (§8); gaps are about data
  completeness, never a civic accusation.
- **Required typed relationships:** `outcome_updates`, `evidence_link.relation:
  corrects`, `source_supports`, `correction_notice` — consumed from the validator's
  `ALLOWED_EDGE_TYPES`/`ALLOWED_LINK_TYPES`, not redefined here.

### Acceptance Evidence

- **Required artifacts:** this contract committed on the GOV-61 branch.
- **Required tests/checks:** none executed in this spec-only pass; no code changed.
  Future implementation must pass the export validator (no-orphan-claims), the §2
  regression diff, the back-gap suite, and QG-1→QG-3.
- **Required issue/PR/screenshot/API/source evidence:** file path + line count in the
  GOV-61 disposition comment; VerificationSafetyReviewer sign-off child issue;
  BackendCrawlerEngineer feasibility sign-off child issue.

---

## 11. Back-gap / regression workflow hooks (governance)

Per `WORKFLOW_GOVERNANCE.md`, the automation/log aspects of the future
implementation must define: command/run trigger for the gap-detection scan and the
regression diff (e.g. pre-promotion at QG-1, scheduled liveness scan); input/output
contract (registry + audit chain + new output in, `gap_record`s + regression report
out); **log location** (local/vault-only, never git-tracked, GOV-22); normal success
output (no unexplained regression, gap inventory current); failure examples (dropped/
downgraded/orphaned/rewritten claim; `known_then` overwrite; coverage marker
mislabeled); retry policy (detection + diff are idempotent re-reads); issue-creation
threshold (any confirmed regression or new high-priority gap → blocker issue to the
named owner); review cadence (gap inventory reviewed each QA cycle); and owner
responsible for checking logs. *(This section reserves the workflow requirements;
the future implementation issue patches the relevant `*_WORKFLOWS.md` when the
checker is built — not in this pass.)*

---

## 12. Coverage Summary (acceptance-criteria map)

| GOV-61 required section | Where in this contract |
|---|---|
| 1. Back-gap detection (identify/record/prioritize/surface, never hidden) | §1, §4 |
| 2. Regression analysis (baseline + check vs prior verified output) | §2, §5.1 |
| 3. Correction vs regression distinction (decision rules + routing) | §3 |
| 4. Coverage definition ("complete enough"; default-honest) | §4 |
| 5. Integration with traceability (1.12) + QA (1.10) | §5 |
| 6. Temporal integrity (fills preserve layers; no retroactive rewrite) | §6 |
| 7. Backend↔frontend handoff (markers, source drawers, field alignment) | §7 |
| 8. Privacy boundary | §8 |
| 9. Similar-product research (2–4 examples, pros/cons/tradeoffs) | §9 |
| 10. Premium success-criteria template (completed) | §10 |
| 11. Stage boundary (locked scope) | §13 |

---

## 13. Stage Boundary — Locked Scope

**Stage 1.13 authorizes only this planning/specification document.** It does
**not** authorize:

- building any gap detector, gap-inventory store, baseline-snapshot tool,
  regression-diff checker, liveness scanner, or back-fill pipeline;
- running any crawler, transcriber, AI step, exporter, validator, scheduler, diff, or
  pipeline against real Alpine targets;
- backfilling, re-deriving, or modifying any record;
- publishing any record, page, export, newsletter, screenshot-as-approved, or API
  surface, including any coverage marker or gap notice on a public surface;
- contacting any official, agency, subscriber, or government system, or requesting
  missing records from any office;
- making a privacy or defamation judgment about a specific real person, or any
  framing that a missing record implies wrongdoing by a named individual or office;
- changing any AI-label, verification, or publication policy;
- redefining `verificationStatus`, `uiStatus-map.v1`, the publication allowlist, the
  `pointer` object, the `layer` enum, the `audit_event` record, or any type enum
  (owned upstream by GOV-36/37/38/39, Stage 1.04/1.05/1.07, Stage 1.12);
- granting any access tier, beta approval, or public launch;
- budget/donation decisions;
- expanding beyond the Town of Alpine (including generalizing the expectation set to
  Star Valley / Lincoln County / Wyoming).

Each of these is an **owner-escalation trigger**: defining the back-gap and
regression-analysis contract is in scope; building or exercising it is **not** —
**stop and route to CEO → Isaac.**

The only downstream unlock is the next sequential Stage 1 planning gate. Stage 1
implementation stays locked until its own gates pass.

## Next Action

1. Commit this contract on branch
   `GOV-61-stage-1-13-cto-define-alpine-back-gap-and-regression-analysis-contract`.
2. Create the **VerificationSafetyReviewer** sign-off child issue (mirror GOV-59),
   assigned to `3f95c8ce`, for correctness / no-orphan / correction-vs-regression
   handling; review target = this file.
3. Create the **BackendCrawlerEngineer** feasibility sign-off child issue (mirror
   GOV-60), assigned to `f26f530c`, for backend feasibility of gap detection +
   baseline diff; review target = this file.
4. Comment the disposition on GOV-61 with file path + line count and mark it `done`
   (the sign-off child issues carry the live next action; this avoids the GOV-49
   `in_review_without_action_path` liveness incident).
