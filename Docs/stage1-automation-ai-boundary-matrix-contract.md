# Stage 1.09 Alpine Automation-vs-AI Boundary Matrix Contract

Issue: GOV-46
Owner role: AutomationOpsEngineer
Stage: Stage 1.09, planning/specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only
Created: 2026-06-08

## Gate Decision

GOV-46 passes when this document gives CTO, AutomationOpsEngineer,
BackendCrawlerEngineer, SourceArchivist, FrontendTimelineEngineer,
NewsletterEditor, VerificationSafetyReviewer, and SecurityPrivacyAgent a single
authoritative answer, for every planned Alpine pipeline step, to three
questions: **who runs it** (deterministic automation, AI/LLM, or human review),
**what that runner may and may not do**, and **what gate sits between this step
and the next**.

This pass does **not** authorize building any scheduler, crawler, transcriber,
AI pipeline, validator, exporter, or UI; running any automation or AI against
real targets; producing any public output; contacting any official or
subscriber; or expanding beyond Alpine. Stage 1 implementation stays locked.

The only downstream unlock is the next sequential Stage 1 planning gate. Any
implementation issue created later must explicitly consume this matrix and name
its own narrow Alpine step, command, log, tests, gate, and reviewer lane.

## Inputs Read (predecessor evidence — daisy chain)

- Required agent instructions:
  `AGENTS.md`, `COMPANY.md`, `SOUL.md`, `TOOLS.md`, `HEARTBEAT.md`,
  `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`,
  `AUTOMATION_OPS_WORKFLOWS.md`, `STAGE0_EXECUTION_WORKFLOW.md`,
  `RISK_ASSESSMENT_WORKFLOW.md`, `GATED_BETA_ACCESS_WORKFLOW.md`,
  `AI_GATEWAY_PROCESSING_WORKFLOW.md`, `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`.
- Staged master plan:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`
- Stage 0.09 base automation-vs-AI matrix: **GOV-18** (backend project,
  goal `3fe1ae73`).
- Stage 1.05 backend/tooling contract `Docs/stage1-backend-tooling-implementation-contract.md`
  (incl. the GOV-36/37/38/39 Card Status Vocabulary patch — authoritative
  `verificationStatus`, `uiStatus-map.v1`, fail-closed publication allowlist).
- Stage 1.07 transcript/evidence/statement contract
  `Docs/stage1-transcript-evidence-statement-contract.md` (GOV-40 done;
  GOV-41/42 APPROVE).
- Stage 1.08 newsletter/briefing/editorial contract
  `Docs/stage1-newsletter-briefing-editorial-contract.md` (GOV-43 done;
  GOV-44/45 APPROVE).
- Authoritative status code:
  `scripts/validate_concept_map_export.py` — `SCHEMA_VERSION =
  "gov-watchdog-card-map.v1"`, `ALLOWED_VERIFICATION_STATUSES`,
  `ALLOWED_UI_STATUSES`, `REVIEWED_VERIFICATION_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`,
  `ALLOWED_NODE/EDGE/CARD/LINK_TYPES` (read this run from the
  `gov-17-newsletter-briefing-contract` branch, which already enforces them).
- Premium template:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`

### Predecessor-evidence note (read before relying on this contract)

The predecessor contracts (1.05/1.07/1.08) and the
`validate_concept_map_export.py` validator currently live on **unmerged task
branches** (`gov-17-newsletter-briefing-contract`,
`GOV-40-…-transcript…`, `GOV-43-…-newsletter…`), not on `main`. This contract is
spec-only and cites those artifacts by stable path + constant name. A future
implementation gate must consume the **merged** versions of those files; if a
constant named here has drifted at merge time, the merge — not this matrix — is
the authority, and this matrix is patched to match (it must never re-define the
vocabulary; see §2 and §10).

## Stage Boundary (allowed / not allowed in Stage 1.09)

Allowed in Stage 1.09:

- enumerate the planned Alpine pipeline steps and classify each as deterministic
  automation, AI/LLM, or human review;
- state the allowed action and hard prohibitions per step;
- define the gate between every adjacent step (what must be true to advance);
- define AI containment, determinism, human-in-the-loop, and failure/escalation
  rules;
- define the automation/AI metadata the backend emits and how the frontend
  labels it, by reference to 1.06/1.07/1.08 field names;
- define the privacy/data boundary for raw and AI-scratch material.

Not allowed in Stage 1.09:

- implement a scheduler, cron, crawler, transcriber, extractor, classifier,
  summarizer, validator, exporter, or UI;
- run any automation or AI against real Alpine targets;
- fetch, transcribe, or process any new source material;
- publish any output to any public surface;
- contact any official, subscriber, or government system;
- write any accusation, motive claim, legal conclusion, or campaign wording;
- redefine `verificationStatus`, `uiStatus-map.v1`, or the publication allowlist
  (owned by GOV-36/37/38/39);
- expand beyond the Town of Alpine.

---

## 1. Boundary Matrix

The Alpine pipeline is read **left to right**: deterministic automation gathers
and proves provenance, AI proposes interpretation as labeled drafts, and humans
gate every promotion toward public. Each step below names its **runner class**,
the **action it MAY take**, the **hard prohibitions**, and the **gate to the
next step**.

Runner classes:

- **DET** — deterministic automation (a script; same inputs ⇒ same outputs; no
  model judgment). Owns provenance, hashing, archiving, validation, gating.
- **AI** — AI/LLM step. Produces only *labeled, non-authoritative drafts* with
  source anchors and confidence; never writes provenance or gating fields.
