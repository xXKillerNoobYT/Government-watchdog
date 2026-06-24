# Stage 5.07 — Transcript / evidence / statement trust model contract (GOV-531)

**Owner:** TranscriptEvidenceEngineer · **Reviewers:** VSR + SecurityPrivacyAgent ·
**Merge:** non-author CTO squash · **Scope:** Alpine, reviewer-internal, NOT public
(public stays Isaac-gated, GOV-420). · **Module:** `scripts/stage5_trust_model.py` ·
**Tests:** `tests/test_gov531_stage5_trust_model.py`.

The Stage 5.07 IMPL head (mirrors 5.05 GOV-520 / 5.06 GOV-524). A deterministic,
idempotent, reviewer-internal **model layer** that formalizes four trust mechanics over
the already-merged statement/evidence/transcript substrate. No AI, no network, additive
only. Every pre-existing serving module stays **byte-0-diff**.

## Substrate consumed (read-only, never mutated)

| Source | Used for |
|---|---|
| `statements.py` (migration 0007 §D-4) | the `layer` enum SSOT + the forward-only `updates_statement_id` correction spine |
| `read_api.reviewer_internal_records` | the web-safe served record set (the only vocabulary) |
| `read_api.assert_no_raw_paths` / `_is_web_url` / `_iter_strings` | the transport boundary |
| `stage5_source_inventory` (5.03) | source lifecycle + archive-availability envelopes |
| `stage3_card_feed` | card handle / composed status / card date (by reference) |

## §0 — Five-way record separation (preserve, do not collapse)

Every meaningful claim stays in exactly one of five conceptual classes, **mapped onto —
never re-typing — the existing `statements.ALLOWED_LAYERS` enum**:

| Record class | `statements.layer` |
|---|---|
| `fact` | `known_then` |
| `summary` | `presented_then` |
| `action_outcome` | `actual_later` |
| `ai_assumption` | `ai_thought_then` |
| `verification_correction` | `corrected_later` |

`LAYER_TO_RECORD_CLASS` is **total over `statements.ALLOWED_LAYERS`** (import-time parity
assertion). An AI assumption is never silently re-bucketed as a verified fact. Source
trail + review status (verification + provenance) ride alongside every record; a record
whose layer does not resolve carries the `layer_unresolved` gap (never treated as a
fact). `build_record_separation(conn)`.

## Model 1 — Correction state model (forward-only)

`build_corrections(conn)` — one typed correction edge per *corrected record*
(`correctionStatus ∈ {replaced, superseded, corrected, amended}` OR its source's 5.03
`lifecycle == replaced`), pointing the corrected `known_then` record at its superseding
record via the forward-only `updates_statement_id` spine. **Never rewrites then-known
context** — `knownThen` is preserved verbatim.

* `correctionEffectiveFrom` = the superseding record's grounded card date (the correction
  date), never invented.
* `effective_view_at(corrections, T)` — a record's effective view at time T reflects only
  corrections with `correctionEffectiveFrom ≤ T`; a later/unresolved correction is not yet
  in force, so the historical then-known record stays preserved + addressable.
* Fail-closed: a corrected record with no resolved superseding ref →
  `correction_unresolved` gap; never a fabricated ref.

## Model 2 — Hot-topic reason model

`build_hot_topic_reasons(conn)` — WHO/WHAT marked a topic (`markedBy`) + WHY (a reason
grounded in resolvable record refs). Distinct from the 5.05 salience *score* — this adds
the *reason/provenance*. `markedBy ∈ {system_signal, auditor, isaac_admin,
public_attention, repeated_discussion, changed_record}` (frozen SSOT). The deterministic
build emits only the markers it can ground in a registry signal:

* `changed_record` — a corrected record on the topic;
* `repeated_discussion` — activity at/above the floor;
* `system_signal` — recent activity within the corpus recency window (anchored to the
  data's own newest scan, not a wall clock).

`auditor` / `isaac_admin` / `public_attention` stay in the vocab for a future
human-sourced marker path but are never fabricated without a grounding source. A record
whose topic/agenda anchor does not resolve is surfaced in `unanchored[]` with the
`topic_anchor_missing` gap (resolving the 5.05 latent agenda_thread anchor honestly).

## Model 3 — Source-change + archive verification model

`build_source_change_archive(conn)` — reuses the 5.03 inventory verbatim (lifecycle ∈
{unchanged, changed, disappeared, replaced} + archive availability keyed to scan_date)
and formalizes the **lifecycle ↔ archive binding** via `derive_archive_binding`:

* `unchanged` → `live_source` (archive optional);
* `changed` / `disappeared` / `replaced` **with** an available-near-scan snapshot →
  `archive_backed` (the changed source is still representable);
* `changed` / `disappeared` / `replaced` **without** one → `archive_gap` +
  `archive_unavailable_for_changed_source` gap (honestly flagged, never hidden).

URLs are http(s)-only (`file://` stripped upstream; re-guarded here).

## Model 4 — Future-fact verification model (past AI assumptions)

`build_assumption_verifications(conn)` — a past AI assumption (`layer ==
ai_thought_then`) can later be marked `verificationOutcome ∈ {supported, contradicted,
partially_supported, corrected, unresolved}`. The verification is a *later* record
attached to the assumption via the same forward-only `updates_statement_id` spine; the
original assumption is **never mutated** (`assumptionThen` preserved). `resolve_verification_outcome(verifier)`:

1. verifier `correction_status == corrected` → `corrected`;
2. verifier `correction_status ∈ {replaced, superseded}` → `contradicted`;
3. verifier composed status `verified` (grounded) → `supported`;
4. any other resolved-but-not-grounded later record → `partially_supported`;
5. no verifying record → **`unresolved`** (fail-closed; never silently upgraded to fact).

Each resolved verification carries `verificationOrigin` (who/what = verifier
`produced_by`), `verificationMethod` (how = verifier evidence `relation`),
`verifyingSourceRef`, and `verificationDate`.

## Boundary invariants (premium I1–I8)

* **I1** every emitted artifact transport-swept by `read_api.assert_no_raw_paths` (FS path
  / `.sha256` / vault marker / `file://` fails LOUDLY at the boundary).
* **I2** `localSourcePath` never emitted.
* **I3** exactly one envelope digest (`trustDigest`) — no per-source raw hash.
* **I4** existing serving modules byte-0-diff (proven by `git diff --name-status` +
  sha256).
* **I6** `access: reviewer_internal` / `scope: alpine` only; absent from any public /
  `published_records` path.
* Deterministic: same DB → byte-identical model envelope. No AI, no network, additive.

## CLI / CI gate

`python scripts/stage5_trust_model.py --db <db> [--check]` — prints the reviewer-internal
trust envelope; `--check` runs the six load-bearing guards (separation total /
corrections resolved-or-gapped / hot-topic markers valid / archive binding consistent /
verifications fail-closed / single envelope digest) and exits **1 on any defect, 0 on
clean**.

## RED-proof (non-tautological, load-bearing)

Neuter `resolve_verification_outcome` to return a constant `unresolved` →
`test_verification_resolver_is_red_proof` goes RED (a resolved `corrected` outcome falls
to `unresolved`) **while the read surface still serves both the assumption and its
verifier** — the RED comes from the resolver, not the input. Restoring returns the module
byte-identical. (A second guard-coupled neuter — `record_class` → an out-of-vocab
constant — drives `--check` to exit 1, demonstrating the CI gate.)
