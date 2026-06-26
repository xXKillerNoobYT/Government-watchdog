# Stage 5.13 — Back-gap / regression analysis contract (GOV-574)

**Owner:** BackendCrawlerEngineer · **Reviewers:** VSR (GOV-576) + SecurityPrivacyAgent
(GOV-577) · **Merge:** non-author CTO squash · **Scope:** Alpine, reviewer-internal, NOT
public (public stays Isaac-gated, GOV-420). · **Module:** `scripts/stage5_backgap.py` ·
**Tests:** `tests/test_gov574_stage5_backgap.py` · **Goal:** `e454596f` (Stage 5.13 —
Back-gap/regression analysis).

> **Repair provenance (GOV-578).** This contract replaces a phantom spec. GOV-574 formerly
> pointed at contract commit `a1fe3d8` (does not exist), branch `stage4-automation-ai-boundary`
> (does not exist), and a JavaScript deliverable (`src/backgap-regression.js` + `node --test`)
> in a repo that has **zero `.js` files, no `package.json`, no `src/`, no `test/`** — it is
> Python/pytest (`pytest.ini`, `scripts/`, `tests/`, 1006 passing pytest tests through Stage
> 5.10). This contract re-grounds 5.13 in the **twice-shipped Python back-gap pattern that
> already ships in this exact repo**: `scripts/stage2_backgap.py` (GOV-322, Stage 2.13) and
> `scripts/stage3_backgap.py` (GOV-411, Stage 3.13). 5.13 is that win **one layer up**, over
> the merged Stage-5 trust substrate.

The Stage 5.13 IMPL head. A deterministic, idempotent, read-only, reviewer-internal
**auditor** that finds gaps left behind by Stage-5 work (Axis A — point-in-time back-gap)
and prevents regressions against a pinned baseline (Axis B — monotonicity). No AI, no
network, additive only. Every pre-existing serving module stays **byte-0-diff**. Mirrors
the GOV-322 / GOV-411 design: independently recompute "should-be-served" membership and
reconcile it against what the live surface actually serves — never compare the assembly to
itself.

## Substrate audited (read-only, never mutated; reused by reference, never forked)

| Source | Used for |
|---|---|
| `stage2_backgap.reviewer_eligible_ids(conn)` | the proven membership oracle — the independently-recomputed reviewer-eligible set (mirrors the read_api gate over canonical columns + SSOT leaf predicates, NOT any assembly loop) |
| `stage2_backgap.publish_eligible_ids(conn)` | the publish-eligible oracle (Alpine reviewer-internal → empty by construction; guards a future silent public-lane gain) |
| `read_api.published_records` / `read_api.completeness_gap_cards` / `read_api.assert_no_raw_paths` | the public-lane set, the canonical completeness-gap rows, the transport boundary |
| `stage5_source_inventory.build_inventory` (5.03) | source lifecycle (`unchanged`/`changed`/`disappeared`/`replaced`) + archive-availability envelope |
| `stage5_record_verifier.build_verified_record` (5.04) | per-record verification state (`verified` vs unverified) |
| `stage5_trust_model.build_trust_model` (5.07) | corrections spine, hot-topic reasons, source-change↔archive binding, assumption verifications |
| `stage5_watchdog_signals.build_signals` (5.05) | corrections ledger, hot-topics, watchdog lanes |
| `stage5_frontend_surface.build_surface` (5.06) | the presentation view-model (the outermost served surface) |

## I/O model (binding — this is the repo's actual model, not the phantom spec's)

