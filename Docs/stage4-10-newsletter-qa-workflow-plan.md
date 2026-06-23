# Stage 4.10 — Reviewer-Internal Newsletter QA / Workflow Testing Plan

> **Issue:** GOV-472 (Stage 4.10 · VerificationSafetyReviewer). **Blocks:** GOV-475 (CEO Stage 4 exit / Stage 5 unlock decision).
> **Depends on (both `done`):** GOV-470 (Stage 4.08 reviewer-internal weekly briefing editorial contract) · GOV-471 (Stage 4.09 automation-vs-AI boundary).
> **Stage:** 4.10 — QA/workflow **plan** for the *current* reviewer-internal newsletter backbone. This is a test plan and go/no-go checklist, not an execution run; running it is the Stage-4 exit-review activity (GOV-475 owner schedules executors).
> **Scope:** Town of Alpine only · **reviewer-internal only** · no public launch · no email/sender · no signup/auth · no person-naming beyond on-record officials · no new crawl. Public deploy stays **GOV-420 / Isaac-gated** and is untouched by this plan and by passing it.
> **Owner:** VerificationSafetyReviewer (this plan + the go/no-go gate). **Escalation:** CEO `e618342a` for the Stage-4 exit decision and any public-risk call; CTO `24fddc65` for backbone/script defects; NewsletterEditor `6b3d5c0e` for editorial-contract defects.

---

## 0. Why this plan exists, and what "the current backbone" is

The Stage 4 reviewer-internal newsletter backbone is **deterministic data + a gated editorial layer + an automation boundary**, split across three repos. QA must follow evidence across all three seams, because that is where binding/label integrity actually breaks. Every artifact below is the **canonical merged** version on its repo's `main`/`origin/main` as of this plan; QA re-pins the exact HEAD at execution time and records it (§6).

| Layer | Stage | Artifact (canonical path) | Repo | What QA proves over it |
|---|---|---|---|---|
| Item feed | 4.03 | `scripts/stage4_newsletter_feed.py` | Python (`Government-watchdog`) | one item per served reviewer-internal record; chronology; labels ⊆ `STAGE3_CLAIM_VOCAB`; `sourceTrail[]`; orphans routed not dropped |
| Preservation auditor | 4.04 | `scripts/stage4_newsletter_preservation_audit.py` | Python | raw never mutated; lossless provenance; reproducible |
| Digest assembler | 4.05 | `scripts/stage4_newsletter_digest_assembler.py` | Python | one digest per ISO-week `newsletterId`; GOV-15 sections as **data**; labels + `sourceTrail` carried verbatim; chronology non-decreasing |
| Binding validator | 4.07 | `scripts/stage4_statement_evidence_binding.py` | Python | every statement-bearing item binds to an **exact-source pointer**; no silent label upgrade; paraphrase ≠ verbatim |
| Editorial contract | 4.08 | `docs/stage4-08-newsletter-editorial-contract.md` | Website (`Government-watchdog-website`) | allowed labels, prohibited language, source-link rule, correction handling; **gaps-led, 0-verified** briefing reality |
| Archive/detail + digest rendering | 4.06 | reviewer-internal `/alpine/` archive + detail routes | Website | renders the digest object + deep-links to source without leaking raw paths |
| Automation/AI boundary | 4.09 | `Docs/stage4-automation-ai-boundary.md` (+ `src/`) | Node (`/Users/IA/GitHub/Government-Watchdog`) | deterministic vs AI split enforced in code; AI never reaches `verified`; fail-closed |

**Grounding reality (from the 4.08 editorial contract, captured 2026-06-20 over `src/fixtures/alpine-card-feed.json`):** **0** `verified` items · **6** `ai_presented` (auto-caption, untimed, grounded) · **213** `source_missing` gaps. The current briefing is **gaps-led with zero verified items**, and every present item carries the locked `AI — not independently verified` label. **A QA pass that shows the UI reading as confident reporting over this corpus is a FAIL,** not a pass — see G-2 and AC-2 below.

---

## 1. Prerequisites before a QA run (gate to even start)

A QA run cannot start — and the go/no-go is auto **NO-GO** — unless all of these hold. Record each in the run log (§6).

