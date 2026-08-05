# Stage 5 — 2026-reopen reconciliation package (GOV-1660, slot 5.01)

**Author:** CTO (`24fddc65`) · **Slot:** Stage 5.01 — Spec package and root-plan reconciliation (`8abe2182`, active)
**Parent stage:** Stage 5 — Corrections, source-version change detection, and verification (`9d3d7fbd`, active)
**Scope:** Alpine-first (Town of Alpine, WY sources only). Planning/spec only — **no rework is implemented here.**
**Nothing in this document promotes any slot to `achieved`.** Promotion is the CEO/VSR gate at the 5.02 acceptance slice.

---

## 0. Why this package exists

Stage 5 (`9d3d7fbd`) is `active`, slot 5.15 is `achieved`, and every other slot 5.01–5.14 is
`planned` — each sitting on a **complete, merged June done-chain** with no open work. That is an
active-by-declaration stage (rulebook S4). It is **not drift**: each reopened slot body carries an
identical `## 2026 owner scope extension` section (verified on 5.01/5.02/5.03/5.04/5.05/5.07/5.12/5.13/5.14),
so this is authorized new scope. CEO set slot 5.01 active as the dependency-correct chain head and routed
this reconciliation to the CTO.

The extended scope adds six requirements the June deliverables predate. This package (a) maps each
requirement to the existing merged 5.x deliverable(s) with a disposition, (b) dispositions every reopened
slot, (c) sequences the rework into a real dependency chain, and (d) restates the Stage 5 parent
acceptance criteria as concrete testable checks.

### The six extended-scope requirements (from the reopening text + Stage 5 parent `9d3d7fbd`)

1. **Source-version comparison** — preserve original + changed versions (URL, retrieval time, hash, provenance) with typed supersession/correction lineage.
2. **Late-change red flags** — flag a material late agenda change, especially near a meeting or after a source was viewed/notified.
3. **Affected-record reprocessing** — invalidate and rerun *only* affected normalization, linkage, tags, source-grounded summaries, the six isolated lens outputs, and independent reviews.
4. **Isolated six-lens reruns** — each affected lens gets old version + new version + deterministic diff; **no lens sees another lens's output.**
5. **Independent review receipts** — a job exit is NOT verified completion; verified state requires an independent review receipt.
6. **Visible completion state** — `detected` / `pending` / `partially-reprocessed` / `review-pending` / `verified-comparison` / `withheld` / `failed`, all auditable.

---

## Part A — Requirement → merged-deliverable mapping (with disposition)

Disposition vocabulary: **`satisfied-by-existing-merge`** (cite PR/commit) · **`partially-satisfied`**
(name the satisfied part + the delta) · **`needs-rework`** (state the specific delta) ·
**`new-build`** (no merged antecedent; a fresh slice).

### A1 — Source-version comparison → **partially-satisfied / needs-rework**

