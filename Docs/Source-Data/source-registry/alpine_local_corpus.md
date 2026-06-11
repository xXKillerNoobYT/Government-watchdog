# Source-registry note — `alpine_local_corpus`

_Sanitized provenance metadata (no raw bytes). The source-of-truth provenance
record for the on-disk Town-of-Alpine corpus ingested by GOV-124. Required durable
deliverable per the SourceArchivist GOV-133 sign-off._

| field | value |
|-------|-------|
| **source_id** | `alpine_local_corpus` |
| **name** | Town of Alpine — local meeting corpus (on-disk archive) |
| **scope / jurisdiction** | `alpine` / Town of Alpine, WY |
| **source_type** | `local_archive` (one corpus-level `sources` row) |
| **authority level** | primary |
| **original path** | `/Users/IA/Documents/TOA/TownOfAlpine` (local/vault-only) |
| **archive / raw store** | `Raw-Corpus/` — managed, **gitignored**, sha-addressed (`<sha[:2]>/<sha><ext>`) |
| **local note path** | `Docs/Source-Data/source-registry/alpine_local_corpus.md` (this file) |
| **scan date** | 2026-06-11 |
| **verification status** | `raw-preserved` (lifecycle: `selection-signed-off` → **`raw-preserved`**; per-file SHA-256 recorded) |
| **ingest tool** | `scripts/ingest_local_corpus.py` (drives the signed shared walk) |
| **selection sign-off** | GOV-133 (SourceArchivist, CONDITIONAL PASS, 2026-06-11) |

## Settled selection (binding — what was ingested)

- **124 date-named meeting folders**, 2023-04-26 → 2026-06-09, ingested **oldest→newest**.
- `.pdf` + `.txt` → **source-of-record** `documents` rows. `.md` → **derived**, provenance-only, **never** ingested as a `documents` source-of-record (hard boundary: AI-written summaries are not primary sources). `.json` / `.DS_Store` / `.err` → excluded.
- **One corpus-level `sources` row** (`alpine_local_corpus`). Per-meeting grouping is modeled by GOV-125's `meetings` table, not flattened into `sources`.
- **Raw storage = COPY** bytes into the gitignored raw store + record SHA-256 at ingest. Reference-in-place was **rejected** — `/Users/IA/Documents/TOA/TownOfAlpine` is a live agent scratch dir (`.inbox/`, `.tmp/`, digests still being written), so referencing would break the sha256-reproducibility guarantee on an upstream rewrite.
- **Binding out-of-folder allowlist (1 file):** `master/PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf` (class `notice`, source-of-record). Added to the shared walk so signed == ingested. The `PRESERVED_media{id}_` prefix marks a captured official record; top-level `.docx` briefings carry no such marker and stay excluded as agent-authored/derived. Names a person (routine public notice) — the GOV-105 PII guard covers the downstream label/alias write boundary.

## Ingest result (2026-06-11)

- **128** source-of-record files preserved → 128 `documents` rows (84 `.pdf` incl. the allowlisted notice + 44 `.txt`); **120** unique blobs in the raw store (8 sha-deduped duplicates across folders).
- **0 orphan documents** (every row resolves to `alpine_local_corpus`).
- **Reproducibility:** `python scripts/raw_preservation.py verify --object-type document` → `checked=128 ok=128 missing=0 mismatch=0`, exit 0. Second full ingest → 0 new rows, 0 bytes copied (idempotent).
- doc_type breakdown: agenda 25 · document 28 · meeting_packet 17 · minutes 3 · notice 1 · ordinance 6 · press_release 2 · report 10 · resolution 3 · staff_report 5 · transcript 18 · transcript_text 10.

## Coverage reality (carry to GOV-125 / GOV-129 — do not fake backfill)

- Only **34 of 124** meeting folders contain primary source (`.pdf`/`.txt`); the other **90 are derived-`.md`-only**.
- **Earliest folder with any primary source: 2024-10-09.** 2023-04-26 → mid-2024 is lead-only.
- Downstream (structuring 1.07, frontend GOV-129) **must render md-only dates with a visible gap / low-confidence label — never "sourced/verified."** This is the true shape of the corpus, recorded to prevent a false "we have the record" claim.

## Data boundary

Raw bytes, the SQLite DB (`Database/gov_watchdog.db`), and the raw store (`Raw-Corpus/`) are local/vault-only and never committed. Only this sanitized note + the tooling + sanitized counts/logs are committable. `read_api.py` additionally denylists the `TownOfAlpine` path token from any web-safe output.
