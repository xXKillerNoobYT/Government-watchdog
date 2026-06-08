# Stage 1 Backend Inventory & Gap Analysis vs Contracts 1.02 / 1.03 / 1.04 / 1.05

Issue: GOV-72 (`[Stage 1 Impl][Backend] A — Backend inventory & gap analysis`)
Stage: Stage 1 Alpine implementation — Slice 1, Issue A (first, unblocked)
Owner role: BackendCrawlerEngineer; technical consult CTO; reviewer VerificationSafetyReviewer
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: **Town of Alpine only.** Local/vault-only. No public surface, no publication, no AI extraction run.
Created: 2026-06-08
Branch: `GOV-72-backend-gap-analysis`
Artifact class: tool/spec analysis (versionable to GitHub per the WORKFLOW_GOVERNANCE data-publication boundary; **not** raw crawled data).

---

## 0. Purpose and method

This document is the planning artifact that lets implementation issues **B–E extend the
existing backend instead of rebuilding it** (COMPANY non-negotiable: *no duplicate
implementations — reuse and extend the existing repo*). It produces:

1. an inventory of the backend at HEAD (`fb5edfb`): schema, scripts, provenance fields, tests;
2. a **clause-by-clause gap matrix** for contracts 1.02/1.03/1.04/1.05 — each clause group →
   `exists` / `partial` / `missing`, with file/line evidence;
3. a **migration plan** — which tables extend additively, which fields/tables are net-new;
4. an **extend-vs-rebuild decision** per component, with rationale.

### Evidence basis (files read)

Backend at HEAD (`fb5edfb`):
- `Database/migrations/0001_init.sql`, `Database/migrations/0002_transcript_title.sql`
- `scripts/db.py`, `scripts/crawl_pdfs.py`, `scripts/fetch_transcripts.py`,
  `scripts/extract_metadata.py`, `scripts/embed.py`, `scripts/phase1_run.py`,
  `scripts/watchdog_brief.py`, `scripts/cleanup_junk.py`
- `tests/test_crawl_pdfs.py`, `tests/test_fetch_transcripts.py`, `tests/test_embed.py`,
  `tests/test_smoke.py`; `pytest.ini`; `.github/workflows/local-runner-smoke.yml`

Contracts (clause sources):
- **1.05** — `Docs/stage1-backend-tooling-implementation-contract.md`
  (on unmerged branch `gov-17-newsletter-briefing-contract`; only 1.15 is on `main`).
- **1.05 uiStatus / verificationStatus enums + validator** —
  `scripts/validate_concept_map_export.py` (same unmerged `gov-17` branch).
- **1.02** (registry schema + raw preservation) —
  vault `Docs/Goal-Spec-Packages First Draft/GOV-GOAL-stage-1-alpine-source-inventory-02-registry-raw-preservation.md`.
- **1.03** (archive / crawl evidence / reproducibility) —
  vault `…-03-archive-crawl-evidence-reproducibility.md`.
- **1.04** (raw preservation / reproducibility) —
  vault `Docs/Source-Data/2026-06-07-GOV-32-Alpine-Raw-Preservation-Reproducibility-Contract.md`
  (SourceArchivist mirror of repo contract `Docs/stage1-alpine-raw-preservation-reproducibility-contract.md`,
  which is **not present in the repo at HEAD** — see §2 finding F4).
