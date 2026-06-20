# Stage 3.04 — Raw-Preservation Contract over the Alpine Surface

> **Issue:** GOV-363 (Stage 3.04 · Plan · CEO→BackendCrawlerEngineer). **Parent/tracker:** GOV-355.
> **Stage:** 3.04 — `contract→impl` **contract (planning) child only**. **NON-implementation. NON-unlock.**
> **Scope:** Town of Alpine only · reviewer-internal · no public launch · no new crawl · no ingestion-scope change.
> **Sequenced after:** GOV-362 (Stage 3.03 source/data inventory contract, merged `origin/main` HEAD `9b7d1aa` / PR #64).
> **Grounded on:** canonical remote `origin/main` HEAD `9b7d1aa`.
> **Goal of record:** HEAD GOAL `5e8b8006-94ed-4489-8fa2-643f8ec16724` (find / understand / verify at original sources).
> **Inputs of record (read-only grounding, no edit by this child):**
> - `scripts/raw_preservation.py` @ `9b7d1aa` — the Stage 1.04 / Stage 2.04 preservation engine this contract *describes and pins*, not re-implements. Anchors: `assert_raw_preserved` (raw-before-parse gate), `verify_reproducibility` (re-hash vs recorded `sha256`), `reconcile_transcript_text` (transcript-text reconcile), `validate_sources` (`sources` preservation-validity), `preservation_replay` (the full Stage-2 gate), `preservation_manifest` (column-stable aggregate digest), `record_crawl_run` (Lane-1 run log).
> - `scripts/publication.py` @ `9b7d1aa` — `WEB_SAFE_FIELD_ALLOWLIST` (`:268`) / `WEB_UNSAFE_FIELDS` (`:345`), the field SSOT. Already names `raw_local_path`, `raw_sha256`, `raw_preservation_status` as unsafe (`:346–:355`). **Consumed read-only; `publication.py` stays 0-diff.**
> - `scripts/read_api.py` @ `9b7d1aa` — `assert_no_raw_paths` (`:125`) + `RAW_PATH_MARKERS` (`:56`), the transport-level raw-leak sweep; `reviewer_internal_records` (`:509`); `build_response` (`:898`, sweeps the whole body `:935`); `_strip_non_web_urls` (`:404`). **The live read surface the auditor joins; `read_api.py` stays 0-diff.**
> - `scripts/stage3_card_feed.py` (GOV-347) — the Stage 3 *separate-additive-module* precedent (read_api.py / publication.py 0-diff), and the principal web-safe projection whose output the no-leak invariant (§4) ranges over.
> - `Database/migrations/0003_sources.sql` (`sources`), `0004_crawl_runs_lane1.sql` (`crawl_runs` Lane-1 columns) — the schema the raw-preservation metadata lives in (read-only; no migration, no new column).
> - `Docs/GOV-262-preservation-replay-evidence.md`, `Docs/stage1-raw-store-layout.md`, `Docs/stage1-security-privacy-publication-gates-contract.md`, `Docs/stage3-03-source-inventory-contract.md` — the accepted Stage 1/2/3 preservation, raw-store, gate, and inventory references this contract extends.

This document pins the **raw-preservation contract** for the Stage 3 Alpine surface: *every parsed/derived record has a
raw predecessor that was retained, content-hashed, and version/as-of-stamped **before** any parse, with an archive
reference where one exists — and none of that raw metadata ever reaches a web-safe projection.* It **describes and
verifies** preservation over the **existing** Alpine pipeline. It authorizes **no** new crawl, **no** new source, **no**
ingestion-scope change, **no** public projection of raw metadata, **no** schema/migration change, and **no**
`publication.py` / `read_api.py` field change. See §7.

The **implementation** child (separate issue, blocked-by THIS doc) builds the read-time auditor/projection + RED tests
per §5. A contract defines the shape; it does not satisfy it.

---

## 0. What this child owns, and what it must not touch

**Owns (contract only):** the four raw-preservation invariants (§1), how each is *already* enforced in the existing
pipeline (§2), the reviewer-internal preservation-status projection shape (§3), the lane-gating + no-leak invariants the
impl must satisfy (§4), the impl shape + RED test list (§5), the risk gate (§6).

**Must NOT touch / re-derive (carry Stage 2 forward — GOV-363 "do not contradict / do not weaken Stage 2"):**
- the `raw_preservation.py` preservation engine — **described, never forked**: the auditor *calls* it (or reads its
  `crawl_runs` output), it does not re-implement hashing, the drift rule, or the raw-before-parse gate;
- the `publication.WEB_SAFE_FIELD_ALLOWLIST` / `WEB_UNSAFE_FIELDS` SSOT — **consumed read-only**, `publication.py` stays **0-diff**;
- the `read_api.assert_no_raw_paths` transport guard + `RAW_PATH_MARKERS` — **reused as the backstop**, `read_api.py` stays **0-diff**;
- the reviewer-internal vs public lane separation (GOV-146 / GOV-298 / GOV-306 / GOV-311 / GOV-347) — **reused, never forked**;
- the `sources` / `crawl_runs` / `documents` / `transcripts` schema — the auditor is **read-only** over it; no migration, no new column;
- the **absolute drift rule** (GOV-262): a recorded `sha256` is NEVER overwritten and a missing/mismatch raw is a preservation
  **DEFECT**, never a re-fetch and never a `completeness_gap`.

**Goal nuance (mirror of GOV-362 / GOV-346 / GOV-337):** at CTO non-author merge of **this doc**, no goal flips to
*achieved*. The 3.04 work is satisfied only when the **implementation** child merges.

---

## 1. The four raw-preservation invariants (pinned)

GOV-363 names four required metadata facts that must hold for every unit **before parse**: *raw record retained,
content hash, version/as-of, archive reference.* Pinned below as named invariants, each with the column(s) of record
and its web-safety. The columns of record are **reviewer-internal** — none is web-safe (§4).

| # | Invariant | Statement (must hold before any parse/derivation) | Column(s) of record | Web-safety |
|---|---|---|---|---|
| **RP-1** | **Raw retained** | The raw artifact bytes (documents) or the preserved `full_text` (transcripts) exist on disk / in-row, addressed by a stored locator, before any extraction reads them. | `documents.local_path`, `transcripts.local_path` / `transcripts.full_text`, `sources.raw_local_path` | **reviewer-internal** (`raw_local_path` ∈ `WEB_UNSAFE_FIELDS`; `local_path` never SELECTed into a web-safe projection) |
| **RP-2** | **Content hash** | A `sha256` of the raw bytes (documents/sources) or the preserved text (transcripts) is recorded at fetch time and re-verifiable. A re-hash mismatch is a tamper/corruption DEFECT that BLOCKS extraction. | `documents.sha256`, `transcripts.sha256`, `sources.raw_sha256` | **reviewer-internal** (`raw_sha256` ∈ `WEB_UNSAFE_FIELDS`; `.sha256` ∈ `RAW_PATH_MARKERS`) |
| **RP-3** | **Version / as-of** | An immutable first-capture timestamp and a latest-validation timestamp stamp every unit, so "what was known/preserved then" is reconstructable. The first-capture value is immutable; validation refreshes the latter only. | `sources.scan_date` (immutable as-of #1), `sources.last_validated_utc` (as-of #2), `documents.fetch_time_utc`, `crawl_runs.started_utc`/`finished_utc` | mixed — `scan_date`/`last_validated_utc` are **allowlisted** (already public-safe in 3.03); `fetch_time_utc` / run timing stay reviewer-internal |
| **RP-4** | **Archive reference** | Where a Wayback/archive copy exists, its URL + status are recorded; where none exists yet, the absence is an explicit recorded state (`not_checked` / `unavailable`), never a silent gap. | `sources.archive_url`, `sources.archive_status` | **allowlisted** (public locators; `archive_url` passes `_strip_non_web_urls`, a non-web value is dropped) |

**Ordering invariant (RP-0, load-bearing): raw-before-parse.** No row in any *derived* table (`documents.raw_text`,
`statements`, `evidence_links`, concept-map nodes/edges, cards) may exist whose raw predecessor did not satisfy
RP-1+RP-2 first. This is the existing `assert_raw_preserved` gate (`raw_preservation.py`), called by `embed.py` before
populating `documents.raw_text`. The Stage 3.04 auditor **verifies** this ordering held; it does not re-gate ingestion.

---

## 2. How each invariant is already enforced (extend, do not fork)

This contract is consistent-by-construction with Stage 2 because every invariant in §1 is **already** enforced by
committed code. Stage 3.04 adds a **read-time auditor** that *proves* the invariants over the current Alpine corpus and
*proves the no-leak* (§4) — it does not add a second, forkable enforcement path.

| Invariant | Already enforced by (`origin/main` @ `9b7d1aa`) | What 3.04 adds |
|---|---|---|
| RP-0 raw-before-parse | `raw_preservation.assert_raw_preserved` (gate called pre-extraction); test `test_raw_before_parse_gate_blocks_extraction_on_tamper` | a read-time assertion that the ordering held for the present corpus (no derived row without a hash-verifiable raw predecessor) |
| RP-1 raw retained | `documents.local_path` / `transcripts.local_path` written at fetch; `validate_sources` "preserved-by-children" rule | auditor reports retained-count + any missing-raw DEFECT (read-only) |
| RP-2 content hash | `verify_reproducibility` (documents), `reconcile_transcript_text` (transcripts), `validate_sources` (sources); `preservation_manifest` column-stable digest | auditor surfaces the manifest `aggregate_sha256` as the one-line "did-not-drift" proof in a reviewer-internal projection |
| RP-3 version/as-of | immutable `scan_date` (GOV-74 §1.02-i), `last_validated_utc`, `crawl_runs` timing | auditor includes both as-of dates per unit (as 3.03 already does for sources) |
| RP-4 archive ref | `sources.archive_url` / `archive_status` (Wayback leg, GOV-74 Issue C) | auditor includes archive presence/state per source |
| no-leak | `publication.WEB_UNSAFE_FIELDS` (raw_local_path/raw_sha256/raw_preservation_status) + `read_api.assert_no_raw_paths` transport sweep | RED tests proving the auditor's web-safe projection (if any) carries **zero** §1 reviewer-internal columns |

The canonical end-to-end preservation verification is `raw_preservation.preservation_replay` writing one
`crawl_runs` row tagged `preservation_replay` (success ⇒ aggregate manifest; failure ⇒ every offending
`{object_type, id, local_path}` listed). The 3.04 auditor **reads** that run-log result; it does not duplicate the pass.

---

## 3. The reviewer-internal preservation-status projection (shape only)

The impl child MAY surface a **reviewer-internal** preservation-status overlay (one row per preserved unit / per source),
so a reviewer can see at a glance that the four invariants hold. This is a *6th reviewer-internal overlay* in the sense of
`Docs/stage2-reviewer-internal-read-surface-reference.md`, a sibling of the 3.03 inventory overlay. **Pinned shape:**

```
PreservationStatusRow (reviewer-internal only — NOT web-safe):
  unit_ref        : {object_type: "document"|"transcript"|"source", id|source_id}   # id/slug only, never a path
  retained        : bool        # RP-1: raw locator present + artifact on disk
  hash_ok         : bool        # RP-2: re-hash == recorded sha256 (DEFECT if false)
  as_of           : {first_captured, last_validated}   # RP-3 (ISO timestamps)
  archive         : {present: bool, status}            # RP-4 (no archive_url string needed for the audit row)
  preservation_state : "preserved" | "defect" | "exception_documented"   # from validate_sources / replay
```

**Hard rule:** even though this row is reviewer-internal, it **must still pass `assert_no_raw_paths`**. Therefore it
carries **no** `raw_local_path`, **no** `raw_sha256` (the boolean `hash_ok` replaces it), **no** `.sha256` filename, and
**no** vault/`Source-Data`/`Raw-PDFs`/`/Users/` substring. The hash itself never appears — only the *verdict* of the hash
check. The aggregate `preservation_manifest.aggregate_sha256` MAY appear as a single opaque digest in the **feed
envelope** (not a path, not a per-unit raw locator), exactly as a reviewer-internal audit fingerprint.

A **public/web-safe** projection of preservation MUST surface **only** the already-allowlisted, already-3.03-cleared
fields — `scan_date`, `last_validated_utc`, `archive_status`, plus the computed `ui_status` (which already folds
`rawPreserved` into rules #3/#10 of `compute_ui_status`). No new web-safe field is introduced by 3.04.

---

## 4. Lane-gating + no-leak invariants the impl MUST satisfy

1. **Reviewer-internal by construction.** The preservation-status overlay (§3) is served only on the reviewer-internal
   lane (alongside `reviewer_internal_records`), never on the public/card lane. Gating holds *by construction* (the
   public builders never SELECT the §1 reviewer-internal columns), mirroring GOV-298/306/311/347.
2. **No raw metadata in any web-safe projection.** No `raw_local_path`, `raw_sha256`, `raw_preservation_status`,
   `fetch_time_utc`, `local_note_path`, or `notes` value crosses into `to_web_safe(...)` output, the card feed, or any
   public response. Enforced three ways, all already present: (a) fail-closed `WEB_SAFE_FIELD_ALLOWLIST` (the field is
   absent), (b) explicit `WEB_UNSAFE_FIELDS` membership (defense-in-depth), (c) the `assert_no_raw_paths` transport sweep
   on the assembled body (`build_response` `:935`).
3. **Hash never published; only the verdict.** A `sha256` value (64-hex) is reviewer-internal; only `hash_ok: bool` and
   the single opaque manifest digest (envelope-level audit fingerprint) may surface, and only on the reviewer-internal lane.
4. **0-diff to the SSOT.** `publication.py` and `read_api.py` are **unchanged**. The auditor is a **separate additive
   module** (the `stage3_card_feed.py` precedent), importing the allowlist/guard read-only.
5. **Fail-closed on defect.** A missing/mismatch raw is a preservation **DEFECT** surfaced as `preservation_state:"defect"`
   / `hash_ok:false`; it is never silently dropped, never a re-fetch, never a `completeness_gap`, and the recorded
   `sha256` is never overwritten (GOV-262 absolute drift rule).

---

## 5. Implementation shape + RED test list (for the blocked-by impl child)

**Shape (implementer's call, within these rails):** a separate additive module (e.g.
`scripts/stage3_preservation_audit.py`) exposing a read-time auditor that (a) reads/triggers the existing
`raw_preservation` verification over the Alpine corpus, (b) emits the reviewer-internal `PreservationStatusRow` overlay
(§3), (c) routes it through the existing `assert_no_raw_paths` backstop. `publication.py` / `read_api.py` **0-diff**.

**RED tests the impl child must add (must fail before the auditor exists, pass after):**

1. `test_preservation_audit_reports_all_invariants_on_intact_corpus` — on a seeded intact corpus, every unit row has
   `retained=True`, `hash_ok=True`, both as-of dates present, archive state present; `preservation_state="preserved"`.
2. `test_preservation_audit_flags_tamper_as_defect` — corrupt one stored artifact ⇒ that unit is `hash_ok=False` /
   `preservation_state="defect"`; the recorded `sha256` is unchanged (drift rule); no re-fetch.
3. `test_preservation_audit_flags_missing_raw_as_defect` — delete one stored artifact ⇒ `retained=False` / defect.
4. `test_preservation_overlay_passes_assert_no_raw_paths` — the full reviewer-internal overlay body passes
   `read_api.assert_no_raw_paths` (no path, no `.sha256`, no vault marker, no 64-hex sha in a per-unit row).
5. `test_preservation_no_raw_metadata_in_web_safe_projection` — `to_web_safe(...)` of any preservation row drops
   `raw_local_path` / `raw_sha256` / `raw_preservation_status` / `fetch_time_utc`; the web-safe view carries only
   `scan_date` / `last_validated_utc` / `archive_status` / `ui_status`.
6. `test_preservation_audit_publication_read_api_zero_diff` — guard test asserting the auditor imports the allowlist/guard
   without re-declaring them (no fork): `publication.WEB_UNSAFE_FIELDS` and `read_api.RAW_PATH_MARKERS` are the only
   sources of those constants.
7. `test_raw_before_parse_ordering_holds_over_corpus` — no derived row (`documents.raw_text` non-null) exists whose raw
   predecessor fails `assert_raw_preserved` (RP-0 verified read-time over the present corpus).

Each step (this contract; then the impl) runs the lane: **VSR (leg-1) → SecPriv (leg-2) → CTO non-author merge.**

---

## 6. Risk gate (per RISK_ASSESSMENT_WORKFLOW)

| Risk category | Touched? | Disposition |
|---|---|---|
| Evidence/source | yes | The contract *strengthens* traceability (proves raw retained + hash-verified + as-of-stamped before parse). No source claim is created. |
| AI-overclaim | no | Docs-only; no AI output, no inferred claim. |
| Privacy/account | yes (mitigated) | Raw locators, hashes, and `raw_preservation_status` are reviewer-internal; §3/§4 forbid them in any web-safe projection; `assert_no_raw_paths` is the transport backstop. No private identity/address/voter data is touched. |
| Defamation/legal/civic | no | No allegation, no public claim, no official contact. |
| Moderation/community | no | No public surface. |
| Publication/readiness | yes (mitigated) | 0-diff to `publication.py`/`read_api.py`; no new web-safe field; no public projection of raw metadata; no scope/launch unlock. |

**No-go lines honored:** no public projection of raw paths/hashes/internal preservation columns; no ingestion-scope
change; no Alpine-boundary expansion; no schema/migration; no goal flip on contract merge.

---

## 7. Explicit non-goals (what this contract does NOT authorize)

- **No new crawl / no new source / no re-fetch.** Read-only over the existing Alpine corpus.
- **No ingestion-scope change.** Preservation behavior at fetch time is unchanged.
- **No public projection of raw metadata.** No raw path, hash, or `raw_preservation_status` on any web-safe lane.
- **No `publication.py` / `read_api.py` / schema change.** SSOT consumed read-only; 0-diff required.
- **No stage/launch/budget unlock; no Alpine-boundary expansion.**
- **No goal → achieved on this doc's merge.** Only the impl child satisfies 3.04.

---

## 8. Acceptance criteria for THIS contract child (Docs-only)

- [x] Four raw-preservation invariants pinned with columns of record + web-safety (§1), plus the RP-0 raw-before-parse ordering invariant.
- [x] Each invariant mapped to its **existing** enforcement in `raw_preservation.py` / `publication.py` / `read_api.py` (§2) — extend-not-fork shown.
- [x] Reviewer-internal preservation-status projection shape pinned, hash-verdict-only, `assert_no_raw_paths`-clean (§3).
- [x] No-leak + lane-gating invariants stated against the existing SSOT/guard (§4); `publication.py`/`read_api.py` 0-diff required.
- [x] Impl shape + 7 RED tests enumerated for the blocked-by impl child (§5).
- [x] Risk gate completed; non-goals explicit (§6–§7).
- [ ] VSR (leg-1) review → SecPriv (leg-2) review → CTO **non-author** merge (the lane; pending).
