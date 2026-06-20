# Stage 3.05 — Card Read-Contract Design + Premium Success-Criteria Block

> **Issue:** GOV-346 (Stage 3.05 · Plan · CTO→BackendCrawlerEngineer). **Parent:** GOV-345.
> **Stage:** 3.05 — `plan→impl` **planning child only**. **NON-implementation. NON-unlock.**
> **Scope:** Town of Alpine only · reviewer-internal · no public launch.
> **Grounded on:** canonical remote `origin/main` HEAD `17275eb` (GOV-337 / PR #61). Stage 3 = 2/15.
> **Goal:** Stage 3.05 `ff16da3a-a8d9-4b1b-b03f-75e053e9bdd9` (premium block in §5 is PATCH-ready; goal stays OPEN — see §0).
> **Inputs of record:**
> - `Docs/stage3-alpine-timeline-card-mvp-spec.md` (GOV-336) — issue map (§1), card↔`read_api` data contract (§2), the §2.2 row mapping, the §2.4 read-surface gap, premium gate (§5), non-unlock (§7).
> - `Docs/stage3-acceptance-criteria-exit-gate.md` (GOV-337) — AC-1…AC-5, the §3 adversarial matrix, the §4 exit gate (EX-1…EX-7), premium gate (§6), non-unlock (§7).
> - `Docs/stage2-reviewer-internal-read-surface-reference.md` (GOV-326) — field source of record for the 5 overlays + gap lane.
> - `scripts/read_api.py`, `scripts/publication.py` at HEAD `17275eb` — the live read surface this contract is grounded against (line refs below).
> - `…/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md` — the framework §5 fills.

This document closes the **two design gaps GOV-336 §2.2 explicitly left to 3.05** — a stable web-safe card
**handle** (§1) and a derived card **status vocabulary** (§2) — and pins the exact **card feed JSON shape**
(§3) the GOV-347 implementation child builds and the GOV-337 AC-1…AC-5 assertions run against. §4 records the
verification/review lane; §5 is the paste-in **premium success-criteria block** for goal `ff16da3a`; §6 is
the explicit non-unlock statement.

It authorizes **no** production code, **no** feed build, **no** implementation child, **no** scope/launch/
budget unlock. See §6.

---

## 0. What this child owns, and the goal-flip nuance

GOV-336 §2.2 named three card-identity / status items as **gaps that 3.05 itself owns**:

- *“`id` (card identity) → gap — no raw id is surfaced … 3.05 must define a **stable web-safe card handle**
  (opaque/derived), not a raw DB id.”* → **§1 of this doc.**
- *“`status` … must be **derived** from `ui_status` + `provenance_status` + `confidence_label` (+ gap lane
  for `source_missing`), never invented; mapping table owned by 3.05/3.06.”* → **§2 of this doc.**
- The exact feed envelope the 3.06 frontend consumes and the AC-1…AC-5 run against. → **§3 of this doc.**

Closing these unblocks the **GOV-347** Stage 3.05 *implementation* child (blocked-by THIS issue) and, after
it, the visible **GOV-348** Stage 3.06 frontend timeline.

**Goal-flip nuance (mirror of GOV-337 leaving the Stage 3 parent open).** At CTO non-author merge of this
doc, the CTO applies the §5 premium block to goal `ff16da3a` and records EX-6 evidence, **but leaves
`ff16da3a` OPEN**. The 3.05 goal flips to *achieved* only when the **implementation** child (GOV-347) merges.
A planning doc defines the contract; it does not satisfy the implementation goal.

---

## 1. Stable web-safe card handle scheme

### 1.1 Requirement (from GOV-336 §2.2 / §3 "Corrected")

A card needs a stable identity for the frontend (drawer open, scroll-to, list keys, AC evidence references).
The read surface **strips internal ids by policy** and the handle must therefore be **(a)** deterministic,
**(b)** collision-resistant, **(c)** derived only from already-web-safe fields, and **(d)** never a raw DB
id and never a vector by which a raw id / internal key leaks.

### 1.2 What is already web-safe (the only legal inputs)

The only fields the handle may consume are fields that have **already crossed both web-safe layers**
(`publication.to_web_safe` field-allowlist at `read_api.py:452` + the `assert_no_raw_paths` transport sweep
at `read_api.py:935`). From `publication.WEB_SAFE_FIELD_ALLOWLIST` (`scripts/publication.py:268`), the
stable natural keys available per card kind are:

| Card kind | Web-safe natural key (allowlisted) | Notes |
|---|---|---|
| statement / info / decision / AI-presented card | `statement_id` | allowlisted ("statement … web-safe subset", `publication.py:299`) |
| meeting card | `meeting_id` | allowlisted ("agenda_item / meeting grouping … slugs + ordinal, no paths") |
| source(-document) card | `source_id` | allowlisted ("identity / classification (presentation-safe)") |
| `source_missing` gap card | `gap_id` | gap-card field set `GAP_CARD_FIELDS` (`read_api.py:579`); raw `source_id`/`detected_run_id`/`detected_utc` are **never SELECTed** (`read_api.py:650–656`) |

### 1.3 Derivation

```
handle = "c1_" + lowerhex( SHA256( utf8( card_type + "\x1f" + natural_key ) ) )[:40]
```

- `card_type` is the resolved card type string (§3.2), e.g. `statement`, `meeting`, `source_missing`.
- `natural_key` is the single web-safe natural key for that kind (table in §1.2).
- `\x1f` (ASCII Unit Separator) is an unambiguous, type-namespacing delimiter (it cannot appear inside a
  slug id), so `(type=meeting, key="a-b")` and `(type=meetin, key="ga-b")` cannot alias.
- `[:40]` keeps **160 bits** of SHA-256 → 40 lowercase-hex chars. URL-safe, fixed-length, no padding.
- `c1_` is a scheme-version prefix (`c` = card, `1` = v1) so the scheme can evolve without ambiguity.
- **Deterministic by construction:** a pure function of two already-web-safe inputs — **no** timestamp, **no**
  randomness, **no** DB rowid, **no** ordering dependence. The same record always yields the same handle on
  every feed build, satisfying AC evidence stability and frontend list-key stability.

### 1.4 Uniqueness / collision argument

- **Distinct records ⇒ distinct preimage.** Within a card kind, `natural_key` is unique (it is the
  record's primary slug). Across kinds, the `card_type + "\x1f"` prefix disambiguates. Therefore two
  distinct cards always have distinct SHA-256 preimages.
- **Distinct preimage ⇒ distinct digest** except for a cryptographic SHA-256 collision. At a 160-bit
  truncation the birthday bound is ≈ `2^80` cards before a 50%-probability collision; Alpine-scale is on the
  order of `10^2`–`10^4` cards, so the collision probability is negligible (< `2^-130`). No application-level
  collision is reachable at this scale.

### 1.5 Negative cases (must hold; the impl child proves them under test)

- **NC-1 — no collision between two distinct records.** `statement_id = "alpine-2025-03-04-stmt-007"` and
  `statement_id = "alpine-2025-03-04-stmt-008"` both as `statement` cards → different preimage byte strings
  → different 160-bit digests → different handles. A test enumerates the whole fixture feed and asserts
  `len(handles) == len(set(handles))` (zero duplicates).
- **NC-2 — no raw-id / internal-key leak through the handle.** The handle is a one-way SHA-256 hex digest:
  - its **inputs** are only fields already past both web-safe layers (no rowid, no FS path, no vault ref,
    no `.sha256`, no internal-only column is ever an input), and
  - SHA-256 is **preimage-resistant**, so even the (already web-safe) natural key cannot be recovered from
    the handle. Hashing even an allowlisted `statement_id` (rather than passing it verbatim) is **defense in
    depth**: the card layer commits to opacity and stability independent of any future allowlist change.
  - A test greps the serialized handle for `/`, `file://`, `.sha256`, `\\`, and any digit-run that matches a
    raw rowid pattern → **zero hits** (a 40-char hex digest contains none of these by shape).

---

## 2. Status-vocab composition table (derived, fail-closed, no invented status)

### 2.1 The hard grounding fact (read it before the table)

The master-plan card status vocab is
`{verified, unverified, ai_presented, disputed, corrected, source_changed, source_missing}`. It must be
**derived** from existing read keys — `ui_status` + `provenance_status` + `confidence_label` + the gap lane —
never invented (GOV-336 §2.2; §2.4 bounds `disputed`/`corrected`).

**Critical constraint discovered in the live read surface:** *both* record lanes drop any record whose
re-derived `ui_status` is not publication-eligible — `published_records` at `read_api.py:475` and
`reviewer_internal_records` at `read_api.py:553` both `continue` when
`ui_status not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES`. That allowlist is exactly
`{"source-backed", "archived-source-backed", "corrected"}` (`publication.py:73`). The full
`compute_ui_status` vocabulary (`publication.py:112–143`) is the 10-state set
`{do-not-publish, disputed, source-missing, source-changed, corrected, needs-clarification, unverified,
pending-review, archived-source-backed, source-backed}`, but **only three of those ten ever reach a served
record.**

Consequence: a normal served card carries `ui_status ∈ {source-backed, archived-source-backed, corrected}`
**only**. So:

- `source_missing` is reachable **only** via the gap lane (`completeness_gaps`), never as a normal card —
  exactly why the gap lane exists.
- `disputed` and `source_changed` are **not surfaceable from a served record today** (their `ui_status`
  values are filtered out, and no dispute/correction *edge* is emitted — GOV-336 §2.4). They are honest,
  bounded gaps to flag forward to 3.07, **not** statuses to fabricate.

This is the company's honesty posture (BACKEND_CRAWLER_WORKFLOWS Isaac directive: "show visible gaps … do
not pretend the backfill is complete"). The table below maps **only what the surface can actually produce**.

### 2.2 Composition table

Resolution is **first-match, top-down** (a single status string per card, matching the master vocab).
Fail-closed default = gated `unverified`.

| Precedence | Output status | Surfaceable today? | Exact predicate (named read keys) | Source / lane | Fail-closed rule |
|---|---|---|---|---|---|
| 1 | `source_missing` | **yes (gap lane only)** | card originates in the **gap lane**: a `completeness_gap_cards` row (`read_api.py:612`) with `gap_type` `no_primary_source`. Never a normal served record (`ui_status` `source-missing` is filtered at `:475`/`:553`). | `completeness_gaps` envelope | gap row is **always emitted**, never padded into normal cards; `gap_type`/`severity`/`resolved_status` floor to `unknown`/`warn`/`open` (`read_api.py:572–574`). |
| 2 | `corrected` | **yes** | served record AND `ui_status == "corrected"` (`compute_ui_status` rule 5, **reviewed-guarded**: requires a reviewed verification status AND `correction_status == "corrected"`, `publication.py:141`). | both lanes | only the computed, reviewed-corrected render state; **no fabricated correction edge** (GOV-336 §2.4). |
| 3 | `ai_presented` | **yes** | served record AND `produced_by == "ai"` (allowlisted, `publication.py:290`; bound at write time by GOV-278). Trust *within* the AI card is the `provenance_status` split (rows 5–6), so an AI card is `ai_presented` **and** carries `grounded`/`unverified`. | both lanes (the `produced_by` flag); `provenance_status` reviewer-internal only | absent/unknown `produced_by` ⇒ **not** flagged AI (it does not upgrade trust); provenance still gates. This uses an **already-emitted allowlisted key**, not a new gate — consistent with §2.4 ("no `ai_presented` gate beyond `provenance_status`"): the *flag* is `produced_by`; the *trust* stays `provenance_status`. |
| 4 | `verified` | **yes** | served record AND `provenance_status == "grounded"` (`read_api.py` envelope key, GOV-311; frozen `PROVENANCE_STATUS_VALUES`, `read_api.py:331`) AND `ui_status ∈ {source-backed, archived-source-backed}`. `confidence_label` is shown alongside but does not gate the status (it refines, floor `auto_caption_untimed`). | reviewer-internal lane (`provenance_status` is attached only under `include_provenance_status=True`, `read_api.py:559`) | requires an **explicit** `grounded`; anything else is not `verified`. |
| 5 | `unverified` | **yes (default)** | served record AND (`provenance_status == "unverified"` OR `provenance_status` absent/unknown). | reviewer-internal lane; **also the global fail-closed default** | **DEFAULT.** Any record whose status cannot be resolved to a higher row collapses here (gated `unverified`), never to a reassuring state. |
| — | `disputed` | **no (today)** | would require `ui_status == "disputed"` (filtered, not publication-eligible) AND/OR a dispute *relationship edge* (not emitted — GOV-336 §2.4). | n/a (filtered + §2.4 gap) | bounded gap → **3.07**; never fabricate a dispute. A card asserting a dispute not derivable from a served key fails review (GOV-337 AC-4 "fabricated correction edge"). |
| — | `source_changed` | **no (today)** | would require `ui_status == "source-changed"` (filtered, not publication-eligible). A record entering that state **drops out of the served set** (degraded ⇒ absent), caught by the back-gap auditor (3.13 / GOV-322), surfaced as a gap — **never** left as a stale "verified" card. | n/a (filtered) | bounded gap → flag forward; honest absence, not a silent stale card. |

### 2.3 What this gives AC-4

GOV-337 AC-4 ("AI / unverified / disputed / corrected gated-block pattern exists, even with limited data")
is satisfied by the **producible** statuses: `ai_presented` (`produced_by="ai"`) + `unverified`
(`provenance_status` fail-closed floor) + `corrected` (`ui_status="corrected"`). `disputed` is the explicit
§2.4 deferral (await 3.07) — exactly as AC-4's scope note already states. No status is fabricated to fill the
pattern.

---

## 3. Card feed JSON shape

### 3.1 Boundary rules (GOV-336 §2.1, restated as feed invariants)

The feed is built **only** on `read_api.reviewer_internal_records(conn)` (`read_api.py:509`) and
`read_api.completeness_gap_cards(conn)` (`read_api.py:612`). The card layer:

- **never** calls `to_web_safe` / `publication.py` (the read surface already crossed both web-safe layers);
- **never** issues a new raw query and **never** re-derives a field the read surface dropped;
- carries **every** field straight from the read-surface envelope keys (each maps to a GOV-336 §2.2 row);
- runs entirely at `access: reviewer_internal` (the whole MVP is behind the gated beta — GOV-336 §2.3).

Because the inputs are already-web-safe, the feed cannot cross the public boundary **by construction**: if a
forbidden value ever appears in feed output, it is a leak upstream of the card layer (a reportable defect),
not an expected state.

### 3.2 Card `type` resolution (bounded to the master concept set)

`type ∈ {meeting, info, decision, statement, source, correction, ai_presented, source_missing}`
(GOV-336 §0). Resolution, fail-closed:

- gap-lane card ⇒ `source_missing` (§2.2 row 1).
- served record with `produced_by == "ai"` ⇒ `ai_presented` (the AI flag dominates the kind label).
- served record with `ui_status == "corrected"` ⇒ `correction`.
- otherwise the record's structural kind from already-allowlisted fields (`statement_id`+`statement_text` ⇒
  `statement`; `meeting_id`/agenda grouping ⇒ `meeting`; `source_id`-anchored source row ⇒ `source`;
  decision/vote layer ⇒ `decision`; else ⇒ `info`).
- unknown/unresolved ⇒ `info` (neutral, non-asserting) — never a stronger type than the evidence supports.

### 3.3 Envelope + per-card field map (every field ↔ a GOV-336 §2.2 row)

```jsonc
{
  "scope": "alpine",                 // read_api envelope (read_api.py:924) — §2.2 jurisdiction row
  "access": "reviewer_internal",     // read_api envelope — entire MVP is gated-beta (§2.3)
  "cards": [
    // --- normal (record-backed) card -------------------------------------
    {
      "handle": "c1_3f9a…<40 hex>",  // §1 — derived; §2.2 "id (card identity)" gap, now closed
      "type": "statement",           // §3.2 — bounded to master concept set
      "title": "Town Council — March 4 2025 …",   // §2.2 title row → allowlisted `title`
      "date": "2025-03-04",          // §2.2 date row → allowlisted timing field the record carries
                                     //   (e.g. `scan_date` / `first_seen_date`); feed never invents a date
      "jurisdiction": "alpine",      // §2.2 jurisdiction row → envelope scope (fixed; broader = planned)
      "reviewed_summary": "…",       // §2.2 "reviewed summary" → `statement_text` (REVIEWER-INTERNAL only, §2.3)
      "status": "verified",          // §2 composition table (single first-match value)
      "confidence_label": "source_anchored_timed",  // §2.2 → envelope `confidence_label` (GOV-283/290)
      "speaker_label": "Jane Doe, Council Member",   // §2.2 → envelope `speaker_label` (GOV-290; floor "Meeting Attendee")
      "provenance_status": "grounded",               // §2.2 → envelope `provenance_status` (GOV-311; REVIEWER-INTERNAL only; floor "unverified")
      "evidence": [                  // §2.2 source-links / source-drawer row → envelope `evidence`
        { "relation": "primary_source", "final_url": "https://…", "locator_kind": "video_timestamp",
          "timestamp_human": "01:12:30", "timestamp_seconds": 4350, "page": null, "section": null }
      ]
    },

    // --- gap card (source_missing); reduced shape, no statement fields ----
    {
      "handle": "c1_a17c…<40 hex>",  // §1 — derived from gap_id
      "type": "source_missing",      // §2.2 completeness row; §2 row 1
      "jurisdiction": "alpine",
      "status": "source_missing",
      "gap_type": "no_primary_source",   // GAP_CARD_FIELDS (read_api.py:579)
      "severity": "warn",                // floor (read_api.py:572–574)
      "resolved_status": "open",
      "detail": "Meeting recorded; primary source not yet located."  // optional; omitted if it trips the PII/raw guard (read_api.py:590–609)
    }
  ]
}
```

**Field → §2.2 row coverage (every card field is grounded):**

| Feed field | GOV-336 §2.2 row | read-surface origin |
|---|---|---|
| `handle` | "id (card identity)" gap | §1 (derived; closes the gap) |
| `type` | (card kind; §0 concept set) | §3.2 (derived from allowlisted kind + `produced_by` + `ui_status` + gap lane) |
| `title` | `title` | allowlisted base field (`publication.py:302`) |
| `date` | `date` | allowlisted timing field (`scan_date`/`first_seen_date`/…); never invented |
| `jurisdiction` | `jurisdiction` | envelope `scope:"alpine"` (`read_api.py:924`) |
| `reviewed_summary` | `reviewed summary` | `statement_text` (reviewer-internal, §2.3) |
| `status` | `status` (composed) | §2 table |
| `confidence_label` | `confidence_label` | envelope key (GOV-283/290) |
| `speaker_label` | `speaker_label` | envelope key (GOV-290) |
| `provenance_status` | `provenance_status` | envelope key (GOV-311; reviewer-internal only) |
| `evidence[]` | source links / source drawer | envelope `evidence` (web-safe drawer) |
| `gap_type`/`severity`/`resolved_status`/`detail?` | `source_missing` cards / completeness | `completeness_gap_cards` (GOV-298) |

No feed field originates anywhere other than a named `read_api` envelope key or a value derived **here** in §1
/ §2 / §3.2 from already-web-safe keys. The feed makes **no new raw query**.

---

## 4. Verification & review

**Verification evidence (this doc).** See the closing PR: file path, `wc -l`, `git diff --stat` proving a
**Docs-only** addition (0 production-code diff), and section greps proving (a) the §1 handle scheme
(deterministic + raw-id-free + negative cases), (b) the §2 status-vocab table (every status from a named
read-key predicate; fail-closed default; `disputed`/`corrected` bounded to §2.4), (c) the §3 feed shape
consuming only `read_api.reviewer_internal_records` + `read_api.completeness_gap_cards`, and (d) the §5
premium block with ≥4 comparable source links.

**Review lane.** `Impl(Plan)` → **VSR** (VerificationSafetyReviewer leg) → **SecPriv**
(SecurityPrivacyAgent leg) → **CTO non-author merge**. The two reviewer legs are created as `todo` child
issues of GOV-346. At merge the CTO applies §5 to goal `ff16da3a`, records **EX-6** evidence (GOV-337 §4),
and **leaves `ff16da3a` OPEN** (flips only at the GOV-347 implementation merge — §0).

**Pass-up trigger.** Any discovered need for public launch, legal/privacy/publication judgment,
budget/donation, official-contact automation, or scope beyond Alpine → STOP, comment, escalate to CEO/Isaac.

---

## 5. Premium success-criteria block (paste-in, ready to PATCH onto goal `ff16da3a`)

Filled against `2026-06-06-Premium-Success-Criteria-Framework.md`. Dimensions **already satisfied by
GOV-337** are *confirmed, not redone* (success/failure → AC-1…AC-5 + the §4 exit gate; source/auditability →
AC-3 + §2.2; verification artifacts → §4 EX lines). The dimensions GOV-337 did **not** cover —
comparable-product research, resident-comprehension/ease-of-use, pros/cons/tradeoffs, plan-before-impl,
text-only interaction sketch, safety check — are filled here.

```markdown
## GOV Premium Success Criteria — Stage 3.05 (card read-contract / feed)

Stage: 3.05 — backend card read-contract / feed (plan→impl; this is the plan)
Scope: Town of Alpine only · reviewer-internal · no public launch
Project/repo: backend xXKillerNoobYT/Government-watchdog (0a1832c4-1556-49a1-bcc5-857f2ca72962)
Owner role: BackendCrawlerEngineer
Reviewer path: Impl(Plan) → VSR leg → SecPriv leg → CTO non-author merge
Blockers / unlock rule: GOV-347 (impl) is blocked-by GOV-346 (this doc) AND blocked until this premium
  block is applied to goal ff16da3a with recorded evidence (GOV-337 EX-6). Goal ff16da3a stays OPEN at
  this doc's merge; it flips to achieved only at GOV-347 merge.

### Success Definition
- Success means: a deterministic JSON card feed, built ONLY on read_api.reviewer_internal_records +
  read_api.completeness_gap_cards, that returns ≥5 sourced Alpine cards each with a stable web-safe handle,
  a derived status, and a non-empty evidence drawer — satisfying GOV-337 AC-1…AC-5.
- Evidence proving success (CONFIRMED via GOV-337 §4 EX-1…EX-7; not redone here): feed JSON path +
  assertion output (EX-1); timestamped evidence URL (EX-2); zero-hit leak scan over feed output (EX-3);
  fixture cards per gated kind + lane-gating proof (EX-4); backend pytest exit-0 + traceability/back-gap
  guards (EX-5).

### Failure Definition
- Failure looks like: a card with an empty/orphan drawer; a card padded in to reach 5; a fabricated
  dispute/correction edge (§2.4); provenance/statement free-text rendered in a public lane; a raw id /
  FS path / file:// / .sha256 in any handle or field; a status reading "verified" on absent provenance
  (fail-open); the feed silently dropping a record/gap the read surface emits.
- Stop/escalation trigger: any need for public launch, legal/privacy/publication judgment, budget,
  official-contact, or non-Alpine scope → STOP, comment, escalate to CEO/Isaac.

### Workability
- Real user/operator workflow: the internal reviewer (and Isaac-as-designer) opens the reviewer-internal
  Alpine timeline behind the gated beta; the feed is its sole data source.
- Inputs: read_api.reviewer_internal_records(conn) + read_api.completeness_gap_cards(conn). No new raw query.
- Outputs: the §3 cards[] envelope.
- Missing/stale/disputed/corrected/unverified source behavior: missing → gap-lane source_missing card;
  corrected → ui_status=corrected card; unverified → fail-closed gated unverified; disputed/source_changed →
  honest bounded gap (not surfaceable today; flag forward to 3.07 / back-gap auditor), never fabricated.
- Resume/retry behavior: the feed is a pure, deterministic re-projection of the read surface — re-running it
  is idempotent; a record/gap that re-appears in read_api re-appears identically in the feed (same handle).

### Ease of Use
- Resident/Isaac comprehension target (30s): a reader sees a chronological Alpine timeline of typed cards;
  each card shows what it is (type + plain label), how trustworthy it is (status + confidence), who spoke
  (speaker_label or a safe generic), and a one-click source drawer. AI / unverified / corrected content is
  visually gated and distinct from plain verified content. Gaps ("source missing") are shown, not hidden.
- Labels/statuses/gaps visible: status vocab (§2) is plain-language; confidence_label and provenance_status
  render as badges; source_missing gap cards are first-class.
- Required screenshot/prototype/wireframe/review note: §5.x text-only interaction sketch below; UI
  screenshots at desktop/tablet/mobile are produced by the GOV-348 frontend child (GOV-337 viewport floor).

### Comparable Research
- Comparable tools reviewed:
  - DocumentCloud — organizes primary-source DOCUMENTS with annotations, OCR, and a public viewer.
    USE: per-document source drawer + page/section locators; verbatim primary source over paraphrase.
    AVOID: a document-centric model that buries the civic *event/timeline*; GOV's unit is the meeting/
    statement event, not the file. FIT: partial — adopt its source-trail rigor, not its doc-as-root model.
  - GovTrack — tracks federal BILLS/votes/members with status badges and plain-language explainers.
    USE: status badges + plain-language summaries that don't overclaim. AVOID: a federal-legislative data
    model (bills/sponsors/roll-calls) that doesn't fit a small town with no structured bill feed. FIT: low
    on data model, high on the "explain trust state in plain language" pattern.
  - Open States — structured STATE jurisdiction/bill/vote/legislator data via an API.
    USE: clean typed-entity separation (jurisdiction/body/person/action) — validates GOV's concept-map
    separation (framework §9). AVOID: assuming a structured upstream feed exists; Alpine has none, so GOV
    must crawl + preserve + label gaps. FIT: model-shape yes, data-availability no.
  - Granicus / govMeetings — government MEETING agendas, minutes, video, public portals.
    USE: the closest analog — meeting-centric, agenda items, timestamped video. Validates AC-2's meeting-
    timestamp link and the evidence drawer's video_timestamp locator. AVOID: a vendor portal that presents
    official content as authoritative-by-default with no independent verification/gap labeling. FIT: high
    on shape; GOV adds the trust/gap/verification layer Granicus does not.
- Lessons GOV should use: per-claim source drawer (DocumentCloud); plain-language trust badges (GovTrack);
  typed-entity/concept separation (Open States); meeting + timestamped-video model (Granicus).
- Patterns GOV should avoid: document-as-root (DocumentCloud); federal-bill data model (GovTrack);
  assuming a structured upstream feed (Open States); authoritative-by-default with no gap labeling (Granicus).
- Source links:
  - https://www.documentcloud.org/
  - https://substack.govtrack.us/about
  - https://docs.openstates.org/api-v3/
  - https://granicus.com/solution/govmeetings

### Tradeoffs
- Main tradeoffs: speed vs source completeness; simple flat cards vs concept-map integrity; AI summary vs
  human verification; private progress dashboard vs public-launch risk; raw preservation vs public boundary;
  local Alpine clarity vs premature Wyoming/US generalization.
- Chosen approach and reason: derive cards ENTIRELY from the already-audited Stage 2 read surface (no new
  data-exposure layer); fail closed on every unknown; show gaps honestly rather than padding; keep the
  status vocab strictly derived (no fabricated dispute/correction edges — those wait for 3.07). This favors
  source completeness, concept integrity, human-verification, and the private-only boundary over speed and
  surface breadth — matching the month-end "good, visible, honest Alpine view" target.

### Plan Before Implementation
- Concept/data model: §1 handle, §2 status vocab, §3 feed shape. Card-model fields name: source-trail
  (`evidence[]`), status (`status` + `confidence_label` + `provenance_status`), correction-state
  (`status=corrected` via ui_status, bounded §2.4), timeline-position (`date`), concept-links (typed
  `evidence.relation` / `to_source_id`; richer edges deferred to 3.07), AI-review-state (`provenance_status`
  + `produced_by`).
- UI/operator behavior: §5 interaction sketch; frontend is GOV-348 (3.06).
- Verification commands or review steps: feed→assert ≥5 sourced cards (GOV-337 AC-1); leak grep zero-hit
  (AC-3); handle-uniqueness assert (NC-1); handle-no-leak grep (NC-2); pytest exit-0 + traceability (3.12 /
  GOV-306/318) + back-gap (3.13 / GOV-322) guards (AC-5).
- Artifact paths: this doc; the GOV-347 feed module under scripts/ + tests under tests/ (impl child).
- Failure handling: fail-closed default unverified; orphan never served; gap never hidden; leak ⇒ loud
  failure at the transport sweep; silent drop ⇒ back-gap guard RED.
- Review lane / pass-up: Impl(Plan) → VSR → SecPriv → CTO non-author merge; pass-up trigger as above.

### Source and Auditability
- (CONFIRMED via GOV-337 AC-3 + GOV-336 §2.2.) Required source fields ride in the `evidence` drawer
  (public http(s):// URL, locator kind, timestamp, page/section); scan date + jurisdiction from allowlisted
  fields. Local raw/source-data paths and Wayback refs never cross to the card (dropped by construction).
  Verification status = the `status` vocab (§2); correction state bounded to §2.4. No orphan claims.

### Timeline and Concept Integrity
- Known-then vs later-outcome: the feed is a point-in-time projection; later outcomes link forward via typed
  edges (3.07), never by rewriting a card. Correction handling: `status=corrected` works forward from the
  reviewed correction (ui_status rule 5); no fabricated edge. Concept records kept separate (framework §9):
  cards are presentation nodes over the read surface, not the source of truth. Required typed relationships:
  the `evidence.relation` / `to_source_id` edges the read surface already emits; correction/dispute
  relationship edges are 3.07.

### Acceptance Evidence
- Required artifacts: feed JSON sample; this contract doc; impl module + tests (GOV-347).
- Required tests/checks: AC-1…AC-5 (GOV-337); NC-1 (handle uniqueness); NC-2 (handle no-leak);
  traceability (3.12) + back-gap (3.13) guards.
- Required issue/PR/screenshot/API/source evidence: GOV-347 PR + backend pytest output; GOV-348 timeline
  screenshots at desktop/tablet/mobile; the §4 EX-1…EX-7 evidence at the Stage 3 exit gate.
```

### 5.x Text-only interaction sketch (framework Timeline/Card req #3)

```
[ Alpine timeline — reviewer-internal beta ]   Filter: Wyoming ▸ Lincoln County ▸ Alpine ●  (broader = planned)

 2025-03-04  ┌───────────────────────────────────────────────┐
             │ 🏛  MEETING · Town Council                     │  status: verified ✓   confidence: source_anchored_timed
             │ "March 4 2025 regular session"                 │  [ open source drawer ▸ ]
             └───────────────────────────────────────────────┘
                 └ drawer ▸ primary source https://…  · video 01:12:30 · scan 2025-03-05

 2025-03-04  ┌───────────────────────────────────────────────┐
             │ 💬  STATEMENT · Jane Doe, Council Member        │  status: ai_presented · unverified ⚠ (gated block)
             │ "…reviewed summary (reviewer-internal)…"        │  confidence: auto_caption_untimed
             └───────────────────────────────────────────────┘   ← visually distinct gated block (icon + label + hover)

 (gap)       ┌───────────────────────────────────────────────┐
             │ 🔍  SOURCE MISSING · meeting known, no primary  │  severity: warn · open
             │ source located yet                              │   ← shown, not hidden
             └───────────────────────────────────────────────┘
```

### 5.y Safety check — what a card must NOT imply without evidence (framework Timeline/Card req #5)

- Must not imply **verified** without `provenance_status == grounded` (fail-closed → gated `unverified`).
- Must not imply a **named speaker** unless `speaker_label` is a vetted on-record official (else generic
  floor; a poisoned name is never read — GOV-290).
- Must not imply a **precise timestamp/claim strength** above its `confidence_label` (floor
  `auto_caption_untimed`).
- Must not imply a **correction or dispute relationship** not derivable from `ui_status`/`provenance_status`
  (no fabricated edge — §2.4; await 3.07).
- Must not surface any **PII / raw path / internal id / file:// / .sha256 / vault ref** — dropped by
  construction; any appearance is a reportable defect, not a state.
- Must not present an **empty/orphan drawer** as a real card (orphans are never served).

---

## 6. Explicit non-unlock statement

This document is **planning/spec only**. It does **NOT**:

- authorize any Stage 3 implementation, code, migration, crawler run, or card-feed build;
- create or activate any Stage 3 implementation child (it only designs the contract GOV-347 will build);
- flip goal `ff16da3a` to achieved (the premium block is applied at merge but the goal stays OPEN — §0);
- unlock public launch, public newsletter send, or any public-facing surface;
- unlock non-Alpine expansion (Star Valley / Lincoln County / Wyoming / US);
- approve budget, donations, paid services, or official-contact automation;
- cross or weaken the public-projection boundary (`to_web_safe` / `publication.py`);
- override any Stage 0 safety/governance gate or any Stage 2 accepted artifact.

Stage 3 implementation remains gated behind: (a) this contract merged, (b) the §5 premium block applied to
goal `ff16da3a` with recorded evidence (GOV-337 EX-6), and (c) CTO/CEO sequencing with real Paperclip
blocker links. **Pass-up trigger:** any discovered need for public launch, legal/privacy/publication
judgment, budget/donation, official-contact, or scope beyond Alpine → STOP, comment, escalate to CEO/Isaac.
Alpine-first, reviewer-internal only, until an owner decision says otherwise.
