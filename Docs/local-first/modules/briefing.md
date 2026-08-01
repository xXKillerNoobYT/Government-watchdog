# Module continuity — `src/briefing.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-briefing`). Keep the **public surface** list byte-aligned with the
> module's named exports.

## Purpose
Correction-aware, hot-topic-surfacing weekly briefing assembler (Stage 5.08,
GOV-568; contract GOV-564). A deterministic layer **above** the Stage 4 publication
gate + digest assembler: it partitions statements by status (excludes
`false_corrected` from the body, surfaces them as correction notices; keeps
`disputed` out of the main body) and flags changed/new/missing Alpine sources by
SHA-256 comparison. It never modifies `evaluatePublicationGate`.

## Public surface
- `BRIEFING_TITLE` — default briefing title constant.
- `detectHotTopics(currentSources, priorHashes?)` — hash/recency triage list (no AI, no network).
- `buildCorrectionNotices(statements, priorDigestTexts?)` — correction notices for `false_corrected` statements.
- `partitionBriefingStatements(statements)` — split into publishable / disputed / corrected pools.
- `assembleBriefing(input?, options?)` — the full deterministic briefing object + body.

## Inputs / outputs
- **In:** `{ statements, currentSources, priorHashes, priorDigestTexts, … }`; injected `now`/`options`.
- **Out:** briefing object with a deterministic `body`, hot-topic triage list, and correction notices; Wayback availability recorded as `unchecked` (no external call).
- **Side effects:** none — pure compute; external calls are a CEO/CTO-gated hard stop and are not made here.

## Invariants
- Pure + deterministic: same inputs → byte-identical output, independent of input array order (every section sorts by a content key).
- Does **not** mutate or re-implement the Stage 4 publication gate; Stage 4 body stays byte-identical for inputs it already accepted.
- Hot-topic detection is hash/recency only — AI never decides what is "hot".

## Test entry
```
node --test test/briefing.test.js
```