- [ ] All four Python backbone CLIs run clean over the canonical reviewer-internal DB: `--check` exits `0` for feed, preservation, digest, and binding. (Command set in §3, Step 0.)
- [ ] The 4.08 editorial contract is the merged version and its frozen label set still equals `STAGE3_CLAIM_VOCAB` (zero new labels). Re-confirm against the imported vocabulary, not from memory.
- [ ] The 4.09 automation boundary repo's suite is green (`npm test`) and `Logs/` is gitignored (no run evidence published).
- [ ] A reviewer-internal archive/detail route + digest rendering build exists to drive (4.06). **If it does not yet exist, this plan still produces the data-layer verdict, and the go/no-go records the UI-evidence rows as `BLOCKED — no built route, owner: frontend/CTO`** rather than silently passing. UI rows cannot be marked PASS without screenshots (AC-3).
- [ ] Run is Alpine-only and reviewer-internal-only. Any non-Alpine record or any `access != reviewer_internal` surface seen mid-run is an immediate hard stop → CEO.

---

## 2. AC-1 — User-like verification steps: archive/detail route + digest rendering

Drive the reviewer-internal UI as a reviewer would. Each step lists the **action**, the **expected**, and the **evidence** to capture. Run every UI step at all three viewports (§4).

**Archive route (list of weekly digests):**

1. **Load the archive.** Action: open the reviewer-internal `/alpine/` newsletter archive. Expected: one entry per `newsletterId` that exists in `assemble_digests` output (`alpine-historical-YYYY-WW` + `alpine-historical-undated`), ordered lexically by `newsletterId`; **no invented/empty period** (a `newsletterId` appears iff ≥1 served item carries it). Evidence: screenshot + the digest-object `newsletterId` list from `--artifact digest`.
2. **Coverage period framing.** Action: read each archive entry's date range. Expected: dated weeks show their Mon→Sun `coveragePeriod`; the undated batch shows an explicit "undated" framing, **never a fabricated date**. Evidence: screenshot; cross-check against `coveragePeriod` in the digest object.
3. **Empty / gaps-led state.** Action: observe the top-line framing. Expected: the archive presents as **gaps-led** (213 source-missing gaps, 0 verified) — it must not headline as a confident report. Evidence: screenshot; note the gap framing text.

**Detail route (one weekly digest):**

4. **Open a digest detail.** Action: click into one `newsletterId`. Expected: the GOV-15 sections render from the digest's `sections` **as structured content** (processed records, source-set progress, timeline chunks, key meetings/documents, topics, corrections/conflicts/later-outcomes, unverified items, source trail). Evidence: screenshot per section present.
5. **Item ↔ section consistency.** Action: spot-check that every item shown in `processedRecords` also appears in the rendered item list, and that an item in `corrections` renders with its correction treatment. Expected: render is a faithful projection of the digest object — no item shown that is not in `items[]`, no item silently dropped. Evidence: screenshot + item-id diff vs `--artifact digest`.
6. **Label visibility.** Action: for each rendered item, confirm its Stage-3 label is visible **without hover/scroll-reveal** on that item. Expected: `AI — not independently verified` (and any `disputed`/`corrected`) labels are visible inline; visual polish never implies verification. Evidence: screenshot showing the label adjacent to the item.

---

## 3. AC-2 — Evidence-integrity checks (binding, deep-link, labels, no future-knowledge)

These are the load-bearing safety checks. Each maps to a backbone guard so the UI claim is provable against data, not eyeballed.

**Step 0 — data-layer baseline (run first, every QA run):**
```
python3 scripts/stage4_newsletter_feed.py            --db <DB> --check
python3 scripts/stage4_newsletter_preservation_audit.py --db <DB> --check
python3 scripts/stage4_newsletter_digest_assembler.py   --db <DB> --check
python3 scripts/stage4_statement_evidence_binding.py     --db <DB> --check
```
All four must exit `0`. The binding overlay (`--artifact overlay --check`) must report `all_bound`, `no_unrouted_orphans`, `labels_conservative`, `verbatim_anchored` all `true`. Capture stdout to the run log.

**A. Exact-source binding (4.07).** For each statement-bearing digest item rendered in the detail route:
- Confirm it carries an exact-source pointer — a resolving `segment_id` edge **or** ≥1 `evidence_link` with a complete valid pointer (`statements.validate_pointer`). Source of truth: the binding `--artifact log` row's `pointerKind` is non-null and `resolves:true`.
- Confirm **no orphan is silently shown**: any orphan must appear in the log's `routing[]` with `routedTo: VerificationSafetyReviewer, status: held` — and the UI must **not** render it as a normal sourced item. An orphan rendered as if sourced is a **FAIL → block** (correction workflow).
- Confirm **no silent upgrade**: each item's displayed status equals the conservatively recomputed `claimStatus` (`labels_conservative:true`). An item shown as verified when the read surface recomputes unverified is a **FAIL → block**.
- Confirm **paraphrase ≠ verbatim**: any item styled as a verbatim quote has a segment or `quoted_text` anchor (`verbatim_anchored:true`). A paraphrase shown in quote styling is a **FAIL → block**.