- **HUM** — human review (specialist / VerificationSafetyReviewer / CTO / CEO).
  Owns every promotion from non-reviewed to reviewed/public.

### 1.1 The matrix

| # | Pipeline step | Runner | MAY do | MUST NOT do | Gate to advance |
|---|---|---|---|---|---|
| 1 | **Source discovery / registry intake** | DET | Read Alpine source rows that satisfy the 1.05 Source Registry Input Contract; reject/quarantine non-Alpine, missing-provenance, or private-data rows. | Add a source by AI judgment; accept a non-Alpine `scope`; infer a missing required field. | Row passes all required 1.05 fields; `scope == alpine`; `robots_policy` present. |
| 2 | **Fetch / crawl** | DET | Fetch only allowlisted Alpine URLs that passed step 1, honoring robots + rate limits; record HTTP status. | Fetch a URL not on the allowlist; let AI choose what to fetch; exceed rate/robots policy; contact an official/government system beyond ordinary public web fetch. | Fetch eligibility + robots/allowlist check pass; response captured. |
| 3 | **Raw preservation** | DET | Write the dated raw artifact with the full 1.05 Raw Preservation metadata (`source_id`, `original_url`, `captured_at_utc`, `content_type`, `byte_size`, …) to **vault-only** paths. | Overwrite a known-then artifact; silently refresh `scan_date`; route raw bytes to GitHub/public. | Artifact exists at a vault-only path with complete metadata. |
| 4 | **Hashing / integrity** | DET | Compute `sha256` over the artifact bytes; record it; compare on re-runs. | Treat a seed-only row as raw-preserved; accept a hash that does not match the bytes. | `sha256` computed and matches artifact bytes; inventory/registry/manifest agree. |
| 5 | **Dedup** | DET | Detect duplicates by `sha256` / `(source_id, captured_at)` and collapse exact duplicates; preserve a new dated artifact when content **changed** (sets `sourceChanged`). | Use AI similarity to "merge" distinct records; delete the older artifact on change; mask a real content change as a duplicate. | Exact dupes collapsed deterministically; changed content preserved side-by-side. |
| 6 | **Archive / Wayback lookup** | DET | Record `archive_status`, `archive_url`/null, `snapshot_date` per the 1.05 Wayback contract. | Interpret an archive gap as motive, wrongdoing, or disappearance; block on a non-critical archive miss. | Archive state recorded (a miss is a warning unless downstream use depends on it). |
| 7 | **Transcription (audio/video → text)** | AI | Produce a machine transcript (`transcript_segment`s with `timestamp_seconds`, `is_verbatim`, `confidence`) labeled `machine_extracted_unreviewed`; write to **vault-only** transcript paths. | Present a machine transcript as verbatim-verified; assign a speaker; drop the AI label. | Transcript stored vault-only at `machine_extracted_unreviewed`; pointer captured. |
| 8 | **Statement / evidence extraction** | AI | Propose `statement` records with an exact-source `pointer` (1.07 §2), `confidence`, and the `ai_thought_then`/`machine_extracted_unreviewed` labels. | Assert a claim with **no resolving pointer** (orphan claim — hard reject, 1.07 §2.3); promote its own status; state motive/legal conclusions. | Every proposed statement has a resolving pointer; status is non-reviewed. |
| 9 | **Speaker attribution** | DET-gate + HUM | DET joins a separate `speaker_attribution` record and sets `attribution_state` only from **official records**; HUM confirms identity. AI may suggest a *candidate* only into a reviewer-only field. | Guess a speaker from voice/context/community knowledge; render a candidate name; name an `on-record-public` speaker (CEO hard stop, 1.07 §3). | `attribution_state == attributed` **only** via official records + review; otherwise renders generic label. **No name > wrong name.** |
| 10 | **Classification / typing (nodes & edges)** | AI-propose, DET-validate | AI proposes node/edge types and typed links; DET validates them against `ALLOWED_NODE/EDGE/CARD/LINK_TYPES`. | Let AI invent a type outside the allowed sets; let an unvalidated type reach a card. | Proposed types validate against the allowed enums; invalid → rejected. |
| 11 | **`verificationStatus` assignment** | HUM | A reviewer promotes a record to `reviewed_source_linked` / `human_verified` after confirming verbatim text matches the source at the pointer; VSR/CEO set `disputed` / `do_not_publish`. | Let AI or a script set a **reviewed** status; auto-promote on confidence score. | Reviewer recorded the promotion against the pointer (Evidence Quality Review). |
| 12 | **`uiStatus` computation** | DET | Run `compute_ui_status` (`uiStatus-map.v1`, rules #1–#12, first-match-wins, fail-closed default `pending-review`) over the authoritative inputs. | Hand-set `uiStatus`; let the frontend re-derive trust from `card.type`/label; add a value outside `ALLOWED_UI_STATUSES`. | `uiStatus` deterministically computed; matches the map for that card's inputs. |
| 13 | **Summary / briefing / digest generation** | AI-draft, HUM-gate | AI drafts editorial copy as a *view over already-reviewed typed records* (1.08), AI-labeled and gated. | Generate copy from non-reviewed records; assert a claim; convert `disputed`→verified; mutate known-then. | Draft cites only reviewed-eligible records; carries AI label; held at private review. |
| 14 | **Correction handling** | DET-structure + HUM-decide | DET appends a forward-only `corrected_later` layer with `correction_date` + source trail and an `outcome_updates` / `evidence_link.relation: corrects` edge; HUM authors/approves the correction. | Overwrite or edit the `known_then` record; let AI silently "fix" a record; publish a correction without review. | Correction links forward, original intact; reviewer approved (1.07 §4 / 1.08 §4). |
| 15 | **Publication gating / export validation** | DET (fail-closed) | Permit `publicExportApproved == true` **only** when `uiStatus ∈ PUBLICATION_ELIGIBLE_UI_STATUSES`; drop §7 private fields; error otherwise. | Let any non-allowlisted `uiStatus` publish; emit a private field on a public payload; publish without review. | Validator passes: allowlisted `uiStatus`, no orphan claim, no private field. |
| 16 | **Backend → frontend handoff** | DET | Emit the reviewed public-subset card + source-drawer payload with the `produced_by` / `review_state` / `confidence` provenance fields (§6). | Hand a record frontend-ready without a resolving pointer + access state; expose AI-only/unverified as fact. | Handoff invariants (§6.3 / `BACKEND_FRONTEND_EVIDENCE_WORKFLOW`) satisfied. |
| 17 | **Scheduling / run orchestration** | DET | Trigger steps 1–6 + 12 + 15 on a defined cadence with `--dry-run` default, vault-only logs, and a run manifest. | Schedule any AI promotion or publication; default to `--apply`; run AI steps unattended into public. | Scheduler drives only DET steps + gated handoffs; AI/HUM steps stay attended. |

### 1.2 How to read the matrix as a rule

The pipeline alternates: **DET gather → AI draft → HUM gate → DET compute/publish.**
The single structural guarantee is that **the only edge from "AI produced this"
to "the public can see this" passes through a HUM `verificationStatus`
promotion (step 11) and a DET fail-closed publication gate (step 15).** There is
no other path. Steps 7, 8, 10, 13 (the AI steps) all terminate at a non-reviewed
status; nothing they emit is publishable until a human moves it and a
deterministic validator allows it.

> `★ Why DET owns gating and AI never does ──────────`
> If an AI step could write `verificationStatus` or `uiStatus`, a confident-but-
> wrong model output could mark itself "human_verified" and walk straight onto a
> public civic-accountability surface. Keeping every gating field deterministic
> and human-promoted means the worst an AI error can do is produce a *labeled,
> gated draft* — visible to reviewers, invisible to residents.
> `─────────────────────────────────────────────────`

---

## 2. AI Containment Rules

AI/LLM output is **draft interpretation, never record of fact**. It binds to the
AI Gateway Processing Workflow rule "AI output is never primary evidence."

1. **Single entry status.** Every AI-produced record enters at a **non-reviewed**
   `verificationStatus` — `machine_extracted_unreviewed` (extraction/transcription)
   or, if merely registered, `source_recorded`. Both compute to a gated
   `uiStatus` (`unverified` / `pending-review`) via `uiStatus-map.v1`.
2. **Always labeled.** AI content carries an explicit label
   (`AI-generated` / `AI-paraphrased` / `AI-summarized`) and lives in the
   `ai_thought_then` layer where applicable (1.07 §4). An AI paraphrase is
   **never** rendered as a verbatim quote (`is_verbatim == false`).
3. **Never writes gating fields.** AI may not set or modify
   `verificationStatus`, `uiStatus`, `correctionStatus`, `publicExportApproved`,
   `attribution_state`, hashes, or archive state. Those are DET-computed or
   HUM-set.
4. **Source-anchored or rejected.** An AI statement with no resolving
   `pointer` is an **orphan claim** and is rejected at extraction (1.07 §2.3),
   not stored and not surfaced.
5. **No promotion path of its own.** No AI confidence score, agreement between
   models, or self-evaluation promotes a record. Only step-11 human review does.
6. **Vault-only scratch.** AI intermediate output (raw transcripts, draft
   statements, chain-of-thought, candidate names, draft summaries) stays
   local/vault-only until reviewed and allowlisted (§7).
7. **No fail-open.** Any AI output that cannot be classified into a known label
   or status falls to the fail-closed default (`pending-review`) — never to a
   reassuring state.

The end-state invariant: **no AI-asserted claim becomes website-ready without
passing the human `verificationStatus` promotion and the deterministic
`uiStatus`/publication-allowlist gates; and no orphan claims exist.**

---

## 3. Determinism Requirements

These steps **MUST** be deterministic and reproducible, and **MUST NOT** be
delegated to AI judgment. Same inputs ⇒ same outputs; runnable and auditable
offline:

- **Source-trail capture** (steps 1–3): registry field validation, fetch
  eligibility, robots/allowlist enforcement, raw artifact metadata.
- **Integrity** (step 4): `sha256` over artifact bytes; cross-file agreement
  (inventory ⇄ registry note ⇄ manifest).
- **Dedup** (step 5): exact-hash dedup and changed-content preservation.
- **Archive linking** (step 6): recording `archive_status`/`archive_url`/
  `snapshot_date`.
- **`uiStatus` computation** (step 12): `compute_ui_status` over fixed inputs;
  the validator must reject any card whose stored `uiStatus` disagrees with the
  map.
- **Publication gating** (step 15): the fail-closed allowlist check and the
  private-field drop.
- **Run manifests + logs** (step 17): the 1.05 manifest/log shapes.

**Reproducibility test (for future implementation issues):** running step 12 or
step 15 twice over the same inputs must produce byte-identical results, and the
validator's module-load parity assertion (`set(_VERIFICATION_STATUS_ROLES) ==
ALLOWED_VERIFICATION_STATUSES`) must hold so the status vocabulary cannot drift
silently (GOV-36 CTO Blocker 6).

Determinism is the property that lets a reviewer or auditor **replay** how a card
reached its status. An AI step can be re-run, but not *reproduced*; that is
exactly why provenance and gating may never live in an AI step.

---

## 4. Human-in-the-Loop Gates

A transition requires a named human before promotion. Default-deny on ambiguity.

| Gate | Transition | Who | Promotion requires |
|---|---|---|---|
| **G1 Speaker** | `uncertain`/`unattributed` → `attributed` | Specialist + official records | Identity from official records; `on-record-public` naming → **CEO hard stop**. |
| **G2 Verification** | non-reviewed → `reviewed_source_linked` / `human_verified` | Specialist reviewer | Verbatim text confirmed against the source at the pointer (Evidence Quality Review). |
| **G3 Dispute/Block** | any → `disputed` / `do_not_publish` | VerificationSafetyReviewer / CEO | Reviewer judgment; these are terminal-gated and never auto-cleared. |
| **G4 Correction** | append `corrected_later` | Specialist + VSR | Correction authored with `correction_date` + source trail; forward-only. |
| **G5 Editorial** | draft → `private_review` → public preview | NewsletterEditor → VSR | No orphan claims; AI labels present; only reviewed-eligible records cited. |
| **G6 Publication** | private → public surface | **CEO / Isaac (owner)** | Owner decision; never an agent or a script. |
| **G7 Contract/vocab** | change a field/enum name | CTO (+ SecurityPrivacy if boundary) | Coordinated patch to *both* sides of the handoff (§6.3). |

On ambiguity at any gate — uncertain speaker, conflicting sources, an unclassified
status, an archive gap that downstream use depends on — the **default is deny**:
the record stays at the gated state and a Paperclip issue/comment routes it to the
gate owner. No gate is satisfied by an AI confidence score.

---

## 5. Failure & Escalation Behavior

Fail-closed everywhere. The system's resting state for anything unproven is
"gated, labeled, not public."

| Failure condition | Deterministic behavior | Escalation |
|---|---|---|
| **Low-confidence AI output** | Stays `machine_extracted_unreviewed` → `unverified`; never auto-promoted; surfaced only to reviewers. | Routine review queue (G2). |
| **Ambiguous speaker attribution** | `attribution_state` stays `uncertain`/`unattributed`; renders generic label; candidate name reviewer-only. **No name beats wrong name.** | Specialist (G1); `on-record-public` → CEO. |
| **Broken / missing archive** | Warning by default; if a live source is gone **and** no preserved artifact or archive exists → `uiStatus: source-missing`, downstream use **blocked**. | SourceArchivist; gate downstream use. |
| **Source changed since review** | `sourceChanged` set → `uiStatus: source-changed`; prior review invalidated; re-review required. | Re-enter G2. |
| **Conflicting sources** | Record stays gated; statements may carry `contradicts` edges; neither asserted as fact. | VerificationSafetyReviewer (G3). |
| **Hash mismatch / metadata disagreement** | Hard validation failure; artifact quarantined; not handed off. | BackendCrawler + AutomationOps; Paperclip issue. |
| **Orphan claim (no pointer)** | Rejected at extraction; never stored, never surfaced. | None needed (auto-reject); recurring → issue. |
| **Unknown `verificationStatus`/`uiStatus`** | Fail-closed default `pending-review`; validator parity assertion fails CI. | CTO (vocab drift, G7). |
| **Private field on a public payload** | Export validation failure; export blocked. | SecurityPrivacyAgent; Paperclip issue. |
| **Scope leak (non-Alpine in an Alpine run)** | Rejected with explicit out-of-scope logging; not processed. | Per `AUTOMATION_OPS_WORKFLOWS` issue threshold. |

**Automation run failure thresholds** (carried from `AUTOMATION_OPS_WORKFLOWS`):
create a Paperclip issue on 3+ consecutive non-transient failures, any scope
leak, any missing-but-expected artifact, or any critical/fatal log entry. Silent
log-only failure is not acceptable; a step that can fail silently is not done.

**Owner-escalation triggers (stop, route to CEO):** running any automation/AI
against real targets, any public output/publication, any official/subscriber
contact, any accusation/legal/campaign wording, any scope beyond Alpine, any
budget, any legal/privacy judgment on a specific individual, or any AI-label /
verification / publication policy change. **None are authorized here.**

---

## 6. Backend ↔ Frontend Handoff (automation/AI metadata)

Aligns field names with the 1.05 card model, 1.06 frontend/product surface, 1.07
statement model, and 1.08 editorial contract. The handoff carries, on every
record, **who produced it and how trustworthy it is**, so the frontend can label
provenance without inference.

### 6.1 Provenance metadata the backend emits

Per record/card, in addition to the existing `gov-watchdog-card-map.v1` fields
(`id`, `type`, `verificationStatus`, `correctionStatus`, `uiStatus`,
`statusLabel`, `sourceCount`, `links[]`, `publicExportApproved`):

- `produced_by` — enum `{ automation, ai, human }`: which runner class
  (§1) created the record's content. Deterministic capture/compute = `automation`;
  transcription/extraction/summary = `ai`; reviewer-authored correction/verdict =
  `human`.
- `review_state` — derived from `verificationStatus`: `not_reviewed` (non-reviewed
  set), `reviewed` (`REVIEWED_VERIFICATION_STATUSES`), `blocked`
  (`disputed`/`do_not_publish`).
- `confidence` — the AI confidence label for `produced_by == ai` records (carried
  from 1.07); absent/`n/a` for deterministic or human records.
- `ai_label` — `{ none, AI-generated, AI-paraphrased, AI-summarized }`.
- `layer` — `{ known_then, presented_then, ai_thought_then, corrected_later,
  actual_later }` (1.07 §4 / 1.08 §4).
- the source-drawer `pointer` (1.07 §2), generic-or-approved `speaker_label`
  (1.07 §3), `is_verbatim`.

`produced_by`, `review_state`, `ai_label`, and `confidence` are **not**
trust-bearing on their own — the frontend still keys publication/badging on
`uiStatus` — but they let the UI render an explicit "Automated capture",
"AI draft — not verified", or "Reviewer-confirmed" provenance chip.

### 6.2 What the frontend surfaces

- A **provenance chip** derived from `produced_by` + `ai_label` (e.g. "AI draft —
  unverified") next to the `uiStatus` badge.
- The `uiStatus` badge verbatim (never re-derived from `card.type`).
- The source drawer (pointer, archive link, timestamp/page) so a resident can
  trace any card to its exact source moment.
- Gated states (`unverified`, `pending-review`, `disputed`, `source-missing`,
  `source-changed`, `do-not-publish`) rendered with visible labels; visual polish
  must never imply verification.

### 6.3 Handoff invariants

- Backend may not call a record frontend-ready without a resolving pointer **and**
  an access/publication state.
- Frontend may not create a public claim from any field that is AI-only,
  unverified, disputed, private, or pending review.
- `produced_by == ai` with `review_state == not_reviewed` is **never** rendered
  as fact, only as a labeled draft, and only in reviewer/preview surfaces.
- A field rename on either side (`produced_by`, `review_state`, `uiStatus`,
  `pointer`, `speaker_label`) requires a coordinated patch to **both** this
  contract and the 1.06/1.07/1.08 contracts — a silent divergence is a
  validation failure (G7).

---

## 7. Privacy / Data Boundary

Raw and unreviewed material and AI scratch output stay **local/vault-only**. Only
reviewed, allowlisted, website-ready content surfaces, and only after the gates.

### 7.1 Stays local / vault-only (never GitHub-public, never public UI/API)

- Raw meeting audio/video and full raw transcripts; `transcript_path`,
  `local_note_path`, raw artifact paths, raw-byte `sha256`, run logs.
- **All AI scratch output**: draft statements before review, chain-of-thought,
  model rationales, candidate (`uncertain`) speaker names, draft summaries/lenses
  before approval.
- Reviewer-only fields: attribution `basis`, reviewer notes, `review_state`
  rationale.
- Any private identity/address/voter-registry/account-validation data — these
  never enter a statement, attribution, or card record at all.

### 7.2 May surface (only when reviewed + allowlisted)

- The reviewed statement/summary text, generic-or-approved `speaker_label`,
  `uiStatus`, `statusLabel`, `produced_by`, `ai_label`, `layer`, `confidence`
  label, and the **public subset** of the pointer (original/current URL, scan
  date, source type, authority, jurisdiction, archive link, timestamp/page/
  section, deep link).

### 7.3 Boundary rules

- A record may surface only when `publicExportApproved == true` ⇒ reviewed +
  allowlisted `uiStatus` (§1 step 15 / 1.07 §5.3).
- No public accusation, motive claim, legal conclusion, or campaign wording is
  derivable from public fields; records carry source pointers, not verdicts.
- The export validator must drop/never-emit §7.1 private fields on any public
  surface; a private field reaching a public payload is a validation failure that
  blocks the export.

---

## 8. Similar-Product Research

Per the Premium framework §4–§5. Civic-data / AI-pipeline / human-in-the-loop
moderation patterns reviewed, for what GOV should use and avoid for **local
Alpine** records. The relevant comparison axis for 1.09 is specifically *where
each draws its automation-vs-human line*.

| Product / pattern | How it splits automation vs human | GOV should **use** | GOV should **avoid** | Fit for local Alpine? |
|---|---|---|---|---|
| **Wikipedia ORES + human patrol** (https://www.mediawiki.org/wiki/ORES) | ML *scores* edits (vandalism/quality) but never blocks or publishes; humans act on the score. The model is an advisor, edits stay live/revertible. | The "AI scores, human decides" split; treating the model output as a *queue signal*, not an action. | ORES still lets the un-reviewed edit be **publicly live** pre-review; GOV must keep AI drafts gated *until* review, not after. | Strong on the advisor split; GOV inverts the default to gated-until-reviewed (stricter, correct for civic evidence). |
| **DocumentCloud + ML add-ons (e.g. entity/OCR)** (https://www.documentcloud.org/) | Deterministic OCR/text extraction + optional ML add-ons produce annotations *alongside* the primary document; the document is the authority. | Page/section-anchored deterministic extraction as the spine; ML annotations as a separate, labeled layer. | Add-on output can blend reviewer interpretation with source text if layers aren't kept distinct — GOV keeps `ai_thought_then` separate (§1/§6). | Good for the document-anchored steps (ordinances, packets, minutes); validates DET-extract + AI-annotate split. |
| **Whisper / ASR transcription pipelines** (https://github.com/openai/whisper) | Fully automated audio→text with per-segment confidence; downstream consumers decide trust. | Per-segment `confidence` + `timestamp_seconds`; treating ASR as `machine_extracted_unreviewed`, never verbatim-verified. | ASR is presented as "the transcript" with no review gate or speaker-uncertainty handling; GOV must label it draft + never auto-attribute speakers. | Direct fit for step 7; the missing review/attribution gate is exactly what GOV adds (steps 9, 11). |
| **Two-tier content moderation (automated flag → human queue)** (https://www.osce.org/ and platform Trust-&-Safety norms) | Automation *flags*; a human reviewer makes the publish/remove decision; default is "hold on uncertainty." | Default-deny on ambiguity; automation as a triage/flag layer; the human owns the irreversible/public action. | Over-trusting automated flags (false positives acted on automatically); opaque, unauditable model decisions. | Strong fit for GOV's gate model: AI triages, human gates, fail-closed default (§4/§5). |

**Cross-cutting lessons.** (1) Every credible system keeps the **irreversible /
public action** (publish, remove, attribute) on the **human** side of the line —
GOV's G6 publication-as-owner-gate matches this. (2) The mature ML pipelines treat
model output as a **score/draft/queue signal**, never an authoritative write —
GOV encodes this as "AI never writes gating fields" (§2). (3) The common failure
mode is **default-open** (the un-reviewed item is publicly live until someone
objects); GOV deliberately inverts to **default-closed** (gated until reviewed),
which is the correct posture for a civic-accountability surface where a wrong
"verified" badge is the high-cost error.

**Tradeoffs (Premium §5), chosen positions.**

- *Speed vs source completeness* → completeness; an un-pointered/un-hashed item is
  held, not shipped.
- *AI summarization vs human verification* → AI draft-only, gated `unverified`
  until a reviewer promotes (step 11).
- *Automation coverage vs auditability* → auditability; every gating field is
  deterministic and replayable, never an AI write.
- *Private progress vs public launch risk* → private/preview first; publication is
  an owner gate (G6).
- *Raw preservation vs public-data boundary* → raw + AI scratch vault-only; only
  reviewed public-subset fields surface.
- *Local Alpine clarity vs premature WY/US generalization* → Alpine-only.

---

## 9. Premium Success-Criteria (GOV-38 template, completed)

> Completed using the paste-in template from
> `2026-06-06-Premium-Success-Criteria-Framework.md`.

```markdown
## GOV Premium Success Criteria

Stage: Stage 1.09 (Alpine automation-vs-AI boundary matrix contract) — planning/spec only
Scope: Town of Alpine only
Project/repo: xXKillerNoobYT/Government-watchdog / 0a1832c4-1556-49a1-bcc5-857f2ca72962
Owner role: AutomationOpsEngineer
Reviewer path: VerificationSafetyReviewer (AI-label/gate + no-orphan-claims correctness) -> CTO (technical contract sign-off); SecurityPrivacyAgent consulted on the data boundary.
Blockers / unlock rule: Consumes Stage 0.09 (GOV-18), Stage 1.05 (GOV-34 + GOV-36/37/38/39), Stage 1.06 (GOV-35), Stage 1.07 (GOV-40 done; GOV-41/42 APPROVE), Stage 1.08 (GOV-43 done; GOV-44/45 APPROVE). Unlocks only the next sequential Stage 1 planning gate; unlocks no implementation, no scheduler/cron, no AI run on real data, no public output, nothing beyond Alpine.

### Success Definition
- Success means: any implementer or reviewer can take the planned Alpine pipeline and, using only this contract, know for every step (§1) whether it is deterministic automation, AI/LLM, or human review; what that runner may and may not do; and the exact gate to the next step. They can see that the single path from "AI produced this" to "public can see this" runs through human verificationStatus promotion (step 11) and the deterministic fail-closed publication gate (step 15) — with no other path — and that AI never writes a gating field (§2), provenance/gating is deterministic and replayable (§3), every promotion has a named human gate (§4), failures fail closed (§5), the backend emits produced_by/review_state/ai_label/confidence provenance the frontend labels without inference (§6), and raw + AI scratch stay vault-only (§7).
- Evidence proving success: this file at Docs/stage1-automation-ai-boundary-matrix-contract.md; enum/field names match scripts/validate_concept_map_export.py (ALLOWED_VERIFICATION_STATUSES, ALLOWED_UI_STATUSES, REVIEWED_VERIFICATION_STATUSES, PUBLICATION_ELIGIBLE_UI_STATUSES, compute_ui_status, ALLOWED_NODE/EDGE/CARD/LINK_TYPES) and the 1.05/1.07/1.08 contracts; VerificationSafetyReviewer + CTO sign-off comments on GOV-46.

### Failure Definition
- Failure looks like: a pipeline step with no runner classification or no next-step gate; an AI step permitted to write verificationStatus/uiStatus/publicExportApproved/attribution_state; an orphan AI claim (no pointer) that is stored or surfaced; a non-reviewed (machine_extracted_unreviewed / source_recorded / null) record reaching a public surface; a guessed/named uncertain-or-community speaker; AI scratch or raw material on a public/GitHub path; a private field in a public payload; a status vocabulary invented here instead of reusing uiStatus-map.v1; or a publication that is not an owner (CEO) gate.
- Stop/escalation trigger: any need to run automation/AI on real targets, publish publicly, contact an official/subscriber, write accusation/legal/campaign copy, name an on-record-public speaker, expand beyond Alpine, set budget, change AI-label/verification/publication policy, or make a legal/privacy judgment on a specific individual -> stop, route to CEO.

### Workability
- Real user/operator workflow: (operator) AutomationOps/Backend builds a future step as a script with --dry-run default + vault-only logs (DET steps), or wires an AI step that emits only machine_extracted_unreviewed drafts with pointers + confidence; the deterministic compute_ui_status + publication validator gate every handoff; a reviewer promotes verificationStatus (G2) and the owner gates publication (G6). (resident, future + gated) sees only reviewed, allowlisted cards with a provenance chip and source drawer.
- Inputs: registry-passing Alpine source rows, vault-only raw artifacts + hashes, reviewed records, the authoritative enums in validate_concept_map_export.py.
- Outputs: the boundary matrix (§1), AI containment / determinism / gate / failure rules (§2-§5), the produced_by/review_state handoff fields (§6), the privacy boundary (§7). No code, no run.
- Missing/stale/disputed source behavior: source-missing / source-changed block or re-review; disputed/do-not-publish never publish; low-confidence AI stays unverified (§5).
- Resume/retry behavior: DET steps are reproducible (§3) and idempotent; an interrupted run re-derives state from source_id/sha256/verificationStatus and resumes; known-then records are never rewritten on resume.

### Ease of Use
- Resident/Isaac comprehension target: in 30 seconds a reader of one card sees "was this captured by a machine, drafted by AI, or confirmed by a person, and how far did it get through review" — via the provenance chip + uiStatus badge, no technical explanation.
- Labels/statuses/gaps visible: produced_by chip, ai_label, uiStatus badge, confidence, and source-missing/changed gaps are visible; nothing trust-bearing is hidden or inferred from card.type.
- Required screenshot/prototype/wireframe/review note: defers UI artifacts to GOV-35 (Stage 1.06) which owns the card + source-drawer + provenance-chip wireframes; this contract supplies the produced_by/review_state/ai_label field inventory those wireframes must render.

### Comparable Research
- Comparable tools reviewed: Wikipedia ORES + human patrol (https://www.mediawiki.org/wiki/ORES), DocumentCloud + ML add-ons (https://www.documentcloud.org/), Whisper/ASR pipelines (https://github.com/openai/whisper), two-tier automated-flag→human-queue moderation. Full pros/cons/tradeoffs in §8.
- Lessons GOV should use: AI scores/drafts, humans gate the irreversible/public action; deterministic extraction spine + separate labeled AI layer; per-segment confidence; default-deny on ambiguity.
- Patterns GOV should avoid: default-open (un-reviewed item publicly live); acting on automated flags automatically; ASR-as-truth with no review/attribution gate; blending AI interpretation into source text.
- Source links: as listed in §8.

### Tradeoffs
- Main tradeoffs: §8 (speed vs completeness; AI summarization vs human verification; automation coverage vs auditability; private preview vs public risk; raw/AI-scratch preservation vs public boundary; Alpine clarity vs premature WY/US).
- Chosen approach and reason: deterministic-owns-gating + AI-draft-only + human-owns-promotion + fail-closed default, because civic-evidence credibility depends on a replayable audit trail and a default-closed posture where a wrong "verified" badge is the high-cost error; speed/coverage are subordinate to "no orphan claims", "no wrong attribution", and "no un-reviewed AI claim in public".

### Plan Before Implementation
- Concept/data model: §1 (the 17-step matrix), §6.1 (produced_by/review_state/ai_label/confidence over the existing card model).
- UI/operator behavior: §6 (backend emits / frontend surfaces) — defers visual artifacts to GOV-35.
- Verification commands or review steps: future implementation issues add, per step they build: a --dry-run/--apply script with vault-only logs + run manifest (DET), a fixture proving AI output enters only at a non-reviewed status with a pointer (AI), and a validator test that compute_ui_status + the fail-closed allowlist reject any non-allowlisted publish and any private field; this contract is spec-only (no code changed on this branch).
- Artifact paths: this file; future: scripts/ (DET tools), Exports/alpine/ (validated public subset), vault-only raw/AI-scratch + run logs.
- Failure handling: §5 (fail-closed table + automation thresholds + owner-escalation triggers).

### Source and Auditability
- Required source fields: §6.1 provenance + the 1.07 public pointer subset (source_id, original_url, scan_date, source_type, source_class, authority, jurisdiction, archive pair, locator).
- Local source-data paths: <vault>/.../Source-Data/ + source-registry notes + vault-only raw/AI-scratch/logs (§7.1).
- Archive/Wayback/timestamp/page requirements: §1 step 6 (archive pair recorded; gap is a warning unless downstream use depends on it, then blocked).
- Verification/correction status handling: §1 steps 11-14 + §3 — 6-value verificationStatus -> compute_ui_status (uiStatus-map.v1) -> 3-value publication allowlist; forward-only corrections.

### Timeline and Concept Integrity
- Known-then vs later-outcome handling: §1 step 14 + §6.1 layer field — append-only known_then/corrected_later/actual_later; known-then never mutated (1.07 §4).
- Correction handling: DET appends corrected_later + outcome_updates edge; HUM authors/approves; corrected uiStatus is reviewed-gated.
- Concept records kept separate: §1/§6 — records reference typed nodes/edges by id and validate against ALLOWED_NODE/EDGE/CARD/LINK_TYPES; AI never collapses the graph.
- Required typed relationships: source_supports / evidence_link (claim->source), outcome_updates (later->earlier), card_presents / card_links_card (presentation).

### Acceptance Evidence
- Required artifacts: this contract file (path + line count).
- Required tests/checks: spec-only, no code changed on this branch; the referenced validator already enforces the cited enums; future implementation issues must add the per-step DET/AI/validator tests named under Plan Before Implementation.
- Required issue/PR/screenshot/API/source evidence: GOV-46 disposition comment with VerificationSafetyReviewer + CTO sign-off path; SecurityPrivacyAgent consulted on §7.
```

---

## 10. Locked Scope (what Stage 1.09 / GOV-46 does NOT authorize)

This contract is planning/specification only. It explicitly does **not** authorize:

- implementing any scheduler, cron, crawler, fetcher, transcriber, extractor,
  classifier, summarizer, validator, exporter, or UI (spec-only; no code changed
  on this branch);
- running any automation or AI against real Alpine targets, or fetching /
  transcribing / processing any new source material;
- publishing any item, summary, quote, card, or metric to the public website,
  email list, API, feed, or any public surface;
- scheduling any automation, or running any `--apply` step (all future scheduled
  automation defaults to `--dry-run`; first `--apply` needs CTO review of the
  dry-run output);
- naming any `on-record-public` speaker, or attaching any name to an
  `uncertain`/`unattributed` statement (CEO hard stop, 1.07 §3);
- writing any accusation, legal conclusion, motive claim, campaign/endorsement,
  or public-pressure messaging;
- contacting any Alpine/Lincoln County official, subscriber, or government system
  beyond ordinary public web fetches;
- changing the AI-label, `verificationStatus`, `uiStatus-map.v1`, or publication
  allowlist policy (owned by GOV-36/37/38/39 and may not be redefined here);
- any legal, defamation, campaign, budget, donation, or privacy judgment about a
  specific individual;
- expanding scope beyond the Town of Alpine.

Any of the above is an **owner-escalation trigger**: stop and route to CEO.

---

## Verification Evidence

- **File:** `Docs/stage1-automation-ai-boundary-matrix-contract.md` (line count
  recorded in the GOV-46 closeout comment).
- **Tests:** Spec-only; no code changed on the `GOV-46` worktree branch. The
  enums and field names cited (`ALLOWED_VERIFICATION_STATUSES`,
  `ALLOWED_UI_STATUSES`, `REVIEWED_VERIFICATION_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`,
  `ALLOWED_NODE/EDGE/CARD/LINK_TYPES`, `SCHEMA_VERSION =
  "gov-watchdog-card-map.v1"`) were read this run from
  `scripts/validate_concept_map_export.py` on the
  `gov-17-newsletter-briefing-contract` branch, which already enforces them. A
  future implementation issue must add the per-step DET/AI/validator tests named
  in §9 (Plan Before Implementation).
- **Coverage confirmation:** the matrix covers determinism vs AI containment
  (§1–§3), fail-closed gates and the single AI→public path through human
  promotion + deterministic publication gate (§1.2, §2, §5), `verificationStatus`
  / `uiStatus-map.v1` integration (§1 steps 11–12, §3), human-in-the-loop gates
  (§4), and the privacy boundary including AI-scratch (§7).
- **Predecessor readback (this run, via control plane):** GOV-46 `in_progress`
  (assigned to AutomationOpsEngineer), goal `9a60de55-…` Stage 1.09 `active`;
  GOV-43 (1.08) `done` with GOV-44/45 `done`; GOV-40 (1.07) `done` with
  GOV-41/42 `done`.

## Reviewer Lanes (agent sign-off, not board request_confirmation)

- **VerificationSafetyReviewer (`3f95c8ce`)** — AI-label/gate correctness, the
  single AI→public path, no-orphan-claims, no-wrong-attribution, fail-closed
  defaults.
- **CTO (`24fddc65`)** — technical contract sign-off: matrix completeness, the
  determinism/AI split, handoff field names, staging coherence, next-gate
  readiness (comment + status).
- **SecurityPrivacyAgent (`72d0eccf`)** — consulted on §7: AI-scratch and raw
  material vault-only, no private fields on public payloads.
- **CEO/Isaac** — stage unlocks, publication, official contact, AI-label /
  verification / publication policy, beyond-Alpine expansion, budget.

## Risk Classification (per RISK_ASSESSMENT_WORKFLOW)

- **Evidence/source risk:** touched; mitigated by DET-owned provenance, hashing,
  archive linking, and orphan-claim rejection.
- **AI-overclaim risk:** primary risk; mitigated by single non-reviewed entry
  status, mandatory AI labels, AI-never-writes-gating-fields, and the single
  AI→public path through human promotion + fail-closed publication gate.
- **Privacy/account risk:** touched; mitigated by vault-only raw + AI scratch, no
  private fields in public/API output, and gated-beta access states.
- **Defamation/legal/civic-harm risk:** touched; mitigated by no-name-over-wrong-
  name attribution, blocking unsupported claims, and keeping accusations/legal
  conclusions an owner gate.
- **Moderation/community risk:** touched; mitigated by default-deny on ambiguity
  and not exposing unreviewed AI drafts to unauthenticated users.
- **Publication/readiness risk:** touched; mitigated by keeping this planning-only
  and making every scheduler/AI-run/publication a locked, gated, owner-approved
  step.

## Next Action

If GOV-46 passes (VerificationSafetyReviewer + CTO sign-off), unlock only the
next sequential Stage 1 planning gate. Do not create any automation/AI
implementation, scheduler, or crawler issue from this contract unless CEO/CTO
creates a fresh, explicit Alpine-only implementation issue with blockers,
acceptance criteria, per-step tests, logs, and reviewer lanes.
