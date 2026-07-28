# Module continuity — `src/refresh-runner.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-refresh-runner`). Keep the **public surface** list byte-aligned with
> the module's named exports.

## Purpose
Weekly refresh orchestration (GOV-479, Stage 4.F2). Re-validates the Alpine source
registry + statements, assembles the digest, surfaces issue candidates, and writes a
timestamped run log. Ships a `--dry-run` (default) / `--apply` CLI; CTO reviews a
dry-run before the first `--apply` (contract §8 pass-up gate).

## Public surface
- `ALPINE_HOSTS` — frozen allow-list of Alpine hosts (scope guard).
- `DEFAULT_LOG_PATH` — default run-log path (gitignored, local/vault-only evidence).
- `formatTimestamp(date)` — deterministic timestamp string from an injected date.
- `formatLogLine(date, level, msg)` — one structured run-log line.
- `runWeeklyRefresh(input, options?)` — async; the core orchestration (pure given injected `now`).
- `renderRunLog(result, options?)` — deterministic text render of the run log.

## Inputs / outputs
- **In:** `{ priorRegistry, statements, sourceDefs }`; injected `now`; CLI flags `--apply | --dry-run | --state | --out | --log`.
- **Out:** `{ ok, registry, statements, digest, issueCandidates, log, now }`. `renderRunLog` → text.
- **Side effects:** the CLI `main()` reads fixtures/state and writes the run log in both modes; `--apply` additionally persists state/digest. The exported functions themselves are read-only given their inputs.

## Invariants
- Off-scope (non-Alpine) hosts fail the run (`ok=false`, non-zero CLI exit) — a scope leak is a hard stop.
- `runWeeklyRefresh` is deterministic given an injected `now`; a healthy run is idempotent.
- No external network call; the run log is the local/vault-only evidence artifact.

## Test entry
```
node --test test/refresh-runner.test.js
```
