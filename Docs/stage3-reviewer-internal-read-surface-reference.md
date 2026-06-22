# Stage 3 Reviewer-Internal Read-Surface Reference

> **Audience:** the first internal reviewer(s) — Isaac first — reading the Alpine timeline behind the gated beta.
> **Scope:** Alpine-only. Reviewer-internal surface only — **not** a public or editorial artifact.
> **Status:** Stage 3.14 (Documentation maintenance / reviewer reference). Source-grounded against the
> merged `scripts/read_api.py`, `scripts/stage3_card_feed.py`, and `scripts/stage3_verify_at_source.py`
> at `origin/main` (`be22431`). Function names and the few line references below point at those files.
> **Drift guard:** `tests/test_stage3_doc_surface_sync.py` fails if this doc and the live code disagree
> about which derived keys each served envelope emits and in which lane. Keep them in sync — do not
> hand-edit the three machine-readable contract blocks at the bottom without re-running that test.
>
> **Obsidian note:** this reference is the Obsidian-vault-syncable reviewer artifact for the full Stage-3
> surface; the drift guard keeps it truthful so prose cannot quietly rot away from the code.

This page explains, for a reviewer, **the full Stage-3 read surface end to end** — what each surfaced
field means, its fail-closed default, and the reviewer-internal-vs-public lane boundary. It documents
field *names and semantics only*: it contains no record data, no PII, no internal ids, and no filesystem
paths, by policy.

Related contracts: [[stage2-reviewer-internal-read-surface-reference]] ·
[[stage3-alpine-timeline-card-mvp-spec]] · [[stage3-05-card-feed-contract]] ·
[[stage3-07-verify-at-source-contract]] · [[stage3-04-raw-preservation-contract]] ·
[[stage1-security-privacy-publication-gates-contract]] · [[stage0-transcript-evidence-statement-model-contract]]

---

## 0. The shape of the Stage-3 surface (read this first)

Stage 3 did **not** add a single new envelope key to `read_api`. The base read surface that a reviewer
sees is byte-identical to Stage 2.14 — the same five derived keys, the same two web-safe layers, the
same lane boundary (documented fully in [[stage2-reviewer-internal-read-surface-reference]] and pinned
again in §1–§2 below). What Stage 3 added on top is **two new reviewer-internal re-projections** plus
**five read-only auditors**:

| Stage-3 slice | What it is | New served envelope? | Lane |
|---|---|---|---|
| 3.05 card feed (`stage3_card_feed`) | the timeline's sole data source — cards over the read surface | **yes** — `{scope, access, cards[]}` | reviewer-internal only |
| 3.07 verify-at-source (`stage3_verify_at_source`) | per-card drill-down: which evidence links resolve to an original source | **yes** — `{scope, access, cards[]}` drill-down | reviewer-internal only |
| 3.04 preservation (`stage3_preservation_audit`) | read-time auditor: every served record's raw source is preserved | no — auditor report | reviewer-internal only |
| 3.03 source/data inventory (`stage3_source_inventory`) | reviewer-internal coverage projection of sources/data | no — coverage projection | reviewer-internal only |
| 3.10 composition integration (`tests` net over the surface) | composition safety net — overlays compose without leaking | no — test net | reviewer-internal only |
| 3.12 traceability + audit trail (`stage3_traceability`) | every served claim traces to its source + reviewer decision | no — auditor report | reviewer-internal only |
| 3.13 back-gap / coverage (`stage3_backgap`) | the surface never silently drops a record/gap it should emit | no — auditor report | reviewer-internal only |

**Why the guard pins three envelopes and not seven.** A drift guard can only deterministically assert
the surfaces that *emit a served envelope a frontend consumes*: the `read_api` record envelope (§1–§3),
the **card feed** (§4), and the **verify-at-source drill-down** (§5). Those three carry the machine
contract blocks in §8. The other four families (3.04 / 3.03 / 3.10 / 3.12 / 3.13) are reviewer-internal
**auditors** — they read the surface and prove an invariant; each ships its own RED-on-regression guard
(cited in §6), so this doc describes them in prose and defers their drift protection to those auditors.
Every Stage-3 surface, with no exception, runs at `access: reviewer_internal` — there is **no public
card feed and no public drill-down**.

---

## 1. The two layers that make a record safe (unchanged from Stage 2)

Before any field reaches a reviewer, every served record crosses **two independent web-safe layers**:

1. **Field allowlist (fail-closed):** `publication.to_web_safe` (`publication.py:390`) keeps only keys in
   `publication.WEB_SAFE_FIELD_ALLOWLIST` (`publication.py:268`); every other column — raw paths,
   internal ids, vault refs — is dropped. A field is *not* surfaced unless explicitly allowlisted.
