# Stage 0.07 Transcript / Evidence / Statement Model Contract

Issue: GOV-250
Owner role: TranscriptEvidenceEngineer
Stage: Stage 0.07, planning / specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / Government Watchdog Backend
Scope marker: Town of Alpine only
Created: 2026-06-18

> Contract-only gate. This document defines the minimum transcript, evidence,
> statement, speaker, correction, and downstream handoff model that future backend
> and frontend work must honor. It does not implement ingestion, crawler/API/UI,
> database migrations, newsletter output, public launch, official contact, legal
> conclusions, campaign messaging, budget decisions, or expansion beyond Alpine.

---

## 1. Input Gate Read Before Drafting

The Stage 0.07 gate consumes the repaired Stage 0.05 and Stage 0.06 chain.

Required predecessor evidence read for GOV-250:

- GOV-248 final Stage 0.06 gate result: comment
  `6c775d2f-bf7b-4022-a8a3-5314c37d2850`.
- GOV-249 Stage 0.06 repair closeout: comments
  `19c24261-a2c5-40e8-b1f1-5748cbd87809` and
  `58562681-a601-4c3b-a276-3a024be2e1fa`.
- Stage 0.05 repaired backend/source-registry contract:
  `Docs/stage0-backend-tooling-implementation-contract.md`.
- Existing deeper transcript/evidence contract available in this repo:
  `Docs/stage1-transcript-evidence-statement-contract.md`.
- Active Stage 0.07 Paperclip goal text via
  `/api/goals/c3c3890f-8136-4c01-bca3-82c441eaff83`.

Carried-forward gates from GOV-248/GOV-249:

- Stage 0.06 passed only after the frontend/product contract explicitly inherited
  GOV-245 / PR #45 source-registry fields and adversarial gates.
- Future surfaces must carry owner/reviewer accountability, verification,
  correction, review, publication, UI state, lifecycle/replacement, archive,
  public-safety, duplicate, unsupported-claim, stale-output, and private/raw
  exclusion rules.
- Stage 0.07 is allowed only as Alpine-only planning/contract work.
- Stage 0.07 does not unlock Stage 1+, public launch, official-contact
  automation, legal/campaign/budget/publication approval, UI/API/crawler/database
  implementation, raw/public data release, or expansion beyond Alpine.

---

## 2. Gate Decision

Stage 0.07 passes when a reviewer can answer these questions from this contract
without inventing policy:

- What is a transcript record?
- What is an evidence citation?
- What is a statement record?
- Which exact source fields are required before a statement may exist?
- How are speakers named, withheld, or marked uncertain?
- How are direct quotes, paraphrases, AI-extracted claims, minutes attributions,
  and reviewer notes separated?
- How are known-then, presented-then, AI-thought-then, corrected-later, and
  actual-later records kept separate?
- Which records may leave local/vault storage?
- Which fields may be handed to product/frontend surfaces?
- Which negative/adversarial cases fail closed?

The only downstream unlock is the next sequential Stage 0 planning gate, Stage
0.08 newsletter/briefing/editorial behavior. Any implementation work must be a
separate issue with its own blockers, tests, artifacts, and review lanes.

---

## 3. Model Overview

The model separates civic evidence into typed records. Cards, summaries, and
newsletter text are presentation surfaces. They are not claim authority.

Canonical chain:

```text
source_record
  -> meeting
    -> agenda_item
      -> transcript_record
        -> transcript_segment
          -> statement_record
            -> evidence_citation
            -> speaker_reference
            -> correction_record when corrected/disputed/superseded
            -> frontend_handoff only when reviewed and public-eligible
```

The chain is intentionally verbose because civic claims need audit trails. A
future resident-facing statement must be traceable back to source owner,
reviewer, source class, URL/archive, timestamp/page/section, review status, and
publication gate.

No record in the chain may silently absorb another record. A transcript segment is
not a statement. A statement is not its evidence. A card is not source authority.
A correction is not an edit to history; it is a linked later record.

---

## 4. Required Records And Fields

### 4.1 `source_record`

A `source_record` is the registry-backed authority for all downstream transcript
and statement work.

Required fields before a source may support transcript/evidence/statement work:

