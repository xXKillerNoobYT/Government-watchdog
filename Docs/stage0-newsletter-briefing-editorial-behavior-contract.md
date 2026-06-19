# Stage 0.08 Newsletter/Briefing/Editorial Behavior Contract

Issue: GOV-17
Owner role: CTO
Stage: Stage 0.08, inside active Stage 0 governance foundation
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only. This contract does not authorize newsletter generation, public sends, briefing publication, campaign messaging, legal conclusions, official contact, API/database migrations, website UI implementation, or expansion beyond Alpine.

## Purpose

This contract defines how future Government Watchdog historical digests, newsletters, briefings, and editorial surfaces must behave before any implementation work begins.

Newsletter and briefing text are presentation layers over reviewed source-linked records. They are not an independent claim source. Every item must trace to source records, evidence citations, statement records, cards, meetings, agenda items, documents, corrections, or later outcomes that already satisfy the earlier Stage 0 contracts.

The immediate goal is to prevent orphan claims, unsupported editorial framing, unsafe AI presentation, and accidental publication while preserving the product direction for later Stage 4 historical digests and Stage 7 pre-meeting briefings.

## Supporting Inputs

- Original Paperclip issue: `GOV-17` / `fa182b32-0248-4536-bcbe-a4c47ff6a5b4`
- Gate-refresh issue: `GOV-251` / `e3c3345a-d440-42a7-84af-b702b8963342`
- Paperclip goal: `5821e35c-3e21-4629-8e38-b82db85d22aa` / Stage 0.08 Newsletter/briefing/editorial behavior
- Parent goal: `51bc7f65-1276-4707-87a5-89fe1eb5a612` / Stage 0 governance foundation
- Historical predecessor issue: `GOV-16` / `41226990-9f5b-4270-ab1e-869b69cb70be` / Stage 0.07 Transcript/evidence/statement model
- Repaired/current predecessor issue: `GOV-250` / `15af4b5e-8c09-4e02-8c99-7501d8f10270` / Stage 0.07 Transcript/evidence/statement model after Stage 0.06 gate refresh
- Repaired/current predecessor contract: `Docs/stage0-transcript-evidence-statement-model-contract.md`
- Source/data inventory contract: `Docs/stage0-source-data-inventory-contract.md`
- Backend/tooling contract: `Docs/stage0-backend-tooling-implementation-contract.md`
- Concept-map/card model reference: `Docs/government-concept-map-card-model.md`
- Planned Stage 4 workflow reference: `Docs/stage4-newsletter-item-feed-workflow.md`
- Staged master plan read from verified relocated path: `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`

Note: the company instruction path for the master plan currently names `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog/Docs/...`, but that path is missing on disk. The verified live path is under `Government-Watchdog v1 Plans/Docs/...`, matching prior Stage 0 evidence.

## Stage And Scope Boundary

Allowed in Stage 0.08:

- define editorial item eligibility for future historical digests and briefings
- define source citation and quote reuse rules
- define AI/unverified/disputed/corrected labels for editorial surfaces
- define historical digest versus pre-meeting briefing distinctions
- define public/private publication states and do-not-publish states
- define correction and unsupported-claim handling
- define reviewer lanes, owner escalation triggers, and future validation evidence
- identify the next sequential Stage 0 issue

Not allowed in Stage 0.08:

- generate a newsletter, digest, or pre-meeting briefing
- create public email sends, public website pages, feeds, or API endpoints
- add database migrations or static export code
- fetch, import, or analyze new Alpine sources
- create campaign language, public-pressure messaging, accusations, or legal conclusions
- contact officials or public figures
- publish raw, unreviewed, AI-generated, or private-risk material
- add Star Valley, Lincoln County-wide, Wyoming statewide, or United States implementation

## Editorial Surface Types

Future implementation must keep these editorial products distinct:

- `historical_digest`: a retrospective summary of reviewed records processed for a historical week or backfill slice.
- `processing_update`: a private progress note that reports what source records, meetings, documents, or cards were added or reviewed.
- `topic_brief`: a source-linked overview of one Alpine topic using reviewed cards, statements, decisions, outcomes, and corrections.
- `pre_meeting_briefing`: a current-operations briefing tied to a future meeting agenda, packet, prior topic cards, unresolved questions, and source links. This remains planned for later Stage 7.
- `correction_notice`: a forward-dated editorial notice that explains a correction, dispute, source change, or later outcome without rewriting known-then context.
- `editorial_review_note`: private reviewer guidance, uncertainty, or quality-control notes. These are not public copy.

