# Stage 2 Reviewer-Internal Read-Surface Reference

> **Audience:** the first internal reviewer(s) reading the Alpine timeline behind the gated beta.
> **Scope:** Alpine-only. Reviewer-internal surface only — **not** a public or editorial artifact.
> **Status:** Stage 2.14 (Documentation maintenance). Source-grounded against the merged
> `scripts/read_api.py` at `origin/main` (`bf35a4f`). Line references below point at that file.
> **Drift guard:** `tests/test_stage2_doc_surface_sync.py` fails if this doc and `read_api.py`
> disagree about which keys appear in which lane. Keep them in sync — do not hand-edit the
> machine-readable contract block at the bottom without re-running that test.

This page explains, for a reviewer, **what each surfaced field means**, **its fail-closed default**,
**its frozen value set**, and **the reviewer-internal-vs-public lane boundary**. It documents field
*names and semantics only* — it contains no record data, no PII, no internal ids, and no filesystem
paths, by policy.

Related contracts: [[stage1-slice4-prereq0-read-api-concept-map]] ·
[[stage2-transcript-evidence-gap-analysis]] · [[stage1-security-privacy-publication-gates-contract]] ·
[[stage0-transcript-evidence-statement-model-contract]] · [[stage1-raw-store-layout]] ·
[[stage1-transcript-evidence-statement-contract]]

---

## 1. The two layers that make a record safe

Before any field reaches a reviewer, every record crosses **two independent web-safe layers**
(`read_api.py` module docstring, lines 11–26):

1. **Field allowlist (fail-closed):** `publication.to_web_safe` (`read_api.py:452`) keeps only keys in
   `publication.WEB_SAFE_FIELD_ALLOWLIST`; every other column — raw paths, internal ids, vault refs —
   is dropped. Default posture: a field is *not* surfaced unless it is explicitly allowlisted.
2. **Transport sweep (independent backstop):** the whole assembled response is walked by
   `assert_no_raw_paths` (`read_api.py:125–146`) before return (`build_response`, `read_api.py:935`).
   It rejects any filesystem/absolute path or raw marker (`RAW_PATH_MARKERS`, `read_api.py:56–72`) that
   slipped past the allowlist — only genuine public `http(s)://` URLs are exempt. This is the GOV-34
   transport-leak defense; it catches a mis-allowlisted field even if layer 1 missed it.

**What this means for a reviewer:** if you ever see an absolute path, a `/Users/...` string, a `.sha256`
file, or a raw internal id in the surface, that is a bug — the surface is built to make those impossible.

---

## 2. The lane boundary (the most important thing to understand)

`read_api.build_response` (`read_api.py:898–935`) assembles a response whose envelope is always
`{"scope": "alpine", "access": "reviewer_internal"}` (`read_api.py:924`). Inside it there are **two
record lanes** plus an opt-in gap lane:

| Envelope key | Source function | Lane meaning | Gate (fail-closed) |
|---|---|---|---|
| `records` | `published_records` (`read_api.py:463–483`) | **Public lane** — owner-published records | `publication_state == 'publishable'` (line 477) AND re-derived `ui_status` eligible AND not an orphan |
| `reviewer_internal_records` | `reviewer_internal_records` (`read_api.py:509–561`) | **Reviewer-internal lane** — reviewer-cleared but **not** owner-published | `publication_state == 'not_publishable'` (line 540 skips publishable) AND a promoting Lane-5 reviewer decision AND no open Lane-4 risk flag AND producing run ok AND `ui_status` eligible AND not an orphan |
| `completeness_gaps` | `completeness_gap_cards` (`read_api.py:612–685`) | **Gap lane** — known missing-source meetings | opt-in via `include_completeness_gaps`; never hidden, fail-closed values |

Two rules a reviewer can rely on:

- **A record is in exactly one record lane.** A `publishable` row is the public lane's and is *never*
  duplicated into the reviewer-internal lane (`read_api.py:540`); the reviewer-internal lane serves
  *only* `not_publishable` rows (`read_api.py:527–531`). So the reviewer-internal view can never become a
  back-door public surface, and the public lane stays empty until the separate owner publish gate flips.
- **`provenance_status` is reviewer-internal ONLY.** It is attached only when
  `include_provenance_status=True`, which is passed from exactly one call site —
  `reviewer_internal_records` (`read_api.py:559`). The public lane (`published_records`,
  `read_api.py:482`) never passes it, so a public record is byte-identical to its pre-2.12 shape and
  carries no `provenance_status`. Statement free-text likewise stays reviewer-internal (Stage 2.06
  contract); this doc never reproduces it.

