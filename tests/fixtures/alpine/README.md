# Alpine integration-smoke fixtures (GOV-77)

These files are **synthetic, sanitized** Alpine-shaped artifacts used only by the
Stage 1 Slice 1 integration smoke (`scripts/slice1_smoke.py` /
`tests/test_slice1_integration_smoke.py`).

## Why they are safe to commit

The data-publication boundary (`WORKFLOW_GOVERNANCE.md`) keeps **raw crawled
data** out of git: `.gitignore` excludes `Raw-PDFs/`, `Transcripts/`, and
`Database/*.db`. These fixtures are the explicitly-allowed exception — they are
*not* crawler output. They contain no real raw bytes, no PII, and no live
source content; they are hand-authored placeholders whose only job is to give
the smoke a small, deterministic input.

The smoke copies a fixture into a **throwaway temp raw store** (never the real
`Raw-PDFs/`) and a throwaway temp DB (never `Database/gov_watchdog.db`), so a CI
run touches no real data and leaves nothing behind.

## Files

| File | Shape | Used to assert |
|---|---|---|
| `alpine-sample-agenda.txt` | a town-council "document" | raw preserved + sha256 reproducibility, provenance (`source_id`/`sha256`/`fetch_time`/archive URL), default not-publishable |

Byte content is fixed on purpose: the smoke re-hashes the file and compares to
the hash it recorded at ingest, so any drift in these bytes is intended to be a
deliberate, reviewed change.