- `source_id`
- `scope` = `alpine`
- `source_type`
- `source_class`
- `jurisdiction`
- `government_body` when known
- `source_authority_level`
- `original_url`
- `current_url` or reviewed unavailable reason
- `scan_date`
- `captured_at_utc` when captured
- `archive_status`
- `archive_url` or reviewed unavailable reason
- `verification_status`
- `correction_status`
- `review_state`
- `publication_state`
- `ui_status`
- `public_safety_status`
- `owner_agent`
- `reviewer_agent`
- `lifecycle_state`
- `replaces_source_id` when applicable
- `replacement_reason` when applicable
- `raw_preservation_status`
- raw artifact hash when available
- local raw path when available, private only

Failure rule: if required source accountability, status, lifecycle, archive,
public-safety, or private/raw state is missing or ambiguous, downstream statement
work is local/private and not frontend-ready.

### 4.2 `meeting`

A `meeting` groups agenda items and transcript records.

Required fields:

- `meeting_id`
- `source_id`
- `jurisdiction`
- `government_body`
- `meeting_date`
- `meeting_title`
- `meeting_kind`
- `meeting_source_url`
- `agenda_source_id` when available
- `minutes_source_id` when available
- `recording_source_id` when available
- `owner_agent`
- `reviewer_agent`
- `verification_status`

### 4.3 `agenda_item`

An `agenda_item` gives local context to transcript segments and statements.

Required fields:

- `agenda_item_id`
- `meeting_id`
- `source_id`
- `order` or reviewed unavailable reason
- `title`
- `description` when present in source
- `agenda_section` when present
- `related_document_source_ids`
- `verification_status`
- `review_state`

If agenda context is absent or uncertain, the statement must say so; it may not
invent context from topic inference.

### 4.4 `transcript_record`

A `transcript_record` describes a full transcript artifact or extraction run.

Required fields:

- `transcript_id`
- `meeting_id`
- `recording_source_id`
- `transcript_method`
- `transcript_tool`
- `transcript_tool_version` when known
- `created_at_utc`
- `transcript_artifact_path` private/local only
- `transcript_artifact_hash` private/local unless sanitized approval exists
- `segment_count`
- `language`
- `speaker_label_source`
- `machine_confidence_summary`
- `owner_agent`
- `reviewer_agent`
- `review_state`
- `publication_state`

Raw transcripts remain local/vault-only unless Isaac approves a sanitized fixture.

### 4.5 `transcript_segment`

A `transcript_segment` is a bounded span of text tied to an exact locator.

Required fields:

- `segment_id`
- `transcript_id`
- `meeting_id`
- `source_id`
- `start_time_seconds` when time-based
- `end_time_seconds` when time-based
- `timestamp_human`
- `page` or `section` when document-based
- `segment_text`
- `is_verbatim`
- `extraction_confidence`
- `speaker_label_raw` private if unreviewed
- `speaker_reference_id` when reviewed or explicitly uncertain
- `verification_status`
- `review_state`

Segment boundaries must be small enough that a reviewer can verify the exact text
without scanning an entire meeting. If the segment is too broad, it is not a
citation-ready segment.

### 4.6 `statement_record`

A `statement_record` is one civic claim, quote, paraphrase, or attribution unit.

Required fields:

- `statement_id`
- `segment_id` or non-transcript evidence pointer
- `meeting_id`
- `agenda_item_id` or reviewed unavailable reason
- `statement_kind`
- `statement_text`
- `normalized_statement_text` when paraphrased
- `layer`
- `is_verbatim`
- `speaker_reference_id`
- `evidence_citation_ids`
- `confidence`
- `ambiguity_note` when confidence is not high
- `verification_status`
- `correction_status`
- `review_state`
- `publication_state`
- `ui_status`
- `public_safety_status`
- `owner_agent`
- `reviewer_agent`

Allowed `statement_kind` values:

- `direct_quote`
- `paraphrase`
- `ai_extracted_claim`
- `minutes_attribution`
- `reviewer_note_claim`

Rules:

- A direct quote requires exact segment text and locator fields.
- A paraphrase must not add facts outside the cited evidence.
- An AI-extracted claim is draft-only until human review.
- A minutes attribution must cite the minutes source and must not override an
  ambiguous transcript speaker.
