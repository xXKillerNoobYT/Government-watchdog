# Stage 1.07 Alpine Transcript / Evidence / Statement Model Contract

Issue: GOV-40
Owner role: TranscriptEvidenceEngineer
Stage: Stage 1.07, planning / specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only
Created: 2026-06-07

> **Spec-only.** This document is a contract, not an implementation. It creates no
> crawler, no transcript ingestion, no publication, no public UI/API, no official
> contact, and no scope beyond Alpine. Stage 1 implementation remains locked. See
> the **Locked Scope** section for the explicit not-authorized list.

---

## Gate Decision

GOV-40 passes when this document gives TranscriptEvidenceEngineer,
BackendCrawlerEngineer, FrontendTimelineEngineer, VerificationSafetyReviewer,
SecurityPrivacyAgent, and CTO a concrete, typed model contract for how an Alpine
meeting becomes — through deterministic, source-grounded, reviewer-gated steps — a
publishable statement with an exact-source pointer and a safe speaker label.

It deepens the Stage 0.07 base transcript/evidence/statement model (GOV-16) for
Alpine execution planning. It does **not** authorize building any of it.

The only downstream unlock is the next sequential Stage 1 planning gate. Any
implementation issue created later must explicitly consume this contract and name
its own narrow Alpine source set, commands, logs, tests, and reviewer gates.

---

## Inputs Read (predecessor evidence — daisy chain)

- Required agent instructions:
  `COMPANY.md`, `SOUL.md`, `TOOLS.md`, `HEARTBEAT.md`,
  `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`,
  `TRANSCRIPT_EVIDENCE_WORKFLOWS.md`, `STAGE0_EXECUTION_WORKFLOW.md`,
  `RISK_ASSESSMENT_WORKFLOW.md`, `GATED_BETA_ACCESS_WORKFLOW.md`,
  `AI_GATEWAY_PROCESSING_WORKFLOW.md`, `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`.
- Stage 0.07 base transcript/evidence/statement model: GOV-16 (backend project,
  goal `c3c3890f`) and the `government-concept-map-card-model.md` /
  `gov-watchdog-card-map.v1` concepts it seeds.
- Stage 1.05 backend/tooling contract:
  `Docs/stage1-backend-tooling-implementation-contract.md` (719 lines incl.
  GOV-36/37/38/39 patches; verification on its branch: 20 tests passed).
- Stage 1.06 frontend/product surface contract: GOV-35 (statement cards, source
  drawers, public/private states), goal `0d2e317f` (done).
- Authoritative status vocabulary: `uiStatus-map.v1` over the 6-value
  `verificationStatus` enum (GOV-36/37/38/39), enforced fail-closed in
  `scripts/validate_concept_map_export.py`
  (`ALLOWED_VERIFICATION_STATUSES`, `ALLOWED_UI_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`).
- Concept-map node/edge/card/link type registries:
  `ALLOWED_NODE_TYPES`, `ALLOWED_EDGE_TYPES`, `ALLOWED_CARD_TYPES`,
  `ALLOWED_LINK_TYPES` in `scripts/validate_concept_map_export.py`.