An editorial surface may combine multiple source-linked items for readability, but it must not collapse concept records. Meetings, agenda items, documents, people/roles, statements, votes, decisions, topics, outcomes, evidence citations, and frontend cards remain separate source-of-truth concepts.

## Editorial Item Eligibility

An editorial item is eligible for a future digest or briefing only when all required upstream records exist:

- `jurisdiction_id` is Town of Alpine.
- `source_ids` is present and non-empty.
- each source has original URL, current URL when different, scan/fetch date, source type/class, authority level, verification status, correction status, archive status, and reviewer-safe note when needed.
- at least one visible link exists to a reviewed card, meeting, agenda item, document, topic, evidence citation, timestamp URL, source URL, correction, or later outcome.
- claim text comes from a reviewed `statement_record`, reviewed card summary, reviewed source note, decision/vote record, correction record, or later-outcome record.
- direct quotes cite an `evidence_citation` with timestamp, page, section, or exact text location when available.
- paraphrases cite the evidence they summarize and do not add facts absent from the cited evidence.
- AI-assisted summaries remain labeled and review-gated until promoted by a reviewer.
- `public_eligibility` is explicit.

The item must stay private/review-only if any required source trail, evidence citation, verification status, correction status, or public eligibility field is missing.

## Required Editorial Item Fields

Future newsletter or briefing item records must include:

- `id`
- `surface_type`
- `stage_origin`
- `jurisdiction_id`
- `jurisdiction_display`
- `coverage_start_date`
- `coverage_end_date`
- `known_then_date` or `known_then_week`
- `record_date`
- `title`
- `summary_text`
- `item_type`
- `source_ids`
- `source_trail_references`
- `evidence_citation_ids`
- `statement_record_ids`
- `card_ids`
- `meeting_ids`
- `agenda_item_ids`
- `document_ids`
- `topic_ids`
- `decision_ids`
- `vote_ids`
- `correction_ids`
- `later_outcome_ids`
- `timestamp_urls`
- `source_record_urls`
- `claim_status`
- `speaker_status`
- `correction_status`
- `ai_presented`
- `ai_method_or_model` when AI assisted
- `verification_status`
- `public_eligibility`
- `publication_status`
- `status_label`
- `backfill_gap_label`
- `reviewer_note`
- `created_utc`
- `updated_utc`

Website-safe exports may omit private local paths, raw hashes, and private reviewer details, but they must preserve public source links, citation IDs, status labels, correction/dispute labels, and AI/unverified gates.

## Claim Selection Rules

Allowed item claims:

- "This meeting/document/source was added to the Alpine backfill."
- "This reviewed source says X at page/section/timestamp Y."
- "Official minutes attribute X to Y" when minutes provide that attribution.
- "The record known then showed X."
- "A later source on YYYY-MM-DD corrected/superseded/disputed X."
- "This item still needs human review" when the status is visibly labeled.
- "The system linked this agenda item to prior reviewed topic cards" when the link is source-backed or review-labeled.

Blocked item claims:

- claims with no source IDs or no visible source/card/timeline link
- conclusions that imply illegality, corruption, bad faith, fraud, misconduct, or liability
- campaign or public-pressure messaging
- unsupported comparisons between officials, candidates, voters, vendors, or private individuals
- AI-generated assertions presented as verified fact
- paraphrases that add facts not in the cited evidence
- speaker names inferred from AI, voice guesswork, local knowledge, or transcript labels alone
- claims based on private identity data, private addresses, voter-registry/private profile data, credentials, or secrets
- statewide or national claims outside the Alpine stage gate

When in doubt, the item should say less and link more. No newsletter or briefing copy may repair missing evidence by sounding confident.

## Quote And Paraphrase Reuse

Direct quotes:

- must reuse exact text from `statement_record.statement_text` or `evidence_citation.evidence_text_exact`
- must preserve timestamp, page, section, or offset where available
- must preserve speaker display label and speaker verification status
- must use safe fallback labels such as `speaker unidentified` when attribution is uncertain
- must not silently clean up transcript errors in a way that changes meaning

Paraphrases:

- must point to one or more evidence citations
- must not blend later facts into known-then context
- must not introduce motive, intent, legal interpretation, or accusation
- must be marked as AI-assisted when drafted by AI before review

Summaries:

- may compress multiple reviewed items for readability
- must keep each underlying item traceable
- must expose correction/dispute/unverified labels rather than flattening them into a single verified summary

## Historical Digest Versus Pre-Meeting Briefing

Historical digests are retrospective. They may include:

- records processed during a backfill slice
- meetings, documents, topics, source links, corrections, conflicts, and later outcomes found in that slice
- what was known then and where to click for source trail
- visible backfill gaps and review statuses

Historical digests must not imply that the project has completed all Alpine history unless a source-completeness gate says so.

Pre-meeting briefings are prospective and remain future Stage 7 work. They may include only after later gates:

- newly detected agenda and packet items
- links to prior reviewed cards/topics/meetings
- unresolved questions clearly labeled as questions
- public comment deadlines when source-backed
- hot-topic or source-change indicators when the trust workflow supports them
- exact source links and AI/unverified labels

Pre-meeting briefings must not become official-contact automation, public lobbying, campaign messaging, legal analysis, or unsupported accusations.

## Status Labels And Publication States

Allowed `claim_status` values:

- `verified`
- `reviewed_source_linked`
- `unverified`
- `ai_presented`
- `disputed`
- `corrected`
- `source_missing`
- `speaker_unidentified`
- `needs_human_review`
- `do_not_publish`

Allowed `publication_status` values:

- `private_draft`
- `private_review`
- `approved_private_preview`
- `owner_review_required`
- `public_ready_blocked_until_launch_gate`
- `published`
- `do_not_publish`

Stage 0.08 authorizes only `private_draft`, `private_review`, `owner_review_required`, and `do_not_publish` as planning states. The `published` state is defined for future validation so validators can reject premature publication before an owner-approved launch gate.

Allowed `public_eligibility` values:

- `private_only`
- `review_required`
- `reviewed_public_safe`
- `blocked`
- `owner_approval_required`

AI, unverified, disputed, corrected, source-missing, and speaker-unidentified states must remain visible in any downstream UI, digest, briefing, source drawer, or static export. They must not be hidden in private metadata.

## Correction And Later-Outcome Handling

Corrections work forward from the correction date.

Editorial copy must preserve separate fields:

- `known_then_summary`
- `presented_then_summary`
- `ai_thought_then_summary`
- `corrected_later_summary`
- `actual_later_summary`

When a correction, dispute, changed source, disappeared source, superseding document, or later outcome exists, future digest or briefing copy must:

- show the original known-then context
- link to the correction or later-outcome record
- include the correction date and source trail
- avoid rewriting old items as if later facts were known then
- avoid converting a disputed item into a verified item without review evidence

Safe correction notice pattern:

```text
This was the information known at the time. This item was corrected on YYYY-MM-DD based on [source/citation].
```

## AI-Assisted Editorial Boundaries

Must be deterministic or exactly preserved:

- source IDs and source URLs
- card, statement, evidence, meeting, agenda item, topic, correction, and later-outcome IDs
- exact quotes
- timestamp/page/section citations
- verification/correction/public eligibility statuses
- publication filtering rules

May be AI-assisted only as draft/review input:

- headline suggestions
- digest grouping suggestions
- short summary drafts
- topic tags
- unresolved-question candidates
- related-card suggestions
- plain-language explanation drafts

AI-assisted editorial output must record the model/tool when known, the cited source IDs used, and reviewer status. It must stay `ai_presented`, `needs_human_review`, `private_review`, or `do_not_publish` until human review promotes it.

## Frontend And Backend Handoff

Backend responsibilities for future implementation:

- produce editorial item records only from reviewed source-linked records
- enforce Alpine-only scope
- block items with missing source IDs, missing visible links, or premature publication status
- preserve AI/unverified/disputed/corrected labels
- provide validation output before website consumption

Frontend responsibilities for future implementation:

- render only reviewed website-safe fields or clearly labeled private preview fixtures
- show source links and status labels near the claim text
- preserve source drawers, related cards, correction notices, and later-outcome links
- never invent claims or hide uncertainty to improve presentation
- keep public launch paths blocked until owner/stage/publication approval

An editorial item feed is a bridge between backend evidence and frontend presentation. It is not the source of truth.

## Future Validation Evidence