- A reviewer note claim is internal unless separately converted into a reviewed,
  source-backed statement.

### 4.7 `evidence_citation`

An `evidence_citation` ties a statement to a precise source pointer.

Required fields:

- `evidence_citation_id`
- `statement_id`
- `source_id`
- `source_type`
- `source_class`
- `source_authority_level`
- `original_url`
- `current_url`
- `archive_status`
- `archive_url`
- `scan_date`
- `captured_at_utc`
- `locator_kind`
- `timestamp_seconds` when `locator_kind = timestamp`
- `timestamp_human` when `locator_kind = timestamp`
- `page` when `locator_kind = page`
- `section` when `locator_kind = section`
- `paragraph` or text offset when available
- `deep_link` when derivable
- `exact_quote_text` for direct quotes
- `evidence_relation`
- `verification_status`
- `correction_status`
- `review_state`

Allowed `evidence_relation` values:

- `substantiates`
- `references`
- `supports`
- `contradicts`
- `corrects`
- `supersedes`

A deep link alone is not evidence. The locator fields are the evidence.

### 4.8 `speaker_reference`

A `speaker_reference` separates what was said from who may safely be named.

Required fields:

- `speaker_reference_id`
- `statement_id`
- `attribution_state`
- `speaker_class`
- `public_display_label`
- `private_candidate_name` private only when uncertain
- `person_id` only when attribution is approved
- `role_id` only when attribution is approved
- `attribution_basis`
- `basis_source_id`
- `reviewer_agent`
- `review_state`
- `confidence`

Allowed `attribution_state` values:

- `attributed`
- `uncertain`
- `unattributed`
- `withheld`

Allowed `speaker_class` values:

- `on_record_official`
- `on_record_public`
- `unidentified`
- `private_context`

Naming rules:

- No name is better than a wrong name.
- `on_record_official` may be named only when official records support the role
  and the statement is within official public-meeting capacity.
- `on_record_public` may be named only after CEO approval.
- `uncertain`, `unattributed`, and `withheld` render as generic ordinary-user
  labels, not candidate names.
- Voice inference, community knowledge, AI diarization, transcript speaker tags,
  and likely-speaker reasoning cannot promote a speaker name by themselves.

### 4.9 `correction_record`

A `correction_record` preserves correction history without erasing the original.

Required fields:

- `correction_id`
- `target_statement_id`
- `correction_kind`
- `correction_summary`
- `corrected_statement_id` when a corrected statement exists
- `correction_source_id`
- `evidence_citation_id`
- `created_at_utc`
- `reviewer_agent`
- `verification_status`
- `correction_status`
- `publication_state`
- `ui_status`

Allowed `correction_kind` values:

- `typo_or_transcription_error`
- `speaker_attribution_change`
- `source_changed`
- `source_replaced`
- `claim_disputed`
- `later_outcome_update`
- `do_not_publish`

Original known-then records remain inspectable. Later corrections link forward.

---

## 5. Layers And Time Semantics

Every statement and evidence citation must carry a `layer`.

Allowed layers:

- `known_then`: what the source record said at the time.
- `presented_then`: how the item was framed or presented at the time.
- `ai_thought_then`: what AI extraction or analysis proposed before review.
- `corrected_later`: a later correction to a prior record.
- `actual_later`: a later outcome or real-world result.

Layer rules:

- `known_then` is append-only.
- `corrected_later` and `actual_later` link to prior records; they do not rewrite
  prior records.
- `ai_thought_then` is always labeled as AI and is not public-eligible until a
  human review converts it into a reviewed source-backed statement.
- Later outcomes must be date-stamped and source-backed.
- Product surfaces must distinguish what was known then from what changed later.

---

## 6. Status And Publication Gate

This Stage 0.07 contract inherits the repaired Stage 0.05/0.06 state model.

Required status families:

- `verification_status`
- `correction_status`
- `review_state`
- `publication_state`
- `ui_status`
- `public_safety_status`
- `lifecycle_state`

Minimum `verification_status` values future implementations must map or preserve:

