# Stage 1 — Slice 2 A — Transcript / Evidence / Statement Gap Analysis vs Contract 1.07

Issue: GOV-80 ([Stage 1 Impl][Backend] Slice 2 A)
Owner role: BackendCrawlerEngineer (TranscriptEvidenceEngineer + CTO consult)
Reviewer: VerificationSafetyReviewer (primary), CTO (feasibility)
Stage / scope: **Stage 1 — Slice 2 — issue A.** Town of Alpine only. Local/vault-only. **No AI run.**
Maps to: **Contract 1.07** — `Docs/stage1-transcript-evidence-statement-contract.md` (GOV-40, on `main`).
Created: 2026-06-08

> **Analysis only.** This document is a clause-by-clause gap matrix and an
> *additive* migration plan. It writes **no schema**, no migration SQL, no
> ingestion, no extraction, no publication, and no scope beyond Alpine. It
> sequences (does not authorize) the Slice-2 implementation issues that follow
> ([GOV-81](/GOV/issues/GOV-81) onward). All field/enum names below were read
> from landed code on `main`, not from prose.

---

## 0. Purpose and method

GOV-80 produces the bridge between the **1.07 typed transcript/evidence/statement
model contract** and the **landed Slice-1 backend**, so the next implementation
issues extend working code instead of rebuilding it.

Method:

1. Read Contract 1.07 §1–§5 (nodes, edges, exact-source pointer, attribution,
   known-then layers, `verificationStatus`/`uiStatus` binding).
2. Inventory the landed schema on `main` (the WEI `documents` / `transcripts` /
   `meetings` / `meeting_documents` tables + the Slice-1 `sources` registry,
   `crawl_runs` Lane-1 fields, and the SSOT/publication columns).
