# Module continuity — `src/doc-continuity.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-doc-continuity`). Keep the **public surface** list byte-aligned with the
> module's named exports — the detector documents itself.

## Purpose
Deterministic, reviewer-internal documentation-maintenance & project-state-continuity
detector (Stage 5.14, GOV-581; contract GOV-580). Given the maintenance manifest, the
real repo file set, and the live module export surface, it reports doc/module drift so
a fresh agent can resume Stage 5 with no out-of-band knowledge. Disjoint from the 5.13
back-gap analyzer (which audits verification records, in the Python substrate repo).

## Public surface
- `CONTINUITY_REPORT_TITLE` — report title constant.
- `FINDING_TYPES` — frozen `{ type: severity }` map enumerating every §4 finding type.
- `analyzeDocContinuity({ manifest, fileSet, moduleExports, priorSnapshot, now })` — the pure detector → §4 report.
- `renderContinuityReport(report)` — deterministic text render (mirrors `refresh-runner.renderRunLog`).

## Inputs / outputs
- **In:** `manifest:{ version, entries[] }`, `fileSet:string[]` (repo-relative paths the caller enumerates), `moduleExports:{ "src/x.js": string[] }`, optional `priorSnapshot`, injected `now`.
- **Out:** `{ generatedAt, manifestVersion, findings[], counts:{ total, bySeverity, byType }, handoffReady, summary }`. `handoffReady === true` iff zero high-severity findings.
- **Side effects:** the exported functions have **none** (pure). The CLI `main()` reads the manifest, enumerates `Docs/`, `src/`, `test/`, and dynamic-imports each `src/*.js` for its live exports, then prints the report and exits non-zero when not handoff-ready. It writes nothing.

## Invariants
- **Read-only / idempotent:** same inputs → deep-equal report; inputs never mutated.
- **Deterministic:** no `Date.now()` / `Math.random()` in the detection path; stable sort `(severity desc, type, subjectId)`.
- **Fail-closed:** absent/unknown inputs degrade to explicit `*_unknown` findings (e.g. `ledger_state_unknown`), never a false high-severity gap; absent `priorSnapshot` ⇒ `carried=false`, never an error.
- **No network, no AI, no public output** — reviewer-internal artifact only (GOV-471/436).

## Test entry
```
node --test test/doc-continuity.test.js
```