Future implementation issues must include focused tests or review artifacts for:

- item with source IDs, evidence citation, card link, and source URL passes
- item with no source IDs fails
- item with source IDs but no visible source/card/timeline link fails
- direct quote preserves timestamp/page/section citation
- paraphrase cannot add uncited facts
- AI-assisted item remains visibly labeled before review
- unverified/disputed/corrected/source-missing states remain visible
- speaker-unidentified item does not promote a name
- correction links forward without rewriting known-then text
- publication status `published` fails unless an owner-approved launch gate is cited
- private paths, raw hashes, private identity/address/voter-registry data, credentials, unsupported accusations, legal conclusions, and campaign language are blocked from website-safe export
- historical digest and pre-meeting briefing item types remain distinct

Smallest current verification for this contract:

```bash
wc -l Docs/stage0-newsletter-briefing-editorial-behavior-contract.md
rg -n "No orphan|do_not_publish|public_ready_blocked_until_launch_gate|historical_digest|pre_meeting_briefing|AI-assisted" Docs/stage0-newsletter-briefing-editorial-behavior-contract.md
```

Future implementation commands can reuse or extend:

```bash
python scripts/validate_newsletter_item_feed.py --input Exports/alpine/newsletter-items.json
pytest tests/test_newsletter_item_feed.py
```

Those future commands validate the existing Stage 4 private feed shape. They do not prove Stage 0.08 implementation, because this issue is planning/contract only.

## Reviewer Lanes And Pass-Up Triggers

CTO owns final interpretation of this editorial behavior contract and any backend/frontend data-contract disputes.

BackendCrawlerEngineer reviews future item-feed feasibility, source linkage, validation, manifests, and export filtering.

FrontendTimelineEngineer reviews downstream digest/briefing display fields, source drawer needs, timeline/card links, mobile/readability needs, and trust-label consumption. No UI implementation is authorized by this issue.

TranscriptEvidenceEngineer reviews quote reuse, timestamp/page/section citation needs, statement-record compatibility, and speaker-confidence safety.

VerificationSafetyReviewer reviews no-orphan-claim behavior, unsupported-claim blocking, correction/dispute handling, and visible AI/unverified labels.

SecurityPrivacyAgent reviews private/public boundaries, private identity/address/voter-registry leakage risk, secret handling, publication-status filters, and public eligibility.

Escalate to CEO/Isaac before any public launch, public send, publication policy change, legal/privacy judgment about specific people, campaign/accusation framing, official contact, budget decision, or scope expansion beyond Alpine.

## Next Sequential Stage 0 Issue

After GOV-17, the next non-duplicate Stage 0 issue should be Stage 0.09.

Recommended issue:

Title: `[Stage 0.09][CTO] Define correction/trust/publication gate contract`

Goal: Stage 0.09 correction/trust/publication gate, if present in Paperclip; otherwise CEO should create or confirm the next sequential Stage 0 goal before work starts.

Owner role: CTO, with VerificationSafetyReviewer, SecurityPrivacyAgent, BackendCrawlerEngineer, FrontendTimelineEngineer, and SourceArchivist review lanes.

Repo/project: start in backend/core project because correction, trust, source-change, and publication eligibility are source-of-truth concerns; note website surfaces as downstream consumers.

Blocked by: GOV-17 until this Stage 0.08 contract is done.

Scope: planning/contract only. Define correction/dispute/source-change/publication gate states, who can promote public readiness, how owner approval is represented, and how website/export validators block premature public use. Do not implement public UI, feeds, sends, crawler changes, API/database migrations, or expansion beyond Alpine.

Acceptance criteria:

- states Alpine-only scope
- preserves no-orphan-claim and evidence-source rules
- defines correction/dispute/source-change/publication gate states
- defines public/private/owner-review boundaries
- names reviewer lanes and owner escalation triggers
- does not authorize implementation or public publication

Evidence:

- Paperclip issue readback showing GOV-17 done and Stage 0.08 goal status
- supporting docs read
- final comment with pass/fail against acceptance criteria
- GitHub sync note if repo files are changed

## GOV-251 Gate Refresh Against Repaired GOV-250 Stage 0.07 Contract

Decision: PASS after smallest contract refresh.