**B. Timestamp / deep-link behavior.** For each item with a source deep-link:
- The deep-link resolves to a reviewer-internal `/alpine/` route (or an in-page source anchor), **never to a raw vault path / `file://` / `.sha256` / `transcript_path`**. Re-run `read_api.assert_no_raw_paths` over the rendered payload / digest object; a raw path must fail **LOUDLY** (`RawPathLeak`). Any raw path reaching the UI is a hard stop → §5.
- The displayed source locator (timestamp / page / section) matches the bound pointer's locator (`pointerKind` + the `statements.LOCATOR_REQUIRED_FIELDS` field). A shown timestamp/page with no backing pointer field is a **FAIL → block**.

**C. Correction / dispute labels.**
- Every item in the digest `corrections` section (itemType `correction` or `correctionStatus != none`) renders with the contract correction treatment `[CORRECTION: Updated YYYY-MM-DD. Original: "..." | Corrected: "..."]`. A correction shown without its note is a **FAIL → block**.
- Every item in `conflicts` (`claimStatus == disputed`) renders the `Disputed — sources conflict` label visibly. `laterOutcomes` (`source_changed` / `source_missing`) render their conservative label, never as fact.

**D. No future-knowledge leakage into historical blocks (as-of-date integrity).** This is the VSR-specific historical-honesty check (Isaac concept-map directive: *"later outcome updates prior event without rewriting known-then context"*):
- For each weekly digest covering ISO-week *N*, confirm **no item, label, correction, or outcome inside that historical block reflects knowledge dated after week *N*'s `coveragePeriod.endDate`**. A later correction/outcome may be **linked** as a later-outcome pointer, but the historical block must read as it was known *then* — it must not be silently rewritten with hindsight.
- Concretely: a `corrections`/`laterOutcomes` entry attached to a week-*N* item must carry its own later date and be presented as an *update to* the prior event, not folded into the original block as if known at the time. Cross-check each correction's `Updated YYYY-MM-DD` against the host block's coverage end date. A hindsight rewrite with no later-dated marker is a **FAIL → block**.
- Confirm digest determinism backs this: `assert_reproducible` (digest + binding) returns byte-identical builds, so "what the block said" is fixed and auditable, not wall-clock-dependent.

---

## 4. AC-3 — Minimum viewport / device evidence floor

Per the Backend/Frontend Evidence Workflow UI viewport floor, **reviewer-internal UI QA cannot pass from mobile/tablet evidence alone.** Required evidence set, per UI route (archive + detail):

| Viewport class | Resolution | Required |
|---|---|---|
| Desktop | 1440×900 | ✅ mandatory |
| Tablet | 768×1024 | ✅ mandatory |
| Mobile | 390×844 | ✅ mandatory |

- Capture a labeled screenshot of **archive** and **detail** at **each** of the three viewports (6 screenshots minimum), each showing item labels visible inline (AC-1 step 6) and at least one source deep-link.
- If any viewport class is missing, the closeout must **name the missing class, the reason, and the next owner** (frontend/CTO) — the go/no-go records that UI row as `BLOCKED`, never PASS.
- Screenshots and run logs are reviewer-internal evidence: store local/vault-only (data-publication boundary). Do not attach raw corpus or screenshots containing raw paths to any public surface.

---

## 5. Failure handling & escalation

- **Evidence/label/binding FAIL** (any §3 A/B/C/D fail): invoke the VSR correction-handling workflow — comment "Content blocked pending correction" on the relevant issue, route the specific defect to its owner (CTO for backbone/script, NewsletterEditor for editorial-contract/UI copy, FrontendTimelineEngineer for render), re-QA after fix.
- **Hard stops → CEO immediately** (do not attempt to pass): any raw path / `file://` / vault path reaching the UI; any private individual or PII rendered; any non-Alpine record; any `access != reviewer_internal` surface; any attempt to treat passing this plan as public-launch readiness (it is **not** — GOV-420 stays the public gate).
- **Backbone CLI non-zero exit**: treat as a backbone regression → CTO, with the failing `--check` stdout attached. QA does not edit the script.

---

## 6. Run log / artifacts (what every QA execution records)

Logs are part of the workflow, not an afterthought (WORKFLOW_GOVERNANCE §Automation/log rule). Each QA run records, reviewer-internal / vault-only:

- Pinned HEADs: Python `origin/main` SHA, website `main` SHA, Node boundary SHA, and the DB path used.
- The four backbone `--check` stdouts (exit codes) + the binding overlay JSON (`all_bound` / `no_unrouted_orphans` / `labels_conservative` / `verbatim_anchored`).
- The 6+ viewport screenshots (archive/detail × desktop/tablet/mobile).
- A per-item evidence table for spot-checked items: itemId · pointerKind · resolves · displayed label · deep-link target (route, not path) · correction/dispute treatment.
- The filled go/no-go checklist (§7) with each row PASS / FAIL / BLOCKED and owner.