2. **Transport sweep (independent backstop):** the whole assembled response is walked by
   `assert_no_raw_paths` (`read_api.py:125`) before return. It rejects any filesystem/absolute path or
   raw marker (`RAW_PATH_MARKERS`, `read_api.py:56`) that slipped past the allowlist — only genuine
   public `http(s)://` URLs are exempt. This is the GOV-34 transport-leak defense.

**Crucially, both Stage-3 re-projections reuse this exact backstop.** `stage3_card_feed.build_card_feed`
and `stage3_verify_at_source.build_verify_at_source` end by passing their assembled body through
`read_api.assert_no_raw_paths` — so a leak that slipped past the read surface fails LOUDLY at the card /
drill-down boundary too, not silently downstream.

**What this means for a reviewer:** if you ever see an absolute path, a `/Users/...` string, a `.sha256`
file, or a raw internal id anywhere in the surface — base record, card, or drill-down — that is a bug.

---

## 2. The lane boundary (the most important thing to understand)

`read_api.build_response` (`read_api.py:898`) assembles a response whose envelope is always
`{"scope": "alpine", "access": "reviewer_internal"}`. Inside it there are **two record lanes** plus an
opt-in gap lane:

| Envelope key | Source function | Lane meaning | Gate (fail-closed) |
|---|---|---|---|
| `records` | `published_records` (`read_api.py:463`) | **Public lane** — owner-published records | `publication_state == 'publishable'` AND re-derived `ui_status` eligible AND not an orphan |
| `reviewer_internal_records` | `reviewer_internal_records` (`read_api.py:509`) | **Reviewer-internal lane** — reviewer-cleared but **not** owner-published | `not_publishable` AND a promoting Lane-5 reviewer decision AND no open Lane-4 risk flag AND producing run ok AND `ui_status` eligible AND not an orphan |
| `completeness_gaps` | `completeness_gap_cards` (`read_api.py:612`) | **Gap lane** — known missing-source meetings | opt-in via `include_completeness_gaps`; never hidden |

Two rules a reviewer can rely on, and the **whole point of the boundary**:

- **A record is in exactly one record lane.** A `publishable` row is the public lane's and is *never*
  duplicated into the reviewer-internal lane; the reviewer-internal lane serves *only* `not_publishable`
  rows. So the reviewer-internal view can never become a back-door public surface, and the public lane
  stays empty until the separate owner publish gate flips.
- **`provenance_status` is reviewer-internal ONLY.** It is attached only when
  `include_provenance_status=True`, passed from exactly one call site — `reviewer_internal_records`
  (`read_api.py:559`). The public lane never passes it. **Every Stage-3 re-projection inherits this:** the
  card feed and the verify-at-source drill-down run at `access: reviewer_internal` and carry
  `provenance_status` because they project *only* the reviewer-internal lane (`reviewer_internal_records`
  + `completeness_gap_cards`) — they never touch `published_records`.

---

## 3. The base read-time overlay keys (the five derived keys — unchanged from Stage 2)

Each served statement is projected by `_serialize_statement` (`read_api.py:426`). Beyond the allowlisted
base fields, it carries these derived keys (full semantics in
[[stage2-reviewer-internal-read-surface-reference]] §3):

- **`ui_status`** — re-derived eligibility/render status. *Allowlisted field whose value is recomputed*
  (`publication.compute_ui_status`), never trusted from storage. **Lanes: public + reviewer-internal.**
- **`confidence_label`** (GOV-283) — how trustworthy the transcript source is; frozen SSOT
  `transcript_class.CONFIDENCE_LABEL_BY_CLASS`; fail-closed floor `auto_caption_untimed`.
  **Lanes: public + reviewer-internal.**
- **`speaker_label`** (GOV-290) — who spoke, name-free unless safely attributed; fail-closed
  `Meeting Attendee`. **Lanes: public + reviewer-internal.**
- **`evidence`** — the web-safe evidence drawer; only public `http(s)://` source/archive URLs survive.
  **Lanes: public + reviewer-internal.**
- **`provenance_status`** (GOV-311) — per-record trust indicator, frozen
  `PROVENANCE_STATUS_VALUES = {grounded, unverified}` (`read_api.py:331`); fail-closed `unverified`.
  **Lane: reviewer-internal ONLY.**

---

## 4. The card feed (3.05, `stage3_card_feed`) — the timeline's sole data source

`build_card_feed` (`stage3_card_feed.py`) emits the reviewer-internal timeline as
`{scope: "alpine", access: "reviewer_internal", cards: [...]}`. It is a **pure re-projection of the read
surface** — it never calls `to_web_safe`, never issues a raw query, and never re-derives a field the read
surface dropped. Every field rides straight from a named read-surface envelope key, or is derived *here*
from already-web-safe keys.

**Record card** (one per served reviewer-internal record). Keys, with whether they are always present:

