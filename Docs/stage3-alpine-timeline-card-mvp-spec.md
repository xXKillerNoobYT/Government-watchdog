# Stage 3 — Alpine Timeline + Card-Model MVP Spec Package

> **Issue:** GOV-336 (Stage 3.01 · Plan/Spec · CTO→BackendCrawlerEngineer). **Parent:** GOV-335.
> **Stage:** 3.01 — planning/spec only. **NON-implementation. NON-unlock.**
> **Scope:** Town of Alpine only · reviewer-internal · no public launch.
> **Grounded on:** canonical remote `origin/main` HEAD `9d2bb13` (GOV-332 #59).
> **Goal:** Stage 3.01 `a0a0a97f-f297-4062-9cd2-6c8d1322c91f`.
> **Data-contract field source:** `Docs/stage2-reviewer-internal-read-surface-reference.md` (GOV-326).

This document is the **first executable Stage 3 artifact**. It reconciles the master Stage 3 scope
(general Alpine timeline + card model) into an ordered, owner-assigned issue map; pins the card model
to the already-audited Stage 2 reviewer-internal read surface as its sole data contract; records the
Stage 2 backward-link carry-forward; reconciles every Stage 3.x Paperclip goal against this map; and
names the premium success-criteria gate as a hard precondition for any Stage 3 **implementation** child.

It authorizes **no** production code, **no** implementation child, and **no** scope/launch/budget unlock.
See §7 for the explicit non-unlock statement.

---

## 0. What "Stage 3" is, in one paragraph

Master-plan Stage 3 is the **Alpine timeline + card-model MVP**: a private (reviewer-internal) route that
opens directly to an Alpine timeline with the jurisdiction filter pre-set to **Wyoming → Lincoln County →
Town of Alpine** (Alpine default view; broader scope shown as *planned*, not live). The timeline renders
typed cards — **meeting / info / decision / statement / source(-document) / correction / dispute /
AI-presented** — each with an emoji + hover explanation, a **source drawer**, and visible **gated blocks**
for AI/unverified/disputed/corrected content. Every meaningful claim carries a source trail; no orphan
claims. Stage 3's value is *presentation of already-reviewed, already-safe records* — it adds a view layer,
it does not add a new data-exposure layer.

---

## 1. Executable Stage 3 issue map (3.02 – 3.15)

This is the ordered build map later issues execute against. **Classification** is the primary posture of
the subgoal's *first* executable child; subgoals marked `plan→impl` produce a planning/spec child first,
then an implementation child gated per §5. Projects: **backend** =
`xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`; **website** =
`xXKillerNoobYT/Government-watchdog-website` / `78066972-3f3b-4075-9c1e-2d6817001099`.

| # | Subgoal (goal id) | Owner role | Project | Class | One-line deliverable |
|---|---|---|---|---|---|
| 3.01 | Spec package + root-plan reconciliation (`a0a0a97f`) | BackendCrawlerEngineer | backend | **planning** (this doc) | This spec package; reconciled issue map + card↔read_api data contract. |
| 3.02 | Acceptance criteria + exit gate (`442f5521`) | VerificationSafetyReviewer | backend | **planning** | Testable Stage 3 exit-gate checklist (≥5 sourced Alpine cards, drawer opens, gated blocks distinct, no orphan claim, viewport floor). |
| 3.03 | Source/data inventory contract (`a380016c`) | BackendCrawlerEngineer | backend | plan→impl | Which Alpine records/gaps feed the MVP timeline; counts + provenance per card type; reuses Stage 2 inventory. |
| 3.04 | Raw preservation + reproducibility (`412540e0`) | BackendCrawlerEngineer | backend | plan→impl | Confirm card-backing records are replayable from preserved raw (reuses GOV-262 preservation-replay); no raw crosses to view. |
| 3.05 | Backend/tooling implementation contract (`ff16da3a`) | BackendCrawlerEngineer | backend | plan→impl | **Card read contract / feed**: a web-safe, stable-id card projection built *on top of* `read_api` (see §2); the timeline's data source. |
| 3.06 | Frontend/product surface contract (`eac4a8db`) | FrontendTimelineEngineer | website | plan→impl | Alpine timeline shell + typed card components + source drawer + gated blocks; consumes 3.05 only. |
| 3.07 | Transcript/evidence/statement model (`3069bd05`) | BackendCrawlerEngineer | backend | plan→impl | Statement-card content contract; **correction/dispute relationship edges** (the one read-surface gap, §2.4). |
| 3.08 | Newsletter/briefing/editorial behavior (`8723a680`) | Editorial/Newsletter owner (CEO-assigned) | backend | planning · **deferred** | Not required for reviewer-internal timeline MVP; defer to a later stage gate (no public send in Stage 3). |
| 3.09 | Automation vs AI boundary matrix (`7c2a9784`) | CTO / BackendCrawlerEngineer | backend | planning | Confirm Stage 3 adds no new AI write-path; cards render `produced_by='ai'` provenance via existing `provenance_status` only. |
| 3.10 | QA + workflow testing plan (`aa2e2302`) | VerificationSafetyReviewer | backend+website | planning | Happy-path + adversarial card QA (missing source, ambiguous name, AI/unverified gate, source_changed); 3-viewport floor. |
| 3.11 | Security/privacy/publication gates (`c26fc575`) | SecurityPrivacyAgent | backend | planning | Confirm the card layer never crosses `to_web_safe`/`publication.py`; lane-gating of reviewer-internal keys (§2.3). |
| 3.12 | Traceability + audit trail (`d5aa250e`) | BackendCrawlerEngineer | backend | plan→impl | Card↔source traceability reuses the three Stage 2 auditors (GOV-306/318/322); source drawer = `evidence` key. |
| 3.13 | Back-gap / regression analysis (`8918271b`) | BackendCrawlerEngineer | backend | plan→impl | Regression guard that the card feed never silently drops a record/gap the read surface emits. |
| 3.14 | Documentation maintenance + Obsidian sync (`798be608`) | BackendCrawlerEngineer / CTO | backend | planning | Keep this spec + ref docs in sync with merged code; extend the GOV-326 doc-drift guard pattern to the card contract. |
| 3.15 | Agent handoff + owner escalation (`c4edbe3f`) | AutomationOps / CTO | backend | planning | Stage 3 routing/anti-loop reuses the GOV-332 escalation guard; names the human-owner escalation points below. |

**Critical path for a visible Alpine timeline MVP:** 3.02 (exit gate) → 3.05 (backend card feed) →
3.06 (frontend timeline + cards + drawer), with 3.03/3.04/3.07/3.12 supplying/validating the records the
feed serves. 3.08 is deferred. 3.09/3.10/3.11/3.13/3.14/3.15 are governance/QA wrappers that gate the impl
children but do not block the planning sequence.

---

## 2. Data contract — the card model builds **only** on the Stage 2 reviewer-internal read surface

The card model's sole data source is the merged, both-legs-audited Stage 2 read surface: the 5-overlay
fail-closed `read_api` projection. **Field names and semantics below are sourced from
`Docs/stage2-reviewer-internal-read-surface-reference.md` (GOV-326)** — that doc is the field source of
record; this section maps Stage 3 card fields onto its keys. No card field may originate anywhere else.

### 2.1 Non-negotiable boundary rule (cite §1–§2 of the GOV-326 reference)

> **A Stage 3 card may surface only fields that appear in `read_api` output. The card/presentation layer
> consumes `read_api` exclusively and never queries raw tables, never calls `to_web_safe` /
> `publication.py`, and never re-derives a field the read surface chose to drop.**

This makes the public-projection boundary impossible to cross at the card layer *by construction*: the read
surface already crosses the two independent web-safe layers (field allowlist `publication.to_web_safe` +
the `assert_no_raw_paths` transport sweep). Internal ids, raw/vault paths, `.sha256` files, and `file://`
URIs cannot reach a card because they never reach `read_api` output. If a card ever shows one, it is a
leak to report — not an expected state.

### 2.2 Card field → read_api projection key mapping

The master-plan card fields (id, type, title, reviewed summary, date, jurisdiction, topic tags, status,
verification metadata, source links, relationship ids) map as follows. Every overlay below is documented in
the GOV-326 reference (§3 for the per-statement overlays, §4 for gap cards).

| Card field | read_api source key | Lane | Notes / fail-closed |
|---|---|---|---|
| `jurisdiction` (state/county/town) | envelope `{"scope":"alpine"}` | both | Fixed Alpine; broader filter values are *planned*, never live. |
| `date` | allowlisted base record field | both | Allowlist-only; no derivation. |
| `title` | allowlisted base record field | both | Reviewer-internal free-text (statement text) is **not** a public title — see `reviewed summary`. |
| `reviewed summary` (statement body) | `_serialize_statement` statement text | **reviewer-internal only** | Statement free-text stays reviewer-internal (Stage 2.06 contract); summary cards render behind the gate. |
| `status` (verified/unverified/ai_presented/disputed/corrected/source_changed/source_missing) | **composed** from `ui_status` + `provenance_status` + `confidence_label` (+ gap lane for `source_missing`) | mixed | Master-plan status vocab must be **derived** from these keys, never invented; mapping table owned by 3.05/3.06. |
| `verification metadata` | `confidence_label`, `provenance_status`, `ui_status` | mixed | See per-key rows below. |
| `confidence_label` | `confidence_label` (envelope key, GOV-283/290) | both | Frozen set {`source_anchored_timed`,`auto_caption_timed`,`auto_caption_untimed`,`minutes_summary`,`derived_summary`}; floor `auto_caption_untimed`. |
| `speaker_label` (statement cards) | `speaker_label` (envelope key, GOV-290) | both | "Name, Role" only if `attributed` ∧ `on-record-official`; else `Community Member` / `Meeting Attendee` (floor). Poisoned names never read. |
| `provenance_status` (trust badge) | `provenance_status` (envelope key, GOV-311) | **reviewer-internal only** | Frozen {`grounded`,`unverified`}; floor `unverified`. Powers the AI/unverified gated block + trust badge. |
| `ui_status` (render/review state) | `ui_status` (re-derived allowlist key) | both | Re-derived via `publication.compute_ui_status`; never trusted from storage. |
| source links / **source drawer** | `evidence` (envelope key; web-safe drawer) | both | Public `http(s)://` source/archive URLs only; `file://`/local refs/internal ids dropped. Orphan records are never served, so a drawer is never empty-with-no-edge. |
| `source_missing` cards / completeness | `completeness_gaps` → `completeness_gap_cards` (GOV-298, opt-in) | gap lane | Fields {`gap_id`,`subject_id`,`subject_node_type`,`gap_type`,`severity`,`resolved_status`,`detail?`}; `source_id`/`detected_run_id`/`detected_utc` never SELECTed. |
| `id` (card identity) | **gap — no raw id is surfaced** | n/a | `read_api` strips internal ids by policy. 3.05 must define a **stable web-safe card handle** (opaque/derived), not a raw DB id. |
| `relationship ids` (known-then / later-outcome / correction / dispute) | **gap — not yet emitted** | n/a | See §2.4. Correction/dispute edges are a Stage 3 backend contract item (3.07), not a public crossing. |

### 2.3 Keys that are reviewer-internal lane-gated (must never render in a public lane)

Per the GOV-326 drift contract, exactly these are **reviewer-internal only** and a card must gate them
behind `access === 'reviewer_internal'`:

- **`provenance_status`** (`grounded`/`unverified`) — attached only under `include_provenance_status=True`
  from the `reviewer_internal_records` call site; the public lane is byte-identical to its pre-2.12 shape.
- **statement free-text / `reviewed summary`** — reviewer-internal per the Stage 2.06 contract.
- the **`reviewer_internal_records` lane itself** — serves only `not_publishable` rows; never duplicated
  into the public `records` lane.

Public-lane-safe overlays (`ui_status`, `confidence_label`, `speaker_label`, `evidence`) are *also*
rendered behind the beta gate in Stage 3, because the entire MVP runs at `access: reviewer_internal`. No
Stage 3 card renders on a public surface in this stage.

### 2.4 The one genuine read-surface gap (record, do not cross)

The master-plan card set includes **correction**, **dispute**, and **AI-presented** card types and
known-then/later-outcome/correction **relationship edges**. The Stage 2 read surface today emits records,
the five overlays, and gap cards — it does **not** emit correction/dispute relationship edges, nor a
dedicated `ai_presented` gate beyond `provenance_status`. This is a **backend contract gap for Stage 3
(3.05/3.07)**, to be designed as an additive, lane-gated, fail-closed read-surface extension — **never** by
loosening the public boundary or reading raw tables at the card layer. Until 3.07 defines it, Stage 3 may
render correction/dispute state only insofar as it is already expressible via `ui_status` /
`provenance_status`; it must not fabricate correction linkage.

---

## 3. Stage 2 backward-link carry-forward (Stage 1–8 build-forward rule)

Stage 3 is an intelligent continuation of Stage 2, not a rewrite. Carry-forward ledger:

**Reused (carried forward unchanged):**
- The **5-overlay reviewer-internal read surface** (`read_api.py`): `confidence_label` (GOV-283/290),
  `speaker_label` (GOV-290), `completeness_gap_cards` (GOV-298), `provenance_status` (GOV-311),
  `ui_status` + `evidence`. This is the Stage 3 data contract.
- **AI↔source separation:** `produced_by='ai'` write-time binding (GOV-278) + read-time
  `provenance_status` (GOV-311). Stage 3 surfaces AI provenance through these; it adds no new AI write path.
- **Three trust auditors:** read-surface traceability (GOV-306), 5-overlay integration safety net
  (GOV-318), back-gap/coverage auditor (GOV-322). Stage 3 traceability (3.12/3.13) reuses these.
- **Reviewer reference doc + doc-drift guard** (GOV-326): the field source for this contract; its drift-
  guard pattern (`tests/test_stage2_doc_surface_sync.py`) is the model for 3.14's card-contract guard.
- **Gated-beta access** (reviewer-internal lane, manual backend approval): Stage 3 MVP renders behind it.
- **Raw preservation / reproducibility** (GOV-262 preservation-replay): card-backing records remain
  replayable from preserved raw; 3.04 confirms.
- **Owner-escalation / anti-loop routing guard** (GOV-332): 3.15 reuses it.

**Corrected / clarified:**
- Card identity must use a **web-safe stable handle**, not a raw DB id (read surface strips ids) — §2.2.
- Master-plan status vocabulary must be **derived** from existing read keys, not invented — §2.2.

**Escalated / flagged forward (not resolved here):**
- The correction/dispute/AI relationship-edge read-surface gap → Stage 3 backend contract (3.07), §2.4.
- Newsletter/editorial (3.08) deferred: no public send in reviewer-internal Stage 3.

---

## 4. Paperclip goal reconciliation (per Stage 3.01 goal text)

Comparison of each Stage 3.x planned goal text against this reconciled map. The Stage 3.x goals share a
generic "criteria set" scaffold (the same 15-subgoal taxonomy as Stage 2); each carries a goal-specific
one-line subgoal contract that matches its title-level deliverable in §1.

| Goal | Outcome | Evidence / action |
|---|---|---|
| 3.01 `a0a0a97f` | **true** | Goal-specific requirement ("reconcile criteria into Paperclip, remove stale cross-references, verify each downstream goal has a usable standalone contract") is exactly this doc's job; satisfied by §1–§7. |
| 3.02–3.07, 3.09–3.15 | **true — no update needed** | Each subgoal's one-line contract matches its §1 deliverable and the boilerplate operating contract (Alpine-first, no-launch, evidence-required) is consistent with this map. Title-level deliverable remains accurate; operative population is each subgoal's own planning child's job. |
| 3.08 `8723a680` | **true but reclassified** | Newsletter/editorial behavior is correct as written but **deferred** for the reviewer-internal Stage 3 MVP (no public send). Recorded here; CEO owns activation timing. No text contradiction → no PATCH. |
| Parent Stage 3 `88190dca` | **true** | Parent scope (Alpine timeline + card model MVP) matches the master plan and §0. |

**Stale cross-reference found (flagged, owner = CEO):** the *embedded historical "company-buildout"
boundary block* inside several Stage 3.x goal texts still lists "Stage 2 is planned." Stage 2 is now
reviewer-internal exit-ready (GOV-335). This line lives in a transferred historical criteria block, not in
any subgoal's operative contract, so it does **not** corrupt this issue map. Because the same line repeats
across many goals and stage-status is CEO-owned staging state, the correct disposition is **escalate to CEO
for a single coordinated stage-status refresh**, not a unilateral mass goal-text PATCH by a Stage 3.01
planning doc. **No goal text is PATCHed by this issue** (recorded here as required; no operative
cross-reference in the map is stale).

---

## 5. Premium success-criteria gate (hard precondition — staging rule #8)

The premium success-criteria framework at
`/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`
**MUST be applied** (its paste-in success-criteria block added to the parent goal) to **any Stage 3
implementation child's parent goal before that child is created or activated**.

- **Planning/spec children** (this issue 3.01; the 3.02 acceptance/exit-gate child; the VSR/SecPriv review
  legs of this doc) may proceed **without** the premium block — but they must **not** authorize
  implementation.
- **Implementation children** (the impl child of any `plan→impl` subgoal: 3.03/3.04/3.05/3.06/3.07/3.12/
  3.13) are **blocked** until the premium success-criteria block is applied to their parent goal and the
  applying agent records the evidence.

This is a hard gate, not advisory. An implementation child created without the applied premium block is
out of order and must be blocked back to this gate.

---

## 6. Verification & review

**Verification evidence (this doc):** see the closing PR — file path, `wc -l`, `git diff --stat` proving a
Docs-only addition (0 production-code diff), and section greps proving (a) the 5-overlay data-contract
mapping (§2), (b) the premium-gate mention (§5), (c) the reconciliation outcomes (§4).

**Review lane:** Impl(Plan) → **VSR** (VerificationSafetyReviewer leg) → **SecPriv**
(SecurityPrivacyAgent leg) → **CTO non-author merge + goal-flip-at-merge** (CTO PATCHes Stage 3.01 goal
`a0a0a97f` → achieved AT merge). The two reviewer legs are created as `todo` child issues of GOV-336.

**Pass-up trigger:** any discovered need for public launch, legal/privacy/publication judgment,
budget/donation, official-contact, or scope expansion beyond Alpine → STOP, comment, escalate to CEO/Isaac.
No implementation is built under this issue.

---

## 7. Explicit non-unlock statement

This document is **planning/spec only**. It does **NOT**:

- authorize any Stage 3 implementation, code, migration, crawler run, or feed build;
- create or activate any Stage 3 implementation child (it only maps and orders them);
- unlock public launch, public newsletter send, or any public-facing surface;
- unlock non-Alpine expansion (Star Valley / Lincoln County / Wyoming / US);
- approve budget, donations, paid services, or official-contact automation;
- cross or weaken the public-projection boundary (`to_web_safe` / `publication.py`);
- override any Stage 0 safety/governance gate or any Stage 2 accepted artifact.

Stage 3 implementation remains gated behind: (a) each subgoal's own planning child, (b) the §5 premium
success-criteria gate, and (c) CTO/CEO sequencing with real Paperclip blocker links. Alpine-first,
reviewer-internal only, until an owner decision says otherwise.
