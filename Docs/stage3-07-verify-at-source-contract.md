# Stage 3.07 — Verify-at-Source Drill-Down Read-Contract over the Live Card Surface

> **Issue:** GOV-375 (Stage 3.07 · Plan · CEO→BackendCrawlerEngineer). **Parent:** GOV-373 (CTO Stage 3 sequencing).
> **Stage:** 3.07 — `contract→impl` **contract (planning) child only**. **NON-implementation. NON-unlock.**
> **Scope:** Town of Alpine only · reviewer-internal · no public launch · no new crawl · no new web-safe field · no ingestion-scope change.
> **Grounded on:** canonical remote `origin/main` HEAD `04f1bc4` (Stage 3.04 raw-preservation auditor merged, PR #68).
> **Goal of record:** Stage 3.07 subgoal `3069bd05-65c2-4b4d-81f5-0e02a6c883db` (Transcript/evidence/statement model) — **NOT** the HEAD goal (goal-link discipline per GOV-373).
> **Inputs of record (read-only grounding, no edit by this child):**
> - `scripts/read_api.py` @ `04f1bc4` — the live reviewer-internal read surface this contract *describes and pins*, never re-implements. Anchors: `_evidence_links_for` (`:154`), `_segment_resolves` (`:163`), `_web_safe_evidence` (`:421`), `_serialize_statement` (`:426`, evidence drawer attached `:453–:455`), `_strip_non_web_urls` (`:404`) + `_PUBLIC_URL_FIELDS` (`:88`), `_provenance_status_for` (`:354`) + `_ai_provenance_ok` (`:334`), `PROVENANCE_GROUNDED`/`PROVENANCE_UNVERIFIED`/`PROVENANCE_STATUS_VALUES` (`:329–:331`), `reviewer_internal_records` (`:509`), `completeness_gap_cards` (`:612`), `assert_no_raw_paths` (`:125`) + `RAW_PATH_MARKERS` (`:56`), `build_response` (`:898`, whole-body sweep `:935`). **Consumed read-only; `read_api.py` stays 0-diff.**
> - `scripts/stage3_card_feed.py` (GOV-347) @ `04f1bc4` — the live Alpine card feed (3.05/3.06) whose served cards this drill-down ranges over. Anchors: `build_card_feed` (`:303`), `_record_card` (`:237`, evidence rides verbatim `:253`), `_gap_card` (`:272`), `sourced_cards` (`:325`), `assert_feed_covers_surface` (`:358`) + `expected_handles` (`:343`). **The Stage 3 separate-additive-module precedent (read_api/publication 0-diff); the surface the impl successor projects over, not forks.**
> - `scripts/stage2_traceability.py` (GOV-306) @ `04f1bc4` — the per-row resolvability predicates this contract REUSES verbatim (mirror, never fork): `statement_grounded` (`:135`), `raw_linked` (`:291`). **Consumed read-only.**
> - `scripts/publication.py` @ `04f1bc4` — `WEB_SAFE_FIELD_ALLOWLIST` (`:268`) / `WEB_UNSAFE_FIELDS` (`:345`) / `to_web_safe` (`:390`), the field SSOT. **Consumed read-only; `publication.py` stays 0-diff.**
> - `Docs/stage3-05-card-feed-contract.md`, `Docs/stage3-04-raw-preservation-contract.md`, `Docs/stage3-03-source-inventory-contract.md`, `Docs/stage3-alpine-timeline-card-mvp-spec.md`, `Docs/GOV-262-preservation-replay-evidence.md` — the accepted Stage 3 card-feed, preservation, inventory, MVP-spec, and reproducibility references this contract extends.

This document pins the **verify-at-source drill-down contract** for the live Stage 3 Alpine card surface:
*every served card either exposes a drill-down path that provably resolves from the card, through its statement, through
each ordered evidence link, to a real original source at the cited locator — or it is honestly labeled unverified/gapped,
never silently dropped and never claiming verify-at-source on a dangling locator.* It **describes and verifies**
resolvability over the **existing** surface. It authorizes **no** new web-safe field, **no** public projection, **no**
new crawl/source, **no** ingestion-scope change, **no** schema/migration change, and **no** `read_api.py` /
`publication.py` field change. See §6.

The **implementation** child (separate issue, blocked-by THIS doc) builds the read-time drill-down projection + auditor +
RED tests per §7, gated on a premium success-criteria block per §6.3. A contract defines the shape; it does not satisfy it.

---

## 0. What this child owns, and what it must not touch

**Owns (contract only):** the drill-path shape (§1), the per-link resolvability status (§2), the per-card
verify-at-source status (§3), the completeness/back-gap invariant (§4), the boundary + no-leak invariants the impl must
satisfy (§5), the NON-unlock + premium-gate + impl shape + RED test list (§6/§7), the declared gaps (§8), the risk/pass-up
gate (§9).

**Must NOT touch / re-derive (carry Stage 2/3 forward — do not contradict, do not weaken):**
- the `read_api` serving gates, evidence-drawer projection, and lane separation (GOV-146 / GOV-298 / GOV-306 / GOV-311 /
  GOV-347) — **reused, never forked**; `read_api.py` stays **0-diff**;
- the `stage2_traceability` per-row predicates (`statement_grounded` `:135`, `raw_linked` `:291`) — **called verbatim**,
  never re-implemented; the resolvability status is DERIVED from them, never a new stored flag;
- the `publication.WEB_SAFE_FIELD_ALLOWLIST` / `WEB_UNSAFE_FIELDS` SSOT — **consumed read-only**; `publication.py` stays
  **0-diff**; **NO new web-safe field is introduced** (§9 pass-up if one is needed);
- the `read_api.assert_no_raw_paths` transport guard + `RAW_PATH_MARKERS` — **reused as the backstop**;
- the `stage3_card_feed` projection and its back-gap guard (`assert_feed_covers_surface` `:358`) — **extended, never
  forked**; the new drill-down is an additive module/projection on top, mirroring how 3.05 sat on top of the read surface;
- the schema (`statements` / `evidence_links` / `sources` / `transcript_segments` / `transcripts` / `documents`) — the
  drill-down is **read-only** over it; no migration, no new column.

**Goal nuance (mirror of GOV-363 / GOV-346 / GOV-337):** at CTO non-author merge of **this doc**, **no goal flips to
achieved**. The Stage 3.07 subgoal `3069bd05` flips only when the **implementation** child merges (goal-flip-at-impl-merge).

---

## 1. Drill-path shape (per served card)

The live card surface (`stage3_card_feed.build_card_feed` `:303`) emits a `{scope, access, cards[]}` envelope. Each
**record card** (`_record_card` `:237`) already carries an ordered `evidence` drawer (rides verbatim from the read
surface, `:253`), where each drawer entry is one web-safe evidence link (`_web_safe_evidence` `:421` =
`to_web_safe` + `_strip_non_web_urls`). The verify-at-source drill path is the **already-present** three-node chain over
that surface — this contract names it, it introduces no new node:

```
card (handle)                          ← stage3_card_feed handle (c1_ + SHA-256[:40]); opaque, NOT a raw id
  └─ statement node                    ← card.type=statement; natural key statement_id (read-surface slug)
       └─ evidence[] (ordered)         ← read_api _evidence_links_for ORDER BY evidence_link_id (:154–:160)
            └─ original-source locator ← per-link web-safe citation locator (the drawer entry)
```

**Per-link original-source locator (the web-safe drawer entry) — already-allowlisted keys ONLY (publication.py SSOT):**

| Drawer key | Allowlist line | Meaning |
|---|---|---|
| `to_source_id` | publication.py `:321` | the **opaque public source-node id** (a slug, NOT a raw DB/internal path) — the citable source identity |
| `relation` | publication.py `:313` | the link's relation type (e.g. supports) |
| `locator_kind` | publication.py `:314` | the kind of locator (page / timestamp / section) |
| `timestamp_human` | publication.py `:315` | human-readable timestamp into the source |
| `timestamp_seconds` | publication.py `:316` | seconds offset into the source |
| `page` / `section` / `paragraph` | publication.py `:317`/`:318`/`:319` | page/section/paragraph locator into the source |
| `original_url` / `final_url` / `archive_url` / `url` | publication.py `:279`/`:320`/`:280`/`:278` | the public citable URL(s) — only `http(s)://` survives `_strip_non_web_urls` (`:404`); a `file://` vault URI is dropped |
| `scan_date` | publication.py `:285` | public as-of date of the cited source capture |
| `source_type` | publication.py `:274` | source classification |

> **Grounded correction (honesty over the issue text):** GOV-375 listed `to_source_id` among "stripped raw join keys."
> It is **not** stripped — it is **allowlisted** (publication.py `:321`) as the *opaque public source-node id*, and it
> rightly rides in the drawer as the citable source identity. The keys that **are** stripped (never cross the web-safe
> boundary) are `segment_id` / `deep_link` / `transcript_path` (all ∈ `WEB_UNSAFE_FIELDS` publication.py `:359–:361`).
> This contract pins the *real* boundary; the impl must not "re-add" `to_source_id` (already present) nor surface the
> stripped keys.

**Mirror, do not extend:** the drill path uses ONLY the keys above — exactly the existing evidence-drawer keys. This
contract introduces **NO new web-safe field**. If the impl finds it needs one to express the path, that is a §9 pass-up,
not a self-authorized add.

---

## 2. Per-link resolvability status (reviewer-internal, fail-closed, frozen SSOT)

Each evidence link in the drawer gets a derived **resolvability status** answering "does this citation locator actually
land on a real, preserved source row?" — a reviewer-internal envelope key, mirroring exactly how `provenance_status`
(`_provenance_status_for` `:354`) is derived and attached.

**Frozen SSOT vocabulary (a `frozenset`, mirror of `PROVENANCE_STATUS_VALUES` `:331`):**

```
RESOLVABILITY_RESOLVED   = "resolved"
RESOLVABILITY_UNRESOLVED = "unresolved"   # the global fail-closed DEFAULT
RESOLVABILITY_VALUES     = frozenset({"resolved", "unresolved"})
```

**Derivation (recomputed from CANONICAL columns, never from the web-safe card body, never a stored flag):**
A link is `resolved` **only if** its grounding unit resolves through the canonical chain — REUSING the GOV-306 predicates
verbatim (`stage2_traceability.statement_grounded` `:135` / `raw_linked` `:291`) and the live serving primitive
(`read_api._segment_resolves` `:163`):

1. the statement's `segment_id` resolves to a real `transcript_segments` row (`_segment_resolves` `:163`), **OR**
2. the link's `to_source_id` resolves to a real `sources` row (the `statement_grounded` evidence-link branch, `:157–:167`), **OR**
3. a preserved raw predecessor exists for the grounding unit (GOV-262 reproducibility — `raw_linked` `:291`: a hashed
   transcript `sha256` `:312`, or a source in `raw_preservation.PRESERVED_STATES` `:326`, or a hashed `documents` child `:328`).

**Fail-closed:** ANY break — no `segment_id`, a `segment_id` resolving to no row, a `to_source_id` resolving to no
`sources` row, an unpreserved raw, a dangling/orphan link — collapses to `unresolved`. Optimistic `resolved` is **never**
the default (GOV-230 §default). The returned value is always a member of the frozen `RESOLVABILITY_VALUES`.

**Boundary:** the derivation reads the CANONICAL `segment_id` / `to_source_id` from the DB row (the trace predicates
already re-fetch the canonical statement, `_canonical_statement`), **not** from the already-web-safe drawer (where
`segment_id` is stripped). The status is attached as an **envelope key AFTER** the web-safe projection — exactly like
`provenance_status` (`:458–:459`). It adds NO raw id, FS path, or PII to the served body (§5).

---

## 3. Per-card verify-at-source status (reviewer-internal, fail-closed, frozen SSOT)

Each served **record card** gets a derived **verify-at-source status** answering "can a reviewer verify this card at an
original source?" — composed from §2 plus the existing grounded-provenance signal.

**Frozen SSOT vocabulary:**

```
VERIFY_AT_SOURCE_VERIFIABLE = "verifiable"
VERIFY_AT_SOURCE_UNVERIFIED = "unverified"   # the fail-closed DEFAULT
VERIFY_AT_SOURCE_VALUES     = frozenset({"verifiable", "unverified"})
```

**Derivation (first-match, fail-closed):**
A card is `verifiable` **only if BOTH** hold:
1. **≥1 evidence link is `resolved`** (§2) — at least one citation provably lands on a real source; **AND**
2. **grounded provenance** — the card's existing `provenance_status` envelope key is `PROVENANCE_GROUNDED` (`:329`), i.e.
   the GOV-311 trust indicator already earned (`statement_grounded ∧ raw_linked ∧ _ai_provenance_ok` `:396–:400`).

Otherwise → `unverified` (DEFAULT). **No card claims verify-at-source on a dangling locator** (clause 1 forbids it), and
**no card claims it on ungrounded provenance** (clause 2 forbids it). An AI-presented card (`produced_by='ai'`) inherits
the stricter `_ai_provenance_ok` leg through `provenance_status`, so an AI row with a missing/failed run is `unverified`.

**No orphan, no fabricated source:** an evidence-less served record (no drawer) has zero `resolved` links → `unverified`
by construction; it is still emitted (§4), never dropped, never padded with a fabricated locator.

---

## 4. Completeness invariant (back-gap: never silently dropped)

Mirrors the GOV-322 back-gap rule and the live feed's own coverage guard (`assert_feed_covers_surface` `:358`,
`expected_handles` `:343`):