- Premium success-criteria framework (GOV-38):
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`.
- Isaac June 6 concept-map directive (separate concepts, typed links) — recorded
  in `TRANSCRIPT_EVIDENCE_WORKFLOWS.md`, reference GOV-36.

### Predecessor-evidence note (read before relying on this contract)

At the time of writing, the Stage 1.05/1.06 artifacts and the `uiStatus-map.v1`
validator (GOV-34/36/37/38/39) exist on the unmerged **`gov-17-newsletter-briefing-contract`**
branch, not on `main` or this `GOV-40` worktree. All enum and field-name
references below were read directly from that branch's
`scripts/validate_concept_map_export.py` and
`Docs/stage1-backend-tooling-implementation-contract.md`, so the names in this
contract match code that already enforces them. Any implementation issue spawned
from this contract must confirm those predecessor branches are merged (or
re-verify the enums) before coding, so the model is grounded in landed code and
not a branch that could still change.

---

## 1. Concept Boundaries & Typed Links

Per the Isaac concept-map directive: Government Watchdog is a real
government/concept graph, **not** a flat pile of AI summaries or cards. A meeting
does not become "a summary." It decomposes into separately-typed records that
each carry their own provenance and verification state, joined by *typed* edges.
Cards are presentation nodes over the graph; they are never the source of truth.

> `★ Concept-separation rationale ─────────────────`
> The failure mode this prevents: a single "AI meeting summary" blob where the
> claim, who said it, when, what document it cited, and whether it was verified
> are all fused into one paragraph that cannot be audited, corrected, or
> partially gated. Splitting into typed nodes means each fact has its own
> source pointer, its own `verificationStatus`, and can be individually
> corrected without rewriting the others.
> `─────────────────────────────────────────────────`

### 1.1 Typed concepts (nodes)

These map 1:1 to `ALLOWED_NODE_TYPES` already enforced by the export validator.
For Stage 1.07 (transcript/evidence/statement), the load-bearing nodes are
**meeting → agenda_item → transcript_segment → statement → person/role →
source_record/document → evidence_link**; the rest are referenced for edge
correctness.

| Node type (`node.type`) | What it is (Alpine context) | Owns these core fields (illustrative) |
|---|---|---|
| `jurisdiction` | Town of Alpine (and, scoped, Lincoln County for Alpine matters) | `jurisdiction_id`, `name`, `level` |
| `government_body` | Alpine Town Council, P&Z, etc. | `body_id`, `name`, `jurisdiction_id` |
| `meeting` | One recorded public meeting | `meeting_id`, `body_id`, `meeting_date`, `meeting_name`, `video_source_id` |
| `agenda_item` | One agenda line within a meeting | `agenda_item_id`, `meeting_id`, `order`, `title`, `agenda_doc_source_id` |
| `transcript_segment` | A timestamped span of transcript | `segment_id`, `meeting_id`, `timestamp_seconds`, `timestamp_human`, `segment_text`, `is_verbatim`, `confidence`, `transcript_path` |
| `statement` | A single claim/utterance asserted at a moment | `statement_id`, `segment_id`, `agenda_item_id`, `speaker_attribution_id`, `statement_text`, `is_verbatim`, `verificationStatus`, `correctionStatus`, `layer` |
| `person` | A real individual | `person_id`, `display_name` (gated; see §3) |
| `role` | An office/role held during a date range | `role_id`, `body_id`, `title`, `start_date`, `end_date` |
| `source_record` | A registry-tracked source (video, agenda PDF, minutes) | `source_id`, `url`, `original_url`, `source_class`, `source_type`, `jurisdiction`, `scan_date`, `archive_status`, `archive_url`, `verification_status`, `correction_status`, `raw_preservation_status`, `local_note_path` |
| `document` | A specific document (ordinance, resolution, packet) | `document_id`, `source_id`, `doc_type`, `doc_date` |
| `evidence_link` | A typed join from a statement/card to its substantiating source pointer | `evidence_link_id`, `from_node_id`, `to_source_id`, `pointer` (see §2), `relation` |
| `vote` | A recorded vote | `vote_id`, `meeting_id`, `agenda_item_id`, `tally` |
| `decision` | An adopted decision/ordinance/resolution outcome of a vote | `decision_id`, `vote_id`, `document_id` |
| `outcome` | A later real-world result that updates an earlier event | `outcome_id`, `updates_node_id`, `outcome_date` |
| `topic` | An issue/theme (sewer, lodging tax, WWTP financing) | `topic_id`, `name` |
| `card` | A presentation node over the graph | `card.type` ∈ `ALLOWED_CARD_TYPES` |

### 1.2 Typed relationships (edges)

These map 1:1 to `ALLOWED_EDGE_TYPES`. A relationship must be one of these typed
edges — never an untyped "related to."

| Edge type (`edge.type`) | Reads as | Endpoints |
|---|---|---|
| `contains_body` | jurisdiction contains body | jurisdiction → government_body |
| `held_meeting` | body held meeting | government_body → meeting |
| `contains_agenda_item` | meeting contains agenda item | meeting → agenda_item |
| `references_source` | agenda item references document/source | agenda_item → source_record/document |
| `served_in_role` | person served in role during date range | person → role |
| `role_in_body` | role exists within a body | role → government_body |
| `statement_from_segment` | statement is extracted from a transcript segment | statement → transcript_segment |
| `made_statement` | person made statement at a timestamp | person → statement |
| `voted_on` | body voted on a decision | government_body/meeting → vote |
| `vote_decided` | vote produced a decision | vote → decision |
| `decision_affects` | decision affects topic/place/entity | decision → topic/place_asset/entity |
| `source_supports` | source substantiates a card/claim | source_record → statement/card |
| `document_supersedes` / `document_amends` / `document_replaces` | document lifecycle | document → document |
| `outcome_updates` | later outcome updates a prior event without rewriting it | outcome → (any earlier node) |
| `topic_groups` | topic groups related nodes | topic → node |
| `card_presents` | card presents a graph node | card → node |
| `card_links_card` | card links to a related card | card → card |

### 1.3 The canonical Alpine chain

The Stage 1.07 spine, expressed in typed edges:

```
jurisdiction(Alpine)
  --contains_body--> government_body(Alpine Town Council)
    --held_meeting--> meeting(2026-05-08 Regular Meeting)
      --contains_agenda_item--> agenda_item("WWTP financing")
         (agenda_item --references_source--> source_record(agenda packet PDF))
  meeting --(implicit timeline)--> transcript_segment(00:42:13, verbatim span)
    <--statement_from_segment-- statement("the financing gap is $X")
       person(role-attributed or unattributed) --made_statement--> statement
       source_record(video) --source_supports--> statement   (via evidence_link, §2)
       statement --(supports/contradicts)--> decision/vote     (typed via decision edges)
  outcome(2026-09 actual bond rate) --outcome_updates--> statement/decision