3. For every 1.07 clause, classify against the landed schema:
   **EXISTS** (landed, reusable as-is) / **PARTIAL** (a related structure exists
   but lacks the contract's fields) / **MISSING** (no structure).
4. Decide the **additive** migration plan: the new tables named by the issue
   (`agenda_items`, `transcript_segments`, `statements`, `evidence_links`,
   `persons`, `roles`, + typed edges) that **extend, not rebuild** the existing
   tables, with explicit **enum reuse** rather than re-typing.
5. Surface conflicts/decisions for CTO + VerificationSafetyReviewer.

### Evidence basis (files read on `main`)

| File | What it provided |
|---|---|
| `Docs/stage1-transcript-evidence-statement-contract.md` | Contract 1.07 §1–§9 (the target model). |
| `Database/migrations/0001_init.sql` | WEI base: `documents`, `transcripts`, `meetings`, `meeting_documents`, `embeddings`, `crawl_runs`. |
| `Database/migrations/0002_transcript_title.sql` | `transcripts.title`. |
| `Database/migrations/0003_sources.sql` | Slice-1 `sources` registry + `documents`/`transcripts` `source_id` FKs. |
| `Database/migrations/0004_crawl_runs_lane1.sql` | `crawl_runs` Lane-1 fields (`lane`, `source_set`, `retry_count`). |
| `Database/migrations/0005_ssot_publication.sql` | SSOT columns on `sources` (`produced_by`, `review_state`, `publication_state`, `source_changed`, `ui_status`). |
| `scripts/publication.py` | Authoritative enums: `ALLOWED_VERIFICATION_STATUSES` (6), `ALLOWED_UI_STATUSES` (10), `PUBLICATION_ELIGIBLE_UI_STATUSES` (3), `compute_ui_status`, `VERIFICATION_STATUS_MAP` (11→6), `WEB_SAFE_FIELD_ALLOWLIST`. |
| `Docs/stage1-backend-gap-analysis.md` | GOV-72 method/format precedent (this doc follows it). |

---

## 1. Decisive context — this is NOT greenfield

Two things are already landed that 1.07 depends on, and **neither may be rebuilt**:

1. **The `sources` registry _is_ 1.07's `source_record` node.** Contract 1.07
   §1.1 lists a `source_record` node owning `source_id`, `url`, `original_url`,
   `source_class`, `source_type`, `jurisdiction`, `scan_date`, `archive_status`,
   `archive_url`, `verification_status`, `correction_status`,
   `raw_preservation_status`, `local_note_path`. **Every one of those columns
   already exists on `sources`** (`0003_sources.sql`). Slice 2 must treat a
   `sources` row as the `source_record`; it must **not** create a parallel
   `source_record` table.

2. **The status vocabulary is already landed and enforced.** Contract 1.07 §5
   explicitly says it "does **not** introduce a new status vocabulary" and binds
   to the existing one. `scripts/publication.py` already holds the
   record-authoritative **6-value `verificationStatus`**, the **10-value
   `uiStatus-map.v1`**, `compute_ui_status()` (rules #1–#12, fail-closed), and
   the **3-value publication allowlist** — with import-time drift guards. Slice 2
   must **import** these, never re-type them.

What is genuinely **missing** is the *typed-record decomposition* of a meeting
into per-segment, per-statement, per-evidence rows with exact-source pointers and
gated speaker attribution. The existing `transcripts` table stores a whole
meeting transcript as `full_text` + a `timestamped_text` blob — it is a
**container**, not the addressable `transcript_segment` / `statement` graph 1.07
requires.

Carry-forward already satisfied: 1.07's downstream carry-forward #1 ("raw-content
SHA-256 must become an explicit `source_record` field") **is already landed** —
`sources.raw_sha256` exists (`0003`). Slice 2 inherits it; no action needed.

---

## 2. Existing backend inventory (`main`, HEAD `de388c1`)

### 2.1 Schema (`Database/migrations/`)

| Table | Key columns (relevant to 1.07) | Role for 1.07 |
|---|---|---|
| `sources` | `source_id` (TEXT PK), `name`, `scope` (CHECK `alpine`), `url`, `original_url`, `source_type`, `source_class`, `source_authority_level`, `jurisdiction`, `scan_date`, `last_validated_utc`, `archive_url`, `archive_status`, `raw_local_path`, `raw_sha256`, `raw_preservation_status`, `local_note_path`, `verification_status` (11-value **registry** TEXT), `correction_status`, `produced_by` (CHECK `automation|ai|human`), `review_state`, `publication_state` (CHECK), `source_changed` (CHECK 0/1), `ui_status`, `topic_tags` | **= 1.07 `source_record`.** Reuse as-is. |
| `documents` | `id` (INT PK), `source_url` (UNIQUE), `title`, `doc_type`, `doc_date`, `local_path`, `sha256`, `wayback_url`, `raw_text`, `source_id` FK | **= 1.07 `document`** (base). Reuse + light extension. |
| `transcripts` | `id` (INT PK), `video_id` (UNIQUE), `video_url`, `meeting_date`, `duration_seconds`, `segment_count`, `full_text`, `timestamped_text`, `local_path`, `sha256`, `fetch_time_utc`, `title`, `source_id` FK | **Transcript container.** The source of `transcript_segment` rows; not itself a segment. |
| `meetings` | `id` (INT PK), `meeting_date`, `body` (free TEXT), `title`, `source_url`, `transcript_id` FK, `notes` | **= 1.07 `meeting`** (base). Reuse + extension (typed `body`, `video_source_id`). |
| `meeting_documents` | (`meeting_id`, `document_id`) join, `role` | A meeting↔document join. **Not** the typed `evidence_link` (different semantics: packet attachment, not statement substantiation). |
| `embeddings` | object_type/object_id/vector | Retrieval; out of 1.07 scope. |
| `crawl_runs` | `lane`, `source_set`, `retry_count`, status/targets/counts | AI-gateway Lane-1 run log. Reuse for Slice-2 ingest runs. |

### 2.2 Enum / publication authority (`scripts/publication.py`)

- `ALLOWED_VERIFICATION_STATUSES` — 6 values (record enum-of-record).
- `ALLOWED_UI_STATUSES` — 10 values (`uiStatus-map.v1`, kebab-case wire form).
- `PUBLICATION_ELIGIBLE_UI_STATUSES` — `{source-backed, archived-source-backed, corrected}`.
- `compute_ui_status(record: dict)` — generic, fail-closed; **reusable verbatim** for statement/evidence records.
- `VERIFICATION_STATUS_MAP` — 11-value registry → 6-value record (+ `sourceChanged`).
- `WEB_SAFE_FIELD_ALLOWLIST` / `to_web_safe()` — fail-closed backend→frontend projection.

---

## 3. Gap matrix — clause by clause (1.07 §1–§5)

### 3.1 Contract 1.07 §1 — Concept boundaries & typed links

#### §1.1 Typed concepts (nodes)

| 1.07 node | Status | Evidence / what is missing |
|---|---|---|
| `jurisdiction` | **PARTIAL** | No table. `sources.jurisdiction` (TEXT) + `sources.scope` CHECK `alpine` carry the value but there is no jurisdiction node to attach `contains_body` edges to. Slice 2 may add a tiny `jurisdictions` table **or** (recommended) keep jurisdiction as a constrained attribute until vote/decision slices need the node. **Decision D-1.** |
| `government_body` | **MISSING** | Only `meetings.body` as free TEXT. No `body_id`, no `jurisdiction_id` link. Needed so `held_meeting` / `role_in_body` edges are typed. |
| `meeting` | **PARTIAL** | `meetings` exists (`meeting_date`, `body`, `title`, `source_url`, `transcript_id`). Missing: typed `body_id` FK (vs free-text `body`), `video_source_id` → `sources`, stable slug id. |
| `agenda_item` | **MISSING** | No table. 1.07 needs `agenda_item_id`, `meeting_id`, `order`, `title`, `agenda_doc_source_id`. **(named additive table)** |
| `transcript_segment` | **PARTIAL** | `transcripts.timestamped_text` is an opaque blob + `segment_count` int; no per-segment rows. 1.07 needs addressable rows: `segment_id`, `meeting_id`/`transcript_id`, `timestamp_seconds`, `timestamp_human`, `segment_text`, `is_verbatim`, `confidence`, `transcript_path`. **(named additive table)** |
| `statement` | **MISSING** | No table. The load-bearing node: `statement_id`, `segment_id`, `agenda_item_id`, `speaker_attribution_id`, `statement_text`, `is_verbatim`, `verificationStatus`, `correctionStatus`, `layer`. **(named additive table)** |
| `person` | **MISSING** | No table. `person_id`, `display_name` (gated, §3). **(named additive table)** |
| `role` | **MISSING** | No table. `role_id`, `body_id`, `title`, `start_date`, `end_date`. **(named additive table)** |
| `source_record` | **EXISTS** | **= `sources`** (§1, §2.1). Reuse; do not duplicate. |
| `document` | **PARTIAL** | `documents` exists (`doc_type`, `doc_date`, `source_url`, `sha256`, `source_id` FK). Missing: stable `document_id` slug + explicit doc-lifecycle edges (`supersedes`/`amends`/`replaces`). Lifecycle deferable to a later slice. |
| `evidence_link` | **MISSING** | `meeting_documents` is a *different* join (packet attachment). 1.07 needs a typed `evidence_link`: `from_node_id`, `to_source_id`, `pointer` (§2), `relation`. **(named additive table)** |
| `vote` | **MISSING — deferred** | Referenced by 1.07 for edge correctness only; not in the Slice-2 named additive set. Defer to a later slice (do not implement ahead of stage). |
| `decision` | **MISSING — deferred** | As `vote`. |
| `outcome` | **MISSING — deferred (but see §3.4 / D-4)** | The `actual_later` layer + `outcome_updates` edge needs a forward-only target; Slice-2 handles forward-only corrections via an `evidence_link.relation: corrects` + nullable self-reference instead of a full `outcome` node. **Decision D-4.** |
| `topic` | **MISSING — partial attribute** | `sources.topic_tags` (TEXT) only. Full `topic` node + `topic_groups`/`decision_affects` edges deferred. |
| `card` | **N/A here** | Presentation/export layer (`gov-watchdog-card-map.v1` in `validate_concept_map_export.py`), not a DB table. Out of Slice-2-A schema scope. |

#### §1.2 Typed relationships (edges) — **MISSING as typed edges**

Current schema expresses relationships only as **implicit integer FKs**
(`meetings.transcript_id`, `meeting_documents` join, `*.source_id`). There is no
typed-edge representation and no `made_statement` / `statement_from_segment` /
`source_supports` / `outcome_updates` concept. **Decision D-3** picks the edge
representation (relational FK columns for the spine vs a generic typed-edge table
for cross-cutting links).

#### §1.3 Canonical Alpine chain — **NOT EXPRESSIBLE today**

`meeting → agenda_item → transcript_segment → statement → person/role →
source_record/document → evidence_link` cannot be built: `agenda_item`,
`transcript_segment` (rows), `statement`, `evidence_link`, `person`, `role` are
all missing. Only the `meeting` and `source_record`/`document` endpoints exist.

#### §1.4 Statement relationship semantics — **MISSING**

The `relation` enum (`references | supports | contradicts | corrects |
substantiates`) lives on `evidence_link`, which does not exist yet. No analysis
edge can be modeled until `evidence_links` lands.

### 3.2 Contract 1.07 §2 — Exact-source pointer contract

| 1.07 §2 element | Status | Evidence / gap |
|---|---|---|
| `pointer` object (per evidence_link) | **MISSING (as a record)** | No table carries a pointer; but most *constituent fields* already exist on `sources` — see below. |
| `source_id` (resolves to registry) | **EXISTS** | `sources.source_id` PK; `documents.source_id`/`transcripts.source_id` FKs already enforce "resolves to a registered source." |
| `original_url`, `final_url` | **PARTIAL** | `sources.original_url` + `sources.url` (≈ final/current). 1.07's `final_url` vs `url` naming to reconcile (D-2). |
| `wayback_url` / `archive_status` | **EXISTS** | `sources.archive_url` (= wayback_url), `sources.archive_status` (default `not_checked`). |
| `scan_date`, `captured_at_utc` | **PARTIAL** | `sources.scan_date` exists; machine `captured_at_utc` ≈ `last_validated_utc` / `*.fetch_time_utc` but no dedicated per-pointer capture timestamp. New evidence_link rows need their own captured_at. |
| `source_type`, `source_class`, `source_authority_level`, `jurisdiction` | **EXISTS** | All on `sources`. |
| `locator_kind` (`timestamp|page|section|paragraph`) | **MISSING** | No locator typing anywhere. **The core §2 gap.** |
| `timestamp_seconds` + `timestamp_human` | **MISSING** | `transcripts.timestamped_text` blob is not addressable; no integer-seconds locator. Lands on `transcript_segment` + `evidence_link`. |
| `page` / `section` / `paragraph` | **MISSING** | No document locator fields on `documents`. |
| `agenda_item_id` | **MISSING** | Depends on `agenda_items`. |
| `transcript_path` | **PARTIAL (private)** | `transcripts.local_path` + `sources.local_note_path`/`raw_local_path` exist and are correctly vault-only; the per-pointer `transcript_path` field is new on evidence_link. |
| `deep_link` | **MISSING (derived)** | Convenience only; derivable from `url` + `timestamp_seconds`. |
| `is_verbatim` | **MISSING** | New on `transcript_segment`/`statement`/`evidence_link`. |
| `verificationStatus`, `correctionStatus`, `confidence` | **EXISTS as enum / PARTIAL on records** | Enum + `correction_status` landed; must be carried on the new statement/evidence rows (see §3.5). `confidence` is new. |
| §2.3 Orphan-rejection rule | **MISSING** | No validator asserts "every statement has an evidence_link with a complete pointer." A future impl issue adds it to the validator test set (consistent with 1.07 §8 plan). |

### 3.3 Contract 1.07 §3 — Speaker attribution rule

| 1.07 §3 element | Status | Evidence / gap |
|---|---|---|
| `speaker_attribution` record | **MISSING** | No table. Entire attribution concept is new. **(implied additive table — pairs with `statements`)** |
| `attribution_state` (`attributed|uncertain|unattributed`) | **MISSING** | Must fail closed to a generic label for `uncertain`/`unattributed`. |
| `speaker_class` (`on-record-official|on-record-public|unidentified|private-context`) | **MISSING** | Drives attribution permission; `on-record-public` naming is a CEO hard-stop. |
| `person_id` / `role_id` linkage | **MISSING** | Depends on `persons` / `roles`. |
| `made_statement` edge gating | **MISSING** | Edge may exist **only** when `attribution_state == attributed` AND `speaker_class ∈ {on-record-official, on-record-public+CEO-approved}`. Encodes as a constrained FK + CHECK / app guard. |
| Private candidate-name field | **MISSING (must be vault-only when added)** | The `uncertain` candidate name is reviewer-only (§7); must be excluded from `WEB_SAFE_FIELD_ALLOWLIST`. |

### 3.4 Contract 1.07 §4 — Known-then vs later layers

| 1.07 §4 element | Status | Evidence / gap |
|---|---|---|
| `layer` enum (`known_then|presented_then|ai_thought_then|corrected_later|actual_later`) | **MISSING** | New field on `statement` + `evidence_link`. No layering concept today. |
| Append-only / forward-only rule | **MISSING (partially expressible)** | `correction_status` exists on `sources`, and `compute_ui_status` already maps `correctionStatus == corrected → corrected` (reviewed-gated) and `needs_clarification → needs-clarification`. But there is no layered record, no `outcome` node, and no `outcome_updates` edge. |
| `outcome_updates` edge / forward correction | **MISSING — Decision D-4** | Slice-2 represents forward-only correction via `evidence_link.relation: corrects` + a nullable `updates_statement_id` self-reference on `statements`; the full `outcome` node is deferred. The `known_then` row is never mutated. |
| `ai_thought_then` separation + AI label | **PARTIAL** | `sources.produced_by` (`automation|ai|human`) exists and is the right reuse hook; statements need their own `produced_by` + `is_verbatim` so AI drafts stay `machine_extracted_unreviewed` and gated. |

### 3.5 Contract 1.07 §5 — verificationStatus integration (uiStatus-map.v1)

| 1.07 §5 element | Status | Evidence / gap |
|---|---|---|
| §5.1 6-value `verificationStatus` enum | **EXISTS — reuse** | `publication.ALLOWED_VERIFICATION_STATUSES`. **Import; never re-type.** |
| §5.2 `uiStatus` (10 values) + `compute_ui_status` | **EXISTS — reuse** | `publication.ALLOWED_UI_STATUSES` + `compute_ui_status(dict)` is record-agnostic → run statement/evidence rows through it unchanged. |
| §5.3 Fail-closed publication allowlist | **EXISTS — reuse** | `PUBLICATION_ELIGIBLE_UI_STATUSES` + `publication_state` DB gate (default `not_publishable`). Statements inherit the same two-gate posture. |
| §5.4 AI / paraphrase labeling | **PARTIAL** | `produced_by` exists; `is_verbatim` + the AI label are new on statement/segment rows. |
| Carrying the SSOT columns onto statement/evidence rows | **PARTIAL** | `sources` has `verification_status`/`correction_status`/`review_state`/`publication_state`/`source_changed`/`ui_status`; the new `statements` / `evidence_links` tables need the equivalent **record-level** columns (see Decision D-5: statements carry the **6-value** enum directly, not the 11-value registry vocabulary). |

---

## 4. Additive migration plan (extends, does not rebuild)

All migrations are **additive + idempotent** (guard each `CREATE`/`ALTER` with a
`PRAGMA table_info` check, per `db.py`'s established splitter constraints in
`0003`–`0005`). New record tables use **TEXT slug PKs** consistent with
`sources.source_id` (Decision D-1). Numbering continues from `0005`.

> No SQL is written in this issue (analysis only). The mapping below sequences
> the Slice-2 implementation issues; each must be its own gated issue with tests
> and evidence.

| Migration | New / changed | 1.07 clause | Notes |
|---|---|---|---|
| `0006_people_roles.sql` | **NEW** `persons` (`person_id` PK, `display_name` *private*), `roles` (`role_id` PK, `body_id`, `title`, `start_date`, `end_date`) | §1.1, §3 | `display_name` excluded from `WEB_SAFE_FIELD_ALLOWLIST`. |
| `0007_bodies.sql` | **NEW** `government_bodies` (`body_id` PK, `name`, `jurisdiction`); **ALTER** `meetings` ADD `body_id` FK (nullable, back-filled from `body` text), ADD `video_source_id` FK → `sources` | §1.1, §1.2 | `meetings.body` free text retained for back-fill; not dropped. Jurisdiction kept as constrained attribute (D-1) unless a later slice needs the node. |
| `0008_agenda_items.sql` | **NEW** `agenda_items` (`agenda_item_id` PK, `meeting_id` FK, `order`, `title`, `agenda_doc_source_id` FK → `sources`) | §1.1, `contains_agenda_item`, `references_source` | |
| `0009_transcript_segments.sql` | **NEW** `transcript_segments` (`segment_id` PK, `transcript_id` FK, `meeting_id` FK, `timestamp_seconds`, `timestamp_human`, `segment_text`, `is_verbatim`, `confidence`, `transcript_path` *private*) | §1.1, §2 | Derived from existing `transcripts.timestamped_text`; container table untouched. `transcript_path` vault-only. |
| `0010_statements_attribution.sql` | **NEW** `statements` (`statement_id` PK, `segment_id` FK, `agenda_item_id` FK, `statement_text`, `is_verbatim`, `produced_by`, `verification_status` **6-value**, `correction_status`, `review_state`, `publication_state`, `source_changed`, `ui_status`, `layer`, nullable `updates_statement_id` self-ref); **NEW** `speaker_attributions` (`speaker_attribution_id` PK, `statement_id` FK, `attribution_state`, `speaker_class`, `person_id` FK, `role_id` FK, `display_label`, `basis` *private*, `reviewer_state`, `confidence`) | §1.1, §3, §4, §5 | `verification_status` here is the **6-value record enum** (D-5), CHECK-/app-guarded against `ALLOWED_VERIFICATION_STATUSES`. `made_statement` modeled as a guarded relationship (person link only when attribution permits). `basis`/candidate name vault-only. |
| `0011_evidence_links.sql` | **NEW** `evidence_links` (`evidence_link_id` PK, `from_node_id`, `from_node_type`, `to_source_id` FK → `sources`, `relation` enum, `layer`, + the §2 pointer fields: `locator_kind`, `timestamp_seconds`, `timestamp_human`, `page`, `section`, `paragraph`, `original_url`, `final_url`, `archive_url`, `archive_status`, `scan_date`, `captured_at_utc`, `is_verbatim`, `verification_status` 6-value, `correction_status`, `confidence`, `transcript_path` *private*, `deep_link` *derived*) | §1.2, §1.4, §2 | The exact-source pointer record. `relation ∈ {references, supports, contradicts, corrects, substantiates}`. Resolves the orphan-claim rule's target. |
| `publication.py` extension (no re-type) | **EXTEND** `WEB_SAFE_FIELD_ALLOWLIST` for the new *public* statement/evidence fields (`statement_text`, `speaker_label`, `ui_status`, `layer`, `is_verbatim`, `confidence`, public pointer subset); keep `transcript_path`, `basis`, candidate name, raw paths in `WEB_UNSAFE_FIELDS` | §5, §7 | **Import the enums; do not redeclare them.** Run statements/evidence through the existing `compute_ui_status`. |

Edge representation (Decision **D-3**): the **spine** edges
(`contains_agenda_item`, `statement_from_segment`, `references_source`) are
modeled as **relational FK columns** on the child rows (cheapest, query-friendly,
matches existing style). The **cross-cutting / analysis** edges (`source_supports`,
`made_statement`, `outcome_updates`/`corrects`) are carried by `evidence_links`
(with `relation`) and the guarded attribution link — not a generic untyped edge
table. A generic typed-edge table is **deferred** unless vote/decision/outcome
slices prove it necessary.

Deferred (not implemented in Slice 2, per stage rule): `votes`, `decisions`,
`outcomes`, `topics` nodes and their edges; document-lifecycle edges
(`supersedes`/`amends`/`replaces`). Listed for edge-correctness only.

---

## 5. Conflicts / decisions surfaced (for CTO + VerificationSafetyReviewer)

- **D-1 — PK style & jurisdiction node.** New record tables use **TEXT slug PKs**
  (e.g. `alpine:2026-05-08:stmt-1043`) consistent with `sources.source_id`, while
  existing `meetings`/`transcripts`/`documents` keep their INTEGER PKs and are
  linked by their integer FKs. Jurisdiction stays a **constrained attribute**
  (`sources.jurisdiction` + `scope` CHECK) rather than a node until a later slice
  needs `contains_body` edges. *CTO feasibility / consistency call.*
- **D-2 — `url` vs `final_url` naming.** 1.07's pointer uses
  `original_url` + `final_url`; `sources` uses `original_url` + `url`. Recommend
  treating `sources.url` as the canonical/current URL and deriving the pointer's
  `final_url` from it (no rename of the landed column). *Reviewer + CTO.*
- **D-3 — Edge representation** (relational FK spine + `evidence_links` for
  cross-cutting; generic edge table deferred). *CTO.*
- **D-4 — Forward-only correction without an `outcome` node.** Slice 2 uses
  `evidence_link.relation: corrects` + nullable `statements.updates_statement_id`
  self-reference; `known_then` rows are never mutated; the full `outcome` node
  + `outcome_updates` edge are deferred. Confirm this preserves the §4
  append-only guarantee to the reviewer's satisfaction. *VerificationSafetyReviewer.*
- **D-5 — Record enum vs registry enum on new rows.** `sources.verification_status`
  legitimately holds the **11-value registry** vocabulary (mapped 11→6 via
  `VERIFICATION_STATUS_MAP`). New `statements`/`evidence_links` are
  **record-level**, so they should carry the **6-value `verificationStatus`
  enum directly** (CHECK/app-guard against `ALLOWED_VERIFICATION_STATUSES`) — **no
  mapping layer** for statements. This is the clean reuse and avoids a second
  11→6 hop. *VerificationSafetyReviewer + CTO sign-off.*
- **D-6 — `made_statement` enforcement site.** The attribution-gated person link
  is enforced in the data layer (CHECK where expressible + an app-level guard in
  the eventual ingest path), and `on-record-public` naming routes to CEO before
  any display name / person link is created (1.07 §3.3 hard stop). *Reviewer.*

---

## 6. Extend-vs-rebuild decision summary (no component rebuilt)

| Existing component | Decision | Why |
|---|---|---|
| `sources` registry | **Extend / reuse as `source_record`** | Already carries every 1.07 §1.1 `source_record` field incl. `raw_sha256` (carry-forward #1 satisfied). |
| `publication.py` enums + `compute_ui_status` + allowlist | **Import / reuse verbatim** | 1.07 §5 mandates reusing this vocabulary; drift guards already enforce it. |
| `transcripts` | **Reuse as container; add `transcript_segments`** | Blob → addressable rows is additive; container untouched. |
| `meetings` | **Extend (`body_id`, `video_source_id`)** | Free-text `body` retained for back-fill. |
| `documents` | **Reuse + light extension** | = 1.07 `document`; lifecycle edges deferred. |
| `meeting_documents` | **Leave as-is** | Distinct join (packet attachment); not `evidence_link`. |
| `crawl_runs` | **Reuse** | Lane-1 run log already formalized (`0004`). |

---

## 7. No-duplication confirmation (acceptance criterion)

- **No proposed change duplicates working Slice-1 code.** `sources`,
  `publication.py` enums, `compute_ui_status`, the publication allowlist,
  `to_web_safe`, `raw_sha256`, and the `crawl_runs` Lane-1 fields are **reused by
  import/FK**, not re-declared.
- Every new table is a 1.07 node/record with **no** landed equivalent
  (`agenda_items`, `transcript_segments`, `statements`, `speaker_attributions`,
  `evidence_links`, `persons`, `roles`, `government_bodies`).
- Enum/field names verified against landed `main` code (`0003`–`0005`,
  `publication.py`) — the 6-value `verificationStatus`, `uiStatus-map.v1`,
  publication allowlist, and `produced_by`/`review_state`/`publication_state`/
  `source_changed`/`ui_status` SSOT columns are **reused, not re-typed**.

---

## 8. Scope, risk & data-boundary posture (RISK_ASSESSMENT_WORKFLOW)

- **Stage / scope:** Stage 1, Slice 2 A, Town of Alpine only. Analysis-only —
  **no schema, no migration, no ingestion, no AI run, no publication** in this
  issue.
- **Evidence/source risk:** addressed — every new statement/evidence row is
  required (by 1.07 §2.3) to resolve to a `sources` row with a complete pointer;
  orphan-claim rejection is carried forward to the impl issue's validator.
- **AI-overclaim risk:** addressed — statements default to
  `machine_extracted_unreviewed`/`source_recorded`; `produced_by`/`is_verbatim`
  keep AI drafts gated `unverified`; `compute_ui_status` is fail-closed.
- **Privacy/account risk:** addressed — `transcript_path`, raw paths,
  attribution `basis`, the `uncertain` candidate name, and reviewer notes are
  **vault-only** and excluded from `WEB_SAFE_FIELD_ALLOWLIST`; no
  identity/address/voter data enters any record.
- **Defamation/legal risk:** addressed — `attribution_state` fails closed to a
  generic label; `on-record-public` naming is a CEO hard stop; the model surfaces
  pointers, not verdicts.
- **Publication/readiness risk:** addressed — two-gate publish posture
  (`publication_state` default `not_publishable` AND allowlisted `uiStatus`)
  inherited unchanged; no record defaults to publishable.

**No-go without escalation:** naming any `on-record-public` speaker; any
publication; expanding beyond Alpine; redefining the `verificationStatus` /
`uiStatus-map.v1` / publication allowlist (owned by GOV-36/37/38/39).

---

## 9. Handoff

- **This issue (GOV-80):** delivers this gap-analysis doc only. Done-bar:
  doc committed, **VerificationSafetyReviewer** sign-off (primary) + **CTO**
  feasibility consult, evidence comment, **PR squash-merged to `main`**. No CI
  gate (no code/migration here).
- **Next ([GOV-81](/GOV/issues/GOV-81), blocked-by GOV-80):** the first Slice-2
  *implementation* issue should consume §4 (additive migration plan) and §5
  (decisions D-1…D-6), starting with `0006`–`0008` (people/roles/bodies/agenda)
  as the lowest-risk additive foundation, then `0009`–`0011`
  (segments/statements/evidence) with the orphan-claim + enum-parity tests, and
  the `WEB_SAFE_FIELD_ALLOWLIST` extension. Each as its own gated issue with
  local Mac runner evidence.
- **Decisions D-1…D-6** require CTO + VerificationSafetyReviewer resolution
  before `0010`/`0011` are written.
