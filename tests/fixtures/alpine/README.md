# Alpine integration-smoke / segmenter fixtures (GOV-77, GOV-81)

These files are **synthetic, sanitized** Alpine-shaped artifacts used only by the
Stage 1 Slice 1 integration smoke (`scripts/slice1_smoke.py` /
`tests/test_slice1_integration_smoke.py`) and the Stage 1 Slice 2 B deterministic
segmenter (`scripts/segment_transcript.py` / `tests/test_segment_transcript.py`).

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
| `alpine-sample-transcript.json` | a preserved timestamped meeting transcript (same on-disk shape `fetch_transcripts.py` writes: `meta` + `transcript.timestamped_text`) | deterministic segmenter (GOV-81) produces addressable `transcript_segments` rows with `timestamp_seconds` / `timestamp_human` / `segment_text` / `is_verbatim` / `confidence` / `transcript_path`, FK-linked to `transcripts`/`meetings`/`sources` |

The transcript fixture contains **no real meeting audio, no raw bytes, and no
speaker names** — it is hand-authored synthetic Alpine-shaped content (a WWTP
financing discussion) whose only job is to give the segmenter a small,
deterministic timestamped input. Real raw transcripts stay vault-only
(`.gitignore` excludes `Transcripts/`; data-publication boundary).

Byte content is fixed on purpose: the smoke re-hashes the file and compares to
the hash it recorded at ingest, so any drift in these bytes is intended to be a
deliberate, reviewed change.
