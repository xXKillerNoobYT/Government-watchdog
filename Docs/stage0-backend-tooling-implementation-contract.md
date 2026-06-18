# Stage 0.05 Backend/Tooling Implementation Contract

Issue: GOV-9
Owner role: CTO
Stage: Stage 0.05, inside active Stage 0 governance foundation
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only. Lincoln County inputs are allowed only when the retained page, document, title, link text, or metadata is Alpine-relevant.

## Purpose

This contract defines the backend and tooling boundary that future implementation issues must follow after Stage 0 planning gates complete.

It does not authorize crawler, API, database, transcript, website, newsletter, public-launch, official-contact, legal, campaign, budget, or expansion implementation. It defines what those future issues must prove before they can be accepted.

## Supporting Inputs

- Paperclip issue: `GOV-9` / `8e7352b7-dceb-4251-bfc3-dde85273edc1`
- Paperclip goal: `5b6500be-8047-4c27-8621-c4a6cae1ad75` / Stage 0.05 Backend/tooling implementation contract
- Parent goal: `51bc7f65-1276-4707-87a5-89fe1eb5a612` / Stage 0 governance foundation
- Predecessor issue: `GOV-8` / `57d051a3-d2fe-4cf3-abc7-b447030d8c4c` / Stage 0.04 raw preservation and reproducibility gate
- Staged master plan read from relocated live path: `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`
- Stage 0.03 source/data inventory contract: `Docs/stage0-source-data-inventory-contract.md`
- Stage 1 automation reference: `Docs/stage1-automation-targets.md`
- Source registry format reference: `Docs/Source-Data/alpine-source-registry-format.md`
- Required agent instructions: `COMPANY.md`, `SOUL.md`, `TOOLS.md`, `HEARTBEAT.md`, `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`, and `CTO_WORKFLOWS.md`

Note: the company instruction path for the master plan currently names `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog/Docs/...`, but that path is missing on disk. The verified live path is under `Government-Watchdog v1 Plans/Docs/...`, matching prior GOV-6 and GOV-8 evidence.

## Stage And Scope Boundary

Stage 0.05 is planning/contract work only.

Allowed:

- define backend script boundaries
- define deterministic command contracts
- define input/output/log/manifest expectations
- define GitHub-safe versus local/vault-only artifacts
- define validation and failure thresholds
- define reviewer lanes and blocker rules
- identify the next sequential Stage 0 issue

Not allowed:

- implement a new crawler
- implement database migrations or API endpoints
- implement transcript ingestion
- implement website UI or public data feeds
- publish raw or reviewed data
- contact officials or public figures
- make legal, campaign, accusation, or budget decisions
- expand beyond Alpine
- treat provisional Stage 1 planning evidence as Stage 1 activation

## Source Inputs Future Tools May Consume

Future backend/tooling issues may consume only source records that satisfy the Stage 0.03 contract:

- `scope` is `alpine`
- retained Lincoln County material is explicitly Alpine-relevant
- source class is approved for the stage
- source record includes original URL, current URL, scan date, source type/class, jurisdiction, authority level, verification status, correction status, archive status, and owner role
- source record includes explicit accountability fields: `owner_agent` and `reviewer_agent`
- source record includes gate-state fields for verification, correction, review, publication eligibility, and UI readiness; missing or ambiguous state is not passable by default
- source record includes lifecycle/replacement fields: `lifecycle_state`, `replaces_source_id`, and `replacement_reason` when a source changed, disappeared, moved, or superseded another source
- source record includes archive/Wayback status and link or a reviewer-visible reason why archive evidence is unavailable
- source record includes fail-closed `public_safety_status`; missing public-safety status means local/private only until reviewed
- seed-only sources are labeled with `raw_preservation_status: seed_only_pending_crawl_preservation`
- fetched raw or semi-raw artifacts include local path and SHA-256 when available
- raw preservation, hash, and local-note fields remain private/local unless a reviewer approves a sanitized evidence reference

Future scripts must reject or quarantine:

- non-Alpine statewide or United States sources
- private identity, private address, voter-registry, credential, or secret-bearing sources
- unsupported accusation/legal/campaign content
- sources with no provenance trail
- sources that try to become public-facing evidence without review

The repaired Stage 0.03 contract from GOV-242 added adversarial registry gates that Stage 0.05 tooling must inherit. Future tooling must fail closed, quarantine, or route for SourceArchivist/Security/Verification review when it encounters:

- missing pages or disappeared sources
- changed pages whose old/new facts, timestamps, hashes, or archive references cannot be separated
- duplicate sources whose canonical source and duplicate disposition are ambiguous
- ambiguous names, titles, bodies, jurisdictions, authorities, or Alpine relevance
- broken archive or Wayback lookups when archive evidence is required for the source class
- private data, identity/address/voter/credential material, or sensitive local notes
- unsupported claims, accusation-risk summaries, legal conclusions, or campaign/political content
- stale outputs that could be mistaken for current civic evidence
- secrets or raw/local paths appearing in logs, manifests, fixtures, or generated evidence bundles