```

No node in this chain may be collapsed into another. A statement without a
`statement_from_segment` edge (or an equivalent non-transcript source pointer) is
an **orphan claim** and is rejected (§2).

### 1.4 Statement relationship semantics (supports / contradicts / references)

A statement's relationship to other graph nodes is itself typed, so the timeline
can show *how* a statement bears on a decision rather than just listing it:

- **statement references document/source** — carried by an `evidence_link` with
  `relation: "references"` to a `source_record`/`document`.
- **statement supports decision** — `evidence_link` with `relation: "supports"`
  to a `decision`/`vote`.
- **statement contradicts decision/earlier statement** — `evidence_link` with
  `relation: "contradicts"`. A `contradicts` link never deletes or rewrites the
  contradicted node; it links forward (consistent with `outcome_updates`
  semantics in §4).

`relation` is an enumerated field on `evidence_link`:
`references | supports | contradicts | corrects | substantiates`. It is distinct
from the edge *type* (`source_supports`) so the graph can carry both "this source
backs this statement" (provenance) and "this statement, per a reviewer, supports
that decision" (analysis). Analysis-type relations (`supports`/`contradicts`)
require a reviewed `verificationStatus` before they may surface publicly (§5).

---

## 2. Exact-Source Pointer Contract

**No orphan claims.** Every `statement` and every `evidence_link` carries a
pointer to the exact location in an original source. A deep link alone is not
evidence; the full pointer record is.

### 2.1 The `pointer` object (required on every evidence_link)

```json
{
  "source_id": "alpine:video:2026-05-08-regular",
  "original_url": "https://...",
  "final_url": "https://...",
  "wayback_url": "https://web.archive.org/web/.../...",
  "archive_status": "available",
  "scan_date": "2026-05-10",
  "captured_at_utc": "2026-05-10T17:04:22Z",
  "source_type": "meeting_video",
  "source_class": "alpine-official",
  "source_authority_level": "primary_official",
  "jurisdiction": "alpine",
  "locator_kind": "timestamp",
  "timestamp_seconds": 2533,
  "timestamp_human": "00:42:13",
  "page": null,
  "section": null,
  "agenda_item_id": "alpine:2026-05-08:item-7",
  "transcript_path": "<vault>/Source-Data/transcripts/2026-05-08_alpine-council.json",
  "deep_link": "https://...?t=2533",
  "is_verbatim": true,
  "verificationStatus": "reviewed_source_linked",
  "correctionStatus": "not_applicable",
  "confidence": "high"
}
```

### 2.2 Field rules

| Field | Rule |
|---|---|
| `source_id` | Required. Must resolve to a `source_record` that passed the Stage 1.05 source-registry input contract. |
| `original_url` | Required. The source as first published. |
| `final_url` | Required when a redirect/canonical was observed; else null. |
| `wayback_url` / `archive_status` | Required pair. `archive_status` ∈ {`available`, `unavailable`, `not_checked`}. If the live source is gone and no archive/preserved artifact exists, the statement is gated `source-missing` (§5). |
| `scan_date`, `captured_at_utc` | Required. Human scan date + machine capture timestamp. |
| `source_type` | Required. e.g. `meeting_video`, `meeting_audio`, `agenda_pdf`, `minutes_pdf`, `ordinance_pdf`. |
| `source_class` | Required. Approved Stage 1 Alpine classes only (`alpine-official`, `lincoln-county-alpine`). |
| `source_authority_level`, `jurisdiction` | Required. |
| `locator_kind` | Required. One of `timestamp`, `page`, `section`, `paragraph`. Selects which locator field is authoritative. |
| `timestamp_seconds` + `timestamp_human` | Required when `locator_kind == timestamp`. Integer seconds from start + `HH:MM:SS`. |
| `page` / `section` | Required when `locator_kind` is `page`/`section`. |
| `agenda_item_id` | Required when the statement sits under an agenda item. |
| `transcript_path` | Required for transcript-derived pointers. Local/vault path; never a public field (§7). |
| `deep_link` | Derived (`{source_url}?t={timestamp_seconds}` or equivalent embed). Convenience only; the locator fields are authoritative. |
| `is_verbatim` | Required. `true` = exact quote; `false` = AI-paraphrased/summarized (must carry the matching label, §5). |
| `verificationStatus`, `correctionStatus`, `confidence` | Required. Drive `uiStatus` (§5). |

### 2.3 Orphan rejection rule

A `statement` is rejected at validation if it has no `evidence_link` with a
complete `pointer`, OR if `locator_kind` is set but the matching locator field is
absent (e.g. `locator_kind: timestamp` with null `timestamp_seconds`). A pointer
whose `source_id` does not resolve to a registry `source_record` is rejected.
This is the model-level expression of the company non-negotiable *"no orphan
claims"* and the Stage 1.05 *"a link alone is not evidence"* rule.

---

## 3. Speaker Attribution Rule

**No name is better than wrong speaker attribution.** Speaker identity is
*never* guessed from voice, context, or community knowledge. Attribution is a
separately-typed record (`speaker_attribution`) joined to a `statement`, so the
attribution can be uncertain or withheld without weakening the statement's
source pointer.

### 3.1 The `speaker_attribution` record

```json
{
  "speaker_attribution_id": "alpine:2026-05-08:seg-1043:spk",
  "statement_id": "alpine:2026-05-08:stmt-1043",
  "attribution_state": "attributed",
  "speaker_class": "on-record-official",
  "person_id": "person:alpine:...",
  "role_id": "role:alpine:council-seat-3",
  "display_label": "Council Member, Town of Alpine",
  "basis": "named in official minutes item 7; voice matched to roll-call order",
  "minutes_source_id": "alpine:minutes:2026-05-08",
  "reviewer_state": "approved",
  "confidence": "high"
}
```

### 3.2 `attribution_state` (the explicit uncertain/withheld states)

| `attribution_state` | Meaning | Renders as |
|---|---|---|
| `attributed` | Identity established from official records and within attribution rules | name/role per `speaker_class` rules below |
| `uncertain` | A candidate identity exists but is not confirmed by official records | **never the candidate name**; renders as the safe generic label + a visible "speaker not confirmed" note |
| `unattributed` | No identity basis at all | safe generic label (`Community Member` / `Meeting Attendee`); no name, no candidate |

`uncertain` and `unattributed` both **fail closed to a generic label**. The
candidate name in an `uncertain` record is stored for reviewer context only and
is a private field (§7); it never reaches the frontend.

### 3.3 `speaker_class` (attribution permission)

Carried over verbatim from `TRANSCRIPT_EVIDENCE_WORKFLOWS.md`:

| `speaker_class` | Attribution rule |
|---|---|
| `on-record-official` | Named officials speaking in official capacity at a public meeting. May be attributed with name + role. |
| `on-record-public` | Community member named in official minutes. May be attributed **only after CEO approval**. |
| `unidentified` | Not identified in official records → `Community Member`/`Meeting Attendee`. Never guess. |
| `private-context` | Any non-public-meeting context. Never attribute. |

### 3.4 Interaction with the rest of the model

- A statement with `attribution_state` ∈ {`uncertain`, `unattributed`} is still a
  valid, publishable statement *as to its content* (it keeps its source pointer
  and `verificationStatus`); only the **speaker label** is gated.
- The `made_statement` edge from a `person` node may exist **only** when
  `attribution_state == attributed` and (`speaker_class == on-record-official`
  OR `on-record-public` with recorded CEO approval). Otherwise the statement has
  no `person` edge — it is anchored to the `transcript_segment` only.
- Naming any `on-record-public` speaker is a **hard stop**: route to CEO before
  the `made_statement` edge or display name is created.

---

## 4. Known-Then vs Later Layers

The product promise (COMPANY.md mission) is that users see *what was known then,
what was presented then, what AI thought then, what was corrected later, and what
actually happened later* — without later knowledge silently rewriting the record.

### 4.1 The `layer` field

Every `statement` and every `evidence_link` carries a `layer` enum. Layers are
**append-only**: a later layer never edits or deletes an earlier one; it links
forward via `outcome_updates` / `evidence_link.relation: corrects`.

| `layer` | Meaning | Example |
|---|---|---|
| `known_then` | What the record/source actually said at the meeting date | "Staff stated the WWTP gap was $X." |
| `presented_then` | How it was framed/presented at the time (may differ from raw fact) | "Presented as a routine financing item." |
| `ai_thought_then` | What AI extraction/analysis proposed at processing time | AI draft: "implies a tax increase" (label `AI-generated`, gated) |
| `corrected_later` | A correction applied after a known-then error was found | "Corrected 2026-06: figure was $Y, minutes erratum." |
| `actual_later` | What actually happened later (real-world outcome) | "Bond closed at rate R in 2026-09." |

### 4.2 Forward-only rule

- `corrected_later` and `actual_later` records carry their own date
  (`correction_date` / `outcome_date`) and an `outcome_updates` (or
  `evidence_link.relation: corrects`) edge to the `known_then` node they update.
- The `known_then` node is **never mutated**. The frontend renders the original
  alongside the correction/outcome with the correction notice, so the historical
  record (what was known then) remains intact and auditable.
- `ai_thought_then` is always a *separate* layer and always carries an AI label
  (§5); it is never merged into `known_then` and never publishes by default.

> `★ Why append-only layers ───────────────────────`
> If a correction overwrote the original statement, the site would silently
> become "what we now know" and lose the civic-accountability value: showing
> that an official *said X at the time*, even if X was later corrected. The
> `outcome_updates` edge is the structural guarantee that "later" links forward
> instead of erasing "then."
> `─────────────────────────────────────────────────`

---

## 5. verificationStatus Integration (uiStatus-map.v1)

Statements and evidence map onto the **authoritative 6-value `verificationStatus`
enum** and the **`uiStatus-map.v1`** display mapping. This contract does **not**
introduce a new status vocabulary; it binds the transcript/evidence/statement
records to the existing one (`scripts/validate_concept_map_export.py`).

### 5.1 Authoritative `verificationStatus` (6 values — `ALLOWED_VERIFICATION_STATUSES`)

`source_recorded`, `machine_extracted_unreviewed`, `reviewed_source_linked`,
`human_verified`, `disputed`, `do_not_publish`.

- A freshly transcribed statement starts at `machine_extracted_unreviewed`
  (whisper/local-model output) or `source_recorded` (registered but not yet
  extracted). Both are **non-reviewed**.
- A reviewer promotes to `reviewed_source_linked` / `human_verified` only after
  confirming the verbatim text matches the source at the pointer (the Evidence
  Quality Review workflow).
- `disputed` / `do_not_publish` are set by VerificationSafetyReviewer/CEO.

`reviewed = verificationStatus ∈ {reviewed_source_linked, human_verified}`
(`REVIEWED_VERIFICATION_STATUSES`).

### 5.2 Display mapping (`uiStatus`, 10 values, `compute_ui_status`)

The backend emits a typed `uiStatus` per record using `uiStatus-map.v1`
(rules #1–#12, first match wins, fail-closed default `pending-review`). The
frontend renders `uiStatus` verbatim and must not re-derive trust from
`card.type` or label text. Inputs: `verificationStatus`, `correctionStatus`,
`sourceChanged`, `sourcePresent`, `archivePresent`, `rawPreserved`.

Statement/evidence-specific reading of the existing rules:

| Situation for a transcript statement | Resulting `uiStatus` | Public? |
|---|---|---|
| Reviewer blocked it | `do-not-publish` | never |
| Claim actively contested | `disputed` | gated |
| Video gone, no archive, no preserved raw | `source-missing` | gated |
| Source video changed since review (hash differs) | `source-changed` | gated |
| Reviewed + correction applied/resolved | `corrected` | **eligible** |
| Open clarification on the statement | `needs-clarification` | gated |
| Whisper/AI extraction, not yet reviewed (`machine_extracted_unreviewed`) | `unverified` | gated |
| Registered, not yet reviewed (`source_recorded` / null) | `pending-review` | gated |
| Reviewed, live video gone but archive/raw backs it | `archived-source-backed` | **eligible** |
| Reviewed + live video present | `source-backed` | **eligible** |

### 5.3 Fail-closed publication allowlist (`PUBLICATION_ELIGIBLE_UI_STATUSES`)

A statement/evidence record may carry `publicExportApproved == true` **only**
when its computed `uiStatus` ∈ {`source-backed`, `archived-source-backed`,
`corrected`}. Every other (and any future) `uiStatus` is gated by default. All
three eligible states require a *reviewed* underlying `verificationStatus` by
construction of the map, so **no non-reviewed transcript statement can publish**.

### 5.4 AI / paraphrase labeling

- `is_verbatim == false` ⇒ the record carries an `AI-paraphrased` or
  `AI-summarized` label and the `ai_thought_then` layer where applicable. An AI
  paraphrase is **never** presented as a verbatim quote.
- AI-extracted statements are `machine_extracted_unreviewed` until a human
  review promotes them; they render `unverified` and are publication-gated.
- This binds the AI Gateway Processing Workflow rule "AI output is never primary
  evidence" to the statement model: AI output is a draft `statement` with source
  anchors, confidence, and reviewer state — not a fact.

---

## 6. Backend ↔ Frontend Handoff

Aligns with GOV-35 (Stage 1.06 frontend/product surface contract) field names:
**statement cards**, **source drawers**, **public/private states**.

### 6.1 What the backend produces

For each reviewed-and-eligible statement, the backend emits a statement record +
its evidence links + provenance, surfaced through `gov-watchdog-card-map.v1` as a
`card` of `card.type: statement` (and supporting `source`, `correction`,
`ai_analysis` cards). Each card carries:

- `id`, `type`, `title`, `primaryNodeId`
- `verificationStatus` (6-value), `correctionStatus`
- `uiStatus` (10-value, backend-computed), `statusLabel` (human, **not**
  trust-bearing)
- `sourceCount`, typed `links[]` (`ALLOWED_LINK_TYPES`)
- `publicExportApproved` (allowlist-gated, §5.3)
- the **source-drawer payload**: the §2 `pointer` object(s), `speaker_label`
  (from §3, generic when uncertain/unattributed), `layer`, `is_verbatim`,
  `confidence`, `label`.

### 6.2 What the frontend consumes

- **Statement card**: renders `statement_text` (or the verbatim quote),
  `speaker_label`, the `uiStatus` badge, and the `layer` framing. Keys
  badge/gating on `uiStatus` only.
- **Source drawer**: renders the pointer (original URL, scan date, source type,
  jurisdiction, archive link, timestamp/page/section, deep link) so a resident
  can trace one card to its exact source moment.
- **Public/private states**: must render empty, loading, error, pending-review,
  unverified, disputed, corrected, source-missing, source-changed, and
  do-not-publish states with visible labels. Visual polish must never imply
  verification.

### 6.3 Handoff invariants (from BACKEND_FRONTEND_EVIDENCE_WORKFLOW)

- Backend may not call a statement frontend-ready without a resolving source
  pointer and a publication/access state.
- Frontend may not create a public claim from a field that is AI-only,
  unverified, disputed, private, or pending review.
- Any mismatch between backend evidence state and frontend display reopens the
  relevant Paperclip gate.
- Field names are the contract: a rename on either side (e.g. `uiStatus`,
  `speaker_label`, `pointer`) requires a coordinated patch to both this contract
  and GOV-35, not a silent divergence.

---

## 7. Privacy / Data Boundary

Raw and unreviewed transcripts and media are **local/vault-only**. Only reviewed,
website-ready statements surface, and only after gates pass.

### 7.1 Stays local / vault-only (never GitHub-public, never public UI/API)

- Raw meeting video/audio files and full raw transcripts
  (`<vault>/Source-Data/transcripts/YYYY-MM-DD_*.json`).
- `transcript_path`, `local_note_path`, raw artifact paths, `sha256` of raw
  bytes, run logs.
- Reviewer-only fields: attribution `basis`, the `uncertain` candidate name,
  reviewer notes, `reviewer_state` rationale.
- Any private identity/address/voter-registry/account-validation data — these
  must never enter a statement, attribution, or card record at all.

### 7.2 May surface (only when reviewed + allowlisted)

- The statement text / verbatim quote, generic-or-approved `speaker_label`,
  `uiStatus`, `statusLabel`, `layer`, `confidence`, `label`, and the **public
  subset** of the pointer: original/current URL, scan date, source type,
  authority level, jurisdiction, archive link, timestamp/page/section, deep link.

### 7.3 Boundary rules

- A statement may surface only when `publicExportApproved == true` (⇒ reviewed +
  allowlisted `uiStatus`, §5.3).
- No public accusation, motive claim, legal conclusion, or campaign wording is
  derivable from this model's public fields; statements carry source pointers,
  not verdicts.
- The export validator must drop/never-emit the §7.1 private fields on any
  public surface; a private field reaching a public payload is a validation
  failure that blocks the export.

---

## 8. Premium Success-Criteria (GOV-38 template, completed)

> Completed using the paste-in template from
> `2026-06-06-Premium-Success-Criteria-Framework.md`. This is the substantial
> design-reasoning block the issue requires.

```markdown
## GOV Premium Success Criteria

