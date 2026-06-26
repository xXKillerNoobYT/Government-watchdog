# Module continuity — `src/source-registry.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-source-registry`). Keep the **public surface** list below byte-aligned
> with the module's named exports — `src/doc-continuity.js` flags drift.

## Purpose
Local-first capture + lifecycle layer for Alpine source records: ingest a local
file for a source URL, hash its bytes (SHA-256), canonicalize the URL, and detect
when a newly captured source **replaces** a prior one. Deterministic and Alpine-only.

## Public surface
- `SOURCE_CLASSES` — frozen list of allowed source classes (`official_record`, `agenda_packet`, …).
- `LIFECYCLE_STATUSES` — frozen list (`current`, `replaced`, `missing_after_capture`, `rejected`).
- `expandHomePath(path)` — resolve `~`/`~/…` and relative paths to an absolute path.
- `canonicalizeUrl(url)` — lowercase host, drop fragment, strip trailing slash.
- `hashFileSha256(path)` — async; hex SHA-256 of the file bytes.
- `buildSourceCapture(input, options?)` — async; build a full source record (hashes the local file; marks `missing_after_capture` on ENOENT).
- `applyReplacementDetection(records, candidate, options?)` — append `candidate`, marking a prior same-URL record `replaced` when content differs.
- `importSources(fixtures, options?)` — async; build a registry from a fixture array.

## Inputs / outputs
- **In:** source definitions `{ id, sourceUrl, sourceClass, title, toaLocalPath, … }`; an optional injected `now`/`actor` via `options`.
- **Out:** plain JSON-serializable source records carrying `contentHash`, `lifecycleStatus`, `replacement`, and an `audit` block. No mutation of inputs.
- **Side effects:** reads the local capture file (`stat`/`readFile`) only; never writes, never networks.

## Invariants
- Deterministic given an injected `now` — no hidden clock in the record shape.
- A missing local file degrades to `lifecycleStatus: "missing_after_capture"` (fail-closed), never throws past ENOENT.
- `canonicalizeUrl` is idempotent; content hashing is byte-stable.

## Test entry
```
node --test test/source-registry.test.js
```