- CTO sequencing recommendation on GOV-71 §0/§2 (the issue's cited source).

> **Numbering note.** The vault goal-spec files are numbered `02`/`03` within the
> source-inventory goal series; the Stage-1 *contract* numbering used by GOV-71/GOV-72
> is 1.02/1.03 (source/data inventory) + 1.04 (raw preservation/reproducibility) + 1.05
> (backend/tooling SSOT + uiStatus). This document maps clauses by topic, so the two
> numbering schemes are reconciled rather than treated as a conflict.

---

## 1. Decisive context — this is NOT greenfield, and there are TWO provenance systems

A working WEI-era Phase 1 implementation already exists in the repo. The single most
important structural finding for sequencing:

> **The backend has two disconnected provenance systems.**
> 1. **The SQLite pipeline** (`documents`, `transcripts`, `meetings`, `crawl_runs`) in the
>    repo — populated by `crawl_pdfs.py` / `fetch_transcripts.py`, with real raw-preservation
>    primitives (`sha256`, `local_path`, `fetch_time_utc`) and a run log.
> 2. **The vault source-registry** (`Docs/Source-Data/alpine-source-inventory.json` +
>    `Docs/Source-Data/source-registry/*.md` + `stage1/latest.*`) — the contract-shaped
>    registry with `source_id`, `source_class`, `raw_preservation_status`,
>    `verification_status`, `archive_status`, etc.
>
> **Neither references the other.** Documents carry a per-row `source_url` but no
> `source_id`; the registry describes sources but is not joined to any crawled artifact.
> The contracts (1.02/1.03/1.05) assume a *single* registry that crawler code consumes and
> that frontend/API reads. **The core work of this slice is to reconcile these two systems
> into one DB-backed registry — an additive extension, not a rebuild.**

Second structural finding: **the registry-tooling code never landed in the repo.** Contracts
1.03/1.05 list `scripts/source_inventory.py`, `scripts/stage1_check.py`,
`scripts/wayback_check.py`, `scripts/validate_sources.py`, `scripts/crawl_summary.py`,
`scripts/data_boundary_check.py` as *existing* backend support scripts. **All six are absent
at HEAD** (verified: `ls scripts/` shows only `crawl_pdfs.py`, `db.py`, `embed.py`,
`extract_metadata.py`, `fetch_transcripts.py`, `phase1_run.py`, `watchdog_brief.py`,
`cleanup_junk.py`). They survived only as vault *data outputs*, not as committed code. The
registry therefore exists today as **hand-maintained vault JSON + notes**, with no committed
generator/validator in the backend repo.

---

## 2. Existing backend inventory (HEAD `fb5edfb`)

### 2.1 Schema (`Database/migrations/`)

| Table | Key provenance-relevant columns | Evidence |
|---|---|---|
| `documents` | `source_url` (UNIQUE), `referer_url`, `title`, `doc_type`, `doc_date`, `local_path`, `sha256`, `size_bytes`, `fetch_time_utc`, `wayback_url`, `cms_signature`, `robots_status`, `raw_text` | `0001_init.sql:6-21` |
| `transcripts` | `video_id` (UNIQUE), `video_url`, `channel_id/title`, `upload_date`, `meeting_date`, `full_text`, `timestamped_text`, `local_path`, `sha256`, `fetch_time_utc`, `title` | `0001_init.sql:25-41`, `0002_transcript_title.sql:5` |
| `meetings` | `meeting_date`, `body`, `title`, `source_url`, `transcript_id` FK, `notes`, `fetch_time_utc` | `0001_init.sql:44-54` |
| `meeting_documents` | `meeting_id` FK, `document_id` FK, `role` | `0001_init.sql:56-61` |
| `embeddings` | `object_type`, `object_id`, `chunk_index`, `chunk_text`, `model`, `dim`, `vector`, `embed_time_utc` | `0001_init.sql:63-75` |
| `crawl_runs` | `started_utc`, `finished_utc`, `status`, `targets`, `new_documents`, `new_transcripts`, `notes` | `0001_init.sql:77-86` |

### 2.2 Scripts

| Script | Role | Provenance behavior (evidence) |
|---|---|---|
| `db.py` | Idempotent migration runner | `apply_migrations()` runs every `*.sql` sorted (`db.py:16-25`). **See §6 idempotency caveat.** |
| `crawl_pdfs.py` | PDF/HTML crawler | robots.txt parse+enforce (`161-185`, skip at `317`); rate-limit ≤20/min + 3–12s jitter (`105-127`); `sha256` (`136`,`337`); `fetch_time_utc` ISO-ms (`132`,`359`); CMS detect (`147`) but **hardcodes `cms_signature="static"`/`robots_status="allowed"` on insert** (`360-361`); idempotent URL+hash dedupe (`244-250`); per-target Alpine link filter `ALPINE_PATTERN` (`98`,`377`,`386`); writes `crawl_runs` (`440`). |
| `fetch_transcripts.py` | YouTube transcript ingest | yt-dlp channel discovery (`56-`); `sha256` of transcript text (`47-48`); writes `transcripts` + `crawl_runs` (`246`). |
| `extract_metadata.py` | Derive `doc_date`/`doc_type`/title from `raw_text` | reads `documents.raw_text` (`156`); never writes `raw_text` (`9`). |
| `embed.py` | PDF text extract + embeddings | **populates `documents.raw_text`** for empty rows (`140-157`); embeds `raw_text`/`full_text`. |
| `phase1_run.py` | Pipeline orchestrator | runs crawl → metadata → embed. |
| `watchdog_brief.py` | Reviewed brief generator (Phase 2 pilot) | reads corpus; out of slice-1 scope. |
| `cleanup_junk.py` | Local junk/log retention cleanup (GOV-54) | out of slice-1 scope. |

### 2.3 Tests / CI
- `tests/test_crawl_pdfs.py` (robots, scope filter, idempotency, dedupe),
  `tests/test_fetch_transcripts.py`, `tests/test_embed.py`, `tests/test_smoke.py`; `pytest.ini`.
- `.github/workflows/local-runner-smoke.yml` — local Mac runner smoke (`IA-Mac-GOV-Backend`).

---

## 3. Gap matrix — clause by clause

Legend: **exists** = present and contract-conformant at HEAD · **partial** = primitive/column
present but not contract-complete (or present only in vault, not repo) · **missing** = no
implementation at HEAD. "Decision" maps the clause to the slice issue (B/C/D/E) and to
extend-vs-add.

### 3.1 Contract 1.02 — Source registry schema & raw-preservation status

| # | Contract clause (1.02) | State | Evidence | Decision |
|---|---|---|---|---|
| 1.02-a | First-class **source registry** as the machine-consumable source contract crawler code consumes | **partial (vault-only)** | Registry exists as vault `alpine-source-inventory.json` + `source-registry/*.md`; **no `sources` table, no committed generator in repo** (§1) | **ADD** `sources` table + Alpine seed loader (Issue **B**). Extend, not rebuild — port vault rows in. |
| 1.02-b | Required registry fields: `source_id`, `name`, `scope`, `url`, `original_url`, `source_type`, `source_class`, `source_authority_level`, `jurisdiction`, `expected_artifacts`, `robots_policy`, `owner_agent`, `scan_date`, `last_validated_utc`, `archive_url`, `archive_status`, `raw_local_path`, `raw_sha256`, `raw_preservation_status`, `local_note_path`, `verification_status`, `correction_status`, `topic_tags`, `notes` | **missing (in DB)** | None of these are DB columns. `documents` has `source_url`/`sha256`/`wayback_url` only — document-level, not source-level | **ADD** as `sources` columns (Issue **B**). |
| 1.02-c | `source_id` stable, slug-like, not title-derived; FK link from crawled artifacts to source | **missing** | `documents`/`transcripts` have **no `source_id`** | **ADD** `source_id` FK column to `documents` + `transcripts` via additive migration (Issue **B**). |
| 1.02-d | `raw_preservation_status` enum (11 values incl. `seed_only_*`, `preserved_stage1`, `blocked_*`) — never omitted | **missing (in DB)** | Vault registry carries it; DB has no equivalent. Crawled-vs-seed distinction is implicit (a `documents` row exists or not) | **ADD** column on `sources` (Issue **B**); reconcile to raw store in **C**. |
| 1.02-e | `verification_status` enum + `correction_status` enum on every source | **missing (in DB)** | Not in schema. **Note enum divergence** — 1.02 lists an 11-value `verification_status`; 1.05/validator define a **6-value** `verificationStatus`. See §5 conflict. | **ADD** on `sources`/records (Issue **D**), resolving to the 6-value enum per §5. |
| 1.02-f | Per-source local registry **note** under `Docs/Source-Data/source-registry/`, repeating ID/class/authority/jurisdiction/URLs/hash/status | **exists (vault)** | Vault `source-registry/*.md` present (e.g. `alpinewy_gov.md`, `alpinewy_meetings.md`) | **REUSE** as `local_note_path` target; loader links DB row → note (Issue **B**). |
| 1.02-g | Source-inventory **command** that fails on non-Alpine scope, rejects invalid URLs, doesn't silently drop stale rows | **missing (in repo)** | `scripts/source_inventory.py` **absent at HEAD** (§1) | **ADD/port** loader+validator (Issue **B**); reuse vault JSON as seed input. |
| 1.02-h | Hash rules: preserved PDF/HTML/transcript require SHA-256; seed-only may be null; null requires status explaining why; mismatch ⇒ review not delete, keep old+new | **partial** | `sha256` computed+stored at fetch (`crawl_pdfs.py:337`, `fetch_transcripts.py:47`); **no null-vs-status rule, no mismatch/keep-both handling** (no re-hash verifier) | **EXTEND** in raw-preservation hardening (Issue **C**). |
| 1.02-i | Scan-date rules: `scan_date` immutable, `last_validated_utc` updates on revalidation, never silently refresh, never delete older raw | **missing** | `fetch_time_utc` exists but there is no `scan_date`/`last_validated_utc` separation and no revalidation policy | **ADD** fields + policy on `sources` (Issue **B**/**C**). |

### 3.2 Contract 1.03 — Archive, crawl evidence & reproducibility

| # | Contract clause (1.03) | State | Evidence | Decision |
|---|---|---|---|---|
| 1.03-a | Every source row requires `archive_url` (nullable) + `archive_status` explaining null (not-checked / unavailable / available / deferred / gap) | **partial** | `documents.wayback_url` column **exists but is never populated** (no `wayback` reference in any script); no `archive_status` field at all | **ADD** `archive_url`/`archive_status` on `sources`; wire helper (Issue **C**). |
| 1.03-b | Wayback lookup: read-only availability checks, prefer snapshot near/after scan date, record snapshot URL+timestamp, record rate-limit/deferred; **no Save-Page-Now in Stage 1** | **missing (in repo)** | `scripts/wayback_check.py` **absent at HEAD**; no archive lookup anywhere | **ADD** Wayback helper, read-only (Issue **C**). Net-new but bounded by BACKEND_CRAWLER Wayback workflow. |
| 1.03-c | Crawl-evidence fields: start/finish, scope, command, source/new/changed/stale/failed/out-of-scope/raw-artifact counts, failed targets, warnings, log path, artifact path, next owner action | **partial** | `crawl_runs` records `started/finished/status/targets/new_documents/new_transcripts/notes` (`0001_init.sql:77-86`) — has run timing + new counts, **lacks** changed/stale/failed/out-of-scope/raw-artifact counts, scope, command, next-owner-action | **EXTEND** `crawl_runs` additively (Issue **C**) to the 1.05 manifest field set. |
| 1.03-d | Reproducible commands write to documented paths, no hidden memory, Alpine scope flag, distinguish warning vs failure, never delete old raw, never silently publish | **partial** | Crawler scripts are reproducible + dry-run; **but the inventory/validation/stage1-check/data-boundary commands referenced by the contract are absent** (§1) | **ADD/port** the missing commands (Issue **B**/**C**); reuse existing crawler reproducibility patterns. |
| 1.03-e | Required test commands: `test_source_inventory`, `test_validate_sources`, `test_stage1_check`, `test_crawl_summary`, `test_wayback_check`, `test_data_boundary_check` | **missing** | None of these test files exist at HEAD (only `test_crawl_pdfs/fetch_transcripts/embed/smoke`) | **ADD** focused tests with each new command (Issues **B/C**). |
| 1.03-f | Log rules: local/vault-only, no secrets/private/allegations, include status+timestamp+scope+warning-class | **partial** | Crawler logs via `logging` to stderr; `Logs/` dir exists; **no structured per-command log contract** | **EXTEND** with structured logs as commands land (Issue **C**). |
| 1.03-g | Robots + humanlike: respect robots.txt + rate limits by default, prefer direct endpoints, realistic headers, log robots policy; controlled robots testing needs CTO/CEO approval | **exists** | `crawl_pdfs.py` robots parse/enforce (`161-185`), rate cap+jitter (`105-127`), identified UA (`165`) | **REUSE as-is.** Only fix: record the **actual** robots policy instead of hardcoded `"allowed"` (`crawl_pdfs.py:361`) when reconciling to `sources.robots_policy` (Issue **B/C**). |
| 1.03-h | Preserve the extraction-depth caveat (source inventory ≠ extracted corpus; crawl summary may report 0 docs/transcripts) | **exists (carry-forward)** | Documented in 1.03 contract + vault rollup | **CARRY FORWARD** into manifest/`stage1_check` output; no code change to invariants. |

### 3.3 Contract 1.04 — Raw preservation & reproducibility

| # | Contract clause (1.04) | State | Evidence | Decision |
|---|---|---|---|---|
| 1.04-a | **Raw-before-parse**: original bytes + `sha256` + `source_id` + archive/wayback URL persisted *before* any extraction | **partial** | Raw bytes saved to `local_path` + `sha256` at fetch (`crawl_pdfs.py:337-359`); **but no `source_id`, no archive URL captured, and parse (`embed.py` raw_text / `extract_metadata.py`) is a later step not gated on a preservation guarantee** | **EXTEND** to enforce raw-store + provenance before parse (Issue **C**). |
| 1.04-b | "Raw-preserved" = artifact exists **and** hash matches bytes **and** inventory row + registry note + run manifest agree; seed-only ≠ preserved | **missing** | Hash is computed at write time but there is **no re-hash reproducibility check** and no three-way agreement check (DB ↔ note ↔ manifest) | **ADD** reproducibility verifier (re-hash stored raw == `raw_sha256`) (Issue **C**). |
| 1.04-c | Raw artifact metadata set: `source_id`, `original_url`, `final_url`, `scan_date`, `captured_at_utc`, `source_class`, `jurisdiction`, `artifact_path`, `content_type`, `byte_size`, `sha256`, `archive_url`, `archive_status`, `verification_status`, `correction_status`, `raw_preservation_status`, `warnings`, `failures`, `next_owner_action` | **partial** | `documents` has `local_path`/`sha256`/`size_bytes`/`fetch_time_utc`/`referer_url`; **missing** `source_id`, `final_url`, `scan_date`/`captured_at` split, `content_type`, `archive_*`, `*_status`, `warnings/failures/next_owner_action` | **EXTEND** `documents` additively + derive rest from `sources` join (Issue **C**). |
| 1.04-d | On source change, preserve a **new dated artifact beside** the old; never overwrite known-then; never silently refresh scan date | **missing** | Crawler **skips** if `source_url` already present (`crawl_pdfs.py` dedupe `244-250`) — it does **not** version a changed source into a dated folder | **ADD** dated-artifact versioning on change (Issue **C**). Real behavioral gap, not just schema. |
| 1.04-e | Reviewer replay steps (open inventory → confirm scope → open note → compare fields → `shasum -a 256` → compare → check run notes → record result) | **partial (manual)** | Replay is documented in 1.04 contract but depends on the vault registry; no committed `validate_sources`/`stage1_check` automates it | **ADD** as `validate_sources.py` / reproducibility test (Issue **C**). |
| 1.04-f | `crawl_runs` formalized as AI-gateway **Lane 1 (deterministic ingest)** run log: input source set, status, retry | **partial** | `crawl_runs` exists with status + new counts; **lacks input source set, retry count** | **EXTEND** `crawl_runs` (Issue **C**), aligning to 1.05 manifest fields. |
| 1.04-g | Raw never committed to GitHub; raw/DB/logs local-vault-only | **exists** | `.gitignore` excludes DB; raw lives under vault `Docs/Source-Data/raw/...`; WORKFLOW_GOVERNANCE boundary honored | **REUSE / enforce** via `data_boundary_check.py` (Issue **C**). |

### 3.4 Contract 1.05 — Backend/tooling SSOT + uiStatus publication allowlist

| # | Contract clause (1.05) | State | Evidence | Decision |
|---|---|---|---|---|
| 1.05-a | Source-registry **input contract**: tooling consumes only rows with `scope==alpine` + all required provenance fields; rejects/quarantines non-Alpine, unsupported class, missing-provenance, private-data, seed-as-raw | **missing (in repo)** | No `sources` table to validate against; per-link Alpine filter exists in crawler (`crawl_pdfs.py:377`) but is not a registry input gate | **ADD** input-validation in loader (Issue **B**), reusing `ALPINE_PATTERN` posture. |
| 1.05-b | **`producedBy`** ∈ {automation, ai, human} on records | **missing** | Not in schema | **ADD** column (Issue **D**). |
| 1.05-c | **`verificationStatus`** — authoritative **6-value** enum: `source_recorded`, `machine_extracted_unreviewed`, `reviewed_source_linked`, `human_verified`, `disputed`, `do_not_publish` (nullable = pre-review) | **partial (export-only, unmerged)** | Implemented as `ALLOWED_VERIFICATION_STATUSES` in `scripts/validate_concept_map_export.py` (gov-17 branch, line 88) — but it validates a **card-map JSON export**, not DB rows; not on `main` | **PORT** enum to DB record fields (Issue **D**); reuse the validator constant as the single definition. |
| 1.05-d | **`reviewState`** on records | **missing** | Not in schema | **ADD** column (Issue **D**). |
| 1.05-e | **`publicationState` / `uiStatus`** with a **publish allowlist**; no record defaults publishable (default = local-only/unreviewed) | **partial (export-only, unmerged)** | `ALLOWED_UI_STATUSES` (10-value) + `PUBLICATION_ELIGIBLE_UI_STATUSES` allowlist + `compute_ui_status()` + fail-closed default exist in `validate_concept_map_export.py` (gov-17, lines 101–179) — **on the card-map export, not on DB records, and unmerged** | **PORT** to data layer; enforce default-not-publishable at the DB layer, not just UI (Issue **D**). |
| 1.05-f | `uiStatus` computed only from backend-authoritative inputs (`verificationStatus`, `correctionStatus`, `sourceChanged`, `sourcePresent`, `archivePresent`, `rawPreserved`) via versioned `uiStatus-map.v1` (first-match-wins, fails closed to `pending-review`) | **partial (export-only)** | `compute_ui_status()` implements the 12-rule table (gov-17 `validate_concept_map_export.py:156-`); its inputs `sourcePresent/archivePresent/rawPreserved` must come from the **new** `sources`/raw-preservation fields (Issues B/C) | **REUSE** mapping; **wire** its inputs to the new DB fields (Issue **D**). |
| 1.05-g | **Structural drift guard**: module-load/CI assertion that `verificationStatus` values consumed by the mapping == `ALLOWED_VERIFICATION_STATUSES` exactly | **exists (export-only)** | Assertion present: `assert set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES` (gov-17 `validate_concept_map_export.py:140`) | **REUSE / carry** into the DB-layer validator (Issue **D**). |
| 1.05-h | Fail-closed publication allowlist enforced **at the data layer** (allowlist = `source-backed`, `archived-source-backed`, `corrected`; all imply reviewed) | **partial (export-only)** | Enforced for `publicExportApproved` on card-map export (gov-17 `validate_concept_map_export.py:311-324`); **not** at the DB-record/API layer | **PORT** to data layer (Issue **D**). |
| 1.05-i | Deterministic command surface (CLI example, inputs/outputs, log, manifest, mutation/network/robots/retry/failure-threshold, test cmd, acceptance) for every backend tool | **partial** | Crawler has dry-run + CLI; **no manifest contract, no apply-style gate on mutating commands** | **EXTEND** per command as B/C land. |
| 1.05-j | Crawl-run **manifest** JSON (`run_id`, command, stage, scope, started/finished, status, input/output paths, log path, source/raw/new/changed/unchanged counts, warning/failure/retry counts, archive counts, validation_passed, next_owner_action) | **partial** | `crawl_runs` table is the DB analogue but lacks most fields; no JSON manifest emitted | **EXTEND** `crawl_runs` + emit manifest (Issue **C**). |
| 1.05-k | Automation-vs-AI boundary: deterministic tooling owns fetch/robots/raw/hash/archive/manifest/validation/reproducibility/data-boundary; AI is draft-only with anchors+labels, never primary evidence | **partial** | Deterministic side exists (crawler/hash/run-log); AI side (`embed.py`, `watchdog_brief.py`) is downstream; **no `producedBy` separation enforced in schema** | **ADD** `producedBy` enforcement (Issue **D**); execution of AI lanes is **out of slice 1**. |
| 1.05-l | Backend→frontend handoff: only reviewed website-ready rows or labeled fixtures; web-safe field allowlist incl. `uiStatus`; never expose raw paths/logs/reviewer notes/private data/AI-as-fact | **partial (planning)** | Web-safe field set defined in 1.05 §handoff + validator allowlist; **no API exists yet** | **PLAN only** in slice 1 (Data API is a later issue); default-not-publishable makes this safe. |
| 1.05-m | Data API planning only (no public feed authorized) | **n/a this slice** | — | **DEFER** to a later API implementation issue. |

---

## 4. Migration plan (additive, idempotent) mapped to issues B→E

> All migrations are **additive** and must be **re-run-safe** (see §6). Default value of every
> new publication/verification field = **not publishable / unreviewed**. Raw bytes, the SQLite
> DB, and logs stay local/vault-only.

### Issue B — Source registry schema + Alpine seed loader (1.02/1.03)
- **`0003_sources.sql` (net-new table `sources`)**: `source_id` (PK, slug), `name`, `scope`
  (CHECK = `alpine`), `url`, `original_url`, `source_type`, `source_class`,
  `source_authority_level`, `jurisdiction`, `expected_artifacts`, `robots_policy`,
  `owner_agent`, `scan_date`, `last_validated_utc`, `archive_url`, `archive_status`,
  `raw_local_path`, `raw_sha256`, `raw_preservation_status`, `local_note_path`,
  `verification_status`, `correction_status`, `topic_tags`, `notes`.
- **`0003`/`0004` additive ALTER**: add `source_id` FK column to `documents` and `transcripts`
  (nullable initially; backfilled by reconciliation).
- **Loader** (`source_inventory.py`, port from vault `alpine-source-inventory.json`): registers
  the 6 known Alpine sources (alpinewy.gov, Alpine-relevant lincolncountywy.gov, municode Alpine
  entry, the Alpine YouTube channel, etc.), fails on non-Alpine scope, links `local_note_path`.

### Issue C — Raw preservation & reproducibility hardening (1.04)
- **ALTER `documents`**: add `final_url`, `content_type`, `scan_date`, `captured_at_utc`,
  `archive_status`, `raw_preservation_status`, `warnings`, `failures`, `next_owner_action`
  (those not derivable from the `sources` join). Populate `wayback_url` (existing, currently
  empty) via the new Wayback helper.
- **ALTER `crawl_runs`**: add `run_id`, `command`, `scope`, `input_paths`, `output_paths`,
  `log_path`, `source_count`, `changed/stale/failed/out_of_scope/raw_artifact/unchanged` counts,
  `retry_count`, `archive_checked/available` counts, `validation_passed`, `next_owner_action`;
  emit a JSON manifest (`Docs/Source-Data/crawl-runs/latest.json`).
- **New commands**: `wayback_check.py` (read-only), `validate_sources.py` (re-hash reproducibility
  + three-way DB↔note↔manifest agreement), `data_boundary_check.py`.
- **Behavioral fix**: changed-source ⇒ new dated artifact beside old (do not overwrite / skip).

### Issue D — SSOT fields + uiStatus publication allowlist (1.05)
- **ALTER records** (`sources` and/or a `record_status` extension on `documents`/cards):
  `produced_by` (CHECK ∈ automation|ai|human), `verification_status` (CHECK = 6-value enum),
  `review_state`, `publication_state`/`ui_status` (CHECK ∈ 10-value enum).
- **Port** `ALLOWED_VERIFICATION_STATUSES` / `ALLOWED_UI_STATUSES` /
  `PUBLICATION_ELIGIBLE_UI_STATUSES` / `compute_ui_status()` / the drift-guard assertion from
  `validate_concept_map_export.py` into a shared module used by the DB-layer validator. **Reuse,
  do not re-type** the enums (the contract's GOV-36/37 drift guard requires a single source).
- **Default**: every record's `publication_state` defaults to not-publishable; allowlist enforced
  at the data layer.

### Issue E — Integration smoke + CI evidence
- Local-Mac-runner smoke: apply migrations → seed sources → ingest a small Alpine fixture →
  assert raw preserved + provenance present + every record **default-not-publishable**. Wire into
  `.github/workflows/local-runner-smoke.yml`; produce a green CI URL.

---

## 5. Conflicts / decisions surfaced (for CTO + reviewer)

1. **`verification_status` enum divergence (11-value vs 6-value).** Contract 1.02 lists an
   11-value `verification_status` (`verified_live_source`, `verified_local_and_live_source`, …,
   `changed_needs_review`, …). Contract 1.05 + the committed validator define the authoritative
   **6-value** `verificationStatus` (`source_recorded`, `machine_extracted_unreviewed`,
   `reviewed_source_linked`, `human_verified`, `disputed`, `do_not_publish`), and explicitly
   moved source-change to the separate `sourceChanged` signal (GOV-36/37 drift correction).
   **Decision (recommended):** the **6-value 1.05 enum is authoritative for record-level
   `verificationStatus`/`uiStatus` computation**; the 1.02 registry vocabulary maps onto it
   (e.g. `changed_needs_review` → `sourceChanged=true`; `verified_*` → `reviewed_source_linked`/
   `human_verified`; `unverified` → `machine_extracted_unreviewed`/null). Issue D must include
   the mapping table and a parity test. **Flagging to CTO/VerificationSafetyReviewer** — this is
   an enum-of-record decision, not an owner/legal decision, so it stays in the reviewer lane.

2. **`uiStatus` logic lives on an unmerged branch and operates on a JSON export, not the DB.**
   The 10-state vocabulary, allowlist, and `compute_ui_status()` are real and reviewed, but on
   `gov-17-newsletter-briefing-contract` (`validate_concept_map_export.py`), unmerged to `main`.
   Issue D should **land that module into the backend** (shared import) rather than re-implement.

3. **Contract-assumed scripts are absent at HEAD** (§1, finding F4): `source_inventory.py`,
   `stage1_check.py`, `wayback_check.py`, `validate_sources.py`, `crawl_summary.py`,
   `data_boundary_check.py`. These are **net-new in the repo** even though the contracts treat
   them as existing. They are not duplicates of any HEAD code — building them does not violate
   the no-duplicate rule.

---

## 6. Migration-mechanism finding (idempotency) — must fix in Issue B

`db.py:apply_migrations()` `executescript`s **every** `*.sql` file on every run (`db.py:16-25`).
This is safe for `CREATE TABLE IF NOT EXISTS` (0001) but **`0002`'s `ALTER TABLE transcripts ADD
COLUMN title`** is **not** idempotent — SQLite has no `ADD COLUMN IF NOT EXISTS`, so a second run
against an existing DB raises `duplicate column name`. Today this is masked only because
migrations are typically applied once to a fresh DB. **Every additive `ALTER` in 0003+ inherits
this hazard.**

**Recommended fix (Issue B):** introduce a lightweight migration-tracking mechanism (a
`schema_migrations(version TEXT PRIMARY KEY, applied_utc TEXT)` table + skip-already-applied loop,
or per-file guards via `PRAGMA table_info` checks before `ADD COLUMN`). This keeps `db.py`'s
"re-run safe" contract true once column-adding migrations exist. A unit test must assert
`apply_migrations()` is safe to run twice.

---

## 7. Extend-vs-rebuild decision summary (no component rebuilt)

| Component | Decision | Rationale |
|---|---|---|
| Crawler (`crawl_pdfs.py`, `fetch_transcripts.py`) | **EXTEND / reuse** | Robots+rate-limit+hash+run-log+Alpine-filter already conformant (1.03-g, 1.04-a partial). Reconcile to `sources`; fix hardcoded `robots_status`/`cms_signature`; add changed-source versioning. **No rebuild.** |
| `documents`/`transcripts`/`meetings` schema | **EXTEND (additive ALTER)** | Add `source_id` FK + provenance/status columns. Existing columns and data preserved. |
| `crawl_runs` run log | **EXTEND (additive ALTER)** | Already the run-log primitive; widen to the 1.05 manifest field set + emit JSON. |
| Source registry | **ADD (`sources` table) + reuse vault data** | No DB registry exists; vault JSON+notes are the seed. Net-new table, **not** a duplicate of any HEAD code. |
| Raw preservation | **EXTEND** | Fetch-time hashing exists; add re-hash reproducibility + raw-before-parse gate + dated versioning. |
| `verificationStatus`/`uiStatus`/allowlist | **PORT (reuse) + ADD DB fields** | Logic exists on unmerged `gov-17` export validator; port the module, add the record-level columns. **Do not re-implement.** |
| Inventory/validation commands | **ADD (net-new in repo)** | Absent at HEAD; build per contracts 1.03/1.05; no HEAD duplicate. |
| `db.py` migration runner | **EXTEND (idempotency guard)** | Add migration tracking so additive `ALTER`s are re-run-safe (§6). |

---

## 8. No-duplication confirmation (acceptance criterion)

Every proposed change is either an **additive extension** of an existing component or a
**net-new** component that has **no implementation at HEAD**. Specifically: no proposal rebuilds
the crawler, the schema tables, the run log, the hashing, or the robots/rate-limit logic — all of
which already work and are reused. The only "new code" items (`sources` table, registry loader,
Wayback/validate/boundary commands, record-level status columns) were verified **absent at HEAD**
(§1, §3.1-g, §3.2-b, §3.3-b) and the `uiStatus` logic is **ported**, not re-typed, from the
existing (unmerged) validator. **No proposed change duplicates a working component.**

---

## 9. Scope, risk & data-boundary posture (RISK_ASSESSMENT_WORKFLOW)

- **Scope:** Alpine-only; local/vault-only; no public surface, no API, no AI extraction run, no
  accounts. Default record state = not-publishable. Stage 2 remains locked.
- **Evidence/source risk:** addressed by the central recommendation (single DB-backed registry
  with `source_id` join, hash reproducibility, archive status).
- **Publication/readiness risk:** mitigated — every new publication field defaults to
  not-publishable and the allowlist is fail-closed; this analysis publishes **no** raw data.
- **Privacy:** no private identity/address/voter data appears here; raw bytes/DB/logs stay
  local-vault-only per WORKFLOW_GOVERNANCE.
- **Escalation:** none triggered (local analysis, no publication). Escalate to CEO only if a
  later issue needs a public surface, accounts, or scope change. The §5 enum decision stays in
  the agent reviewer lane (CTO + VerificationSafetyReviewer), not owner escalation.

---

## 10. Handoff

- **Reviewer lane:** primary **VerificationSafetyReviewer**; technical consult **CTO**. Author
  (BackendCrawlerEngineer) may not self-review (1.15 §2.2). Reviewer records APPROVE as a comment;
  then GOV-72 → `done`.
- **Next executable:** Issue **B** (source registry schema + Alpine seed loader) is unblocked once
  this analysis is approved; C→D→E follow on first-class blockers per the GOV-71 A→E chain.
- **Decision needed from CTO/reviewer before B starts:** confirm the §5.1 6-value
  `verificationStatus` as authoritative and the §6 migration-idempotency fix as in-scope for B.