**Every served card is EITHER verify-at-source-capable (§3 `verifiable`) OR honestly labeled (`unverified`, or a
`source_missing` gap card via `_gap_card` `:272`). No served card is silently omitted from the drill-down projection.**

The impl's auditor must independently recompute the drill-down over `reviewer_internal_records` (`:509`) +
`completeness_gap_cards` (`:612`) and assert a 1:1 bijection with the feed's cards (extending, not forking,
`assert_feed_covers_surface`): a card present in the feed but missing a verify-at-source verdict → RED; a verdict
fabricated for a card the surface does not emit → RED. Gap cards are verify-at-source-N/A by construction (no statement,
no evidence) and are labeled `source_missing`, never `verifiable`.

---

## 5. Boundary + no-leak invariants (web-safe 0-diff, reviewer-internal only)

| # | Invariant | How the impl must satisfy it |
|---|---|---|
| **B-1** | **No new web-safe field.** | The drill-down reuses ONLY the §1 allowlisted drawer keys. Resolvability/verify-at-source statuses are **reviewer-internal envelope keys** attached AFTER `to_web_safe`, never added to `WEB_SAFE_FIELD_ALLOWLIST`. `publication.py` stays **0-diff**. |
| **B-2** | **`read_api.py` / `publication.py` 0-diff.** | The impl is a **separate additive module** (mirror of `stage3_card_feed.py`): it *calls* `read_api` / `stage2_traceability`, it does not edit them. Asserted by `git diff` showing 0 production lines changed in either. |
| **B-3** | **Raw join keys never cross.** | `segment_id` / `deep_link` / `transcript_path` ∈ `WEB_UNSAFE_FIELDS` (`:359–:361`) are read only as CANONICAL inputs to the resolvability derivation; they are NEVER placed in the projected body. |
| **B-4** | **Transport sweep is the backstop.** | The assembled drill-down body is passed through `read_api.assert_no_raw_paths` (`:125`): a `file://` / FS path / `.sha256` / raw-marker leak fails LOUDLY at the boundary (AC-3 mirror), not silently downstream. |
| **B-5** | **Reviewer-internal lane only.** | The whole drill-down runs at `access: reviewer_internal` (the MVP is behind the gated beta — GOV-336 §2.3). The public lane (`published_records` `:463`, `to_web_safe` public serialization) stays **byte-identical** — resolvability/verify-at-source are NEVER emitted on the public lane, exactly as `provenance_status` is reviewer-internal-only (`include_provenance_status=True` `:559`, never on `published_records`). |
| **B-6** | **No mutation, no AI, no network.** | Pure function of stored fields: same DB → byte-identical drill-down (idempotent re-projection). |

