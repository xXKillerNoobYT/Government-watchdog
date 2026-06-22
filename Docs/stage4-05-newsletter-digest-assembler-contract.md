# Stage 4.05 — Deterministic Digest Assembler Contract over the Newsletter Item Feed

> **Issue:** GOV-457 (Stage 4.05 · CTO / backend-deterministic side). **Sequenced after:** GOV-453 (Stage 4.04 raw-preservation auditor, merged `origin/main` PR #78 / HEAD `82e7e72`).
> **Stage:** 4.05 — `contract → assembler → RED tests`, all in this CTO-owned issue (mirrors the 4.03 GOV-449 and 4.04 GOV-453 bundled split).
> **Scope:** Town of Alpine only · reviewer-internal · no public launch · no email/sender · no new crawl · no ingestion-scope change · **no rendering/markup (that is 4.06)** · **no editorial voice/summarization (that is 4.08, separately gated)**.
> **Grounded on:** canonical remote `origin/main` HEAD `82e7e72`.
> **Goal of record:** Stage 4 parent (reviewer-internal newsletter backbone); 4.06–4.15 stay `planned` until this lands.
> **Inputs of record (read-only grounding, no edit by this child):**
> - `scripts/stage4_newsletter_feed.py` @ `82e7e72` (GOV-449) — the item feed this assembler *consumes and groups*, never re-implements. Anchors: `build_newsletter_feed`, `build_readiness_record`, `_sort_key`, `SCOPE`, `ACCESS`, `STAGE3_CLAIM_VOCAB`, `CORRECTION_NONE`, `ALLOWED_ITEM_TYPES`, `_iter_strings`, `_assert_local_safe`.
> - `scripts/read_api.py` @ `82e7e72` — `assert_no_raw_paths` (`:125`) + `RAW_PATH_MARKERS` (`:56`) + `RawPathLeak` (`:91`). **Consumed read-only; `read_api.py` stays 0-diff.**
> - `scripts/publication.py` @ `82e7e72` — public-contract surface. **Not imported; stays 0-diff.**
> - `scripts/stage4_newsletter_preservation_audit.py` (GOV-453) — the Stage 4.04 reviewer-internal overlay precedent this file's `--check`/overlay shape mirrors.

This document pins the **deterministic digest-assembler contract** for the Stage 4 reviewer-internal Alpine
newsletter: *the GOV-449 item feed, grouped into **one structured digest per Alpine coverage period**, emitting the
required **GOV-15 newsletter template sections as structured data** (never prose), with every item's Stage-3 label and
`sourceTrail[]` carried through **unmodified** and chronology non-decreasing within each digest.* The assembler is a
**pure structured projection over the existing item feed** — it adds no new source of truth, invents no item, generates
no prose, performs no rendering/markup, and applies no editorial voice or summarization. It authorizes **no** new crawl,
**no** new source, **no** ingestion-scope change, **no** public projection, **no** schema/migration change, and **no**
`publication.py` / `read_api.py` / `stage4_newsletter_feed.py` field change. See §6.

The assembler + RED tests in the same issue build the projection per §4–§5. A contract defines the shape; it does not
satisfy it.

---

## 0. What this child owns, and what it must not touch

**Owns:** the digest grouping rule (§1), the GOV-15 section-mapping rule over the existing item vocabulary (§2), the
reviewer-internal digest object shape (§3), the assembler API + audit overlay shape (§4), the RED test list (§5), the
risk gate (§6).

**Must NOT touch / re-derive (carry Stage 3/4 forward):**
- the `stage4_newsletter_feed.py` item projection — **consumed, never forked**: the assembler *calls* `build_newsletter_feed`
  / `build_readiness_record` and *groups* the items it returns; it does not re-assemble items, re-sort the global feed,
  re-assign item ids, re-derive labels, or rebuild `sourceTrail`;
- the `read_api.assert_no_raw_paths` transport guard + `RAW_PATH_MARKERS` — **reused as the backstop**, `read_api.py` 0-diff;
- the Stage-3 claim vocabulary (`STAGE3_CLAIM_VOCAB`) — **imported by reference**; GOV-15 sections classify existing
  labels into buckets, they never mint a new label (EG-7 stays intact one layer up);
- the reviewer-internal vs public lane separation (GOV-146 / GOV-347 / GOV-420) — **reused, never forked**. Public stays
  Isaac-gated (GOV-420); this assembler never emits a public lane.

---

## 1. Digest grouping rule (one digest per Alpine coverage period)

A **digest** is the set of all feed items sharing a `newsletterId` (the `alpine-historical-YYYY-WW` ISO-week coverage
batch GOV-449 already assigns, plus the named `alpine-historical-undated` batch). Grouping is a **pure partition** of
`build_newsletter_feed(conn)["items"]` by `newsletterId`:

- **Determinism.** Digests are ordered by `newsletterId` (lexical, total); within a digest the items keep the feed's
  global `_sort_key` order (oldest→newest by `recordDate`, then `coveragePeriod.startDate`, then card handle). No
  wall-clock, no RNG, no insertion-order dependence — same DB ⇒ byte-identical digest object (NF-A below).
- **Coverage period.** Every dated item in a weekly batch shares the same Mon→Sun `coveragePeriod` (it is derived from
  the same ISO week); the digest carries that period (the undated batch carries `null`). Never a coverage claim beyond
  the grounded record dates.
- **No invented digest.** A `newsletterId` exists in the output **iff** at least one served feed item carries it. No
  empty/placeholder period is fabricated.

---

## 2. GOV-15 section mapping (sections are DATA, not prose)

Each digest exposes the GOV-15 newsletter template sections as **structured data** — a classification index over the
digest's own items (id lists) plus deduped graph aggregates. An item id MAY appear in more than one section (a disputed
correction is both a `corrections` and a `conflicts` entry); this is an index, not a partition. Mapping (all derived
from fields the feed already emits — no new vocabulary, no prose):