---

## 3. The read-time overlay keys (what each surfaced field means)

Each served statement is projected by `_serialize_statement` (`read_api.py:426–460`). Beyond the
allowlisted base fields, it carries these **derived keys**:

### `ui_status` — re-derived eligibility/render status
- **Source:** injected at `read_api.py:451`; value re-derived by `_eligible_ui_status`
  (`read_api.py:174–189`) via `publication.compute_ui_status` — **never trusted from storage**, so a
  stale stored status cannot fail open.
- **Lanes:** public **and** reviewer-internal. (It is an allowlisted field whose *value* is recomputed.)
- **Fail-closed:** only values in `publication.PUBLICATION_ELIGIBLE_UI_STATUSES` are served at all
  (`read_api.py:475`, `553`); anything else is silently not served.
- **How to read it:** the label the frontend renders verbatim for the record's review/source state.

### `confidence_label` — how trustworthy the transcript source is (GOV-283)
- **Source:** `_confidence_label_for` (`read_api.py:203–245`); envelope key set at `read_api.py:456`.
- **Lanes:** public **and** reviewer-internal.
- **Frozen value set:** the SSOT `transcript_class.CONFIDENCE_LABEL_BY_CLASS` —
  `source_anchored_timed`, `auto_caption_timed`, `auto_caption_untimed`, `minutes_summary`,
  `derived_summary` (`transcript_class.py:68–75`).
- **Fail-closed default:** `auto_caption_untimed` (the lowest-confidence mapping,
  `_CONSERVATIVE_CONFIDENCE_LABEL`, `read_api.py:200`). Every break in the
  `statement → segment → transcript → transcript_class` chain collapses here — a statement is **never**
  projected at a *higher* confidence than its resolvable source class permits.
- **How to read it:** higher = better-sourced. `source_anchored_timed` is an official, timestamped
  transcript; `auto_caption_untimed` is the conservative floor (ASR-only or unresolved).

### `speaker_label` — who spoke, name-free unless safely attributed (GOV-290)
- **Source:** `_speaker_label_for` (`read_api.py:259–316`); envelope key set at `read_api.py:457`.
- **Lanes:** public **and** reviewer-internal.
- **Value set:** a real `"Name, Role"` string **only** when the attribution row is `attributed` AND the
  `speaker_class` is in `speakers.AUTO_NAMEABLE_CLASSES` (`{"on-record-official"}`,
  `speakers.py:55`) — the persisted write-time-safe label is surfaced verbatim. Otherwise a generic
  label: `Community Member` (`SAFE_COMMUNITY_LABEL`) for an `on-record-public` speaker, else
  `Meeting Attendee` (`SAFE_GENERIC_LABEL`, `speakers.py:68–69`).
- **Fail-closed default:** `Meeting Attendee`. For any non-safely-named row the stored free-text label is
  **never read** (`read_api.py:312–316`), so a name poisoned past the write gate cannot leak.
- **How to read it:** a named label is a vetted on-record official; a generic label means the speaker is
  not safely nameable — treat the name as unknown, not hidden by accident.

### `provenance_status` — per-record trust indicator (GOV-311) · **reviewer-internal only**
- **Source:** `_provenance_status_for` (`read_api.py:354–401`); envelope key set at `read_api.py:458–459`
  **only** under `include_provenance_status=True`.
- **Lanes:** reviewer-internal **only** (see §2).
- **Frozen value set:** `read_api.PROVENANCE_STATUS_VALUES` = `{"grounded", "unverified"}`
  (`read_api.py:329–331`).
- **Fail-closed default:** `unverified`. A record reads `grounded` **only** when all three canonical legs
  pass, recomputed from canonical columns (never a stored flag): the grounding chain resolves
  (`stage2_traceability.statement_grounded`), the raw source is preserved
  (`stage2_traceability.raw_linked`), and — if `produced_by='ai'` — the producing run resolves and is
  `ok` (`_ai_provenance_ok`, `read_api.py:334–351`). Any break collapses to `unverified`; optimism is
  never the default.
- **How to read it:** `grounded` = this record's citation chain is complete and reproducible and its AI
  provenance (if any) checks out. `unverified` = at least one leg could not be confirmed — trust the
  record less, not more.

