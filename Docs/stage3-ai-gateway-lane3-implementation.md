# Stage 1 Impl — Slice 3 C: Lane 3 Verification Layer (GOV-90)

Issue: GOV-90 · Owner: TranscriptEvidenceEngineer (`09b5d302`) · VerificationSafetyReviewer consult
Stage: Stage 1 implementation — Slice 3 C (Lane 3). Alpine-only, local/vault-only.
Blocked by: GOV-89 (Slice 3 B Lane-2 AI writer) — **resolved/merged** (PR #20).
Implements: `Docs/stage3-ai-gateway-gap-analysis.md` §4.2 (Lane 3 L3-1/L3-5/L3-6) against
1.09 (automation-vs-AI boundary, step 11 prep), 1.11 (publication gates §5),
`AI_GATEWAY_PROCESSING_WORKFLOW.md` lane 3 ("compare AI output to primary source,
assign verification label, flag uncertainty").

## What shipped

| Artifact | Role |
|---|---|
| `Database/migrations/0010_ai_verification_results.sql` | `ai_verification_results` ledger — one row per (Lane-3 run, AI statement) verdict. Additive + idempotent; **no table rebuild** (Lane 3 only adds storage). |
| `scripts/ai_verification.py` | Lane-3 layer: deterministic token-grounding compare of each AI draft to its primary source at the pointer; assigns a verdict label + uncertainty flag; writes the verdict **beside** the claim (never on it); records the run on the shared `ai_extraction_runs` ledger as `lane='3_verification'`. |
| `scripts/slice3c_smoke.py` | End-to-end offline Lane-2 → Lane-3 smoke over the sanitized Alpine fixture. |
| `tests/test_ai_verification.py`, `tests/test_slice3c_integration_smoke.py` | Unit + integration coverage for the Lane-3 invariants. |
| `.github/workflows/local-runner-smoke.yml` | New CI step runs `slice3c_smoke.py`. |

## The load-bearing design choice — Lane 3 writes NO gating field

The gap analysis (§4.2 L3-1) specifies Lane 3 as a deterministic compare that
"reads pointer, flags mismatch — **writes NO gating field**." So the verdict for
each AI statement lands in a **new, append-only side table** keyed to the
statement; the `statements` / `evidence_links` rows are **never mutated** by
Lane 3. Because the claim row is untouched, an AI claim stays
`machine_extracted_unreviewed` + `not_publishable` **by construction** — Lane 3
can flag a claim contested but can **never promote** it. Promotion stays the human
G2 gate (1.09 step 11 / G2, 1.11 §5). This is the same single AI→public path 1.09
§1.2 / 1.11 §0 commit to, implemented without weakening it.

A `source_match` verdict means only "the draft is grounded in its source, ready
for a **human** reviewer" — it does not make the claim publishable. And a
**low-confidence** claim is never auto-validated: it is capped at `uncertain` even
on a perfect text overlap (1.09 §5 low-confidence → reviewer).

## The deterministic compare

`classify()` scores the AI draft against the resolved primary-source text using
token **containment** (fraction of the claim's *content* tokens present in the
source — the right measure for a paraphrase, which is shorter than its source).
The `"AI paraphrase:"` lead-in and function words are stripped so they cannot
inflate grounding. Verdict bands (fail-closed):

| Condition | Verdict | Uncertainty |
|---|---|---|
| source span cannot be resolved | `unverifiable` | high |
| score ≥ 0.60 **and** claim not low-confidence | `source_match` | low |
| score ≤ 0.20 | `source_mismatch` | high |
| otherwise (incl. high-overlap-but-low-confidence) | `uncertain` | medium |

`contested = 0` only for `source_match`; every other verdict flags the row for a
reviewer. Lane 3 is deterministic, so the run records `model_name = NULL` and a
`tool_version` — "verification" is a check of the AI draft against source, never a
second model trusting the first.

## Invariants enforced (GOV-90 acceptance + Slice-3 AI gates)

1. **Label assigned** — a verdict + uncertainty flag is written per AI statement.
2. **Mismatch flags, never promotes** — a low-confidence/mismatched claim stays
   `machine_extracted_unreviewed` + `not_publishable` (proven by test:
   `test_lowconfidence_claim_stays_unreviewed_and_not_publishable`,
   `test_mismatched_claim_flagged_not_promoted`).
3. **No gating write** — the `statements` digest is byte-identical pre/post Lane 3
   (`test_lane3_writes_no_gating_field`).
4. **Attribution safety preserved** — Lane 3 adds/modifies no `speaker_attributions`.
5. **Gateway run-log** — the Lane-3 run is on `ai_extraction_runs` with
   `lane='3_verification'` + input set / tool version / errors / reviewer state /
   retry (AI_GATEWAY §17).
6. **Fail-closed downstream** — `verification_blocks_publication()` returns True for
   every verdict except a `source_match` a human separately approved; no verdict and
   a failed run block too.
7. **Data-publication boundary** — `ai_verification_results`, its `source_excerpt`
   and `detail` are vault-only; a column-name guard test asserts none can reach the
   web-safe projection (`publication.WEB_SAFE_FIELD_ALLOWLIST`).

## What this slice does NOT do (correctly out of scope)

- It does **not** promote anything, set any reviewed `verification_status`, or flip
  `publication_state` — that is the Lane-5 human gate (a later reviewer-tooling slice).
- It does **not** build the reviewer queue UI — `latest_verdict()` is the backend
  read the queue will consume; the surface is a frontend/gated-beta slice.
- No live model, no network: the Lane-2 proposer and the Lane-3 compare are both
  offline/deterministic for reproducible CI.