- `handle` *(always)* — stable opaque card identity `"c1_" + SHA-256(card_type ␟ statement_id)[:40]`
  (`card_handle`). Not a raw DB id; preimage-resistant (NC-2).
- `type` *(always)* — `ai_presented` / `correction` / `statement` / `info`, fail-closed to `info`
  (`_resolve_record_type`).
- `jurisdiction` *(always)* — fixed `"alpine"`.
- `status` *(always)* — single status composed first-match top-down: `corrected` → `ai_presented` →
  `verified` (requires explicit `grounded` + verified `ui_status`) → fail-closed `unverified`
  (`_compose_record_status`). No fabricated `disputed` / `source_changed` edge.
- `evidence` *(always)* — the read surface's web-safe evidence drawer, passed through verbatim.
- `confidence_label`, `speaker_label`, `provenance_status` *(always for a reviewer-internal card)* —
  ride straight from the read record (the reviewer-internal lane always attaches all three).
- `title` *(optional)* — only when the record carries an allowlisted `title`.
- `date` *(optional)* — a record-level timing field, else the earliest evidence `scan_date`, else absent
  (`_card_date` never invents a date).
- `reviewed_summary` *(optional)* — the record's `statement_text`. **Reviewer-internal free text** (§2.3
  of the card contract) — present on the reviewer-internal card only.

**Gap card** (one per completeness gap — the only source of `source_missing`):

- `handle`, `type` (`source_missing`), `jurisdiction`, `status` (`source_missing`) *(always)*.
- `gap_type`, `severity`, `resolved_status` *(always)* — SSOT-validated, fail-closed in the gap
  projection.
- `detail` *(optional)* — present ONLY when it cleared the read-time raw-path + structured-PII guards;
  the gap **row is always emitted** even when `detail` is omitted (GOV-125 "never silently dropped").

The whole feed is transport-swept by `assert_no_raw_paths`. `assert_feed_covers_surface` proves the feed
never silently drops a record/gap the read surface emits (the 3.13 back-gap invariant, in-module).

---

## 5. Verify-at-source drill-down (3.07, `stage3_verify_at_source`) — does the source resolve?

`build_verify_at_source` (`stage3_verify_at_source.py`) emits a reviewer-internal
`{scope, access, cards: [...]}` drill-down — a 1:1 cover of the live feed (same `handle`s, same order) —
that attaches, per evidence link, whether it **resolves to an original source**. Reviewer-internal lane
only (`access: "reviewer_internal"`, never public).

**Record drill-down** keys (all always present — no conditional key on a record drill-down):

- `handle`, `type`, `jurisdiction` *(always)* — reused verbatim from the card feed (genuine 1:1 cover).
- `provenance_status` *(always)* — the read record's trust indicator, fail-closed `unverified`.
- `verify_at_source_status` *(always)* — `verifiable` only when provenance is `grounded` AND at least one
  link resolves; otherwise fail-closed `unverified` (`verify_at_source_status`). No card claims
  verify-at-source on a dangling locator.
- `links` *(always)* — a list; each entry has:
  - `locator` *(always)* — the already-web-safe drawer entry (allowlisted keys only).
  - `resolvability_status` *(always)* — `resolved` / `unresolved`, derived from the **canonical**
    evidence-link row, never the web-safe body (`resolvability_status`).

**Gap drill-down** keys: `handle`, `type` (`source_missing`), `jurisdiction`, `verify_at_source_status`
(`source_missing` — N/A by construction), `links` (always, empty). All always present.

---

## 6. The reviewer-internal auditors (3.04 / 3.03 / 3.10 / 3.12 / 3.13) — what they prove