Every entry point takes a **sqlite `conn`** and reads via `read_api` / `db`, exactly like
every Stage-5 substrate module. There is **no** in-memory-array signature
(`analyzeBackGapRegression({sources, statements, priorSnapshot, expectedCoverage, now})`
from the phantom spec is void — `priorSnapshot`/`expectedCoverage` schemas were defined
nowhere on disk and so could only be invented). The "prior snapshot" is a **committed
golden baseline JSON** loaded via `--baseline PATH` (see Axis B). Determinism is achieved
the repo way: SELECT-only + corpus-anchored recency (no `Date.now()`/wall-clock; anchor to
the data's own newest scan where a time horizon is needed).

## Axis A — point-in-time back-gap (independent recomputation vs the live surface)

`build_backgap(conn)` reconciles the independently-recomputed should-be-served membership
against what the Stage-5 surface actually serves. Each finding type below is a target in
the **reconciliation vocabulary**; the impl MUST ground each against the real substrate and
**honestly surface (never fabricate)** any the substrate cannot support, documenting the
consolidation in the PR — mirroring the 5.05/5.07 latent-anchor precedent (do not invent a
field the substrate lacks).

1. **`untraced_statement`** — a `reviewer_eligible_ids(conn)` member the Stage-5 served
   surface fails to carry: `eligible − served ≠ ∅`. The `served` set is read back from the
   outermost projection (`stage5_frontend_surface.build_surface` / `stage5_watchdog_signals`),
   so a regression dropping a record class from EITHER `read_api` OR a Stage-5 layer is
   caught. The core back-gap.
2. **`orphan_source`** — a source in `stage5_source_inventory.build_inventory` lifecycle set
   that no served record traces back to (`inventory − referenced`).
3. **`dangling_trace`** — a served record referencing a source/statement id absent from the
   canonical inventory/eligible set (`served-ref − canonical`). The inverse of orphan.
4. **`coverage_hole`** — a canonical `read_api.completeness_gap_cards` row whose 1:1
   surfacing through the Stage-5 trust/watchdog/frontend surfaces is missing (the gap is
   recorded but not carried up).
5. **`coverage_unknown`** — a record whose lifecycle/verification state cannot be resolved
   from the substrate. **Fail-closed**: surfaced as unknown, never assumed covered.
6. **`archive_unchecked`** — a `changed`/`disappeared`/`replaced` source (5.03 lifecycle)
   with no archive-availability determination near `scan_date`. Wayback is **default-closed**
   and **mock-tested only — no live call** (live call requires CEO/CTO authorization).
7. **`archive_missing`** — a changed source whose archive availability WAS determined and is
   absent (the 5.07 `archive_gap` / `archive_unavailable_for_changed_source` honest flag,
   reconciled here, never hidden).

## Axis B — regression / monotonicity vs a pinned baseline

`build_regression(conn, baseline)` compares the current surface against a **committed golden
baseline** (a `tests/fixtures/` JSON snapshot of the prior Stage-5 surface) loaded via
`--baseline PATH`. Monotonicity: trust state and coverage must not silently shrink.

1. **`verification_regressed`** — a statement `verified` in the baseline now no longer
   verified in the current surface.
2. **`publish_regressed`** — a record in the baseline's served/publish-eligible set dropped
   from the current surface (Alpine publish lane stays empty by construction; this guards a
   future silent drop).
3. **`capture_lost`** — a source/archive capture present in the baseline now absent.
4. **`digest_item_dropped`** — an item present in a baseline envelope-digest item set now
   missing (the served item set shrank).
5. **`correction_not_propagated`** — a correction edge present in the baseline
   (`stage5_trust_model` corrections spine) not reflected in the current surface.
6. **`baseline_absent`** — emitted **fail-closed** when no `--baseline` is supplied or it
   cannot be parsed: the regression axis is reported **unverifiable** and the CLI exits 1.
   Absence of a baseline is NEVER silently reported as "no regressions."

## Finding shape, ordering, determinism

Each finding: `{axis, type, severity, subjectId, detail}`. Stable sort
`(axis, severity desc, type, subjectId)`. The full run is **idempotent**: two independent
passes over the same `(conn, baseline)` are byte-identical (run-twice deep-equal asserted).

## Boundary invariants (premium I1–I8)

* **I1** every emitted artifact transport-swept by `read_api.assert_no_raw_paths` (FS path /
  `.sha256` / vault marker / `file://` fails LOUDLY at the boundary).
* **I2** `localSourcePath` never emitted; raw paths/hashes stay backend-only.
* **I3** exactly one envelope digest (`backgapDigest`) — no per-source raw hash.
* **I4** existing serving modules byte-0-diff (proven by `git diff --name-status` + sha256):
  `read_api.py`, `stage2_backgap.py`, `stage3_backgap.py`, and every `stage5_*.py`.
* **I5** a PHYSICAL on-disk RED-proof (below) — load-bearing, non-tautological.
* **I6** `access: reviewer_internal` / `scope: alpine` only; absent from any public /
  `published_records` path.
* **I7** read-only: SELECT-only by construction; **no `--apply`**, zero row delta, no socket,
  no subprocess (auditable).
* **I8** deterministic: same `(DB, baseline)` → byte-identical envelope. No AI, no network,
  additive module + test only.

## CLI / CI gate

```
python scripts/stage5_backgap.py --db <db> [--baseline PATH] [--json] [--check]
```

Prints the reviewer-internal back-gap/regression report. **Exits 1 on any finding** (any
non-clean back-gap OR regression OR `baseline_absent`), **0 only on a fully clean audit with
a parsed baseline** — so it doubles as a CI gate. No `--apply` exists (inherently dry-run /
read-only).

## RED-proof (non-tautological, load-bearing) — required AC

Neuter one resolver (e.g. the membership reconciliation that computes `eligible − served`,
or the baseline monotonicity comparator) so it returns a constant empty/clean result →
a targeted test goes RED (a real back-gap or regression is no longer detected) **while the
read surface still serves the same records** — the RED comes from the auditor logic, not the
input. Restoring returns the module **byte-identical** (sha256 match). Paste the neuter →
RED → restore → byte-identical evidence in the PR comment.

## Acceptance criteria (corrected — pytest, not node)

- `scripts/stage5_backgap.py` + `tests/test_gov574_stage5_backgap.py` committed (**additive
  only; zero production diff** to any serving module — I4).
- Every Axis A and Axis B finding type is covered by a test (or, where the substrate cannot
  ground a type, the consolidation is documented and the honest-gap surfacing is tested).
- Read-only + deterministic/idempotent invariants asserted (run twice, deep-equal);
  fail-closed degradation tested (`coverage_unknown`, `baseline_absent`); the Wayback-gated
  branch tested with a **mock** (no live call).
- The committed golden baseline fixture under `tests/fixtures/` is present and the
  `--baseline` path + `baseline_absent` fail-closed branch are both asserted.
- `python -m pytest tests/test_gov574_stage5_backgap.py` green **AND** the full
  `python -m pytest` suite green.
- PHYSICAL on-disk RED-proof (neuter → targeted test RED → restore byte-identical).
- **Verification evidence in a comment:** commit SHA, new file paths, per-file + full-suite
  pytest output, the `git diff --name-status` (additive-only) + sha256 byte-0-diff proof for
  serving modules, and the RED-proof transcript.

## Hard stops

No public/email/editorial output (deferred x.08 lane, GOV-572 HOLD). No live Wayback call
without CEO/CTO authorization (mock-only). No scope beyond Alpine. No migration, no schema
change, no mutation, no new public-projection key. On completion, mark `done` so the VSR leg
(GOV-576) dispatches.