- `source_recorded`
- `machine_extracted_unreviewed`
- `reviewed_source_linked`
- `human_verified`
- `disputed`
- `do_not_publish`

Website/product-safe states must fail closed. Public eligibility requires all of:

- reviewed or human-verified source linkage;
- complete exact evidence citation;
- source present or archive/raw preservation sufficient for review;
- no unresolved private/raw path leakage;
- no unsupported accusation or legal/campaign conclusion;
- publication state explicitly approved;
- public safety state explicitly safe or approved for the intended surface.

Any unknown status, missing status, ambiguous duplicate, changed source, missing
source, broken archive, private-data warning, unsupported-claim warning, or stale
output warning is not public-eligible by default.

---

## 7. Pass / Fail Gates

### 7.1 Positive gate

A future statement may be considered ready for downstream product handoff only
when all of these are true:

- It is Alpine-scoped.
- It has a registry-backed `source_record`.
- It has exact citation locator fields.
- It has owner and reviewer accountability.
- It has separate verification, correction, review, publication, UI, and public
  safety states.
- It has a speaker reference with safe public label.
- It has no private/raw/local-only field in the public handoff.
- It has no unsupported claim, legal conclusion, campaign message, or accusation
  beyond what the source directly supports.
- It has correction/supersession history preserved when applicable.

### 7.2 Negative/adversarial gates

A future implementation must reject, quarantine, or route to review when it sees:

- missing pages;
- changed pages without old/new separation;
- duplicate sources with ambiguous canonical source;
- ambiguous names, roles, bodies, jurisdictions, or Alpine relevance;
- broken archive or Wayback lookup when archive evidence is required;
- private identity, address, voter-registry, account-validation, credential, or
  secret-bearing material;
- unsupported accusations;
- legal conclusions;
- campaign or political persuasion language;
- stale outputs that could be mistaken for current civic evidence;
- unreviewed AI claims;
- raw/local paths in public payloads;
- source links without timestamp/page/section locator details;
- speaker names derived only from AI, voice, context, or transcript labels.

---

## 8. Owner And Reviewer Responsibilities

### TranscriptEvidenceEngineer

Owns this contract and future statement/evidence practicality review:

- segment boundary practicality;
- quote versus paraphrase separation;
- statement kind separation;
- timestamp/page/section locator sufficiency;
- speaker confidence and generic-label safety;
- correction/dispute path clarity.

### CTO

Owns technical contract interpretation:

- schema/API/export compatibility;
- backend/frontend field-name disputes;
- blocker and next-stage sequencing;
- whether implementation work needs a separate issue.

### BackendCrawlerEngineer

Reviews source-registry and raw-preservation compatibility:

- source IDs;
- archive/Wayback fields;
- lifecycle/replacement fields;
- local/raw path handling;
- hashes and run manifests.

### VerificationSafetyReviewer

Reviews no-orphan-claim and public-safety gates:

- evidence sufficiency;
- unsupported-claim quarantine;
- AI/unverified/disputed/corrected labels;
- correction and dispute behavior;
- public eligibility.

### SecurityPrivacyAgent

Reviews private/raw/publication boundaries:

- private identity and address risk;
- local path leakage;
- transcript/raw media privacy;
- secrets and credentials;
- account-validation or voter-registry leakage.

### FrontendTimelineEngineer

Reviews only downstream handoff shape:

- ordinary-user labels;
- source drawer fields;
- status label display requirements;
- empty/error/gated state requirements.

Frontend review under this contract does not authorize UI implementation.

---

## 9. Privacy And Publication Boundaries

Local/vault-only by default:

- raw audio/video;
- raw transcript files;
- machine extraction batches;
- intermediate evidence bundles;
- run logs;
- local SQLite databases;
- source-validation reports with local paths;
- private speaker candidate names;
- reviewer rationale notes;
- raw hashes and local paths unless sanitized approval exists;
- private identity, address, voter-registry, account-validation, secrets, or
  credential-bearing material.

May be handed downstream only after review and public-eligibility approval:

- statement text;
- direct quote text;
- safe public speaker label;
- source type/class;
- source authority level;
- original/current URL;
- archive URL/status;
- timestamp/page/section locator;
- scan/capture date;
- verification/correction/UI labels;
- correction summary;
- ordinary-user source drawer labels.