| Layer | Merged antecedent | State |
|---|---|---|
| Raw snapshot preservation (content hash + retrieval provenance for a civic source) | `dae7f83` (#65 GOV-363, Stage 3.04 raw-preservation contract); replay proof `e3e689b` (#47 GOV-262) | substrate present |
| Typed supersession / correction lineage **model** | `d9e6109` (#89 GOV-531, Stage 5.07 Model 3 "source-change + archive verification"); `6616d3b` (#87 GOV-520, Stage 5.05 §1 `correctionsLedger` typed correction edges) | **contract** present, not implemented over civic sources |
| Two-versions-retained + red-flag-on-supersede **pattern** | `0aba779` (#140 GOV-1578, migration 0030 supplied-file versioning) | proven pattern — but for **user-supplied files**, not crawled civic sources |

**Delta (needs-rework):** there is no implemented record that binds `{original, changed}` versions of the
**same crawled civic source URL** with typed supersession lineage and per-version `{url, retrieval_time,
content_hash, provenance}`. The 5.07 model and the 0030 supplied-file pattern are the template; the civic
writer over the crawl/preservation path is the build. → **Slice 1** (homes: 5.04, 5.03).

### A2 — Late-change red flags → **new-build** (has a home slot already)

Merged antecedents give only signal substrate: 5.05 §2 `hotTopics` deterministic salience (`6616d3b`) and
the "red-flag on supersede" pattern (`0aba779`). There is **no** civic late-change detector, no materiality
criteria, no meeting-proximity or "viewed/notified since" trigger. CEO has already created the dedicated
home: **Stage 5.16 — Late source-change red flags, diff, and affected-work reprocessing** (`a05b55f1`, planned).

**Delta:** deterministic materiality rule set (what makes a change "material") + proximity-to-meeting window
+ "source was viewed/notified after retrieval" trigger, all in code with no model in the loop. → **Slice 2**
(home: 5.16).

### A3 — Affected-record reprocessing → **new-build**

No merged code computes an affected set or performs selective invalidation. `analysis.py` (`ff88681`) can
run a lens job, but nothing maps "this diff invalidates these normalization rows / linkages / tags /
summaries / lens outputs / reviews."

**Delta:** a diff → affected-set resolver keyed to the diff anchor (page / section / agenda item / meeting /
attachment), plus **idempotent selective invalidation** that touches *only* affected records and leaves the
rest byte-identical. → **Slice 3** (homes: 5.16 mapping, 5.05 orchestration, 5.07 statement/evidence binding).

### A4 — Isolated six-lens reruns → **partially-satisfied / needs-rework**

| Sub-requirement | State |
|---|---|
| **No lens sees another lens's output** | **`satisfied-by-existing-merge`** — `ff88681` (#107 GOV-736): `analysis.py` assembles the evidence set **once**, content-hashes it, and every lens consumes that identical context; the only write path is `submit_output` into its own staging row. The gate-bypass test proves canonical tables are byte-identical before/after a full run (AM-3); the fairness test asserts the evidence hash is equal across lenses. Isolation is structural, not policy. |
| **Six** isolated lenses (current + foundational) | **needs-rework** — only **three** lens packs exist today (libertarian / original-historical / liberal-progressive frames, `lenses.py`). Parent requires "six isolated current/foundational lens outputs." |
| Rerun input = **{old version, new version, deterministic diff}** | **needs-rework** — the runner today consumes a single evidence set, not a versioned diff triple. |

**Delta:** expand 3 → 6 lens packs along the current/foundational axis (preserving the D6 fairness-by-construction
symmetry — identical `SHARED_REQUIREMENTS`/`SHARED_PROHIBITIONS`), and extend the runner's assembled context to
carry `{old, new, diff}` from Slice 1/Slice 2. Isolation invariant is reused **by reference, not re-implemented.**
→ **Slice 4** (homes: 5.09 boundary matrix + the `scripts/mcp_service/` lens layer). Depends on Slices 1–3.

### A5 — Independent review receipts → **partially-satisfied / needs-rework**

Substrate present: `review_state` bookkeeping on the staging row (`analysis.py`, `ff88681`), the
`reviewer_decisions` table + reviewer-promotion path (`reviewer:isaac`), and the standing rule that
`submit_output` → staging is explicitly **not** verified on exit. Parallel precedent for a review-transition
audit event: **GOV-1619** (supplied-file `supplied_file_review_events`, migration 0031, backlog).

**Delta (needs-rework):** a first-class **review-receipt** record that *gates* the `verified-comparison`
completion state, plus the invariant "a reprocess job's exit never advances completion state without a matching
independent review receipt." → **Slice 5** (homes: 5.12 traceability/audit; the 5.02 acceptance gate enforces it).

### A6 — Visible completion state → **new-build** (display substrate exists)

Display substrate present: 5.06 frontend surface (`afdba0e` #88, corrections cards / hot-topic ranking /
watchdog board) and 5.05 §3 `watchdogView` lanes (`6616d3b`). There is **no** completion-state enum or state
machine.

**Delta:** a completion-state enum `{detected, pending, partially-reprocessed, review-pending,
verified-comparison, withheld, failed}` with an auditable transition log; **fail-closed default = `withheld`**;
public surface renders the state, never computes it (Directive 9 — the frontend must not derive trust). →
**Slice 5** (state machine + audit) + **Slice 7** (surface it on 5.06). The 5.02 gate defines pass/fail for it.

### A-summary

| # | Requirement | Disposition | Primary slice |
|---|---|---|---|
| 1 | Source-version comparison | partially-satisfied / needs-rework | Slice 1 (5.04, 5.03) |
| 2 | Late-change red flags | new-build | Slice 2 (5.16) |
| 3 | Affected-record reprocessing | new-build | Slice 3 (5.16, 5.05, 5.07) |
| 4 | Isolated six-lens reruns | isolation **satisfied** (`ff88681`); six-lens + diff-input needs-rework | Slice 4 (5.09, mcp lens layer) |
| 5 | Independent review receipts | partially-satisfied / needs-rework | Slice 5 (5.12) |
| 6 | Visible completion state | new-build (display substrate exists) | Slice 5 + Slice 7 (5.12, 5.06) |

**No requirement is `satisfied-by-existing-merge` in full.** Exactly one *invariant* — lens output isolation
(A4) — carries forward unchanged and is reused by reference.

---

## Part B — Per-reopened-slot disposition

Every reopened slot carries the identical `## 2026 owner scope extension` section. None is silently carried.

| Slot | June merge (antecedent) | Disposition | Delta the rework must add |
|---|---|---|---|
| **5.02** Acceptance criteria & exit gate (`17dd1c9e`) | June exit gate | **needs-rework** | Restate pass/fail for the extended scope (Part D of this doc lands here). Consumes this 5.01 package. **VSR-owned exit review.** |
| **5.03** Source/data inventory (`6f3df42f`) | `e9de073` (#84 GOV-484) | **needs-rework (partial)** | Inventory must enumerate civic **source-version** records + archive-status-near-scan (feeds A1). Contract predates versioning. |
| **5.04** Raw preservation & reproducibility (`7038ecfa`) | `dae7f83` (#65), `e3e689b` (#47) | **needs-rework** | Home for A1: retain both versions with `{url, retrieval_time, hash, provenance}`, reproducibly, without exposing unreviewed data. |
| **5.05** Backend/tooling impl contract (`e75adeb3`) | `6616d3b` (#87 GOV-520 watchdog signals) | **needs-rework** | Reprocessing orchestration (A3) + late-change detector wiring (A2). `correctionsLedger`/`hotTopics`/`watchdogView` are the signal substrate, reused by reference. |
| **5.07** Transcript/evidence/statement model (`29f19a58`) | `d9e6109` (#89 GOV-531 trust model) | **needs-rework (partial)** | Models 3 (source-change+archive) & 4 (future-fact) **satisfy the model layer of A1**. Delta: bind statements/evidence to the versioned diff so reprocessing knows which statements are affected (feeds A3). |
| **5.12** Traceability & audit trail (`17eba456`) | June traceability | **needs-rework** | Home for A5 (review receipts) + A6 (completion-state audit) + the end-to-end source→receipts→reviews trace. |
| **5.13** Back-gap / regression analysis (`e454596f`) | `cd6dc02` (#93 GOV-574) | **needs-rework (partial)** | Add a regression axis: a reprocess must never silently drop or alter a **prior verified comparison** (monotonicity of correction lineage — history is preserved, not overwritten). |
| **5.14** Documentation & project-state continuity (`43937de7`) | June doc-maintenance | **needs-rework (thin)** | Keep control-plane project state current for the reopened chain; closeout slot, runs last. |

### Slots referenced but **out of this reconciliation's rework scope**

- **5.16 — Late source-change red flags, diff, reprocessing** (`a05b55f1`, planned, **newly created by CEO**):
  the dedicated home for A2/A3/A4-input. In scope of the *chain* (Slices 2–3) but a new slot, not a "reopened
  June" slot, so it is dispositioned as **new-build**, not reconciled against a June merge.
- **5.06 — Frontend/product surface** (`634f6955`): not in the issue's reopened list; it is the render surface
  for A6 (Slice 7). Contract `afdba0e` reused by reference.
- **5.09 — Automation vs AI boundary matrix** (`057024bf`): must **re-certify** that the reprocess + six-lens
  reruns keep every deterministic step (versioning, diff, affected-set, invalidation) in code with no model in
  the loop, and AI strictly behind labels (matrix antecedent `2177f15`, #17 GOV-87). Part of Slice 4.
- **5.08 — Newsletter/briefing/editorial** (`d96ceaed`): **explicitly excluded.** Standing owner decision
  GOV-545 Option A ("hold Alpine, stop 5.15") directs *not* to re-seed 5.08. No rework is sequenced into it.
  Flag only.
- **5.10 — QA & workflow testing** (`1125526e`) and **5.11 — Security/privacy/publication gates**
  (`5bcc2b9b`): not reopened with the 2026 extension in the set verified here; the acceptance checks in Part D
  are executed at 5.02 (VSR) and the publication/fail-closed checks are co-signed by **SPA** (`72d0eccf`).

---

## Part C — Dependency-ordered rework slice sequence

A real dependency chain, not a flat list. Each slice = **one impl leg (BCE `f26f530c`) + one review leg**
(VSR `3f95c8ce` by default; **SPA `72d0eccf`** where the slice touches publication/privacy/fail-closed — marked
⚑). Chain cap: one impl + one review per slice, no separate sequencing/checkpoint tickets.

```
Slice 1  ──►  Slice 2  ──►  Slice 3  ──►  Slice 4  ──►  Slice 5 ⚑ ──►  Slice 6  ──►  Slice 7 ⚑ ──►  Slice 8 (5.02 exit, VSR + SPA co-sign)
(5.04,5.03)   (5.16)        (5.05,5.07)    (5.09,mcp)     (5.12)         (5.13)        (5.06,5.14)
```

| Slice | Slots | Delivers (requirement) | Depends on | Why the order |
|---|---|---|---|---|
| **1** | 5.04, 5.03 | Civic **source-version preservation & inventory** — both versions, `{url, retrieval_time, hash, provenance}`, typed supersession (A1) | — | Nothing can be diffed until two versions of the same source are preserved. Foundation. |
| **2** | 5.16 | **Late-change detection + structured before/after diff** anchored to page/section/agenda-item/meeting/attachment (A2; diff artifact for A4) | Slice 1 | A diff needs a preserved version pair. |
| **3** | 5.05, 5.07 | **Affected-set resolver + selective invalidation**; statement/evidence↔diff binding (A3) | Slice 2 | Needs the diff + anchors to know what is affected. |
| **4** ⚑ | 5.09, `scripts/mcp_service/` | **Six-lens reruns** with `{old, new, diff}` input; isolation reused by reference; boundary-matrix re-certification (A4) | Slices 1,3 | Needs the affected set (what to rerun) + the versioned diff (rerun input). ⚑ lens output touches publication. |
| **5** ⚑ | 5.12 | **Independent review receipts** + **completion-state machine** `{detected…failed}` + audit; fail-closed `withheld` (A5, A6) | Slices 3,4 | Gates the jobs that Slices 3–4 produce; can't gate what doesn't run yet. ⚑ fail-closed/withheld = SPA. |
| **6** | 5.13 | **Regression axis**: a reprocess never silently drops/alters a prior verified comparison (monotonic correction lineage) | Slice 5 | Needs the completion state to regress against. |
| **7** ⚑ | 5.06, 5.14 | **Render** the completion state (never compute it — Directive 9); keep control-plane project state current | Slice 5 | Needs the state machine before it can be surfaced. ⚑ public surface = SPA co-sign. |
| **8** | 5.02 | **Acceptance/exit gate** — VSR verifies the Part-D checks end-to-end; **SPA co-signs publication** | Slices 1–7 | Terminal. Promotion to `achieved` is decided here by CEO/VSR — **not by GOV-1660.** |

**Chain head after this package:** Slice 1 (5.04/5.03). **Chain terminal:** Slice 8 (5.02 exit).

---

## Part D — Stage 5 parent acceptance criteria, restated as concrete testable checks

The parent `9d3d7fbd` acceptance criteria, made determinate. These are the checks the **5.02 acceptance gate
(VSR)** runs against the completed chain; each names the artifact and the intended `pytest` target the impl
slices must make real. (Targets are the contract for the impl slices — they do not exist until those slices land.)

**D-1 — End-to-end trace of a changed agenda.**
> *Given* a civic source with a preserved original version and a later changed version, *when* the change is
> detected, *then* a single query traces: source-detection → both preserved versions (hash-verified) → the
> structured diff → every invalidated record → each affected six-lens rerun → each independent review receipt.
> No hop is missing or dangling.
> Intended check: `pytest tests/test_stage5_reprocess_trace.py::test_changed_agenda_traces_end_to_end`.

**D-2 — Completeness is receipt-gated, never exit-gated.**
> *Given* a change comparison with any affected stage or lens `pending`, `failed`, or `unreviewed`, *then* its
> completion state is **never** `verified-comparison`; a job exit alone never advances it; `verified-comparison`
> requires a matching independent review receipt for every affected unit.
> Intended check: `pytest tests/test_stage5_completion_state.py::test_not_verified_while_any_unit_pending_or_unreviewed`.

**D-3 — Materiality, change detail, citations, and status are visible and auditable.**
> *Given* a detected material change, *then* the red-flag materiality reason, the before/after change detail,
> the source citations for both versions, and the current reprocessing status are all present in the audit
> record and rendered on the surface — none derived client-side.
> Intended check: `pytest tests/test_stage5_audit_visibility.py::test_materiality_change_citation_status_all_present`.

**D-4 — Public output stays source-linked, reviewed, and fail-closed.**
> *Given* any completion state other than `verified-comparison`, *then* no unreviewed comparison, diff, or lens
> output reaches a public surface; the default state is `withheld`; every published unit is source-linked and
> carries a review receipt. **SPA co-signs.**
> Intended check: `pytest tests/test_stage5_fail_closed.py::test_withheld_default_and_no_unreviewed_publication`.

**D-5 — History is preserved, not overwritten (monotonicity).**
> *Given* a reprocess of an already-verified comparison, *then* the prior user-facing/history state survives
> with correction lineage; nothing is overwritten in place; the regression axis (Slice 6) flags any silent loss.
> Intended check: `pytest tests/test_stage5_regression.py::test_reprocess_preserves_prior_verified_state`.

---

## Part E — Boundaries & routing (restated for the impl phase)

- **This slice (5.01) is planning/spec only.** No rework is implemented here. Impl issues are created *after*
  this reconciliation is accepted at the 5.02 gate — one impl (BCE) + one review leg per slice, per Part C.
- **Production owner** of the reopened slices: **BCE** (`f26f530c`). **Stage exit review:** **VSR**
  (`3f95c8ce`). **SPA** (`72d0eccf`) co-signs the publication/fail-closed slices (⚑: Slices 4, 5, 7, 8).
- **Nothing is promoted to `achieved` by GOV-1660.** Promotion is the CEO/VSR gate at Slice 8 (5.02).
- **Alpine-first:** Stage 5 stays scoped to Alpine, WY sources. No expansion.
- **Determinism law (Directive 7 / slot .09):** versioning, hashing, diffing, affected-set resolution, and
  selective invalidation are deterministic code with **no model in the loop**. The six lenses are *readings* of
  already-verified evidence and write only to staging; they never mutate a canonical record. Any slice that puts
  a model on a hash/diff/match path does not merge.
- **Trust is never computed on the frontend (Directive 9):** the surface renders the completion state; it never
  derives, averages, or rounds it.

---

## Provenance

Source: reopened slot bodies (`9d3d7fbd`, `8abe2182`, `a05b55f1`, and the seven reopened slots) + merged
June/July commits cited inline · Date: 2026-07-30 · Confidence: high (commit refs verified in-repo against
`origin/main` `dabad1d`) · Owner: CTO (`24fddc65`). Routed by CEO from the 2026-07-30 heartbeat gate audit.