GOV-251 re-read the repaired/current Stage 0.07 model from GOV-250 and reconciled this Stage 0.08 editorial behavior contract against it. The original GOV-17 contract already required reviewed source-linked records, evidence citations, statement records, safe speaker labels, known-then/corrected-later separation, visible AI/unverified/corrected labels, public/private publication states, and no-orphan-claim behavior. This refresh makes the inheritance explicit and imports the current GOV-250 predecessor artifact into this branch so future reviewers do not accidentally rely on the older GOV-16-era Stage 0.07 text.

Required GOV-250 inheritance for Stage 0.08 editorial items:

- Editorial items must originate from typed GOV-250 records: `source_record`, `meeting`, `agenda_item`, `transcript_record`, `transcript_segment`, `statement_record`, `evidence_citation`, `speaker_reference`, and `correction_record`.
- Editorial copy must preserve statement kinds: `direct_quote`, `paraphrase`, `ai_extracted_claim`, `minutes_attribution`, and `reviewer_note_claim`.
- Direct quotes require exact locator evidence: timestamp, page, section, paragraph/text offset, and exact quote text when available. A deep link alone is not evidence.
- Paraphrases and summaries may not add facts outside the cited evidence and must keep the underlying statement/evidence records traceable.
- AI-extracted or AI-presented claims remain private/review-only until human review promotes them; AI output must stay visibly labeled as AI-assisted before review.
- Speaker handling must follow GOV-250 speaker states and classes: `attributed`, `uncertain`, `unattributed`, `withheld`; `on_record_official`, `on_record_public`, `unidentified`, `private_context`.
- On-record public speaker names require CEO approval before public naming. Uncertain, unattributed, and withheld speakers must render as generic ordinary-user labels, not candidate names.
- Editorial surfaces must preserve layers: `known_then`, `presented_then`, `ai_thought_then`, `corrected_later`, and `actual_later`.
- Correction, supersession, dispute, source-change, and later-outcome records link forward without rewriting prior known-then records.
- Product handoff must preserve `verification_status`, `correction_status`, `review_state`, `publication_state`, `ui_status`, `public_safety_status`, `lifecycle_state`, replacement, archive/Wayback, duplicate, unsupported-claim, stale-output, and private/raw exclusion states inherited from GOV-250 and its repaired Stage 0.05/0.06 inputs.
- Website-safe or newsletter-safe exports must exclude raw/local paths, raw hashes unless sanitized approval exists, raw transcripts/media, private reviewer rationale, private speaker candidates, private identity/address/voter-registry/account-validation data, credentials, secrets, unsupported accusations, legal conclusions, official-contact automation outputs, and campaign/public-pressure messaging.
- Any missing required source, evidence locator, status family, review owner, publication gate, ambiguous duplicate, source-change, missing source/archive, unsupported claim, stale-output, or private-data warning fails closed to private review or `do_not_publish`.

Stage 0.09 inheritance after this refresh:

- Stage 0.09 may inherit this refreshed Stage 0.08 contract only for planning/contract work around correction/trust/publication gates.
- Stage 0.09 may not treat newsletter/editorial content as source authority; it must continue to treat Stage 0.07 GOV-250 typed evidence records as claim authority.
- Stage 0.09 may not unlock public launch, public newsletter sends, public website publication, API/database/crawler/UI implementation, legal/campaign/budget decisions, official contact, or expansion beyond Alpine without separate owner-approved gates.

## Acceptance Checklist For GOV-17 / GOV-251

- Alpine-only scope is explicit.
- Historical digest and pre-meeting briefing behavior are distinct.
- Newsletter/briefing items reuse reviewed `statement_record`, `evidence_citation`, source, card, meeting, agenda item, correction, and later-outcome concepts instead of raw transcript text or freeform claims.
- No-orphan-claim behavior is explicit for quotes, paraphrases, summaries, AI-assisted drafts, corrections, and briefing questions.
- Required source links, visible card/topic/meeting/timestamp/source links, review states, public eligibility, and publication status fields are named.
- Public/private editorial boundaries and do-not-publish states are explicit.
- AI/unverified/disputed/corrected/source-missing/speaker-unidentified labels remain visible.
- Correction and later-outcome handling preserves known-then versus corrected-later separation.
- Unsupported accusations, legal conclusions, campaign/public-pressure language, private identity/address/voter-registry data, credentials, and official-contact automation are forbidden.
- Reviewer lanes and CEO/Isaac escalation triggers are named.
- Implementation boundaries are clear: planning/spec only.