Never derive public conclusions from this contract alone:

- legal fault;
- corruption or misconduct accusation;
- campaign or persuasion messaging;
- budget/donation commitment;
- official-contact decision;
- public launch decision.

---

## 10. Backend To Product Handoff

A future backend/static export/API handoff must include a website-safe payload
that has already dropped private fields.

Minimum safe handoff shape:

```json
{
  "statement_id": "string",
  "statement_kind": "direct_quote|paraphrase|ai_extracted_claim|minutes_attribution|reviewer_note_claim",
  "statement_text": "string",
  "layer": "known_then|presented_then|ai_thought_then|corrected_later|actual_later",
  "speaker_label": "safe public label",
  "verification_status": "string",
  "correction_status": "string",
  "ui_status": "string",
  "publication_state": "string",
  "source_drawer": {
    "source_id": "string",
    "source_type": "string",
    "source_class": "string",
    "source_authority_level": "string",
    "original_url": "string",
    "current_url": "string|null",
    "archive_status": "string",
    "archive_url": "string|null",
    "scan_date": "YYYY-MM-DD",
    "locator_kind": "timestamp|page|section|paragraph",
    "timestamp_human": "HH:MM:SS|null",
    "page": "string|null",
    "section": "string|null",
    "deep_link": "string|null"
  },
  "correction": {
    "correction_status": "string",
    "correction_summary": "string|null",
    "corrects_statement_id": "string|null"
  }
}
```

Handoff rules:

- Frontend must not compute trust from copy or card type.
- Frontend must render backend-provided status labels and source drawer fields.
- Frontend must show pending, unverified, disputed, source-changed, source-missing,
  corrected, archive-backed, and do-not-publish states as explicit states.
- Frontend must not fill missing backend fields with invented civic claims.
- Any rename to status/source/speaker fields requires a coordinated contract patch.

---

## 11. Future Implementation Requirements

If a future issue implements any part of this model, it must define before coding:

- exact owner role;
- project/repo;
- source input set;
- command path and CLI example;
- network behavior;
- raw/local output path;
- sanitized output path;
- log path;
- manifest path;
- mutation behavior;
- retry behavior;
- failure thresholds;
- test command;
- fixture privacy classification;
- reviewer lanes;
- Paperclip blocker/unlock rule.

Required future tests or review cases:

- happy-path reviewed quote with timestamp;
- paraphrase that remains source-bounded;
- unreviewed AI claim gated;
- uncertain speaker rendered generically;
- on-record-public speaker blocked pending CEO approval;
- source missing with no archive gated;
- source changed/replaced with lifecycle fields;
- ambiguous duplicate quarantined;
- unsupported accusation quarantined;
- private/local path excluded from public payload;
- correction linked without overwriting known-then.

---

## 12. Locked Scope

Stage 0.07 / GOV-250 does not authorize:

- transcript ingestion implementation;
- crawler implementation;
- API/database migrations;
- frontend UI implementation;
- newsletter generation or sending;
- public API/static export publication;
- raw media, raw transcript, or raw evidence publication;
- official-contact automation;
- legal conclusions;
- campaign messaging;
- budget/donation commitments;
- naming on-record public speakers without CEO approval;
- expanding beyond Alpine;
- treating Stage 1+ planning artifacts as Stage 1 activation.

If any of those are needed, stop and create a separate child/next-stage issue with
CTO/CEO routing.

---

## 13. Acceptance Evidence For GOV-250

This artifact satisfies the Stage 0.07 planning/contract gate when paired with a
Paperclip closeout comment containing:

- artifact path: `Docs/stage0-transcript-evidence-statement-model-contract.md`;
- verification command output (`wc -l`, required-term check, git diff summary);
- predecessor evidence read: GOV-248/GOV-249 comments and active Stage 0.07 goal;
- statement that this is contract-only and no implementation was performed;
- pass/fail against the acceptance criteria;
- next gate: Stage 0.08 may be considered for planning/contract work only.

Downstream work remains blocked from implementation until a separate issue names
this contract as input and supplies its own tests, fixtures, and review evidence.
