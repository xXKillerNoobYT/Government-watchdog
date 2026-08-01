# Module continuity — `src/digest-assembler.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-digest-assembler`). Keep the **public surface** list byte-aligned with
> the module's named exports.

## Purpose
Deterministic, reviewer-internal digest assembler (GOV-478, GOV-471 §7 F1). Selects
the statements that may appear in the weekly digest and renders a byte-identical
body. A statement enters only when `evaluatePublicationGate(...).publishable` **and**
it carries at least one source link with a trace hash.

## Public surface
- `DIGEST_TITLE` — default digest title constant.
- `assembleDigest(statements, options?)` — `{ title, body, included, excluded, log }`.

## Inputs / outputs
- **In:** array of statements (from `statement-verification`); `options.title?`.
- **Out:** `{ title, body, included[], excluded[], log[] }`. `body` is a deterministic text block; `excluded` records every drop with its gate `failures`; `log` parity = no silent drops.
- **Side effects:** none — pure compute.

## Invariants
- Reuses `evaluatePublicationGate` (does not re-derive publish rules).
- Output `body` is byte-identical regardless of input array order (content-only sort: `createdAt`, then `id`).
- Every exclusion is logged (`result.log.length === result.excluded.length`) — no silent drops.
- Throws on non-array input (programming-error guard).

## Test entry
```
node --test test/digest-assembler.test.js
```