## GitHub-Safe And Local-Only Outputs

GitHub-safe:

- source code
- tests
- workflow files
- schema or migration definitions
- sanitized contract docs
- deterministic sample shapes with no real raw data

Local/vault-only unless Isaac explicitly approves a sanitized fixture:

- crawler outputs
- raw source caches
- local SQLite databases
- raw PDFs, transcripts, or media
- generated intermediate evidence bundles
- run logs
- Paperclip snapshots
- unreviewed research notes
- source-validation reports that expose local paths or raw contents

Never public:

- credentials or tokens
- private identity/address/voter-registry data
- unsupported accusations
- legal conclusions
- official-contact automation outputs
- campaign/political messaging
- unreviewed AI interpretations

## Deterministic Command Contract

Every future script or workflow issue must define these fields before implementation:

- command path and exact CLI example
- stage and owner role
- accepted input paths
- rejected input conditions
- generated output paths
- log path
- manifest or run-summary path
- mutation behavior
- network behavior
- retry policy
- failure thresholds
- issue-creation threshold
- review cadence
- test command
- acceptance evidence

Commands that gather, validate, or transform source data must be deterministic enough that a reviewer can re-run them against the same inputs and understand any delta.

## Required Run Manifest Fields

Crawler or automation runs must write a manifest or equivalent run summary with:

- `run_id`
- `command`
- `stage`
- `scope`
- `started_utc`
- `finished_utc`
- `status`
- `input_paths`
- `output_paths`
- `log_path`
- `source_count`
- `raw_artifact_count`
- `new_artifact_count`
- `changed_artifact_count`
- `unchanged_artifact_count`
- `warning_count`
- `failure_count`
- `retry_count`
- `archive_checked_count`
- `archive_available_count`
- `validation_passed`
- `owner_agent`
- `reviewer_agent`
- `public_safety_status`
- `review_status`
- `publication_eligibility`
- `ui_readiness_status`
- `lifecycle_state`
- `replacement_source_count` or equivalent replacement/rebaseline summary
- `quarantine_count`
- `adversarial_case_count` grouped by missing, changed, duplicate, ambiguous, archive, private-data, unsupported-claim, stale-output, and secrets/log-exposure classes
- `next_owner_action`

The manifest must not include secrets or raw private data.

## Log Contract

Logs must be readable enough for agents to triage failures without inspecting raw data first.

Every log-producing command must record:

- timestamp
- command name
- scope
- input summary
- output summary
- warning class
- failure class
- retry class
- terminal status
- next owner action

Logs remain local/vault-only by default. A log excerpt may be included in GitHub or Paperclip only when it is sanitized and does not expose raw private data, secrets, unsupported claims, or sensitive local paths beyond approved evidence references.

## Failure Thresholds

Future implementation issues must treat these as failures unless the issue explicitly documents a narrower exception:

- non-Alpine source accepted into an Alpine run
- required source registry field missing
- raw artifact written without preservation status
- fetched raw artifact missing a hash when hashing is available
- validation report cannot distinguish warning from failure
- command mutates raw/source records without an explicit `--apply` style gate
- run deletes evidence-bearing artifacts without review
- script output would be published publicly by default
- source data leaks into GitHub-safe fixture paths
- credentials, private identity data, or unsupported accusations appear in output
- `owner_agent`, `reviewer_agent`, review/publication/UI state, lifecycle/replacement fields, archive/Wayback state, or `public_safety_status` is missing when a source record is used for downstream tooling
- duplicate or ambiguous sources are silently merged without canonical-source/replacement evidence
- changed, missing, or disappeared pages are treated as current without old/new separation and reviewer-visible archive evidence
- stale outputs are presented as current civic evidence
- secrets, raw local paths, or sensitive local notes appear in logs, manifests, fixtures, Paperclip comments, or generated evidence bundles

Warnings may include:

- source temporarily unreachable
- archive lookup unavailable or rate-limited
- seed-only source pending crawl
- incomplete source note pending SourceArchivist review

Warnings must be visible in manifests and issue evidence. They must not be silently converted into passes.

## Backend Crawler Lane

BackendCrawlerEngineer owns future crawler/API/storage implementation only after CTO routing.

Future backend crawler issues must:

- consume Stage 0.03 source records
- enforce `scope: alpine`
- preserve raw artifacts before downstream analysis
- hash raw artifacts when possible
- record archive status
- write manifests and logs
- avoid public publication
- include focused tests
- cite exact source registry inputs

Backend crawler issues must stop and escalate to CTO/CEO when they need:

- new source classes
- non-Alpine scope
- paid APIs
- public data feeds
- public launch behavior
- official-contact automation
- database/storage migration that changes raw preservation policy

## Automation Lane

AutomationOpsEngineer owns repeatable command orchestration, scheduled checks, logs, retries, and runner workflow behavior.

Automation issues must:

- wrap proven commands only
- define log paths
- define retry behavior
- define issue thresholds
- define review cadence
- avoid raw-public publication
- avoid destructive cleanup without approved review
- include test coverage for success, warning, and failure paths

Scheduled runs should create or update private status artifacts, not public claims.

## Source Archivist Lane

SourceArchivist owns source registry completeness, raw/source-note evidence, and source status labels.

SourceArchivist review is required when:

- source records are incomplete
- sources are stale, unavailable, changed, disappeared, or rejected
- raw artifact paths or hashes are missing
- a source class or jurisdiction is uncertain
- a county/regional source may or may not be Alpine-relevant

## Security And Verification Lane

SecurityPrivacyAgent and VerificationSafetyReviewer own private/public boundaries and publication safety.

Their review is required before:

- any data becomes website-ready
- source validation output is exposed beyond local/Paperclip evidence
- raw sample fixtures are proposed for GitHub
- local paths, private data, or sensitive claims appear in generated artifacts
- AI output is displayed as more than unverified draft content
- correction or disputed-status behavior reaches public UI

## Frontend Handoff Boundary

FrontendTimelineEngineer may receive only reviewed, website-ready backend outputs or clearly labeled prototype fixtures.

Website-facing contracts must preserve:

- source ID
- original URL
- source type/class
- jurisdiction
- scan/crawl date
- archive status/link when available
- verification status
- correction status
- owner/reviewer accountability state
- review/publication/UI readiness state
- lifecycle/replacement state when a source moved, changed, disappeared, or superseded another source
- public-safety status that defaults to private/local when absent or unresolved
- known-then versus corrected-later separation
- AI/unverified/disputed labels

The website must not invent claims to fill missing backend data.

## API And Static Export Boundary

Stage 0.05 allows API/static export planning only.

Future API or static export issues must define:

- schema or JSON shape
- source record linkage
- public eligibility rule
- privacy filter
- validation command
- snapshot/golden fixture
- frontend consumer issue
- blocker relationship to backend preservation/verification work

No public API or website feed is authorized until source records are reviewed and the publication boundary is approved.

## Review And Issue Creation Rules

Every child implementation issue must include:

- owner role
- repo/project
- stage
- exact scope
- acceptance criteria
- evidence
- review lane
- pass-up trigger
- blocker/unlock rule

Use real `blockedByIssueIds` when one issue must finish before another. Text-only dependencies are not enough.

## Next Sequential Stage 0 Issue

After GOV-9, the next non-duplicate Stage 0 issue should be Stage 0.06.

Recommended issue:

Title: `[Stage 0.06][CTO] Define frontend/product surface contract`

Goal: `e94a008d-0b9d-4167-b45e-0d1f8c2bef39` / Stage 0.06 Frontend/product surface contract.

Owner role: CTO, with FrontendTimelineEngineer, BackendCrawlerEngineer, SecurityPrivacyAgent, and VerificationSafetyReviewer review lanes.

Repo/project: start in backend project because source truth and publication eligibility are backend-owned; create a linked website issue only after the handoff contract identifies executable frontend work.

Blocked by: GOV-9.

Scope: planning/contract only. Define user-facing/reviewer-facing surfaces, status labels, source drawers, filters, empty/error/mobile states, evidence visibility, and how backend-reviewed Alpine source records/static exports/API shapes are handed to the website without raw/private leakage.

Acceptance criteria:

- states Alpine-only scope
- names backend and website project IDs
- lists website-safe fields and blocked/private fields
- preserves no-orphan-claim rule
- preserves known-then/presented-then/AI-thought-then/corrected-later/actual-later separation
- defines trust/status labels, source drawers, filters, empty states, error states, and reviewer evidence
- names validation commands and reviewer lanes
- does not implement API, frontend UI, crawler, or public launch

Evidence:

- Paperclip issue readback showing GOV-9 done and GOV-10/next Stage 0 issue blocked correctly if created
- supporting doc paths read
- final comment with pass/fail against acceptance criteria

## Verification

Smallest useful verification for this contract:

```bash
wc -l Docs/stage0-backend-tooling-implementation-contract.md
python -m pytest tests/test_source_inventory.py tests/test_publication.py -q
python -m pytest -q
```

The focused pytest command checks the repaired Stage 0.03 registry/publication gates. The full-suite command checks all deterministic surfaces available in the checked-out repo, including any Stage 0.05-adjacent tests present in that workspace. These commands do not prove future implementation is complete.