Stage: Stage 1.07 (Alpine transcript/evidence/statement model contract) — planning/spec only
Scope: Town of Alpine only
Project/repo: xXKillerNoobYT/Government-watchdog / 0a1832c4-1556-49a1-bcc5-857f2ca72962
Owner role: TranscriptEvidenceEngineer
Reviewer path: VerificationSafetyReviewer (label/gate correctness) -> CTO (technical contract sign-off)
Blockers / unlock rule: Consumes Stage 0.07 (GOV-16), Stage 1.05 (GOV-34 + GOV-36/37/38/39), Stage 1.06 (GOV-35). Unlocks only the next sequential Stage 1 planning gate; unlocks no implementation.

### Success Definition
- Success means: a reviewer or implementer can take one Alpine meeting and, using only this contract, know exactly which typed records to create (meeting -> agenda_item -> transcript_segment -> statement -> speaker_attribution -> evidence_link with pointer), how each maps to verificationStatus/uiStatus, when a name may be attached, how known-then is kept separate from later outcomes, and which fields may ever reach the public site.
- Evidence proving success: this file at Docs/stage1-transcript-evidence-statement-contract.md; field/enum names match scripts/validate_concept_map_export.py (ALLOWED_NODE_TYPES, ALLOWED_EDGE_TYPES, ALLOWED_VERIFICATION_STATUSES, ALLOWED_UI_STATUSES, PUBLICATION_ELIGIBLE_UI_STATUSES); reviewer + CTO sign-off comments on GOV-40.

