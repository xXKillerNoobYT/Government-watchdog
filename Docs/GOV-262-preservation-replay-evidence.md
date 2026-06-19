# GOV-262 — Stage 2.04 preservation-replay evidence

**Issue:** GOV-262 · **Stage:** 2.04 implementation · **Owner:** BackendCrawlerEngineer
**Scope:** Town of Alpine corpus ONLY (deterministic lane). No frontend, no public projection.
**Contract:** Stage 2.04 goal `7e4434b1` (GOV-230) — `transcript_class` migration is
deferred to Stage 2.05, so the 2.04 deliverable is a **preservation-VALIDITY pass**,
not a migration. No unit may be read by Stage 2.05+ extraction until this is green.

This file is the committed snapshot of the operator-facing `verify` output. The DB,
raw bytes, and run logs stay local/vault-only (git-ignored) — only the tooling, tests,
and this sanitized snapshot are committable.

---

## Deliverable

`scripts/raw_preservation.py preservation-replay` — composes three deterministic legs
and writes ONE `crawl_runs` row tagged `preservation_replay`:

1. **Document reproducibility** (`verify_reproducibility`, `object_types=('document',)`,
   mandatory/never disabled): re-hash stored file **bytes** vs recorded `sha256`.
2. **Transcript-text reconcile** (`reconcile_transcript_text`): re-hash stored
   `full_text` vs recorded `sha256` — a **text** hash (`sha256(full_text)`), NOT the
   JSON cache file's bytes, so file-byte re-hashing cannot false-positive transcripts.
3. **Sources validity** (`validate_sources`): every `sources` row must be preserved
   (own bytes that re-hash, OR all child documents valid). `seed_only` /
   `seed_only_unconfigured` is INVALID for Stage 2 → upgraded to `preserved` (with
   `--apply`) or documented as a deliberate `no_primary_source` exception
   (`--gap-exception SOURCE_ID`); otherwise the source is a fail-closed defect.

**Fail-closed (issue §4):** any missing/mismatch document, transcript drift/missing-text,
or invalid source ⇒ `crawl_runs.status='failed'` with every offending unit listed
verbatim in `notes`, and (strict mode) `RawPreservationError` raised **after** the
failed run row is durably written. `status='success'` with a non-empty miss list is a
structural defect the code makes impossible.

**Absolute drift rule (issue §5):** drift (recorded `sha256` ≠ stored bytes/text) is a
preservation **defect** — never a `completeness_gap`, never a re-fetch trigger, and the
recorded `sha256` is **never** overwritten by the verifier.

---

## Real-corpus run (local Mac runner)

Corpus rebuilt deterministically from `/Users/IA/Documents/TOA/TownOfAlpine` via
`scripts/ingest_local_corpus.py` (220 folders → 134 preserved documents, GOV-124 ingest).

```
$ python scripts/raw_preservation.py preservation-replay --apply
preservation-replay: status=success run_id=4 apply=True
  documents: checked=134 ok=134 missing=0 mismatch=0
  transcripts: checked=0 ok=0 mismatch=0 missing_text=0
  sources: preserved=1 upgraded=0 exception_documented=0 invalid=0
  manifest: unit_count=134 aggregate_sha256=07e9e563d5ce3b2d23c851c090f83acd2afef2c5e0132c332ca67ac9eec1f9d8
APPLY_EXIT=0
```

| Metric | Value |
|---|---|
| `crawl_runs` row id | 4 (targets include `preservation_replay`) · `status=success` |
| Documents | 134 checked / 134 ok / 0 missing / 0 mismatch |
| Transcripts | 0 rows (the local ingest produces transcript-type **documents**, not `transcripts` rows; reconcile is a no-op until timed transcripts land) |
| Sources | 1 (`alpine_local_corpus`) — preservation-valid **by its 134 valid children** |
| `seed_only` family, before → after | **0 → 0** (the GOV-124 ingest already writes `raw_preserved`; no undocumented seed remains) |
| Aggregate manifest | `unit_count=134`, `aggregate_sha256=07e9e563…f9d8` |
| Determinism | re-run yields the **identical** `aggregate_sha256` (column-stable over `(object_type, id, sha256)`) |

The corpus is **PRESERVATION-VALID** → the Stage 2.05 extraction migration precondition
is satisfied for the Alpine corpus as built.

---

## Tests (`tests/test_gov262_preservation_replay.py`, 16 cases)

- transcript-text reconcile: intact pass, **drift detected**, text-hash-not-file-hash guard;
- aggregate manifest column-stable & deterministic;
- full pass green → `success` run row + manifest;
- **drift-injection fail-closed** (headline): tampered document → `RawPreservationError`
  + `failed` run row listing the unit + recorded `sha256` unchanged;
- missing-file fail-closed; `success` never coexists with misses;
- `seed_only` upgraded by own bytes / by valid children; undocumented `seed_only` → invalid+fail;
- deliberate-exception `no_primary_source` gap path → pass; `preserved`-marked-but-missing → invalid;
- dry-run persists nothing; document leg mandatory under `('transcript',)` scope;
- no preservation field crosses `to_web_safe`.

```
$ python -m pytest -q
512 passed
```

## Verification commands (reviewer replay)

```
python scripts/ingest_local_corpus.py --source-dir /Users/IA/Documents/TOA/TownOfAlpine --report
python scripts/raw_preservation.py preservation-replay --apply
python -m pytest tests/test_gov262_preservation_replay.py tests/test_raw_preservation.py -q
```
