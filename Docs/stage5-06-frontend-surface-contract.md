# Stage 5.06 — Frontend/Product Surface Contract (GOV-524)

*Reviewer-internal · Alpine-only. Public launch stays gated on Isaac via GOV-420.*

**Owner:** FrontendTimelineEngineer · **JIT-created by** CTO at the 5.05 merge gate
(GOV-523) · **Depends on:** Stage 5.05 Watchdog signals (GOV-520, merged to main
`6616d3b`). Per GOV-483 §C linear backbone (CEO-accepted — no 5.06/5.07/5.09 fan-out).

This is the Stage-5 **frontend/product surface contract**: the deterministic, idempotent
*presentation view-model* a reviewer UI renders over the three Stage 5.05 signal envelopes.
It is a **data/presentation contract, NOT a public launch and NOT a renderer** — it emits
structured presentation nodes (cards / badges / board columns), never HTML, never a public
feed. Isaac's concept-map directive is the spine: *cards are presentation nodes over the
graph, not the source of truth*, so every surface here is a **read-only projection that
adds labelling and grouping, never a new claim**.

Reference module: [`scripts/stage5_frontend_surface.py`](../scripts/stage5_frontend_surface.py).
Reference driver: [`tests/test_gov524_stage5_frontend_surface.py`](../tests/test_gov524_stage5_frontend_surface.py).

---

## §0 — Scope lock

- **Alpine only.** Broader jurisdictions stay *planned*, never implied.
- **Reviewer-internal only.** The body is tagged `access: reviewer_internal` /
  `scope: alpine`. No public / `published_records` path emits any surface key.
- **Additive, no fork.** A *separate additive module* layered on top of the already-
  web-safe 5.05 envelope (`stage5_watchdog_signals.build_signals`) and the merged read
  surface (`read_api.reviewer_internal_records`). It **never** mutates or re-derives the
  seven prod modules it consumes by reference (I4): `read_api.py`, `publication.py`,
  `stage4_newsletter_feed.py`, `stage4_newsletter_digest_assembler.py`,
  `stage5_source_inventory.py`, `stage5_record_verifier.py`, `stage5_watchdog_signals.py`.

---

## §1 — `correctionsSurface` (correction cards)

One correction *card* per 5.05 `correctionsLedger` edge, in the ledger's (byte-stable)
order. Each card:

- preserves the corrected `known_then` context **verbatim** — `knownThen.{status,
  statusBadge, recordDate, confidenceBadge}` (never rewritten);
- carries `correctionStatusBadge` (exact display text — `Replaced` / `Superseded` /
  `Corrected` / `Amended`, fail-closed to the raw code);
- when **resolved**: `supersedingRef` set, `resolutionBadge: "Superseding record linked"`,
  `gapBadges: []`;
- when **unresolved**: `supersedingRef: null`, `resolutionBadge: "Unresolved"`, and the
  fail-closed `correction_unresolved` gap rendered as a **visible gap badge**
  (`"Correction unresolved — superseding record not yet in registry"`). **A gap is never
  hidden** — that is the §1 promise.

## §2 — `hotTopicsSurface` (ranked topic cards)

The 5.05 `hotTopics` ranking presented as ranked topic cards (`rank` follows the
envelope's already-deterministic salience order). Each card surfaces the raw counts +
`salienceScore` and a `salienceBadge` (`Ranked`, or `Insufficient data — below activity
floor` for a topic under the floor — a thin topic is never presented as a confident hot
signal).

**Honest topic-anchor disclosure** (`classify_topic_anchor`): each card declares whether
its anchor is a real evidence `topic_id` edge (`topic_edge`) or the `agenda_item_id`
fallback (`agenda_thread`). Per VSR GOV-521, `topic_id` is structurally absent today, so
**every anchor resolves to `agenda_thread`** and the disclosure says so plainly — the
surface **never implies a topic edge that isn't in the data**.

## §3 — `watchdogBoard` (Kanban-precursor columns)

The 5.05 `watchdogView` grouped into the six frozen lanes, presented as board columns in
canonical order: `upcoming → active → pending-decision → decided → follow-up →
correction`. **All six columns are emitted** — an empty lane is shown as an empty column
(a board never hides an empty lane). Each column carries its `laneLabel` + `cardCount`;
each card carries `statusBadge`, `confidenceBadge` (the GOV-283 read-time label), and
visible `gapBadges`. Cards within a lane are ordered by statement id (byte-stable).

The status badge is the deterministic fail-closed mapping of the composed status
(`status_badge`): an unknown / non-verified status NEVER wears the `Verified` badge.

---

## §4 — Boundary invariants (premium I1–I8)

- **I1** — every emitted artifact is transport-swept by `read_api.assert_no_raw_paths`;
  a FS path / `.sha256` / vault marker / `file://` that slipped a column fails LOUDLY.
- **I2** — `localSourcePath` is never emitted.
- **I3** — exactly one hash, the envelope `surfaceDigest`; no per-source raw hash.
- **I4** — the seven prod modules are consumed **by reference**, byte-0-diff.
- **I5** — RED-proof is NON-tautological. Two load-bearing derivations:
  - neutering `present_gap_badges` (→ `[]`) makes `assert_gaps_visible` go RED — a gap the
    5.05 envelope still carries vanishes from the surface;
  - neutering `classify_topic_anchor` (→ always `topic_edge`) makes
    `assert_topic_anchors_honest` go RED — the surface claims a topic edge absent from the
    evidence graph, while the read surface + hotTopics envelope are byte-unchanged.
  Each RED fires **while the read surface still serves identical data** (the surface layer
  adds genuine derivation, not a re-echo). Guards: `assert_no_false_verified`,
  `assert_gaps_visible`, `assert_topic_anchors_honest`, `assert_board_complete`,
  `assert_single_surface_digest`, `assert_reviewer_internal`.
- **I6** — `access: reviewer_internal` / `scope: alpine`; absent from any public /
  `published_records` path. Public launch stays Isaac-gated (GOV-420), untouched.
- **I7** — additive, no fork; same DB → byte-identical surface (idempotent).
- **I8** — full suite green (958 passed = 946 baseline + 12 new), exit 0.

## §5 — CLI / CI gate

```
python3 scripts/stage5_frontend_surface.py --db <db> [--check]
```

`--check` runs every load-bearing guard and is the **CI gate**: exit 0 when sound, exit 1
on any defect (false-verified badge, hidden gap, dishonest anchor, dropped/duplicated
board card, missing lane column, stray per-source hash, wrong access tag).

## §6 — UI viewport note (not yet exercisable)

Per COMPANY.md / FRONTEND_TIMELINE_WORKFLOWS, any *rendered* reviewer UI must be verified
across desktop 1440×900 + tablet 768×1024 + mobile 390×844. **This slice emits the
presentation data contract only — there is no rendered surface to screenshot yet**, so the
three-viewport floor is owned by the future render slice that consumes this contract. Named
here so the gap is explicit, not silently skipped.

## §7 — Disposition

Author marks done + opens PR off `origin/main` (delegated lane — **no self-merge**).
Review lane: VSR `3f95c8ce` (adversarial) ‖ SecPriv `72d0eccf` (no-leak) → CTO 3-of-3
non-author squash-merge (mirrors GOV-521/522/523). Scope / render-distribution / owner /
publication decisions escalate to CEO.
