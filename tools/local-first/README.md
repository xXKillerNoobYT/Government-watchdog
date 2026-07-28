# local-first tooling — extracted reference implementations

**Provenance.** Built June–July 2026 in the local-first tooling workspace
(`~/GitHub/Government-Watchdog`, snapshot branch `GOV-585-handoff-escalation`), whose git
history is unrelated to this repository — which is why this content arrives as fresh
commits rather than a merge. Extracted 2026-07-28 at Isaac's request.

**Status: candidate tooling, not production.** The Python backend is the system of record.
These are working, tested reference implementations of the deterministic contracts in
`Docs/local-first/` — useful as executable specifications until each is ported, adopted,
or explicitly archived:

| Module | Contract it implements | Tests |
|---|---|---|
| `src/briefing.js` | stage5-08 newsletter/briefing/editorial (correction-aware assembler) | `test/briefing.test.js`, `test/briefing-vsr.test.js` |
| `src/digest-assembler.js` | GOV-478 deterministic digest (D10/F1) | `test/digest-assembler.test.js` |
| `src/doc-continuity.js` | stage5-14 doc-maintenance/continuity detector | `test/doc-continuity.test.js` |
| `src/handoff-escalation.js` | stage5-15 agent-handoff/owner-escalation evaluator | `test/handoff-escalation.test.js` |
| `src/refresh-runner.js` | GOV-479 weekly refresh runner (D1–D9/F2) | `test/refresh-runner.test.js` |
| `src/source-registry.js` | source registry scaffold | `test/source-registry.test.js` |
| `src/statement-verification.js` | statement-verification publication gate | `test/statement-verification.test.js` |

Run the suite with `node --test` from this directory (plain `node:test`, no dependencies).

**Adoption rule.** A module graduates by being ported into the Python backend with its
tests translated, then deleted from here in the same PR — this directory only shrinks.