These do not add a served envelope key; they read the surface and prove an invariant. Each is
reviewer-internal and ships its own RED-on-regression guard, so this doc describes what they tell a
reviewer and defers drift protection to those guards (not to this doc's machine contract).

- **3.04 raw preservation** (`stage3_preservation_audit`, GOV-367) — every served record's raw source is
  preserved and hash-verifiable. The reviewer sees a per-unit `hash_ok` boolean only — never a sha256, a
  path, or a vault marker. Guard: `tests/test_gov367_*` + in-module RED proofs.
- **3.03 source/data inventory** (`stage3_source_inventory`, GOV-364) — reviewer-internal coverage of
  sources vs served data. Counts and coverage only; `statement_text` is never projected (COUNT-only).
  Guard: `tests/test_gov364_*`.
- **3.10 composition integration** (GOV-393) — the overlays *compose* without any one of them leaking a
  field another would have dropped. Guard: `tests/test_gov393_*` (test net over the live surface).
- **3.12 traceability + audit trail** (`stage3_traceability`, GOV-406) — every served claim traces to its
  source and to the reviewer decision that promoted it (Lane-5 ledger). Guard: `tests/test_gov406_*`.
- **3.13 back-gap / coverage regression** (`stage3_backgap`, GOV-411) — the surface never silently drops
  a record or gap it is obligated to emit. Guard: `tests/test_gov411_*` + the feed's own
  `assert_feed_covers_surface`.

A reviewer reading any of these auditor reports sees an invariant **proven over the live surface**, not a
claim. If an auditor goes RED, the surface — not the auditor — is wrong.

---

## 7. "How to read the Stage-3 surface" — a legend a first reviewer can trust

When you open the reviewer-internal Alpine timeline:

1. Each **card** carries a stable `handle`, a `type`, a fail-closed `status`, an `evidence` drawer, and
   (reviewer-internal only) `confidence_label` / `speaker_label` / `provenance_status` and a
   `reviewed_summary`.
2. **Gap cards** tell you what is *missing* — meetings with no primary source yet — so gaps are visible,
   not pretended-complete.
3. The **verify-at-source drill-down** tells you, per evidence link, whether it `resolved` to an original
   source, and gives the card a single `verify_at_source_status` (`verifiable` only when grounded AND a
   link resolves).
4. The **auditors** (§6) prove, over the live surface, that raw sources are preserved, claims trace to
   their source and reviewer decision, the overlays compose cleanly, and nothing is silently dropped.

You will **never** see, by design: internal ids, filesystem/vault paths, `.sha256` files, `file://` URIs,
or `provenance_status` / `reviewed_summary` in a *public* lane (there is no public Stage-3 lane at all).
If you do, it is a leak — report it.

---

## 8. Machine-readable drift contracts

`tests/test_stage3_doc_surface_sync.py` parses the three blocks below and asserts each against the
**live** code output on a fixture record. If the code adds, removes, or re-lanes a derived key without the
matching block being updated, the test goes RED. Edit a block **only** alongside a matching code change,
and re-run the drift guard.

- `envelope_key:` — a derived key attached by `read_api` *after* `to_web_safe` (not in the allowlist).
- `rederived_allowlist_key:` — an allowlisted field whose value is re-derived at read time.
- `lanes:` — `public`, `reviewer_internal`, or both (comma-separated). Used by the read_api block.
- `presence:` — `always` (the key is on every such envelope) or `optional` (data-dependent). Used by the
  card-feed and verify-at-source blocks, which are reviewer-internal only (no `lanes:` — there is no
  public lane for either).

### 8.1 read_api record envelope (lane-split; unchanged from Stage 2.14)

<!-- STAGE3-READ-API-CONTRACT:BEGIN -->
rederived_allowlist_key: ui_status | lanes: public, reviewer_internal
envelope_key: confidence_label | lanes: public, reviewer_internal
envelope_key: speaker_label | lanes: public, reviewer_internal
envelope_key: evidence | lanes: public, reviewer_internal
envelope_key: provenance_status | lanes: reviewer_internal
<!-- STAGE3-READ-API-CONTRACT:END -->

### 8.2 card feed (`stage3_card_feed`; reviewer-internal only)

<!-- STAGE3-CARD-FEED-CONTRACT:BEGIN -->
record_card_key: handle | presence: always
record_card_key: type | presence: always
record_card_key: jurisdiction | presence: always
record_card_key: status | presence: always
record_card_key: evidence | presence: always
record_card_key: confidence_label | presence: always
record_card_key: speaker_label | presence: always
record_card_key: provenance_status | presence: always
record_card_key: title | presence: optional
record_card_key: date | presence: optional
record_card_key: reviewed_summary | presence: optional
gap_card_key: handle | presence: always
gap_card_key: type | presence: always
gap_card_key: jurisdiction | presence: always
gap_card_key: status | presence: always
gap_card_key: gap_type | presence: always
gap_card_key: severity | presence: always
gap_card_key: resolved_status | presence: always
gap_card_key: detail | presence: optional
<!-- STAGE3-CARD-FEED-CONTRACT:END -->

### 8.3 verify-at-source drill-down (`stage3_verify_at_source`; reviewer-internal only)

<!-- STAGE3-VERIFY-AT-SOURCE-CONTRACT:BEGIN -->
record_drilldown_key: handle | presence: always
record_drilldown_key: type | presence: always
record_drilldown_key: jurisdiction | presence: always
record_drilldown_key: provenance_status | presence: always
record_drilldown_key: verify_at_source_status | presence: always
record_drilldown_key: links | presence: always
gap_drilldown_key: handle | presence: always
gap_drilldown_key: type | presence: always
gap_drilldown_key: jurisdiction | presence: always
gap_drilldown_key: verify_at_source_status | presence: always
gap_drilldown_key: links | presence: always
link_key: locator | presence: always
link_key: resolvability_status | presence: always
<!-- STAGE3-VERIFY-AT-SOURCE-CONTRACT:END -->