| GOV-15 section (issue list)            | Digest key            | Derivation (over the digest's items)                                                            |
|----------------------------------------|-----------------------|-------------------------------------------------------------------------------------------------|
| processed records                      | `processedRecords`    | `{count, itemIds[]}` — every item in the digest, in feed (chronological) order.                 |
| source-set / backfill progress         | `sourceSetProgress`   | per-digest `{sourceCategoriesReviewed[], chronologicalRange{oldest,newest}|null, orderingPreserved, knownGaps[], completionFraming}` — categories/range from this digest's items; `knownGaps`/`completionFraming` carried verbatim from `build_readiness_record`. |
| timeline chunks                        | `timelineChunks`      | item ids with `itemType == "timeline_chunk"`.                                                    |
| key meetings / documents               | `keyMeetings`         | sorted distinct `meetingIds` across the digest's items.                                          |
|                                        | `keyDocuments`        | sorted distinct `sourceIds` across the digest's items (the reviewed source/document set).        |
| topics                                 | `topics`              | sorted distinct `topicIds` across the digest's items.                                            |
| corrections / conflicts / later outcomes | `corrections`       | item ids with `itemType == "correction"` **or** `labels.correctionStatus` not in `{none}`.      |
|                                        | `conflicts`           | item ids with `labels.claimStatus` in `{disputed}`.                                              |
|                                        | `laterOutcomes`       | item ids with `labels.claimStatus` in `{source_changed, source_missing}`.                        |
| unverified items                       | `unverifiedItems`     | item ids with `labels.claimStatus` in `STAGE3_CLAIM_VOCAB \ {verified}` (conservative; never styles unverified/AI as fact). |
| source trail                           | `sourceTrail`         | the digest's `sourceTrail[]` entries, deduped by `sourceId` (first occurrence kept), sorted by `sourceId`, **each entry carried unchanged**. |

The classification sets (`{disputed}`, `{source_changed, source_missing}`, `STAGE3_CLAIM_VOCAB \ {verified}`) are
**buckets over existing labels**, not new labels: every value is already a member of the imported Stage-3 vocabulary.

---

## 3. Reviewer-internal digest object shape

```jsonc
{
  "scope": "alpine",
  "access": "reviewer_internal",            // never "public" — public is GOV-420 / Isaac-gated
  "digests": [
    {
      "newsletterId": "alpine-historical-2026-19",
      "coveragePeriod": { "startDate": "2026-05-04", "endDate": "2026-05-10" },  // or null (undated batch)
      "items": [ /* the feed items for this batch, carried VERBATIM (labels + sourceTrail unchanged) */ ],
      "sections": {
        "processedRecords": { "count": 3, "itemIds": ["alpine-newsletter-item-001", ...] },
        "sourceSetProgress": { "sourceCategoriesReviewed": ["agenda_packet"], "chronologicalRange": {"oldest": "...", "newest": "..."},
                                "orderingPreserved": "oldest_to_newest", "knownGaps": [...], "completionFraming": "..." },
        "timelineChunks":  ["alpine-newsletter-item-001", ...],
        "keyMeetings":     [],
        "keyDocuments":    ["alpine_packet"],
        "topics":          [],
        "corrections":     ["alpine-newsletter-item-007"],
        "conflicts":       [],
        "laterOutcomes":   [],
        "unverifiedItems": ["alpine-newsletter-item-006", ...],
        "sourceTrail":     [ { "sourceId": "alpine_packet", "localSourcePath": null, ... } ]
      }
    }
  ]
}
```

The **audit overlay** (the `--check`/CLI summary, GOV-453 precedent) is a separate, swept envelope:

```jsonc
{
  "scope": "alpine",
  "access": "reviewer_internal",
  "digest_count": <int>,
  "item_count": <int>,
  "sections_complete": true,        // EG-5 — every required section present as structured data in every digest
  "chronology_ok": true,            // EG-3 — recordDate non-decreasing within every digest
  "labels_preserved": true,         // every digest item's labels == the feed item of the same id
  "source_trail_preserved": true,   // every digest item's sourceTrail == the feed item of the same id
  "reproducible": true,             // NF-A — re-assembling is byte-identical
  "digest_digest": "<sha256 of the canonical digest object JSON>",   // single opaque envelope fingerprint
  "violations": { "sections": [...], "chronology": [...], "labels": [...], "source_trail": [...], "reproducibility": [...] }
}
```

`digest_digest` is the **single opaque fingerprint** of the canonical digest object (envelope-level only — never a
per-item hash, never a path). The **digest object** (which embeds the feed items' reviewer-internal `/alpine/` route
links) is swept by the feed's own route-aware guard `stage4_newsletter_feed._assert_local_safe` — it single-sources
`read_api`'s leak vocabulary (`RAW_PATH_MARKERS` / `RawPathLeak` / `_is_web_url`) but exempts those routes from the
absolute-path rule, so the route exemption lives in exactly one place (extend-not-fork); a raw vault path / `..` /
`file://` still fails LOUDLY. The **overlay** (no route links — counts + fingerprint only) is swept by the stricter
`read_api.assert_no_raw_paths`.

---

## 4. Assembler shape (impl, same issue)

Additive module `scripts/stage4_newsletter_digest_assembler.py` (the GOV-347 / GOV-367 / GOV-453 separate-additive-module
precedent — `read_api.py` / `publication.py` / `stage4_newsletter_feed.py` all stay 0-diff). Public API:

- `assemble_digests(conn, feed=None) -> dict` — partition the item feed by `newsletterId` (§1), build the §2 sections per
  digest, carry items verbatim, route-aware transport-sweep (`stage4_newsletter_feed._assert_local_safe`), and return the
  §3 digest object. Pure function of the feed.
- `assert_section_presence(digests) -> bool` — EG-5: raise `DigestSectionError` if any digest is missing a required
  section key, or a section value is a string (prose smell) rather than structured data (list/dict).
- `assert_digest_chronology(digests) -> bool` — EG-3: raise `DigestChronologyError` if any digest's items are not
  non-decreasing by `recordDate` (undated sentinel sorts last).
- `assert_labels_preserved(conn, digests, feed=None) -> bool` / `assert_source_trail_preserved(conn, digests, feed=None) -> bool`
  — raise `DigestPreservationError` if any digest item's `labels` / `sourceTrail` differs from the feed item of the same id.
- `assert_reproducible(conn) -> str` — assemble twice; raise `DigestReproducibilityError` if the canonical JSON differs.
  Returns the canonical `digest_digest`.
- `build_digest_overlay(conn) -> dict` — run all five checks, assemble the §3 overlay, route it through
  `read_api.assert_no_raw_paths`. Fail-closed.
- CLI `--db` `[--artifact digest|overlay]` `[--check]` → prints the digest object (default) or the overlay; `--check` runs
  the five guards; exit 0 clean, non-zero on a raised invariant.

The module imports `stage4_newsletter_feed` and `read_api` **by reference** and re-declares none of their constants
(`SCOPE` / `ACCESS` / `STAGE3_CLAIM_VOCAB` / `CORRECTION_NONE` / `_sort_key` / `RAW_PATH_MARKERS` are all reused).

---

## 5. RED test list (write first, must fail before the assembler exists, pass after)

1. **Intact corpus** — `assemble_digests` returns `scope=alpine` / `access=reviewer_internal`, ≥1 digest, every digest
   carries all required sections; the object passes `assert_no_raw_paths`.
2. **NF-A reproducibility load-bearing** — `assert_reproducible` returns a digest on the real (pure) feed; monkeypatching
   the feed to return a per-call-varying value makes it raise `DigestReproducibilityError` (a tautological compare would
   still pass).
3. **EG-5 section presence load-bearing** — `assert_section_presence` passes on a real digest; dropping a required
   section key, and replacing a section value with a prose string, each raise `DigestSectionError`.
4. **EG-3 chronology load-bearing** — `assert_digest_chronology` passes on the real digest; reversing two items within a
   digest raises `DigestChronologyError`.
5. **Label + sourceTrail preservation load-bearing** — both guards pass on a real digest; mutating one digest item's
   `labels.claimStatus`, and one item's `sourceTrail`, each raise `DigestPreservationError` (proves carried unchanged).
6. **GOV-15 mapping correctness** — over the seeded corpus, the correction record lands in `corrections`, the AI/unverified
   records land in `unverifiedItems`, every item lands in `processedRecords`, and `keyDocuments`/`sourceTrail` reflect the
   reviewed source set; sections are id-lists/dicts, never prose.
7. **No-leak** — fixture plants raw vault paths on every evidence link; none cross the digest object or the overlay;
   every `sourceTrail[].localSourcePath` is `null`; no per-item 64-hex hash; both pass the transport guard.
8. **Extend-not-fork** — `read_api.py` / `publication.py` / `stage4_newsletter_feed.py` byte-0-diff vs `origin/main`; the
   assembler imports the feed module + SSOT guards by reference and re-declares none of their constants.
9. **CLI smoke** — `--db` over a seeded corpus exits 0 and prints a reviewer-internal digest object; `--artifact overlay
   --check` exits 0 and prints a reviewer-internal overlay with `sections_complete`/`chronology_ok` true.

---

## 6. Risk gate

- **Alpine-only, reviewer-internal-only.** No public lane, no email/sender, no naming non-officials, no non-Alpine
  coverage. Public deploy stays GOV-420 / Isaac-gated and does **not** block this work.
- **No rendering, no editorial voice.** This slice is the deterministic **assembler** only — structured data, never
  markup (4.06) and never prose/summarization (4.08). No AI output anywhere; AI items keep their Stage-3 unverified label
  and are surfaced as `unverifiedItems`, never as fact.
- **0-diff public contract.** `read_api.py` / `publication.py` / `stage4_newsletter_feed.py` are consumed read-only.
- **No new crawl / no schema change / no mutation.** The assembler is a pure read-time projection over the feed.
- **Review gate:** both legs (VSR + SecPriv no-leak / no-public-surface) PASS before a non-author merge — reuse the
  GOV-454 / GOV-455 / GOV-456 pattern.
