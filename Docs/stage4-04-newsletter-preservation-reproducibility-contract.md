# Stage 4.04 — Raw-Preservation & Reproducibility Contract for the Newsletter Feed

> **Issue:** GOV-453 (Stage 4.04 · CTO / backend-deterministic side). **Sequenced after:** GOV-449 (Stage 4.03 item feed, merged `origin/main` PR #77 / HEAD `689b9d5`).
> **Stage:** 4.04 — `contract → auditor → RED tests`, all in this CTO-owned issue (mirrors 3.04's GOV-363→GOV-367 split, bundled).
> **Scope:** Town of Alpine only · reviewer-internal · no public launch · no email/sender · no new crawl · no ingestion-scope change.
> **Grounded on:** canonical remote `origin/main` HEAD `689b9d5`.
> **Goal of record:** Stage 4 parent (reviewer-internal newsletter backbone); 4.05–4.15 stay `planned` until this lands.
> **Inputs of record (read-only grounding, no edit by this child):**
> - `scripts/stage4_newsletter_feed.py` @ `689b9d5` (GOV-449) — the projection this contract *describes and audits*, never re-implements. Anchors: `build_newsletter_feed`, `build_readiness_record`, `source_link_validation`, `expected_card_ids`, `_sort_key`, `_source_trail`, `classify_orphan`, `_assert_local_safe`.
> - `scripts/read_api.py` @ `689b9d5` — `reviewer_internal_records` (`:509`, the served read surface), `assert_no_raw_paths` (`:125`) + `RAW_PATH_MARKERS` (`:56`), `_evidence_links_for` (`:154`). **Consumed read-only; `read_api.py` stays 0-diff.**
> - `scripts/stage3_card_feed.py` (GOV-347) — `card_handle`, `_resolve_record_type`, `_compose_record_status`, `_card_date`: the Stage-3 card-identity SSOT the auditor re-derives provenance from (independent of the feed builder, so the cross-check is not a tautology).
> - `scripts/publication.py` @ `689b9d5` — `WEB_SAFE_FIELD_ALLOWLIST` / `WEB_UNSAFE_FIELDS` field SSOT. **Consumed read-only; `publication.py` stays 0-diff.**
> - `scripts/stage3_preservation_audit.py` (GOV-367) — the Stage 3.04 read-time-auditor precedent this file mirrors one layer up (projection layer, not file layer).

This document pins the **raw-preservation & reproducibility contract** for the Stage 4 reviewer-internal Alpine
newsletter feed: *the feed projected by `scripts/stage4_newsletter_feed.py` is **deterministically reproducible**
(re-running the projection over the same reviewed Stage-3 inputs yields byte-identical output), it **preserves raw
provenance losslessly** (every emitted item traces to a real reviewed Stage-3 record; every `sourceTrail[]` / `sourceIds`
linkage rides through unmodified — no invented source, no dropped source, no lossy transform), and projecting the feed
**mutates no reviewed raw record** (the read surface is read-only over `statements` / `evidence_links` / `sources`).*
It **describes and verifies** these properties over the **existing** Stage-4 feed. It authorizes **no** new crawl,
**no** new source, **no** ingestion-scope change, **no** public projection, **no** schema/migration change, and **no**
`publication.py` / `read_api.py` / `stage4_newsletter_feed.py` field change. See §6.

The auditor + RED tests in the same issue build the read-time check per §4–§5. A contract defines the shape; it does
not satisfy it.

---

## 0. What this child owns, and what it must not touch

**Owns:** the three feed-preservation invariants (§1), how each is already true in the existing feed (§2), the
reviewer-internal preservation-overlay shape (§3), the auditor shape (§4), the RED test list (§5), the risk gate (§6).

**Must NOT touch / re-derive (carry Stage 3/4 forward):**
- the `stage4_newsletter_feed.py` projection — **described, never forked**: the auditor *calls* `build_newsletter_feed` /
  `build_readiness_record` / `source_link_validation`, it does not re-assemble items, re-sort, or re-assign ids;
- the `read_api.assert_no_raw_paths` transport guard + `RAW_PATH_MARKERS` — **reused as the backstop**, `read_api.py` 0-diff;
- the `publication` allowlist SSOT — **consumed read-only**, `publication.py` 0-diff;
- the reviewer-internal vs public lane separation (GOV-146 / GOV-347 / GOV-420) — **reused, never forked**. Public stays Isaac-gated (GOV-420); this auditor never emits a public lane.

---

## 1. The three invariants (NF = newsletter feed)

- **NF-1 — Reproducibility (idempotent regeneration).** For a fixed DB state, `build_newsletter_feed`,
  `build_readiness_record`, and `source_link_validation` are **pure functions**: two consecutive calls produce
  byte-identical JSON (`json.dumps(..., sort_keys=True)`). Identity (`alpine-newsletter-item-NNN`) and order
  (`_sort_key` — a total order: `recordDate`, then `coveragePeriod.startDate`, then the stable card handle) are derived
  from grounded record data only — **no wall-clock timestamp, no RNG, no insertion-order dependence**.

- **NF-2 — Lossless provenance preservation.** Every emitted item traces to **one real reviewed Stage-3 record**: its
  `cardIds[0]` is a card handle that the served read surface (`reviewer_internal_records`) actually mandates, and its
  `sourceIds` / `sourceTrail[].sourceId` set is **exactly** the source set of that served record's evidence drawer — no
  source invented, none dropped, no lossy transform of the reviewed raw linkage. (Orphans — empty source set / no
  Stage-3 anchor — are held out and routed to VSR by `source_link_validation`, never silently promoted.)

- **NF-3 — Zero raw mutation (read-only projection).** Building any feed artifact leaves the reviewed raw records
  byte-stable: the full contents of `statements`, `evidence_links`, and `sources` content-hash identically before and
  after the projection. No raw filesystem path / `.sha256` / vault marker crosses any artifact (transport-swept;
  `sourceTrail[].localSourcePath` is always `null`).

All three are **fail-closed**: the auditor raises a dedicated `AssertionError` subclass on any violation; an honest
clean corpus is the only path to a passing run.

---

## 2. How each invariant is already true in the existing feed (GOV-449)

- **NF-1** — `build_newsletter_feed` reads `reviewer_internal_records` (a deterministic `ORDER BY statement_id` query),
  projects each via `_item`, filters orphans, `sort`s by the total `_sort_key`, and assigns ids by enumeration. No
  `datetime.now`, no `random`. GOV-449 already asserts byte-identical re-projection
  (`test_item_ids_are_deterministic_namespaced_sequence`). 4.04 adds an explicit auditor over all three artifacts.
- **NF-2** — `_item` reuses the Stage-3 card handle (`card_feed.card_handle`) verbatim and builds `sourceTrail` /
  `sourceIds` straight from the served record's web-safe evidence drawer (`_ids_from_evidence`, `_source_trail`); orphans
  are filtered by `classify_orphan`. The auditor re-derives the served-record→(card handle, source set) ground truth
  **independently from `card_feed` + the evidence drawer** and diffs the feed against it.
- **NF-3** — every feed function is a `SELECT`-only read of the read surface; `_source_trail` hard-codes
  `localSourcePath: None`; every artifact is `_assert_local_safe`-swept. The auditor content-hashes the raw tables around
  the build to *prove* no write occurred.

---

## 3. Reviewer-internal preservation overlay (auditor output shape)

```jsonc
{
  "scope": "alpine",
  "access": "reviewer_internal",          // never "public" — public is GOV-420 / Isaac-gated
  "reproducible": true,                    // NF-1
  "provenance_ok": true,                   // NF-2
  "raw_mutation_ok": true,                 // NF-3
  "item_count": <int>,
  "feed_digest": "<sha256 of the canonical feed JSON>",   // opaque reproducibility fingerprint, envelope-level only
  "violations": { "reproducibility": [...], "provenance": [...], "raw_mutation": [...] }  // empty on a clean corpus
}
```

`feed_digest` is the **single opaque fingerprint** of the canonical feed (envelope-level only — never a per-item hash,
never a path). The whole overlay is swept by `read_api.assert_no_raw_paths`.

---

## 4. Auditor shape (impl, same issue)

Additive module `scripts/stage4_newsletter_preservation_audit.py` (the GOV-347 / GOV-367 separate-additive-module
precedent — `read_api.py` / `publication.py` / `stage4_newsletter_feed.py` all stay 0-diff). Public API:

- `assert_reproducible(conn) -> str` — build each artifact twice; raise `NewsletterReproducibilityError` if any pair
  differs byte-wise. Returns the canonical `feed_digest`.
- `provenance_violations(conn) -> list[dict]` — independently index `{card_handle: sorted(sourceIds)}` from
  `reviewer_internal_records` + `card_feed` (NOT from the feed builder), then return one entry per emitted item that
  (a) has a `cardIds[0]` absent from the index (fabricated item), or (b) whose `sourceIds` / `sourceTrail` source set
  diverges from the index (lossy / invented linkage). Empty list = NF-2 holds.
- `raw_mutation_violations(conn, build=...) -> list[dict]` — content-hash `statements` / `evidence_links` / `sources`,
  run `build` (default: all three artifacts), re-hash; return one entry per table whose digest changed. Empty = NF-3.
- `build_preservation_overlay(conn) -> dict` — assemble the §3 overlay and route it through
  `read_api.assert_no_raw_paths`. Fail-closed.
- CLI `--db` → prints the overlay JSON; exit 0 clean, non-zero on a raised invariant.

---

## 5. RED test list (write first, must fail before the auditor exists, pass after)

1. **Intact corpus** — `reproducible`/`provenance_ok`/`raw_mutation_ok` all true; overlay `access == reviewer_internal`;
   overlay passes `assert_no_raw_paths`.
2. **NF-1 load-bearing** — neuter `build_newsletter_feed` to return a nondeterministic value → `assert_reproducible`
   raises (a tautological check would still pass).
3. **NF-2 load-bearing (fabricated item)** — inject a feed item with a `cardIds`/`sourceIds` no served record backs →
   `provenance_violations` flags it.
4. **NF-2 load-bearing (lossy linkage)** — drop / add a source on an emitted item's trail → flagged.
5. **NF-3 load-bearing** — a `build` callable that writes to a raw table → `raw_mutation_violations` flags that table;
   and the clean build leaves all three tables byte-stable.
6. **No-leak** — fixture plants raw vault paths on every evidence link; none cross the overlay; `localSourcePath` null;
   no 64-hex hash per item; overlay passes the transport guard.
7. **Extend-not-fork** — `read_api.py` / `publication.py` / `stage4_newsletter_feed.py` byte-0-diff vs `origin/main`;
   the auditor imports the feed module + SSOT guards by reference and re-declares none of their constants.
8. **CLI smoke** — `--db` over a seeded corpus exits 0 and prints a reviewer-internal overlay.

---

## 6. Risk gate

- **Alpine-only, reviewer-internal-only.** No public lane, no email/sender, no naming non-officials, no non-Alpine
  coverage. Public deploy stays GOV-420 / Isaac-gated and does **not** block this work.
- **0-diff public contract.** `read_api.py` / `publication.py` / `stage4_newsletter_feed.py` are consumed read-only.
- **No new crawl / no schema change / no mutation.** The auditor is a pure read-time check; NF-3 *proves* it.
- **AI output is never primary evidence;** the auditor reads only grounded, reviewer-cleared records off the read surface.
- **Review gate:** both legs (VSR + SecPriv no-leak / no-public-surface) PASS before a non-author merge — reuse the
  GOV-450 / GOV-451 / GOV-452 pattern.