### Failure Definition
- Failure looks like: a statement modeled without an exact-source pointer (orphan claim); a guessed speaker name on an uncertain/unattributed record; an AI paraphrase modeled as a verbatim quote; a correction that overwrites the known-then record; a non-reviewed statement reaching publicExportApproved; a private field (transcript_path, candidate name, address) in a public payload; or a status vocabulary invented here instead of reusing uiStatus-map.v1.
- Stop/escalation trigger: any need to name an on-record-public speaker, publish, contact officials, expand beyond Alpine, make a legal/privacy judgment on a specific individual, or change the AI-label/verification policy -> stop, route to CEO.

### Workability
- Real user/operator workflow: (operator) TranscriptEvidenceEngineer ingests an Alpine meeting video confirmed by SourceArchivist, generates a timestamped transcript locally, extracts statements as machine_extracted_unreviewed, attaches pointers, sets attribution_state; VerificationSafetyReviewer reviews each statement against the source at the pointer and promotes verificationStatus; only allowlisted uiStatus statements become publicExportApproved. (resident) opens a statement card, reads the claim + speaker_label + uiStatus badge, opens the source drawer, and clicks through to the exact timestamp.
- Inputs: confirmed Alpine source_record (registry-passing), raw video/audio (vault-only), local transcript.
- Outputs: typed statement/segment/attribution/evidence_link records; card-map cards; source-drawer payloads.
- Missing/stale/disputed source behavior: source-missing (no live + no archive/raw) and source-changed (hash differs) are gated uiStatus, never public; disputed renders dispute label only; do-not-publish never renders.
- Resume/retry behavior: ingestion writes a dated artifact beside any prior one and never overwrites known-then; an interrupted run re-derives from the registry + existing artifacts by source_id and resumes at the first un-pointered statement.

