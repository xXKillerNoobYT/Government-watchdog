# Stage 4.07 — Digest Statement→Exact-Source Binding Contract + Validator over the 4.05 Digest

> **Issue:** GOV-467 (Stage 4.07 · TranscriptEvidenceEngineer / backend-deterministic side). **Orchestrator + non-author merge gate:** CTO GOV-466. **Sequenced after:** GOV-457 (Stage 4.05 digest assembler, merged `origin/main` PR #79 / HEAD `cf61ea5`).
> **Stage:** 4.07 — `contract → validator → RED tests`, all in this owner-authored issue (mirrors the 4.03 GOV-449 / 4.04 GOV-453 / 4.05 GOV-457 bundled split).
> **Scope:** Town of Alpine only · reviewer-internal · no public launch · no email/sender · no signup/auth · no person-naming · no new crawl · no ingestion-scope change · **no editorial prose / no AI-generated statements (that is 4.08)**.
> **Grounded on:** canonical remote `origin/main` HEAD `cf61ea5`.
> **Goal of record:** Stage 4 parent (reviewer-internal newsletter backbone); 4.08–4.15 stay `planned` until later gates pass.
> **Inputs of record (read-only grounding, no edit by this child):**
> - `scripts/statements.py` @ `cf61ea5` (GOV-82, Stage-2 exact-source model) — the upstream exact-source discipline this validator re-proves one layer up. Anchors: `is_orphan`, `validate_pointer`, `LOCATOR_REQUIRED_FIELDS`, `ALLOWED_LOCATOR_KINDS`, `PointerError`, `OrphanClaimError`. **Consumed read-only; stays 0-diff.**
> - `scripts/stage4_newsletter_digest_assembler.py` @ `cf61ea5` (GOV-457) — the 4.05 digest this validator *consumes* (`assemble_digests`). **Consumed read-only; stays 0-diff.**
> - `scripts/stage4_newsletter_feed.py` @ `cf61ea5` (GOV-449) — `SCOPE`, `ACCESS`, `STAGE3_CLAIM_VOCAB`, `VSR`. **Imported by reference; stays 0-diff.**
> - `scripts/stage3_card_feed.py` @ `cf61ea5` (GOV-347) — `card_handle`, `_resolve_record_type`, `_compose_record_status`. **The forward card→statement index + the independent label recompute reuse these; stays 0-diff.**
> - `scripts/read_api.py` @ `cf61ea5` — `assert_no_raw_paths` (`:125`), `RAW_PATH_MARKERS` (`:56`), `RawPathLeak` (`:91`), `_evidence_links_for` (`:154`), `_segment_resolves` (`:163`), `reviewer_internal_records` (`:509`). **Consumed read-only; stays 0-diff.**
> - `scripts/publication.py` @ `cf61ea5` — public-contract surface. **Not imported; stays 0-diff.**

This document pins the **deterministic statement→exact-source binding contract** for the Stage 4 reviewer-internal
Alpine newsletter digest: *every digest item that surfaces a statement/quote/claim must bind to its **exact-source
evidence** — a pointer to the exact transcript segment / source document / meeting record — reusing the Stage-2
exact-source model. No statement may be presented without an exact-source pointer; a paraphrase is never presented as
verbatim.* The exact-source discipline already exists **upstream**: `statements.py` enforces no-orphan-claims +
complete-valid-pointer at **write** time, and `read_api.py` enforces it at **serve** time (`read_api.py:19` — "No orphan
claim is served"). **4.07 is the deterministic validator one layer up over the assembled digest** — a defense-in-depth
regression net proving the digest never *loses* the statement→exact-source binding, exactly as 4.04
(`stage4_newsletter_preservation_audit.py`) sat one layer up over the feed.

The validator + RED tests in the same issue build the projection per §4–§5. A contract defines the shape; it does not
satisfy it.

---

## 0. What this child owns, and what it must not touch

**Owns:** the statement→exact-source binding rule (§1), the exact-source pointer-resolution rule over the digest (§2),
the reviewer-internal statement-link validation log shape (§3), the validator API + audit overlay shape (§4), the RED
test list (§5), the risk gate (§6).

**Must NOT touch / re-derive (carry Stage 2/3/4 forward):**
- the `statements.py` exact-source model — **consumed, never forked**: the validator *calls* `is_orphan` /
  `validate_pointer` and reuses `LOCATOR_REQUIRED_FIELDS`; it never re-types the locator kinds, the pointer-required set,
  or the no-orphan disjunction;
- the `stage4_newsletter_digest_assembler.assemble_digests` projection — **consumed, never re-assembled**: the validator
  groups nothing and re-derives no item, label, or `sourceTrail`;
- the `read_api.assert_no_raw_paths` transport guard + `RAW_PATH_MARKERS` — **reused as the backstop**, `read_api.py` 0-diff;
- the Stage-3 claim vocabulary (`STAGE3_CLAIM_VOCAB`) — **imported by reference**; the label-conservatism check classifies
  existing labels, it never mints a new label (EG-7 stays intact one layer up);
- the reviewer-internal vs public lane separation (GOV-146 / GOV-347 / GOV-420) — **reused, never forked**. Public stays
  Isaac-gated (GOV-420); this validator never emits a public lane, and emits **no AI prose** (4.08 is the gated layer).

---

## 1. Statement→exact-source binding rule (no orphan claim, paraphrase ≠ verbatim)

A **statement-bearing digest item** is any item in a digest's `items[]` — each is projected (GOV-449) from exactly one
served reviewer-internal statement record, carried verbatim through the 4.05 grouping. The binding rule, reusing the
Stage-2 exact-source model (`statements.py`) verbatim:

- **Exact-source pointer (no orphan).** A statement-bearing item resolves to its real statement record and **must** carry
  an exact-source pointer: a **resolving `segment_id` segment edge** (`statement_from_segment`, resolving to a
  `transcript_segments` row) **OR ≥1 `evidence_link` with a complete, valid pointer** per `statements.validate_pointer`
  (the locator field matching `locator_kind` is present, per `statements.LOCATOR_REQUIRED_FIELDS`, and `to_source_id`
  resolves to a registry `sources` row). A statement-bearing item with **neither** is an **orphan** — and is **routed to
  VSR, never silently dropped**. This is strictly stronger than the serve gate: `read_api` serves a statement whenever its
  `evidence_links` list is non-empty (it never re-checks pointer *completeness*), so a served statement whose only link has
  an *incomplete* pointer is bound by serving but caught as an orphan here.
- **Conservative label (never silently upgraded to verified).** A `speaker_unidentified` / `unverified` / `disputed`
  statement carries the correct Stage-3 label and is **never styled as verified fact**. The item's claim-axis and
  speaker-axis labels must be members of `STAGE3_CLAIM_VOCAB` (the conservative vocabulary), and the item's `claimStatus`
  must equal the claim status **independently recomputed from the live read surface**
  (`stage3_card_feed._compose_record_status` over the re-served record) — so a digest item silently upgraded to `verified`
  is caught (the recompute still yields the true, conservative status).
- **Paraphrase ≠ verbatim.** A statement whose record is **verbatim-styled** (`is_verbatim` truthy) must bind to a
  **verbatim anchor**: a resolving `segment_id` segment edge **OR** an `evidence_link` carrying non-empty `quoted_text`
  (the `char_span` exact-quote anchor). A verbatim-styled statement with neither is a verbatim-overclaim — a paraphrase
  presented as a verbatim quote — and is flagged.

---

## 2. Exact-source pointer resolution over the digest (forward index, never a reverse-hash)

The digest item carries `cardIds[0]` — a **one-way** `card_handle = sha256(card_type ␟ statement_id)[:40]`, not the raw
`statement_id`. The validator therefore resolves a digest item to its statement **forward**, never by reversing the hash:

- **Forward card→statement index.** Iterate `read_api.reviewer_internal_records(conn)`; for each served record compute
  `card_handle(_resolve_record_type(record), record["statement_id"])` — the *same* derivation the feed used to assign
  `cardIds` — and map that handle to the record's `statement_id`. The handles match the digest's `cardIds` by
  construction (no parsing, no reverse).
- **Resolve to the canonical statement.** Look the item's `cardIds[0]` up in the index to get its `statement_id`, then
  read the **raw** `statements` row + raw `evidence_links` (`read_api._evidence_links_for`) from the DB — the canonical
  exact-source columns, which `to_web_safe` strips from any served body. A `cardIds[0]` with no index entry is itself an
  unresolved orphan (routed to VSR).
- **pointerKind.** `"segment"` when the segment edge resolves; else the `locator_kind` of the first valid pointer; else
  `null` (orphan). Reading raw DB columns is reviewer-internal and **never emitted** — only the §3 log/overlay (slugs +
  enums, transport-swept) crosses any boundary.

---

## 3. Reviewer-internal statement-link validation log shape

The **statement-link validation log** (§3) — one row per statement-bearing digest item:

```jsonc
{
  "scope": "alpine",
  "access": "reviewer_internal",          // never "public" — public is GOV-420 / Isaac-gated
  "rows": [
    {
      "itemId": "alpine-newsletter-item-001",
      "statementId": "stmt-1",            // a slug (read_api-web-safe), never a raw path / 64-hex hash
      "pointerKind": "page",              // "segment" | a statements.ALLOWED_LOCATOR_KINDS member | null (orphan)
      "resolves": true,                   // bound to an exact-source pointer
      "label": "unverified",              // the item's conservative claimStatus (a STAGE3_CLAIM_VOCAB member)
      "route": null                       // VSR for an orphan; null when bound
    }
  ],
  "routing": [ /* one entry per orphan: {itemId, statementId, reason, routedTo: "VerificationSafetyReviewer", status: "held"} */ ],
  "passed": true                          // zero UNROUTED orphans (an orphan is always routed, never dropped)
}
```

The **audit overlay** (the `--check` / CLI summary, GOV-453 / GOV-457 precedent) is a separate, swept envelope:

```jsonc
{
  "scope": "alpine",
  "access": "reviewer_internal",
  "statement_item_count": <int>,
  "bound_count": <int>,
  "orphan_count": <int>,
  "all_bound": true,                  // EG-2 — every statement-bearing item carries an exact-source pointer
  "no_unrouted_orphans": true,        // EG-4 — every orphan (if any) is routed to VSR, never dropped
  "labels_conservative": true,        // non-verified statements keep their conservative Stage-3 label; no silent upgrade
  "verbatim_anchored": true,          // every verbatim-styled statement has a segment / quoted_text anchor
  "binding_digest": "<sha256 of the canonical validation log>",  // single opaque envelope fingerprint
  "violations": { "orphans": [...], "routing": [...], "labels": [...], "verbatim": [...] }
}
```

`binding_digest` is the **single opaque fingerprint** of the canonical log (envelope-level only — never a per-item hash,
never a path). Both artifacts (no `/alpine/` route links — ids + enums + a single fingerprint only) are swept by the
stricter `read_api.assert_no_raw_paths`; a raw vault path / `file://` / `.sha256` / `transcript_path` fails LOUDLY.

---

## 4. Validator shape (impl, same issue)

Additive module `scripts/stage4_statement_evidence_binding.py` (the GOV-347 / GOV-367 / GOV-453 / GOV-457
separate-additive-module precedent — `statements.py` / `read_api.py` / `publication.py` /
`stage4_newsletter_digest_assembler.py` / `stage4_newsletter_feed.py` all stay 0-diff). Public API:

- `statement_index(conn) -> dict[str, str]` — the forward `card_handle → statement_id` map (§2), built from
  `reviewer_internal_records` via `card_handle` / `_resolve_record_type`. Pure function of the read surface.
- `statement_link_validation(conn, out=None) -> dict` — the §3 log: one row per statement-bearing digest item, orphans
  routed to VSR, transport-swept. `out` defaults to `assemble_digests(conn)`.
- `assert_every_statement_bound(conn, out=None) -> bool` — EG-2: raise `OrphanStatementError` if any statement-bearing
  item lacks an exact-source pointer.
- `assert_no_unrouted_orphans(log) -> bool` — EG-4: raise `UnroutedOrphanError` if any orphan row is not routed to VSR.
- `assert_labels_conservative(conn, out=None) -> bool` — raise `LabelUpgradeError` if any item's claim/speaker axis label
  is outside `STAGE3_CLAIM_VOCAB`, or its `claimStatus` ≠ the independently recomputed read-surface status (silent upgrade).
- `assert_verbatim_anchored(conn, out=None) -> bool` — raise `VerbatimAnchorError` if any verbatim-styled statement lacks
  a segment / `quoted_text` anchor.
- `assert_reproducible(conn) -> str` — build the log twice; raise `BindingReproducibilityError` if the canonical JSON
  differs. Returns the canonical `binding_digest`.
- `build_binding_overlay(conn) -> dict` — run all guards (fail-closed, collect rather than raise), assemble the §3
  overlay, route it through `read_api.assert_no_raw_paths`.
- CLI `--db` `[--artifact log|overlay]` `[--check]` → prints the log (default) or the overlay; `--check` runs the guards;
  exit 0 clean, non-zero on a raised invariant (an orphan → non-zero).

The module imports `statements`, `read_api`, `stage3_card_feed`, `stage4_newsletter_feed`, and
`stage4_newsletter_digest_assembler` **by reference** and re-declares none of their constants (`SCOPE` / `ACCESS` /
`STAGE3_CLAIM_VOCAB` / `VSR` / `LOCATOR_REQUIRED_FIELDS` / `RAW_PATH_MARKERS` are all reused).

---

## 5. RED test list (write first, must fail before the validator exists, pass after)

1. **Intact corpus** — `statement_link_validation` returns `scope=alpine` / `access=reviewer_internal`, one row per
   statement-bearing item, every row `resolves=true` with a non-null `pointerKind`, `passed=true`; the log passes
   `assert_no_raw_paths`.
2. **EG-2 every-statement-bound load-bearing** — `assert_every_statement_bound` passes on the real digest; poisoning one
   served statement's only pointer to be *incomplete* (a page locator with null `page`, no segment edge — still served by
   `read_api`) makes it raise `OrphanStatementError` and the log route that item to VSR (proves the defense-in-depth gap
   over the serve gate, non-tautological).
3. **EG-4 orphan-routing load-bearing** — `assert_no_unrouted_orphans` passes on the real log; a hand-built log with an
   orphan row whose `route` is not VSR raises `UnroutedOrphanError`.
4. **Label-conservatism load-bearing** — `assert_labels_conservative` passes on the real digest; mutating one item's
   `claimStatus` to `verified` (when the read surface recomputes `unverified`) raises `LabelUpgradeError`; a claim label
   outside `STAGE3_CLAIM_VOCAB` also raises.
5. **Paraphrase ≠ verbatim load-bearing** — `assert_verbatim_anchored` passes on the real digest (the verbatim record is
   `quoted_text`-anchored); flipping that record's anchor to a bare page pointer (valid, but no `quoted_text` / segment)
   raises `VerbatimAnchorError`.
6. **Reproducibility** — `assert_reproducible` returns a `binding_digest`; the log is byte-identical across two builds.
7. **No-leak** — fixture plants raw vault paths on every evidence link; none cross the log or the overlay; `statementId`
   is a slug, never a path; no per-item 64-hex hash (only the single `binding_digest`); both pass the transport guard.
8. **Extend-not-fork** — `statements.py` / `read_api.py` / `publication.py` /
   `stage4_newsletter_digest_assembler.py` / `stage4_newsletter_feed.py` byte-0-diff vs `origin/main`; the validator
   imports those modules + their constants by reference and re-declares none.
9. **CLI smoke** — `--db` over a seeded corpus exits 0 and prints a reviewer-internal log; `--artifact overlay --check`
   exits 0 and prints a reviewer-internal overlay with `all_bound` / `no_unrouted_orphans` / `labels_conservative` /
   `verbatim_anchored` true. Feeds exit-gate EG-2 / EG-4 / EG-7.

---

## 6. Risk gate

- **Alpine-only, reviewer-internal-only.** No public lane, no email/sender, no signup/auth, no naming non-officials, no
  non-Alpine coverage. Public deploy stays GOV-420 / Isaac-gated and does **not** block this work.
- **No editorial prose, no AI-generated statements.** This slice is the deterministic statement→evidence **binding +
  validation** only — structured data, never prose/markup (4.06) and never AI output / editorial voice (4.08). AI output
  is never primary evidence; AI items keep their Stage-3 unverified label and are flagged, never styled as verified fact.
- **0-diff public contract.** `statements.py` / `read_api.py` / `publication.py` /
  `stage4_newsletter_digest_assembler.py` / `stage4_newsletter_feed.py` are consumed read-only.
- **No new crawl / no schema change / no mutation.** The validator is a pure read-time projection over the digest + the
  canonical record store; it reads raw exact-source columns reviewer-internally and emits only the swept log/overlay.
- **Review gate:** both legs (VSR GOV-468 + SecPriv GOV-469 no-leak / no-public-surface) PASS before the CTO (GOV-466)
  non-author merge — reuse the GOV-458 / GOV-459 / GOV-460 pattern.