---

## 6. NON-unlock, goal discipline, and the premium-criteria gate

### 6.1 NON-unlock
This doc is **Docs-only, 0 production diff**. It does **not** flip the Stage 3.07 subgoal `3069bd05` to achieved, does not
unlock any later stage, and authorizes no implementation by itself.

### 6.2 Goal-link discipline (GOV-373)
This child links the **Stage 3.07 subgoal** `3069bd05-65c2-4b4d-81f5-0e02a6c883db`, **not** the HEAD goal. The subgoal
flips planned→achieved only at the **impl successor's** CTO non-author merge.

### 6.3 Premium-criteria gate (HARD precondition for the impl successor)
Docs-only/planning does **not** require a premium success-criteria block here. But the impl successor (the drill-down
projection/auditor child) **MUST NOT** start until a **premium success-criteria block** (GOV-38 framework) is applied to
its goal of record, **carrying the timeline/cards section research forward** (mirror of GOV-337 EX-6 / GOV-346 §5). This is
a named **hard precondition**: the impl child is blocked-by both (a) this contract and (b) the premium-criteria
application. CEO/CTO own that gate; the impl must not self-authorize past it.

---

## 7. Implementation successor — shape + RED test list (built by the blocked-by child, NOT here)

**Shape:** a **separate additive module** (e.g. `scripts/stage3_verify_at_source.py`), mirroring `stage3_card_feed.py`:
projects a reviewer-internal drill-down over `reviewer_internal_records` + `completeness_gap_cards`, attaches per-link
`resolvability_status` (§2) and per-card `verify_at_source_status` (§3) as envelope keys, sweeps the body with
`assert_no_raw_paths`, and exposes a back-gap auditor (§4). `read_api.py` / `publication.py` **0-diff**.