### Ease of Use
- Resident/Isaac comprehension target: in 30 seconds a resident understands "who said what, when, at this exact source moment, and how trustworthy it is" — without technical explanation.
- Labels/statuses/gaps visible: uiStatus badge, Verbatim/AI-paraphrased label, speaker label (generic when not confirmed), correction notice, and source-missing/changed gaps are all visible; nothing trust-bearing is hidden.
- Required screenshot/prototype/wireframe/review note: defers UI artifacts to GOV-35 (Stage 1.06 frontend contract) which owns statement-card + source-drawer wireframes; this contract supplies the field/state inventory those wireframes must render.

### Comparable Research
- Comparable tools reviewed:
  - DocumentCloud (https://www.documentcloud.org/) — primary-source document mgmt, annotation, page-anchored highlights.
  - GovTrack (https://substack.govtrack.us/about) — votes/representatives/civic tracking.
  - Open States (https://docs.openstates.org/api-v3/) — structured jurisdiction/bill/vote/sponsor/action/committee/legislator data via API.
  - Granicus / govMeetings (https://granicus.com/solution/govmeetings) — government meeting agendas, minutes, and video portals with agenda-anchored video.
- Lessons GOV should use: page/timestamp-anchored evidence (DocumentCloud highlights, Granicus agenda->video deep links) validates the §2 exact-source pointer; Open States validates separating jurisdiction/body/meeting/vote/decision/person as typed records rather than one blob.
- Patterns GOV should avoid: Granicus-style portals present official video as authoritative with no "what was corrected later"/AI-label layer and no uncertain-speaker handling; legislative tools (GovTrack/Open States) model only named, on-record legislators — they have no equivalent of the unattributed/uncertain community-member safety rule GOV requires for open-mic public comment.
- Source links: as listed above.

### Tradeoffs
- Main tradeoffs: AI summarization speed vs human verification (chosen: AI is draft-only, gated unverified until reviewed); simple cards vs concept-map integrity (chosen: typed graph, cards are presentation only); local Alpine clarity vs premature WY/US generalization (chosen: Alpine-only); raw preservation vs public-data boundary (chosen: raw vault-only, only reviewed fields public).
- Chosen approach and reason: typed concept graph + fail-closed publication, because civic-evidence credibility depends on auditability and not over-claiming; speed and convenience are subordinate to "no orphan claims" and "no wrong attribution."

### Plan Before Implementation
- Concept/data model: §1 (nodes/edges), §2 (pointer), §3 (attribution), §4 (layers), §5 (status binding).
- UI/operator behavior: §6 (backend produces / frontend consumes) — defers visual artifacts to GOV-35.
- Verification commands or review steps: future implementation issue adds a statement/evidence fixture to scripts/validate_concept_map_export.py's test set and the Evidence Quality Review workflow checklist; this contract is spec-only (no code changed).
- Artifact paths: this file; future: Exports/alpine/concept-map.cards.json (validated), vault-only transcripts/run logs.
- Failure handling: orphan-claim rejection (§2.3), fail-closed uiStatus default, fail-closed publication allowlist (§5.3), private-field drop (§7.3).

### Source and Auditability
- Required source fields: §2.1/§2.2 pointer (source_id, original_url, scan_date, source_type, source_class, authority, jurisdiction, archive, locator).
- Local source-data paths: <vault>/.../Source-Data/transcripts/ and source-registry notes (vault-only).
- Archive/Wayback/timestamp/page requirements: §2 — wayback_url + archive_status required; timestamp/page/section per locator_kind.
- Verification/correction status handling: §5 — 6-value verificationStatus -> uiStatus-map.v1 -> publication allowlist.

### Timeline and Concept Integrity
- Known-then vs later-outcome handling: §4 append-only layers + outcome_updates forward links; known-then never mutated.
- Correction handling: corrected_later layer + evidence_link.relation: corrects; corrected uiStatus is reviewed-gated.
- Concept records kept separate: §1.1 — all 18 node types distinct; cards are presentation nodes.
- Required typed relationships: §1.2 — made_statement, statement_from_segment, references_source, source_supports, decision_affects, outcome_updates, etc.

### Acceptance Evidence
- Required artifacts: this contract file (path + line count).
- Required tests/checks: spec-only, no code changed on this branch; note that the referenced validator already enforces the cited enums; future implementation issue must add a statement/evidence fixture + parity test.
- Required issue/PR/screenshot/API/source evidence: GOV-40 disposition comment with reviewer (VerificationSafetyReviewer) + CTO sign-off path.
```

---

## 9. Locked Scope (what Stage 1.07 / GOV-40 does NOT authorize)

This contract is planning/specification only. It explicitly does **not** authorize:

- implementing any transcript ingestion, extraction, card-map export, or
  validator code (it is spec-only; no code changed on this branch);
- running any crawl, transcription, or live source fetch;
- publishing any statement, quote, transcript segment, or card to the public
  website, newsletter, API, or any public surface;
- creating any `made_statement` edge or display name for an `on-record-public`
  (community-member) speaker — that is a CEO hard stop;
- attaching any real person's name on an `uncertain`/`unattributed` record;
- using external AI APIs to process meeting audio;
- changing the AI-label or verification/publication policy
  (`verificationStatus` / `uiStatus-map.v1` / publication allowlist are owned by
  GOV-36/37/38/39 and may not be redefined here);
- contacting Alpine/Lincoln County officials or government systems beyond
  ordinary public web access;
- any legal, defamation, campaign, budget, donation, or privacy judgment about a
  specific individual;
- expanding scope beyond the Town of Alpine.

Any of the above is an **owner-escalation trigger**: stop and route to CEO.

---

## Verification Evidence

- **File:** `Docs/stage1-transcript-evidence-statement-contract.md`
  (line count recorded in the GOV-40 closeout comment).
- **Tests:** Spec-only; no code changed on the `GOV-40` worktree branch.
  The enums and field names cited (`ALLOWED_NODE_TYPES`, `ALLOWED_EDGE_TYPES`,
  `ALLOWED_VERIFICATION_STATUSES`, `ALLOWED_UI_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`) were read directly
  from `scripts/validate_concept_map_export.py` on the
  `gov-17-newsletter-briefing-contract` branch, which already enforces them
  (predecessor evidence: 20 tests passed on that branch). A future
  implementation issue must add a statement/evidence fixture and an enum⇄model
  parity test to that validator's test set.
- **Coverage confirmation:** concept separation (§1), typed links (§1.2),
  exact-source pointer / no orphan claims (§2), speaker attribution rule with
  explicit `unattributed`/`uncertain` states (§3), known-then vs later layers
  (§4), `verificationStatus`/`uiStatus-map.v1` integration with fail-closed
  publication (§5), backend↔frontend handoff aligned to GOV-35 (§6), and the
  privacy/data boundary (§7) are all covered. Premium success-criteria template
  completed (§8). Locked-scope section present (§9).

## Reviewer Lanes

- **VerificationSafetyReviewer** (`3f95c8ce`): label/gate correctness — attribution
  states, AI/verbatim labeling, `verificationStatus`→`uiStatus` mapping,
  fail-closed publication. **Verdict: APPROVE** (GOV-41, `done`) — enums/gates
  verified against the enforcing `compute_ui_status` code, not the prose.
- **CTO** (`24fddc65`): technical contract sign-off — typed-model coherence with
  the concept-map registries and Stage 1.05/1.06 contracts, field-name alignment,
  next-gate readiness. Agent sign-off via comment + status, not a board
  `request_confirmation` (per WORKFLOW_GOVERNANCE). **Verdict: APPROVE**
  (GOV-42, `done`) — node/edge/card/link/status registries verified 1:1 against
  `scripts/validate_concept_map_export.py`.

## Carry-Forward for the Downstream Implementation Gate (non-blocking)

Recorded from the two APPROVE sign-offs so the next Stage 1 planning/implementation
gate inherits them. **Neither blocks GOV-40** and neither is authorized here:

1. **Raw-content SHA-256 must become an explicit `source_record` field.** The hash
   drives the `sourceChanged` → `source-changed` gate (§5.2) but is currently only
   referenced as a vault-only field (§7.1), not declared on the §1.1
   `source_record` schema. The implementation issue should add it to the Stage 1.05
   source registry so `sourceChanged` is deterministically computable. The pointer
   object (§2.1) correctly keeps the hash itself private.
2. **Exact public-facing AI-label display strings** (e.g. "AI-generated — not
   independently verified") are owned at render time by GOV-35 and must be
   applied/audited when the frontend is built. This model contract correctly
   mandates only that a label + layer exist, not the display copy.
3. **Predecessor-branch merge-or-re-verify gate.** The enums/validator and the
   Stage 1.05/1.06 artifacts live on the unmerged `gov-17-newsletter-briefing-contract`
   branch. No implementation issue may be spawned from this contract until those
   are merged to `main` or re-verified against the implementation branch.

## Risk Classification

- **Evidence/source risk:** touched; mitigated by the required exact-source
  pointer and orphan-claim rejection (§2).
- **AI-overclaim risk:** touched; mitigated by the `ai_thought_then` layer,
  `is_verbatim` labeling, and AI-extracted statements being gated `unverified`
  until reviewed (§4, §5).
- **Privacy/account risk:** touched; mitigated by vault-only raw/transcript data,
  reviewer-only private fields, and the public-field allowlist (§7).
- **Defamation/legal/civic-harm risk:** touched; mitigated by "no name is better
  than wrong attribution" with explicit uncertain/unattributed states (§3) and by
  surfacing pointers not verdicts (§7.3).
- **Moderation/community risk:** touched indirectly; community-member naming is a
  CEO hard stop (§3.3).
- **Publication/readiness risk:** touched; mitigated by keeping this planning-only
  and binding publication to the fail-closed allowlist (§5.3, §9).

## Next Action

If GOV-40 passes (VerificationSafetyReviewer + CTO sign-off), it unlocks only the
next sequential Stage 1 planning gate. No implementation, crawl, ingestion, or
publication issue may be created from this contract unless CEO/CTO opens a fresh,
explicit Alpine-only implementation issue with blockers, acceptance criteria,
tests, and evidence that names this contract as its input.
