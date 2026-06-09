# Stage 1 Impl — Slice 3 A: AI-Gateway Gap Analysis & Interface Design vs 1.09 / 1.11

Issue: GOV-88
Owner role: BackendCrawlerEngineer (`f26f530c`); CTO (`24fddc65`) consult
Stage: Stage 1 implementation — Slice 3 A, **analysis-only** (no runtime AI code under this issue)
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only · local/vault-only · AI lane gated
Created: 2026-06-08

## 0. Purpose & disposition of the Step-0 blocker

GOV-88 was blocked by **Step 0**: the governing contracts had to be on `main`
before anything could be designed against them. That blocker is **resolved** —
`origin/main` now carries both, merged by GOV-87:

- **1.09** Automation-vs-AI Boundary Matrix — `Docs/stage1-automation-ai-boundary-matrix-contract.md` (PR #17, commit `2177f15`).
- **1.11** Security/Privacy/Publication Gates — `Docs/stage1-security-privacy-publication-gates-contract.md` (PR #16, commit `ce0e6e6`).

This document is the Slice-3-A deliverable: it (1) inventories the on-`main`
1.07 model + Slice-1 SSOT fields actually implemented in migrations `0001→0008`,
(2) designs the AI-gateway interface by which **Lane 2** writes `produced_by=ai`
rows anchored to `evidence_links`, (3) defines the **gateway-run log/ledger
schema** required by `AI_GATEWAY_PROCESSING_WORKFLOW.md`, (4) gives a
clause-by-clause **gap matrix** mapping each gateway Lane 2–5 to 1.09 + 1.11, and
(5) decides the **additive** migration plan (`0009+`) that extends — never
rebuilds — the landed schema.

**This issue ships no runtime AI code.** It is a spec artifact a future
implementation issue consumes; that issue must name its own narrow Alpine step,
command, log, tests, gate, and reviewer lane (per 1.09 §10, 1.11 §12).

## Inputs read (daisy-chain evidence)

- Workflow: `AI_GATEWAY_PROCESSING_WORKFLOW.md` (the 6 processing lanes + the five
  gateway-log requirements), `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`,
  `RISK_ASSESSMENT_WORKFLOW.md`, `GATED_BETA_ACCESS_WORKFLOW.md`,
  `WORKFLOW_GOVERNANCE.md`, `BACKEND_CRAWLER_WORKFLOWS.md`.
- Governing contracts (on `main`): **1.09** boundary matrix (17-step §1, AI
  containment §2, determinism §3, HITL gates §4, failure §5, handoff §6, privacy
  §7), **1.11** publication gates (vocab §0, publication gate P1–P8 §1, PII
  boundary §2, access tiers §3, defamation §4, AI-content gating §5, security §6,
  handoff §9).
- Predecessor model (on `main`): **1.07** transcript/evidence/statement contract
  `Docs/stage1-transcript-evidence-statement-contract.md`; the Slice-2 gap
  analysis `Docs/stage2-transcript-evidence-gap-analysis.md`.
- Implemented schema (on `main`): `Database/migrations/0001_init.sql` →
  `0008_speakers_persons_roles_temporal.sql`.
- Enforcement code (on `main`): `scripts/publication.py` (the enum + `compute_ui_status`
  + web-safe allowlist of record), `scripts/statements.py` (no-orphan-claims +
  pointer validation), `scripts/speakers.py` (speaker safety),
  `scripts/db.py` (the additive/idempotent migration ledger).

---

## 1. Inventory — the on-`main` model this slice extends

### 1.1 Migration ledger (implemented, `0001`→`0008`)

| File | Lands | Slice |
|---|---|---|
| `0001_init.sql` | base `meetings` / `transcripts` (INTEGER PKs) | pre-Stage-1 (WEI) |
| `0002_transcript_title.sql` | `transcripts.title` | WEI |
| `0003_sources.sql` | `sources` registry (TEXT `source_id` PK) — the 1.07 `source_record` | Slice 1 |
| `0004_crawl_runs_lane1.sql` | `crawl_runs` (deterministic Lane-1 run records) | Slice 1 |
| `0005_ssot_publication.sql` | **SSOT columns on `sources`**: `produced_by`, `review_state`, `publication_state`, `source_changed`, `ui_status` | Slice 1 D |
| `0006_agenda_transcript_segments.sql` | `agenda_items`, `transcript_segments` (TEXT slug PKs) | Slice 2 B |
| `0007_statements_evidence.sql` | `statements`, `evidence_links` (the load-bearing claim + pointer) | Slice 2 C |
| `0008_speakers_persons_roles_temporal.sql` | `persons`, `roles`, `served_in_role`, `speaker_attributions`, `made_statement`, `outcomes`, `outcome_updates` | Slice 2 D |

Migration runner (`scripts/db.py::apply_migrations`) is **re-run safe**: a
`schema_migrations` ledger skips applied files, and each `ADD COLUMN` is guarded
by a `PRAGMA table_info` existence check. New `0009+` files must hold this same
additive + idempotent posture.

### 1.2 SSOT publication columns (Slice 1, `0005`)

On `sources` (and mirrored onto record tables in `0007`/`0008`):

- `produced_by` — `CHECK (produced_by IN ('automation','ai','human'))`, default
  `'automation'`. **On `sources` the `'ai'` value is already legal.**
- `review_state` — default `'unreviewed'` (reviewer-state; never web-projected).
- `publication_state` — `CHECK (... IN ('not_publishable','publishable'))`,
  default `'not_publishable'`. The data-layer publish gate.
- `source_changed` — `0|1`, default `0` (the `sourceChanged` signal split out of
  the verificationStatus enum).
- `ui_status` — nullable; `NULL` = not-yet-computed = treated non-publishable.

### 1.3 The 1.07 record model (Slice 2, `0006`–`0008`)

- `statements` — the claim node. Carries the **6-value record** `verification_status`
  enum directly (`source_recorded`, `machine_extracted_unreviewed`,
  `reviewed_source_linked`, `human_verified`, `disputed`, `do_not_publish`),
  plus `layer` (`known_then`/`presented_then`/`ai_thought_then`/`corrected_later`/`actual_later`),
  `confidence` (`high`/`medium`/`low`), `publication_state`, `review_state`,
  `correction_status`, `source_changed`, `ui_status`, a forward-only
  `updates_statement_id` self-reference, and a forward-pointer `speaker_attribution_id`.
  **`statements.produced_by CHECK (produced_by IN ('automation','human'))` — `'ai'`
  is deliberately EXCLUDED at the DB layer this far** (0007 scope lock: "no AI
  extraction path lands here").
- `evidence_links` — the typed join from a node to its substantiating source.
  `to_source_id` is **REQUIRED** and FK-resolves to `sources` (a pointer whose
  source doesn't resolve is rejected). Carries `relation`
  (`references`/`supports`/`contradicts`/`corrects`/`substantiates`), the flat
  pointer object (`locator_kind` ∈ `timestamp`/`page`/`section`/`paragraph` + the
  matching locator field), `archive_status`, and the same 6-value
  `verification_status`/`confidence`.
- `speaker_attributions` — separately-typed so identity can be uncertain WITHOUT
  weakening the statement's pointer. `attribution_state`
  (`attributed`/`uncertain`/`unattributed`), `speaker_class`
  (`on-record-official`/`on-record-public`/`unidentified`/`private-context`),
  `candidate_person_id` (reviewer-only hint, never web-projected), and the
  row-level invariant `CHECK (person_id IS NULL OR attribution_state='attributed')`.
- `persons`/`roles`/`served_in_role`/`made_statement` — public-record identity
  graph with **privacy-by-schema-absence** (no address/voter/PII column exists; a
  column-name scan test enforces it).
- `outcomes`/`outcome_updates` — forward-only temporal layering (later updates
  earlier; known-then never mutated).

### 1.4 Enforcement layer (`scripts/publication.py`, `statements.py`, `speakers.py`)

- `ALLOWED_VERIFICATION_STATUSES` (6), `REVIEWED_VERIFICATION_STATUSES`
  (`reviewed_source_linked`, `human_verified`), `ALLOWED_UI_STATUSES` (10),
  `PUBLICATION_ELIGIBLE_UI_STATUSES` (3: `source-backed`, `archived-source-backed`,
  `corrected`).
- `compute_ui_status()` — rules #1–#12, top-down first-match-wins, **fail-closed
  default `pending-review`**. Rules 5/10/11 (the only publishable branches) are
  each guarded by `reviewed = status ∈ REVIEWED_VERIFICATION_STATUSES`.
  `machine_extracted_unreviewed → unverified` (rule 7, NOT publishable).
- Import-time parity guard: `set(_VERIFICATION_STATUS_ROLES) ==
  ALLOWED_VERIFICATION_STATUSES` (a 7th status without a role fails import, not
  fail-opens).
- `WEB_SAFE_FIELD_ALLOWLIST` / `to_web_safe()` — the single fail-closed serializer
  for the backend→frontend handoff; every non-allowlisted key (raw paths, reviewer
  notes, `candidate_person_id`, `transcript_path`) is dropped.
- `statements.py::insert_statement` + `validate_pointer` — the **no-orphan-claims**
  rule (1.07 §2.3): a statement is valid only with a `segment_id` edge OR ≥1
  `evidence_link` carrying a complete, FK-resolving pointer. `STATEMENT_PRODUCED_BY`
  is the app-layer set, currently `{automation, human}` (a strict subset of
  `publication.ALLOWED_PRODUCED_BY`).

**Net inventory finding:** the model is AI-ready *by design but not yet by
permission*. The SSOT layer (`sources.produced_by`, `confidence`, `layer:
ai_thought_then`, `review_state`, `compute_ui_status` rule 7) already anticipates
an AI lane; the only deliberate blocks on Lane 2 are (a) the
`statements.produced_by` DB CHECK excluding `'ai'`, (b) the matching app-layer
`STATEMENT_PRODUCED_BY` set, and (c) the absence of a run-provenance ledger. Slice
3 adds exactly those — nothing in the 1.07 model is re-typed or rebuilt.

---

## 2. AI-gateway interface design — how Lane 2 writes `produced_by=ai`

Lane 2 (AI-assisted extraction) is the only gateway lane that *creates record
content*. Its single job is to propose `statements` (and their `evidence_links`)
as **labeled, non-authoritative drafts**, then stop at a non-reviewed status. It
never touches a gating field (1.09 §2.3).

### 2.1 The write contract (what a Lane-2 writer MUST do)

A Lane-2 extraction, for each proposed claim, calls the **same**
`statements.insert_statement` path Slice 2 uses, with these AI-specific bindings:

| Field | Lane-2 (`produced_by=ai`) binding | Rule source |
|---|---|---|
| `produced_by` | `'ai'` | 1.09 §6.1, 1.05-b |
| `verification_status` | `'machine_extracted_unreviewed'` (single entry status; never a reviewed value) | 1.09 §2.1, 1.11 §5 |
| `review_state` | `'unreviewed'` | 1.09 §6.1 |
| `publication_state` | `'not_publishable'` (default; AI never flips it) | 1.11 §1 P1/P2 |
| `layer` | `'ai_thought_then'` where the row is an AI interpretation | 1.07 §4, 1.09 §6.1 |
| `confidence` | the model's confidence label (`high`/`medium`/`low`) | 1.07, 1.09 §6.1 |
| `is_verbatim` | `0` for any AI paraphrase (an AI paraphrase is NEVER rendered as a verbatim quote) | 1.09 §2.2 |
| `evidence_link` | ≥1 complete, FK-resolving pointer to `sources` — else the claim is an **orphan and rejected at extraction** | 1.07 §2.3, 1.09 §2.4, 1.11 P3 |
| `ai_extraction_run_id` | the FK to the gateway-run ledger row (§3) — NEW this slice | `AI_GATEWAY` §17, 1.11 §6.5 |

**The anchor-to-`evidence_links` requirement is the load-bearing rule.** A
`produced_by=ai` statement with no resolving pointer is an orphan claim;
`validate_pointer` already rejects it, and Lane 2 inherits that rejection
unchanged. AI output that cannot produce a pointer is *not stored and not
surfaced* — it does not fall back to a softer state.

### 2.2 What a Lane-2 writer MUST NOT do (inherited prohibitions)

- MUST NOT set or modify `verification_status` to any reviewed value,
  `publication_state='publishable'`, `ui_status`, `correction_status`,
  `attribution_state`, hashes, or archive state (1.09 §2.3 — AI never writes
  gating fields).
- MUST NOT assign a speaker. A Lane-2 writer may write a
  `speaker_attributions.candidate_person_id` (reviewer-only hint, `uncertain`
  state) but never a bound `person_id`, never a `made_statement` edge, never an
  `on-record-public` name (CEO hard stop — 1.07 §3, 1.09 step 9 / G1).
- MUST NOT promote itself by confidence score, model agreement, or
  self-evaluation (1.09 §2.5). The only promotion is the human G2 gate.
- MUST NOT write any accusation / motive / legal conclusion (1.11 §4 — held at T0).

### 2.3 The single AI→public path (structural invariant)

```
Lane 2 (AI write)            Lane 3 (compare/flag)      Lane 5 (human gate)        Lane 6 (DET publish)
produced_by=ai          →    DET compares draft     →   HUM promotes verification → compute_ui_status()
verification=machine_…       to source at pointer;      status to reviewed_source_  returns source-backed;
publication=not_pub          flags uncertainty          linked / human_verified     publication gate P1–P8;
ai_thought_then              (writes NO gating field)   (1.09 step 11 / G2)         to_web_safe() projects
```

There is **no other edge** from "AI produced this" to "the public can see this".
Every Lane-2 row terminates at `machine_extracted_unreviewed → unverified` (rule
7), invisible to residents, visible only to reviewers, until a human moves it and
the deterministic publication gate allows it. This is the same invariant 1.09
§1.2 and 1.11 §0 commit to; Slice 3 implements it without weakening it.

---

## 3. Gateway-run log / ledger schema

`AI_GATEWAY_PROCESSING_WORKFLOW.md` §17 requires that gateway logs record **input
source set, model/tool version, output artifact, errors, reviewer state, and
retry status**; 1.09 step 17 requires a vault-only run manifest; 1.11 §6.5
requires every gate decision be auditable without leaking secrets/PII. One new
table, `ai_extraction_runs`, satisfies all three, plus a thin provenance link so
every AI record names the run that produced it.

### 3.1 `ai_extraction_runs` (the gateway-run ledger) — required fields

| Column | Type / domain | Maps to requirement |
|---|---|---|
| `run_id` | TEXT slug PK (e.g. `alpine:ai-extract:2026-06-08:001`) | run identity (1.09 §17 manifest) |
| `lane` | TEXT `CHECK IN ('2_extraction','3_verification','4_risk')` | which gateway lane the run executed |
| `input_source_ids` | TEXT JSON array of `sources.source_id` | **input source set** (§17) |
| `input_segment_ids` | TEXT JSON array of `transcript_segments.segment_id` (nullable) | input artifact set |
| `model_name` | TEXT (e.g. `claude-…`) nullable | **model/tool version** (§17) |
| `model_version` | TEXT nullable | model/tool version |
| `tool_version` | TEXT (the GOV gateway script version/git sha) | tool version |
| `prompt_id` | TEXT (versioned, source-grounded prompt identifier) | reproducibility / §16 grounded-prompt |
| `output_statement_ids` | TEXT JSON array of produced `statements.statement_id` | **output artifact ids** (§17) |
| `output_evidence_link_ids` | TEXT JSON array of produced `evidence_links.evidence_link_id` | output artifact ids |
| `output_count` | INTEGER | run summary (the only count surfaced to Paperclip comments — 1.11 §2.1) |
| `orphan_rejected_count` | INTEGER | claims rejected for no pointer (1.09 §2.4 / §5) |
| `error_status` | TEXT `CHECK IN ('ok','partial','failed')`, default `'ok'` | **errors** (§17); fail-closed |
| `error_detail` | TEXT nullable (vault-only; never web-projected) | error detail |
| `reviewer_state` | TEXT `CHECK IN ('unreviewed','in_review','approved','rejected')`, default `'unreviewed'` | **reviewer state** (§17), 1.09 §4 |
| `retry_of_run_id` | TEXT nullable FK → `ai_extraction_runs(run_id)` | **retry status** (§17) — forward-only retry chain |
| `retry_count` | INTEGER default 0 | retry status |
| `dry_run` | INTEGER `0|1` default `1` | 1.09 step 17 (`--dry-run` default) |
| `started_utc` / `finished_utc` | TEXT | run manifest timing |

### 3.2 Ledger rules (carry the workflow guarantees)

- **Vault-only.** The ledger and its `error_detail` are local/vault-only
  (1.11 §2.1 "run logs: local only; summary counts only in Paperclip comments";
  1.09 §7.1). `ai_extraction_runs` is NOT on `WEB_SAFE_FIELD_ALLOWLIST`; a
  column-name guard test asserts it can never be web-projected.
- **Fail-closed errors block downstream.** `error_status='failed'` or `'partial'`
  must block presentation of that run's outputs until repaired or owner-waived
  (`AI_GATEWAY` rule "failed gateway processing must block downstream"). The
  produced rows stay `not_publishable` regardless (default posture), so a failed
  run cannot leak.
- **Retry is forward-only.** `retry_of_run_id` points back at the superseded run;
  the prior run row is never mutated (mirrors `outcome_updates` / `updates_statement_id`).
- **Auditable, no secrets.** The ledger records *what ran, on what, with which
  model, producing what, with what reviewer state* — never a token, prompt
  secret, or PII (1.11 §6.1 / §6.5).
- **Threshold issue.** 3+ consecutive `failed` runs, any scope leak, or any
  missing-but-expected artifact opens a Paperclip issue (1.09 §5 automation
  thresholds; `BACKEND_CRAWLER_WORKFLOWS`).

### 3.3 Per-record provenance link

Each AI-produced `statements` / `evidence_links` row names its run. Decision
(see §5 D-2): add a nullable `ai_extraction_run_id TEXT` column to `statements`
and `evidence_links` (FK → `ai_extraction_runs`), rather than a separate join
table — the relationship is run→many-records (1:N), the column is additive and
idempotent-guardable, and it keeps "which run produced this claim" a single
join. The column is reviewer/provenance state — **not** on the web-safe allowlist.

---

## 4. Clause-by-clause gap matrix — gateway Lanes 2–5 vs 1.09 + 1.11

Legend: **HAVE** = enforced on `main` today · **GAP** = Slice-3 (or later) must add ·
**N/A-here** = correctly out of scope for this slice (later slice / owner gate).

### 4.1 Lane 2 — AI-assisted extraction (propose statements/events/topics with confidence + anchors)

| # | Clause (1.09 / 1.11) | State | Where / what closes it |
|---|---|---|---|
| L2-1 | AI enters at non-reviewed status only (1.09 §2.1) | **HAVE** | `statements.verification_status` default `machine_extracted_unreviewed`; `compute_ui_status` rule 7 → `unverified` |
| L2-2 | `produced_by='ai'` permitted for record rows (1.09 §6.1) | **GAP** | `statements.produced_by` CHECK excludes `'ai'`; widen via §5 D-1 |
| L2-3 | Every AI claim anchored to a resolving `evidence_link` pointer; orphan rejected (1.07 §2.3, 1.09 §2.4, 1.11 P3) | **HAVE** | `statements.py::validate_pointer` + no-orphan-claims, FK on `to_source_id` |
| L2-4 | AI content carries an explicit label + `ai_thought_then` layer; paraphrase `is_verbatim=0` (1.09 §2.2) | **HAVE (schema)** / **GAP (label field)** | `layer` enum + `is_verbatim` exist; no explicit `ai_label` column — decide §5 D-3 |
| L2-5 | AI never writes a gating field (1.09 §2.3) | **HAVE** | enforced by app-layer writers + DB defaults; Lane-2 writer inherits |
| L2-6 | Confidence captured per row (1.09 §6.1) | **HAVE** | `statements.confidence` / `evidence_links.confidence` |
| L2-7 | AI scratch (drafts, chain-of-thought, candidate names) vault-only (1.09 §7.1, 1.11 §2.1) | **HAVE (boundary)** / **GAP (ledger)** | `WEB_SAFE_FIELD_ALLOWLIST` drops them; `ai_extraction_runs` ledger is new (§3) |
| L2-8 | Run records input source set / model version / outputs / errors / retries (`AI_GATEWAY` §17) | **GAP** | `ai_extraction_runs` table (§3) |
| L2-9 | Per-record run provenance (1.11 §6.5 audit) | **GAP** | `ai_extraction_run_id` FK (§3.3, §5 D-2) |

### 4.2 Lane 3 — Verification layer (compare AI output to primary source, assign label, flag uncertainty)

| # | Clause | State | Where / what closes it |
|---|---|---|---|
| L3-1 | Deterministic compare of draft to source at the pointer (1.09 step 11 prep, §3) | **IMPLEMENTED (GOV-90)** | `scripts/ai_verification.py::classify` token-grounding compare; reads pointer, flags mismatch — writes NO gating field (verdict in the `ai_verification_results` side table, claim row untouched) |
| L3-2 | The *verification label* (promotion to reviewed) is a HUMAN action, not Lane-3 automation (1.09 step 11 / G2, 1.11 §5) | **HAVE (rule)** | only a reviewer may set `reviewed_source_linked`/`human_verified`; Lane 3 may only flag |
| L3-3 | Source-trail completeness check — no orphan claims (1.11 P3) | **HAVE** | export/insert validation; FK-resolving pointer |
| L3-4 | `sourceChanged` / `source-missing` re-review signals (1.09 §5, `compute_ui_status` rules 3/4) | **HAVE** | `source_changed` column + rules 3/4 |
| L3-5 | Uncertainty flag surfaced to reviewer only, never auto-promoted (1.09 §5 low-confidence) | **IMPLEMENTED (GOV-90)** / **GAP (queue UI)** | `ai_verification_results.uncertainty_flag` + `contested`; a low-confidence claim is capped at `uncertain` (never auto-matched) and stays `unverified`; `latest_verdict()` is the backend read the reviewer-queue UI (a later slice) will consume |
| L3-6 | Lane-3 run is auditable (`reviewer_state` on the ledger) (1.11 §6.5) | **IMPLEMENTED (GOV-90)** | `ai_verification.run_verification` opens `ai_extraction_runs.lane='3_verification'` with `reviewer_state` + input set / tool version / errors / retry (§3) |

### 4.3 Lane 4 — Risk layer (privacy / legal / publication / moderation no-go)

| # | Clause | State | Where / what closes it |
|---|---|---|---|
| L4-1 | Private/PII never collected, never published; redact-before-store (1.11 §2.1/§2.3) | **HAVE** | privacy-by-schema-absence (0008 header + column-scan test); no PII columns exist |
| L4-2 | Zero private field in any web projection (1.11 P4) | **HAVE** | `to_web_safe()` fail-closed allowlist + `WEB_UNSAFE_FIELDS` guard |
| L4-3 | Defamation/legal gate — no accusation/motive/legal conclusion without owner record (1.11 §4, P6) | **HAVE (rule)** / **N/A-here** | reviewer + owner sign-off; this slice writes no such content (owner gate) |
| L4-4 | Speaker safety — no name over wrong name; uncertain → gated label (1.07 §3, 1.09 step 9, 1.11 §4.2) | **HAVE** | `speaker_attributions` CHECK + `speakers.safe_speaker_label` |
| L4-5 | Risk run flags no-go and blocks downstream (`AI_GATEWAY` lane 4 + "failed blocks downstream") | **GAP** | `ai_extraction_runs.lane='4_risk'` + `error_status` blocking semantics (§3.2) |
| L4-6 | Scope = Alpine-only; non-Alpine rejected (1.09 step 1, 1.11 P7) | **HAVE** | registry `scope` gate (Slice 1) |

### 4.4 Lane 5 — Human/reviewer gate (approve / correct / dispute / hold / reject)

| # | Clause | State | Where / what closes it |
|---|---|---|---|
| L5-1 | Only a human promotes non-reviewed → reviewed (1.09 step 11 / G2, 1.11 P1) | **HAVE (rule)** / **GAP (tooling)** | enum + `reviewed` guard exist; a reviewer-action tool/CLI is a later slice |
| L5-2 | Dispute / do-not-publish are terminal-gated, never auto-cleared (1.09 G3, 1.11 §4.3) | **HAVE** | `verification_status` values + `compute_ui_status` rules 1/2 |
| L5-3 | Correction is forward-only; known-then never overwritten (1.09 step 14 / G4, 1.11 §4.3) | **HAVE** | `updates_statement_id`, `outcomes`/`outcome_updates`, `corrected` rule 5 (reviewed guard) |
| L5-4 | Publication is an OWNER (CEO/Isaac) decision (1.09 G6, 1.11 P8) | **N/A-here** | owner gate; this slice authorizes no publication |
| L5-5 | Every gate decision auditable (who/when/reason) (1.11 §6.5) | **HAVE (Paperclip)** / **GAP (backend audit)** | Paperclip artifact today; `ai_extraction_runs.reviewer_state` adds the backend hook |
| L5-6 | Access tiers T0/T1/T2; AI drafts never leave T0 unreviewed (1.11 §3) | **HAVE (boundary)** / **N/A-here (tiering)** | raw/AI scratch vault-only; the T1/T2 surface is a frontend/gated-beta slice |

**Coverage statement:** every Lane 2–5 clause derived from `AI_GATEWAY` §1, the
1.09 17-step matrix (steps 7–14, 17), and the 1.11 publication gate (P1–P8, §2,
§4, §5, §6.5) is mapped above to HAVE / GAP / N/A-here. The **GAPs Slice 3
implementation must close** reduce to: (a) permit `produced_by='ai'` on records
[L2-2], (b) the `ai_extraction_runs` ledger + per-record provenance [L2-8/9,
L3-6, L4-5, L5-5], (c) an optional explicit `ai_label` field [L2-4], and (d)
deterministic Lane-3 compare + Lane-5 reviewer tooling [L3-1/5, L5-1] (which a
*later* implementation issue owns; this slice specs them, builds none).

---

## 5. Additive migration plan (`0009+`) — extend, do not rebuild

All changes are additive and idempotent-guarded, reuse the 1.07 enums/fields
verbatim (no re-typing), and duplicate no Slice-1/2 code. No table is dropped.

### Decision D-1 — permit `produced_by='ai'` on `statements` (and `evidence_links` if it gains the column)

`statements.produced_by CHECK (... IN ('automation','human'))` (0007) physically
blocks `'ai'`. **SQLite cannot `ALTER` an existing CHECK constraint** — this is
the one place "additive" needs a documented technique, not a one-liner.

**CTO ruling (GOV-88 sign-off, agent `24fddc65`):** the impl issue MUST widen the
**DB-level** CHECK on `statements.produced_by`; app-layer widening alone is
**necessary but not sufficient**. Rationale + the two options weighed:

- **App-layer widening alone — REJECTED as insufficient.** Widening
  `statements.py::STATEMENT_PRODUCED_BY` to `{automation, ai, human}` is required,
  but it does **not** make `'ai'` rows land. A SQLite CHECK is enforced by the
  engine on **every** INSERT/UPDATE — including parameterized writes through
  `insert_statement` — not only "direct SQL". With the 0007 CHECK left at
  `IN ('automation','human')`, a `produced_by='ai'` write fails at the DB layer
  regardless of code path, so Lane 2 would not function. (Corrects the earlier
  "value space governed by the Python set" framing.)
- **Controlled `statements` rebuild in `0009` — SELECTED.** SQLite cannot `ALTER`
  a CHECK, so widen it via the guarded 12-step rebuild: `statements_new` with
  `CHECK (produced_by IN ('automation','ai','human'))`,
  `INSERT INTO statements_new SELECT * FROM statements`, drop, rename, recreate
  indexes/triggers — each step `IF NOT EXISTS`/`PRAGMA`-guarded so a re-run is a
  no-op. **This is low-risk, not high-risk: it mirrors landed precedent** —
  migration `0005_ssot_publication.sql:29` already carries the identical widened
  literal `CHECK (produced_by IN ('automation','ai','human'))`. The rebuild
  reproduces an existing in-schema constraint, touches no row values, and only
  grows the value space (strictly additive in effect).

**Both layers move together:** the Python `STATEMENT_PRODUCED_BY` set and the new
DB CHECK must be widened in the same `0009` change, and the **D-4 parity guard**
(`set(STATEMENT_PRODUCED_BY) == ALLOWED_PRODUCED_BY` and the CHECK literal == the
Python enum) must assert they agree — preventing the two-layer drift that caused
the rejected option's confusion. Wrap the rebuild in a transaction with
`foreign_keys=OFF` for the swap, re-enable + `PRAGMA foreign_key_check` after, per
the standard SQLite recipe.

### Decision D-2 — `ai_extraction_runs` ledger + per-record provenance (`0009`)

- `CREATE TABLE IF NOT EXISTS ai_extraction_runs (…)` per §3.1 — a NEW node, no
  landed equivalent, TEXT slug PK, all CHECK literals mirroring the §3 domains.
- `ALTER TABLE statements ADD COLUMN ai_extraction_run_id TEXT REFERENCES ai_extraction_runs(run_id)`
  (nullable; `PRAGMA table_info`-guarded). Same on `evidence_links`. Nullable so
  every existing automation/human row is untouched and valid.

### Decision D-3 — explicit `ai_label` (defer / optional, `0009` or later)

1.09 §6.1 names an `ai_label` ∈ `{none, AI-generated, AI-paraphrased,
AI-summarized}` for the provenance chip. The model can already *derive* the chip
from `produced_by` + `is_verbatim` + `layer`. **Decision:** add a nullable
`ai_label TEXT` column to `statements` only if the frontend slice (1.06) needs it
as a stored value rather than a derived one; otherwise the frontend computes it.
Recommend deferring to the frontend-handoff implementation issue to avoid a
column the UI may never read. Not a blocker for Lane 2.

### Decision D-4 — no new status vocabulary

`0009` introduces **no** new `verification_status`, `ui_status`, publication
allowlist, `layer`, or `confidence` value. It reuses
`publication.ALLOWED_VERIFICATION_STATUSES` etc. verbatim. The import-time parity
guard (`set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES`) must
stay green; a parity test asserts any new CHECK literal matches the Python enum
(same pattern as `tests/test_statements_evidence.py`).

### Decision D-5 — tests the implementation issue must add (named, not run here)

- A fixture proving a Lane-2 write enters at `machine_extracted_unreviewed` →
  `unverified`, `not_publishable`, with a resolving pointer (and an orphan AI
  claim is rejected).
- A test that `ai_extraction_runs` and `ai_extraction_run_id` are NOT web-projected
  by `to_web_safe()` (extends the §2 private-field test).
- A parity test that the widened `produced_by` set == `ALLOWED_PRODUCED_BY` and
  any `0009` CHECK literal matches its Python enum.
- A `statements`-rebuild safety test (per CTO D-1 ruling): row count and a content
  digest of every landed row are identical pre/post `0009`; a `PRAGMA
  foreign_key_check` returns empty after the swap; the migration is idempotent
  (second run is a no-op); and a `produced_by='ai'` INSERT through `insert_statement`
  now **succeeds** at the DB layer (proving the CHECK was actually widened).
- A test that `error_status ∈ ('partial','failed')` keeps the run's outputs
  non-publishable (fail-closed downstream block).

---

## 6. Acceptance criteria — self-check

| Criterion (GOV-88) | Met by | Status |
|---|---|---|
| Gap matrix complete (every Lane 2–5 clause → 1.09/1.11) | §4.1–§4.4 (L2-1…L5-6) with HAVE/GAP/N/A-here + closure | ✅ |
| Gateway-run-log schema defined | §3 (`ai_extraction_runs` fields, rules, provenance link) | ✅ |
| Additive migration plan decided; reuses 1.07 enums/fields (no re-typing); no Slice-1/2 duplication | §5 D-1…D-5 — CTO ruling: `0009` guarded `statements` CHECK rebuild (mirrors landed `0005` literal) + matching Python-set widening + parity guard + `ai_extraction_runs` ledger; no new vocab; effect strictly additive | ✅ |
| Analysis-only — no runtime AI code | this issue ships one `Docs/` file; all code is named for a future impl issue | ✅ |
| Reviewer sign-off + PR squash-merged to `main` | §7 reviewer lanes; PR + CTO merge | ⏳ pending review |

---

## 7. Reviewer lanes, risk, and scope lock

### 7.1 Reviewer lanes (agent sign-off)

- **CTO (`24fddc65`)** — technical consult/sign-off: the §5 D-1
  app-layer-vs-rebuild decision for `produced_by='ai'`, ledger schema
  completeness, the additive/no-rebuild posture, and next-gate readiness. Per the
  GOV done-bar pattern, CTO pairs feasibility sign-off with the merge.
- **VerificationSafetyReviewer (`3f95c8ce`)** — the single AI→public path (§2.3),
  AI-never-writes-gating-fields, no-orphan-claims, fail-closed defaults.
- **SecurityPrivacyAgent (`72d0eccf`)** — consulted on §3.2 / §4.3 (ledger +
  `error_detail` vault-only, no PII/secret in the audit record, no web projection).

### 7.2 Risk classification (per `RISK_ASSESSMENT_WORKFLOW`)

- **AI-overclaim (primary):** mitigated by single non-reviewed entry status,
  mandatory anchor-to-`evidence_links`, AI-never-writes-gating-fields, and the
  single AI→public path through human promotion + the deterministic publication
  gate.
- **Evidence/source:** mitigated by reuse of the no-orphan-claims validator and
  FK-resolving pointers; the ledger records the input source set.
- **Privacy/account:** mitigated by ledger vault-only + not on the web-safe
  allowlist; privacy-by-schema-absence is unchanged.
- **Defamation/legal & moderation:** unchanged owner gates; this slice writes no
  accusation/legal/campaign content and authorizes no publication.
- **Publication/readiness:** mitigated by keeping this analysis-only; every Lane
  2–5 runtime step stays a locked, gated, future implementation issue.

### 7.3 Locked scope (what GOV-88 does NOT authorize)

No runtime AI code; no scheduler/crawler/transcriber/extractor/validator/exporter/UI;
no AI or automation run against real Alpine targets; no fetch/transcribe/process of
new material; no publication to any surface; no official/subscriber contact; no
accusation/legal/campaign wording; no `on-record-public` naming; no redefinition of
`verificationStatus`/`uiStatus-map.v1`/the publication allowlist (owned by
GOV-36/37/38/39); no scope beyond the Town of Alpine. Each is an owner-escalation
trigger → stop, route to CEO/Isaac.

---

## 8. Verification evidence

- **File:** `Docs/stage3-ai-gateway-gap-analysis.md` (this artifact; line count in
  the GOV-88 closeout comment).
- **Inputs verified this run:** 1.09 (`2177f15`) + 1.11 (`ce0e6e6`) confirmed on
  `origin/main`; migrations `0005`/`0006`/`0007`/`0008` and `scripts/publication.py`
  (enums, `compute_ui_status` rules 1–12, `WEB_SAFE_FIELD_ALLOWLIST`),
  `scripts/statements.py` (no-orphan-claims, `STATEMENT_PRODUCED_BY`),
  `scripts/db.py` (additive ledger) read directly from the worktree.
- **Tests:** spec-only; no code changed on this branch. The per-step tests a
  future implementation issue must add are named in §5 D-5.
- **Next action:** open the GOV-88 PR (this single file) targeting `main`; route
  to VerificationSafetyReviewer + SecurityPrivacyAgent for sign-off and CTO for
  the technical consult + squash-merge. The downstream unlock is the next Slice-3
  implementation issue, which must consume this analysis and name its own Alpine
  step, command, log, tests, gate, and reviewer lane.
