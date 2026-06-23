# Stage 5.05 — Watchdog Signals Layer Contract (GOV-520)

*Reviewer-internal · Alpine-only. Public launch stays gated on Isaac via GOV-420.*

**Owner:** BackendCrawlerEngineer · **Parent contract:** GOV-519 (CTO, comment
`11c34e6a`) · **Predecessors consumed:** Stage 5.03 inventory (GOV-484), Stage 5.04
record verifier (GOV-488), Stage 4.03 newsletter feed (GOV-449).

This is the Stage-5 **Watchdog signals layer**: three deterministic, idempotent
reviewer-internal *signal envelopes* derived purely from the already-merged registry —
no AI, no network, no new claims. Each envelope is a **presentation-precursor
projection over the existing read surface, NOT a new source of truth**. The frontend /
render layer is **5.06** (out of scope here — this is the backend data contract only).

Reference module: [`scripts/stage5_watchdog_signals.py`](../scripts/stage5_watchdog_signals.py).
Reference driver: [`tests/test_gov520_stage5_watchdog_signals.py`](../tests/test_gov520_stage5_watchdog_signals.py).

---

## §0 — Scope lock

- **Alpine only.** Broader jurisdictions stay *planned*, never implied.
- **Reviewer-internal only.** Every emitted artifact is tagged
  `access: reviewer_internal` / `scope: alpine`. No public / `published_records` path
  emits any Watchdog signal key.
- **Additive, no fork.** A *separate additive module* layered on top of the already-
  web-safe read surface (`read_api.reviewer_internal_records`), the merged 5.03
  inventory (`stage5_source_inventory.build_inventory`), and the Stage-3 card-status
  vocabulary (`stage3_card_feed`). It **never** mutates or re-derives the six prod
  modules it consumes by reference (I4): `read_api.py`, `publication.py`,
  `stage4_newsletter_feed.py`, `stage4_newsletter_digest_assembler.py`,
  `stage5_source_inventory.py`, `stage5_record_verifier.py`.

---

## §1 — `correctionsLedger` (typed correction edges)

One typed correction edge per **corrected record**. A served record is a *corrected
record* when EITHER its claim-level `correction_status ∈ {replaced, superseded,
corrected, amended}` (`CORRECTION_ACTIVE`) OR one of its evidence sources has 5.03
`lifecycle == replaced`.

The edge points the corrected `known_then` record at its **superseding
record/document**, resolved by inverting the forward-only `updates_statement_id`
correction spine (migration 0007 §D-4): a `corrected_later` row carries
`updates_statement_id` pointing back at the `known_then` row it supersedes. The edge
**preserves the known-then context** (`knownThen.{status,recordDate,sourceConfidence}`)
verbatim — it is never rewritten.

- **Resolved** → `supersedingRef` (the superseding record's card handle) is set,
  `resolved: true`, `gaps: []`.
- **Unresolved** (no superseding record served) → fail-closed `gaps:
  ["correction_unresolved"]`, `supersedingRef: null`, `resolved: false`. **Never a
  fabricated ref.**

Order is by corrected statement id (byte-stable).

## §2 — `hotTopics` (deterministic salience)

A salience score per topic/issue anchor. The **salience unit** is resolved per served
record by `_record_topic_anchors`: any explicit evidence `topic_id` (the direct topic
edge — sparse today), else the record's `agenda_item_id` (the agenda thread the claim
sits in — Isaac's concept map: "agenda item references topic"). A record with neither
anchor is uncategorized and contributes to no topic (an honest gap, never invented).

Score is **pure arithmetic** (`salience_score`), no AI / editorializing:

```
salienceScore = 3·activityCount + 2·recencyCount + 5·correctionChurn
```

- **activityCount** — records anchored to the topic.
- **recencyCount** — those whose newest evidence `scan_date` falls within
  `RECENCY_WINDOW_DAYS` (31) of the **corpus's own newest scan date** (the anchor is the
  data, NOT a wall clock — so the score is a pure function of the DB; idempotent).
- **correctionChurn** — those that are corrected records (per §1's trigger set).

Ranked `(salienceScore desc, topicId asc)`. A topic below `INSUFFICIENT_DATA_FLOOR` (2
activity items) is still emitted (a gap is never hidden) but labelled
`insufficientData` so a thin topic is never ranked as a confident "hot" signal.

## §3 — `watchdogView` (Kanban-precursor lanes)

A composed status surface over **verified / source-linked records only** (the read
surface already drops orphans). Each entry carries a lane, composed status, recordDate,
derived `sourceConfidence` (the GOV-283 read-time label), and fail-closed `gaps[]`.

Lane vocabulary (`WATCHDOG_LANES`, frozen) — most-specific-signal-wins (`derive_lane`):

1. `correction` — the record is a corrected record (in §1's ledger);
2. `follow-up` — its source changed (`source_changed` truthy OR source 5.03
   `lifecycle == changed`) — the citation needs a re-check;
3. `decided` — composed status `verified` (a settled record);
4. `pending-decision` — an `ai_presented` observation awaiting a human decision;
5. `active` — anchored to a meeting thread but not yet decided;
6. `upcoming` — the source-linked floor (no stronger signal yet).

Gap labels: `correction_unresolved` (unresolved §1 edge), `archive_unavailable`
(source 5.03 `snapshotAvailability == not_available`), `low_confidence` (the GOV-283
conservative label — a thin source honestly flagged, never hidden).

---

## §4 — Boundary invariants (premium I1–I8)

- **I1** — every emitted artifact is transport-swept by `read_api.assert_no_raw_paths`;
  a FS path / `.sha256` / vault marker / `file://` that slipped a column fails LOUDLY.
- **I2** — `localSourcePath` is never emitted.
- **I3** — exactly one hash, the envelope `watchdogDigest`; no per-source raw hash.
- **I4** — the six prod modules are consumed **by reference**, byte-0-diff.
- **I5** — RED-proof is NON-tautological: neutering `resolve_correction_edge` drops a
  genuinely-resolvable edge to its fail-closed gap, and neutering `salience_score`
  flattens the ranking — each makes a specific test go RED **while the read surface
  still serves the same records** (the signal layer adds genuine derivation, not a
  re-echo). Guards: `assert_lanes_valid`, `assert_corrections_resolved_or_gapped`,
  `assert_hot_topics_ranked`, `assert_single_envelope_digest`.
- **I6** — `access: reviewer_internal` / `scope: alpine`; absent from any public /
  `published_records` path. Public launch stays Isaac-gated (GOV-420), untouched.
- **I7** — additive, no fork.
- **I8** — full suite green (≥930 baseline + new), exit 0.

## §5 — CLI / CI gate

```
python3 scripts/stage5_watchdog_signals.py --db <db> [--check]
```

`--check` runs every load-bearing guard and is the **CI gate**: exit 0 when sound,
exit 1 on any defect (out-of-vocab lane, unflagged unresolved correction, mis-ordered
ranking, stray per-source hash).

## §6 — Disposition

Author marks done + opens PR off `origin/main` (delegated lane — **no self-merge**).
Review lane: VSR (adversarial) ‖ SecPriv (no-leak) → CTO 3-of-3 non-author squash-merge.
Scope / owner / publication decisions escalate to CEO.