---

## 7. AC-4 — Stage 4 exit go/no-go checklist

This is the gate VSR posts into the Stage-4 exit review (GOV-475, CEO). **GO requires every row PASS.** Any `FAIL` ⇒ **NO-GO** (block + route). Any `BLOCKED` ⇒ **NO-GO** until the named owner clears it. Reviewer-internal exit only — **GO here never means public launch** (that stays GOV-420 / Isaac-gated).

**Data layer (Python backbone):**
- [ ] Feed `--check` exit 0 — one item per served record, labels ⊆ `STAGE3_CLAIM_VOCAB`, chronology ok, orphans routed.
- [ ] Preservation `--check` exit 0 — raw never mutated, lossless provenance, reproducible.
- [ ] Digest `--check` exit 0 — GOV-15 sections as data, labels + `sourceTrail` carried verbatim, chronology non-decreasing, `reproducible:true`.
- [ ] Binding overlay — `all_bound`, `no_unrouted_orphans`, `labels_conservative`, `verbatim_anchored` all `true`.
- [ ] No-leak — every artifact passes `read_api.assert_no_raw_paths`; every `sourceTrail[].localSourcePath` is `null`; no per-item 64-hex hash.

**Editorial + automation:**
- [ ] 4.08 editorial contract: zero new labels (frozen `STAGE3_CLAIM_VOCAB`), prohibited-language list honored, source-link/no-orphan rule met, corrections handled, **nothing implies publication readiness**.
- [ ] Briefing reality honored: gaps-led, 0 verified items surfaced as fact; every present item carries `AI — not independently verified`.
- [ ] 4.09 automation boundary: suite green; AI confined to `ai_analysis`, structurally barred from `verified`; fail-closed table holds; `Logs/` gitignored.

**UI (archive/detail + digest rendering):**
- [ ] Archive renders one entry per real `newsletterId`, correct ordering, no invented period, gaps-led framing.
- [ ] Detail renders GOV-15 sections faithfully; item↔section consistent; labels visible inline without hover/scroll.
- [ ] Exact-source binding visible: no orphan shown as sourced, no silent upgrade, paraphrase ≠ verbatim.
- [ ] Deep-links resolve to reviewer-internal routes/anchors — **no raw path reaches the UI**; shown timestamp/page/section backed by the bound pointer.
- [ ] Correction/dispute treatments render; no future-knowledge rewrite of historical blocks (as-of-date integrity).
- [ ] Viewport evidence floor met: desktop 1440×900 **and** tablet 768×1024 **and** mobile 390×844 screenshots for archive **and** detail.

**Scope & safety:**
- [ ] Alpine-only, reviewer-internal-only throughout; no non-Alpine, no public surface, no email/auth.
- [ ] No private individual / PII rendered; speaker attribution within on-record-official bounds.
- [ ] Public-launch gate GOV-420 remains closed and untouched by this exit.

**Disposition line (VSR posts to GOV-475):**
`STAGE 4 EXIT — GO / NO-GO — <date> — reviewer-internal Alpine newsletter backbone — [n PASS / n FAIL / n BLOCKED] — public launch NOT in scope (GOV-420 gated).`

---

## 8. Acceptance criteria coverage (GOV-472)

| AC | Where satisfied |
|---|---|
| 1. User-like verification steps for archive/detail route + digest rendering | §2 (6 steps, archive + detail) |
| 2. Exact-source binding, timestamp/deep-link, correction/dispute labels, no future-knowledge leakage | §3 A–D |
| 3. Minimum viewport/device evidence for reviewer-internal QA | §4 (desktop+tablet+mobile floor, 6-screenshot minimum) |
| 4. Go/no-go checklist for Stage 4 exit review | §7 |

## 9. Owner & review

- **Owner:** VerificationSafetyReviewer maintains this plan; update it when the backbone gains a slice (4.06 render, 4.11+ ) or when a label/contract rule changes durably (patch this file + the contract, not an issue comment — WORKFLOW_GOVERNANCE §10).
- **Consumer:** CEO `e618342a` at the Stage-4 exit decision (GOV-475). VSR runs §7 and posts the GO/NO-GO; the CEO/Isaac own the actual Stage-4 closeout and any Stage-5 unlock.
- **Review cadence:** re-run §7 at each Stage-4 exit attempt and whenever any of the three repos' newsletter artifacts change.