**RED tests the impl must ship (each must fail when the guard is neutered — load-bearing, mirror GOV-367/GOV-350):**
1. **R-1 resolvability resolves** — a link whose `to_source_id`/`segment_id` resolves → `resolved`; neuter the predicate call → test goes RED.
2. **R-2 resolvability fail-closed** — a dangling `to_source_id` (no `sources` row) and a `segment_id` resolving to no row → `unresolved`; a planted "default resolved" → RED.
3. **R-3 unpreserved raw** — a grounded link whose unit has no preserved raw (GOV-262) → `unresolved` (verify-at-source not claimed on an un-reproducible citation).
4. **R-4 verify-at-source requires BOTH legs** — a card with a resolved link but `provenance_status=unverified` → `unverified`; a card grounded but with zero resolved links → `unverified`.
5. **R-5 verifiable positive** — a card with ≥1 resolved link AND `PROVENANCE_GROUNDED` → `verifiable`.
6. **R-6 back-gap bijection** — planting a drop (a feed card absent from the drill-down) → RED; gap cards labeled `source_missing`, never `verifiable`.
7. **R-7 no-leak / 0-diff** — `assert_no_raw_paths` over the body passes with a planted raw locator on every link being stripped; `git diff` shows `read_api.py`/`publication.py` 0 production diff and the public lane byte-identical.