### `evidence` — the web-safe evidence drawer
- **Source:** envelope key set at `read_api.py:453`, built from `_evidence_links_for` via
  `_web_safe_evidence` (`read_api.py:421–423`).
- **Lanes:** public **and** reviewer-internal.
- **Contents:** each entry is an already-web-safe evidence-link projection (allowlist + non-web-URL
  strip). Only public `http(s)://` source/archive URLs survive; a `file://` vault URI is dropped
  (`_strip_non_web_urls`, `read_api.py:404–418`). No raw local refs, no internal ids.
- **How to read it:** the citation pointers backing the statement. An empty drawer with no segment edge
  cannot happen — orphan records are never served (`read_api.py:480`, `556`).

---

## 4. The completeness-gap cards (GOV-298) — what a gap card does and does NOT expose

`completeness_gap_cards` (`read_api.py:612–685`) projects the first-class `completeness_gaps` table
(the ~90 `no_primary_source` Alpine meetings) onto a web-safe card. It is opt-in via
`include_completeness_gaps` and surfaces under the `completeness_gaps` envelope key.

- **Exposes only these keys** (`GAP_CARD_FIELDS`, `read_api.py:579–587`): `gap_id`, `subject_id`,
  `subject_node_type`, `gap_type`, `severity`, `resolved_status`, and an optional `detail`.
- **Never exposes** the internal/provenance columns `source_id`, `detected_run_id`, `detected_utc` —
  these are **not even SELECTed** (`read_api.py:650–656`), so they cannot reach any projected body. The
  card is built explicitly and deliberately **not** routed through `to_web_safe` (which would pass an
  allowlisted `source_id` straight through — `read_api.py:624–631`).
- **`detail` is re-guarded** at read time (`_safe_gap_detail`, `read_api.py:590–609`): a `detail` that
  trips the raw-path sweep or the structured-PII guard is **omitted** (the field is simply absent) — but
  the gap **row itself is always emitted**. A gap is never hidden (GOV-125 "never silently dropped").
- **Fail-closed values:** a `gap_type` / `severity` / `resolved_status` not in the frozen SSOT
  vocabulary collapses to a conservative placeholder (`unknown` / `warn` / `open`,
  `read_api.py:572–574`) rather than being trusted.
- **How to read it:** a gap card says "we know a meeting exists but its primary source is missing." It is
  a transparency signal, not a record claim — it carries no statement text and no citation.

---

## 5. "How to read a record" — a legend a first reviewer can trust

When you open the reviewer-internal timeline, each record carries:

1. **`ui_status`** — its review/source state (the render label). It is eligible by construction.
2. **`confidence_label`** — how good the underlying transcript is. Floor = `auto_caption_untimed`.
3. **`speaker_label`** — who spoke; a name only if vetted-and-on-record, else a safe generic.
4. **`provenance_status`** *(reviewer-internal only)* — `grounded` if the citation chain + raw
   preservation + AI provenance all check out, else `unverified`. Treat `unverified` with caution.
5. **`evidence`** — the citation drawer (public source/archive URLs only).

And separately, **completeness-gap cards** tell you what is *missing* — meetings with no primary source
yet — so gaps are visible, not pretended-complete.

You will **never** see, by design: internal ids (`segment_id`, `speaker_attribution_id`,
`ai_extraction_run_id`, raw `source_id`), filesystem/vault paths, `.sha256` files, or `file://` URIs.
If you do, it is a leak — report it.

---

## 6. Machine-readable drift contract

`tests/test_stage2_doc_surface_sync.py` parses the block below and asserts it against the **live**
`read_api` output on a fixture record. If `read_api.py` adds, removes, or relane-s a derived key without
this block being updated, the test goes RED. Edit this block **only** alongside a matching code change,
and re-run the drift guard.

- `envelope_key:` — a derived key attached *after* `to_web_safe` (not in `WEB_SAFE_FIELD_ALLOWLIST`).
- `rederived_allowlist_key:` — an allowlisted field whose value is re-derived at read time.
- `lanes:` — `public`, `reviewer_internal`, or both (comma-separated).

<!-- DRIFT-GUARD-CONTRACT:BEGIN -->
rederived_allowlist_key: ui_status | lanes: public, reviewer_internal
envelope_key: confidence_label | lanes: public, reviewer_internal
envelope_key: speaker_label | lanes: public, reviewer_internal
envelope_key: evidence | lanes: public, reviewer_internal
envelope_key: provenance_status | lanes: reviewer_internal
<!-- DRIFT-GUARD-CONTRACT:END -->
