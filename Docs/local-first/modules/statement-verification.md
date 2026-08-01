# Module continuity — `src/statement-verification.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-statement-verification`). Keep the **public surface** list byte-aligned
> with the module's named exports.

## Purpose
Statement lifecycle + the single reviewer-internal **publication gate**. Creates
statements with traceable source links, applies verification transitions
(verify / dispute / correct / do-not-publish), and decides whether a statement is
publishable — the one gate every downstream assembler reuses.

## Public surface
- `STATEMENT_STATUSES` — frozen (`unverified`, `verified`, `disputed`, `false_corrected`).
- `STATEMENT_KINDS` — frozen (`fact_claim`, `ai_analysis`).
- `createStatement(input, options?)` — build a normalized statement (injected `now`/`actor`).
- `createSourceLink(input)` — build a source link, computing its `traceHash` if absent.
- `computeSourceTraceHash(link)` — deterministic SHA-256 over the link's identifying fields.
- `applyVerificationTransition(statement, transition, options?)` — return a new statement after a lifecycle transition.
- `evaluatePublicationGate(statement)` — `{ publishable, failures }`; the canonical publish predicate.

## Inputs / outputs
- **In:** statement definitions `{ id, text, kind?, status?, sourceLinks[], evidenceLimits, … }`; transitions `{ action, reason? }`; injected `now`/`actor`.
- **Out:** new normalized statement objects (inputs never mutated); gate result `{ publishable: boolean, failures: string[] }`.
- **Side effects:** none — pure compute (SHA-256 hashing only).

## Invariants
- `evaluatePublicationGate` is the **single** publish predicate; downstream code must reuse it, never re-implement it.
- Trace hashes are content-stable (whitespace-normalized quote) → byte-identical across runs.
- Transitions return new objects; the input statement is never mutated.

## Test entry
```
node --test test/statement-verification.test.js
```