---

## 8. Declared gaps (honest, today)

- **No `partially_resolved` / per-locator granularity.** Resolvability is binary per link (`resolved`/`unresolved`) by
  the existing predicates; a "some locators resolve, some don't" middle state is **not surfaceable today** and is NOT
  fabricated. (Bounded — a future slice, not this contract.)
- **No `disputed` / `source_changed` verify-at-source edge.** Inherited from the 3.05 card-feed bound
  (`stage3_card_feed.py:118–120`): those statuses are not producible from the current surface; verify-at-source does not
  invent them.
- **Transcript-class confidence is NOT a resolvability signal.** `confidence_label` (GOV-283) is a separate
  reviewer-internal label; it does not promote a link to `resolved`.
- **`final_url`/`original_url` reachability is NOT live-checked.** Resolvability proves the locator lands on a real,
  preserved *source row* (DB + GOV-262 preservation), not that the public URL is currently HTTP-200. Live-fetch
  verification is out of Alpine-reviewer-internal scope and would be a §9 pass-up (network).
- **No public projection.** Resolvability/verify-at-source are reviewer-internal-only; the public lane stays 0/byte-identical.

---

## 9. Risk gate / pass-up trigger

**STOP and escalate to CTO/CEO (Isaac-gated) — do not self-authorize — if the impl grounding shows the verify-at-source
path needs ANY of:**
- a **new web-safe field** (any addition to `WEB_SAFE_FIELD_ALLOWLIST`);
- a **public projection** of resolvability/verify-at-source (anything beyond the reviewer-internal lane);
- a **live network fetch** of a source URL (reachability check);
- a **non-Alpine source**, a **new crawl/source**, an **ingestion-scope change**, or a **schema/migration change**.

Otherwise the impl is a pure additive reviewer-internal projection within the existing boundary.

---

## 10. Review lane (this child)

Impl (this doc, Docs-only) → **VSR** (leg-1) → **SecPriv** (leg-2: no-public-leak / boundary-by-construction) → **CTO
non-author squash merge**. Both legs PASS before merge. Each in-review node stays owned when chaining (avoid the
GOV-340/341 liveness trip).

**Evidence required at done:** file path + diff stat (Docs-only, 0 prod diff), line-ref grounding (this §0 input list),
VSR + SecPriv PASS comments, PR URL, CTO merge SHA.
