# Government Watchdog — Phase 1 Spec (Alpine, WY data foundation)

**Issue:** WEI-255
**Standard:** Chaos Coding §5 (spec-before-code) — must be approved before any non-spike code.
**Author:** CTO (agent 328fddb9)
**Date:** 2026-05-03
**Status:** Draft v1 — awaiting approval.
**Scope marker:** Alpine, Wyoming **only**. Lincoln County and statewide are out of scope until Phase 1 is locked.

Source-of-truth references:
- Vault main spec: `01_projects/Government-Watchdog/Docs/Government-Watchdog.md`
- Crawler spec v2: `01_projects/Government-Watchdog/Docs/Crawler-Spec-Humanlike-and-Robots.md`
- Sourcing/auditability rules: `01_projects/Government-Watchdog/Docs/Strict-Sourcing-Auditability-and-Testing-Rules.md`
- Audit of carry-forward artifacts: `01_projects/Government-Watchdog/AUDIT-2026-04-25.md`
- Restart memo: `01_projects/Government-Watchdog/STATE-2026-04-23.md`

---

## 1. Problem statement

The Government Watchdog project needs a clean Phase 1 data foundation for Alpine, WY before any analysis or newsletter generation. The previous (Hermes-era) team left behind one verifiable PDF, an empty SQLite file, a basic crawler script, and an unrun transcript fetcher. No transcripts, no real schema, no government-mechanics doc, and no end-to-end pipeline.

Phase 1 freezes the data layer: a crawler that produces a reliably indexed corpus of Alpine PDFs and YouTube transcripts with full provenance, a real SQLite schema (including embeddings produced by local Ollama `nomic-embed-text`), and a one-page reference of how Alpine + Wyoming local government actually works. Nothing in Phase 1 generates editorial content; that is Phase 2.

## 2. Scope

In-scope:
1. **PDF crawler** for `alpinewy.gov`, `lincolncountywy.gov` (Alpine-relevant pages only), and the Alpine entry on `library.municode.com` → store under `Raw-PDFs/` with provenance metadata.
2. **YouTube transcript bulk-pull** for Alpine town meetings, last 12–24 months prioritized → store under `Transcripts/`.
3. **SQLite schema** under `Database/gov_watchdog.db` covering `documents`, `transcripts`, `meetings`, `embeddings`, with a 768-dim embedding column populated by local Ollama `nomic-embed-text`.
4. **`Docs/Alpine-Government-Mechanics.md`** — short reference of charter, codes, transparency laws, meeting cadence.

Out-of-scope (deferred):
- Newsletter generation, prompt invocation, lens output (Forge / Horizon / Sentinel) — Phase 2.
- Honesty Tracker, History Looks Back, Transparency Alert pipelines — Phase 2.
- Lincoln County beyond what is needed for Alpine context.
- Wayback Machine submission automation (deferred to Phase 1.5; capture URLs only in Phase 1).
- Telegram delivery / heartbeat reporting plumbing (already covered separately).
- A custom JS-rendering crawler. Phase 1 uses static HTTP + sitemap parsing only; Playwright is a Phase 1.5 escalation if static crawling misses material content.

## 3. User stories

- **As Isaac**, after Phase 1 lands I can run one command and see ≥10 PDFs and ≥5 transcripts indexed in `Database/gov_watchdog.db` with source URL + fetch time on every row.
- **As Herm (project agent)**, my daily heartbeat can `SELECT` the new rows from `documents` and `transcripts` since the last run without scraping the filesystem.
- **As a future Phase-2 agent**, I can do a vector search across PDFs + transcripts using `nomic-embed-text` embeddings already stored in the DB — no re-embedding required.
- **As an auditor**, I can pick any row and trace back to a verifiable source URL, exact UTC fetch timestamp, SHA256, and a Wayback link (live or queued).

## 4. Acceptance criteria

1. **Spec frozen** — this document approved; any change after freeze re-opens it (Chaos Coding §3.2).
2. **Schema migration committed** — `Database/migrations/0001_init.sql` creates the tables in §5; running it on a fresh empty DB is idempotent (`CREATE TABLE IF NOT EXISTS`).
3. **End-to-end run** produces, against the real Alpine sources (no fixtures):
   - ≥ 10 unique PDFs in `Raw-PDFs/` with rows in `documents`.
   - ≥ 5 transcripts in `Transcripts/` with rows in `transcripts`.
   - Every row has `source_url`, `fetch_time_utc` (ISO-8601), `sha256` (PDFs) or `video_id` (transcripts), and `embedding` populated.
4. **Mechanics doc** committed at `Docs/Alpine-Government-Mechanics.md` — covers Wyoming municipal classification, Alpine charter form, meeting cadence, public-records / open-meetings statutes, with cited URLs.
5. **Idempotency** — re-running the crawler against an unchanged web state adds zero new rows and downloads zero new files (verified by row count + filesystem mtime).
6. **No paid API calls** — `git grep` for `anthropic`, `openai`, `api.openai.com`, `api.anthropic.com` returns nothing in committed source. All embeddings via local Ollama.
7. **No human-in-the-loop** — pipeline runs end-to-end from a single entrypoint (`scripts/phase1_run.py` or equivalent) without prompts.
8. **Local CI sanity** — `python -m pytest tests/test_smoke.py` passes; the smoke test checks schema apply + idempotent crawl + ≥1 sample insert (against a fixture; the live ≥10/≥5 acceptance is checked by an `Acceptance` log written by the run).

## 5. Data model

SQLite at `Database/gov_watchdog.db`. All tables created via `Database/migrations/0001_init.sql`.

```sql
-- Documents (PDFs)
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    source_url      TEXT NOT NULL UNIQUE,
    referer_url     TEXT,
    title           TEXT,
    doc_type        TEXT,            -- agenda|minutes|ordinance|resolution|code|packet|other
    doc_date        TEXT,            -- ISO-8601 if extractable from URL/text
    local_path      TEXT NOT NULL,   -- relative to repo root, under Raw-PDFs/
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER,
    fetch_time_utc  TEXT NOT NULL,   -- ISO-8601 with milliseconds
    wayback_url     TEXT,            -- queued or null in Phase 1
    cms_signature   TEXT,            -- e.g. "civicplus", "granicus", "wordpress", "static"
    robots_status   TEXT,            -- "allowed" | "disallowed-skipped" | "test-mode"
    raw_text        TEXT             -- pdftotext output, nullable until extracted
);
CREATE INDEX IF NOT EXISTS idx_documents_doc_date ON documents(doc_date);
CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);

-- YouTube transcripts
CREATE TABLE IF NOT EXISTS transcripts (
    id              INTEGER PRIMARY KEY,
    video_id        TEXT NOT NULL UNIQUE,    -- 11-char YouTube id
    video_url       TEXT NOT NULL,
    channel_id      TEXT,
    channel_title   TEXT,
    upload_date     TEXT,                    -- ISO-8601
    meeting_date    TEXT,                    -- ISO-8601, derived/asserted
    duration_seconds INTEGER,
    language        TEXT,
    segment_count   INTEGER,
    full_text       TEXT NOT NULL,           -- joined transcript
    timestamped_text TEXT,                   -- "MM:SS line\n..."
    local_path      TEXT NOT NULL,           -- under Transcripts/<year>/<video_id>.json
    sha256          TEXT NOT NULL,           -- of full_text
    fetch_time_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_meeting_date ON transcripts(meeting_date);

-- Meetings (links docs ⇄ transcripts; one row per Alpine town council meeting)
CREATE TABLE IF NOT EXISTS meetings (
    id              INTEGER PRIMARY KEY,
    meeting_date    TEXT NOT NULL,           -- ISO-8601
    body            TEXT NOT NULL,           -- "alpine-town-council" | "alpine-planning" | etc.
    title           TEXT,
    source_url      TEXT,                    -- agenda page URL
    transcript_id   INTEGER REFERENCES transcripts(id),
    notes           TEXT,
    fetch_time_utc  TEXT NOT NULL,
    UNIQUE (meeting_date, body)
);

-- Many-to-many: documents attached to a meeting (agenda packet, minutes, etc.)
CREATE TABLE IF NOT EXISTS meeting_documents (
    meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role            TEXT,                    -- "agenda" | "minutes" | "packet" | "ordinance"
    PRIMARY KEY (meeting_id, document_id)
);

-- Embeddings (separate table so we can re-embed without rewriting documents/transcripts)
CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER PRIMARY KEY,
    object_type     TEXT NOT NULL,           -- "document" | "transcript" | "meeting"
    object_id       INTEGER NOT NULL,
    chunk_index     INTEGER NOT NULL,        -- 0-based within the source object
    chunk_text      TEXT NOT NULL,
    model           TEXT NOT NULL,           -- "nomic-embed-text:v1.5" or whatever Ollama tag
    dim             INTEGER NOT NULL,        -- 768 for nomic-embed-text
    vector          BLOB NOT NULL,           -- float32 little-endian, length = dim*4
    embed_time_utc  TEXT NOT NULL,
    UNIQUE (object_type, object_id, chunk_index, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_object ON embeddings(object_type, object_id);

-- Crawl log (one row per crawler invocation, for idempotency + heartbeat)
CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY,
    started_utc     TEXT NOT NULL,
    finished_utc    TEXT,
    status          TEXT NOT NULL,           -- "running" | "ok" | "error"
    targets         TEXT NOT NULL,           -- JSON array of seed URLs
    new_documents   INTEGER DEFAULT 0,
    new_transcripts INTEGER DEFAULT 0,
    notes           TEXT
);
```

Embedding storage: float32 LE bytes in `vector`. Decode with `numpy.frombuffer(blob, dtype='<f4')`. No vector-search extension required for Phase 1 — Phase 2 can switch to `sqlite-vec` or a sidecar index without schema change (just add an index table).

Chunking for embeddings: token-naive 800-char windows with 100-char overlap, applied to PDF `raw_text` and transcript `full_text`. One row per chunk in `embeddings`.

## 6. Interfaces

Single repo at `C:/Users/weird/GitHub/Government-watchdog/`. Layout:

```
Government-watchdog/
  Docs/
    phase1-spec.md                 (this file)
    Alpine-Government-Mechanics.md (deliverable §2.4)
  Database/
    gov_watchdog.db                (gitignored)
    migrations/0001_init.sql
  Raw-PDFs/<YYYY>/<source>/<filename>.pdf   (gitignored)
  Transcripts/<YYYY>/<video_id>.json        (gitignored)
  scripts/
    phase1_run.py        (entrypoint: db init → crawl → transcripts → embed)
    crawl_pdfs.py        (PDF discovery + download)
    fetch_transcripts.py (YouTube bulk-pull)
    embed.py             (Ollama nomic-embed-text wrapper)
    db.py                (schema apply + helpers)
  tests/
    test_smoke.py
  .gitignore             (excludes Database/*.db, Raw-PDFs/, Transcripts/, .venv)
  requirements.txt
  README.md
```

External interfaces:
- **HTTP** to alpinewy.gov, lincolncountywy.gov, library.municode.com — `requests` + `beautifulsoup4` + sitemap parsing.
- **YouTube transcripts** — `youtube-transcript-api` (already used by the carry-forward `fetch_transcript.py`).
- **YouTube channel discovery** — yt-dlp `--flat-playlist` against the Alpine town channel; channel ID is an open question (§8 Q1).
- **Ollama** at `http://localhost:11434/api/embeddings` with `model=nomic-embed-text`.

CLI surface:
- `python scripts/phase1_run.py` — full pipeline.
- `python scripts/phase1_run.py --crawl-only` / `--transcripts-only` / `--embed-only` — partial reruns.
- `python scripts/phase1_run.py --as-of YYYY-MM-DD` — backtest mode (per sourcing rules §"Date-Based Testing").

## 7. Risks

1. **Carry-forward DB schema drift.** The vault `Database/gov_watchdog.db` shows zero tables when copied (the audit's "1 row in `documents`" no longer reproduces). Treat the carry-forward DB as untrusted. Mitigation: this spec rebuilds from `0001_init.sql` on a fresh path inside the repo; the vault DB is not imported.
2. **Carry-forward crawler hardcoded to a WSL Obsidian path.** `watchdog_crawler.py` `__init__` uses `/mnt/c/Users/weird/Obsidain/...`. We rewrite — do not import.
3. **YouTube transcript availability.** Alpine town meetings may not have auto-captions; some may be private/unlisted. Mitigation: log every miss, accept that "≥5 transcripts" is the floor, and treat misses as a Phase-1.5 follow-up (e.g. Whisper local transcription).
4. **Robots.txt + rate-limiting.** Crawler must respect robots.txt by default (per Crawler Spec v2). Risk of soft-banning if delays are too tight. Mitigation: 3–12s jittered delays already specified; cap at 20 req/min/domain.
5. **PDF text extraction.** `pdftotext` may produce garbage on scanned PDFs. Mitigation: store raw bytes + best-effort `raw_text`; flag `raw_text IS NULL OR length(raw_text) < 200` rows for Phase 1.5 OCR.
6. **Embedding model availability.** `nomic-embed-text` must be pulled into local Ollama. Mitigation: `phase1_run.py` checks `ollama list`, fails fast with an instruction line if missing.
7. **Wayback submission cost.** Submitting every URL on the first run is slow and brittle. Mitigation: store the canonical Wayback URL pattern only in Phase 1 (`https://web.archive.org/web/*/<url>`), defer save-page-now to Phase 1.5.
8. **Re-download avoidance.** The carry-forward script logged the same PDF twice. Mitigation: idempotency is enforced by `(source_url) UNIQUE` in `documents` + SHA256 dedupe before insert.
9. **Out-of-scope creep.** "All PDFs from Lincoln County" is in the spec narrative but Alpine is the locked scope. Mitigation: §2 explicitly limits Lincoln County to Alpine-relevant pages; statewide is excluded.

## 8. Open questions

1. **Alpine town YouTube channel ID** — not yet documented. Action: look up before kicking off transcripts (1-shot manual check, not a code blocker). Acceptable answer: a single channel ID, or a list of search queries if no official channel exists.
2. **Municode coverage** — does Alpine's Municode page expose downloadable PDFs of the code, or only an HTML viewer? If only HTML, Phase 1 captures the HTML as a snapshot (saved as PDF via `wkhtmltopdf`?) — Isaac to confirm scope. **Default if no answer:** capture HTML to a `.html` file with the same provenance schema; do not block Phase 1 on full code-PDF rendering.
3. **`meeting_date` extraction** — confirm whether Phase 1 needs date-extraction heuristics or whether storing `NULL` for unrecognized dates is acceptable. **Default if no answer:** allow NULL, expose a Phase-2 enrichment task.
4. **Heartbeat integration** — does the daily heartbeat agent (Herm) pull from `crawl_runs` directly, or do we emit a markdown summary to `Logs/Heartbeats/<date>.md`? **Default if no answer:** emit both — DB row is canonical, MD is human-readable.

Defaults above are explicit so that if Isaac does not answer, Phase 1 still ships and the choices are auditable.

## 9. Plan / sequencing

Once spec is approved, work splits into four child issues (one per deliverable). All four can proceed in parallel after the schema is committed:

- **Child A** — `0001_init.sql` migration + `scripts/db.py` + `tests/test_smoke.py` (unblocks B/C/D).
- **Child B** — `scripts/crawl_pdfs.py` (depends on A).
- **Child C** — `scripts/fetch_transcripts.py` (depends on A).
- **Child D** — `Docs/Alpine-Government-Mechanics.md` (parallel; no code dependency).
- **Closeout** — `scripts/embed.py` + `scripts/phase1_run.py` end-to-end, then run against live sources to satisfy §4.3.

Estimated effort once spec is approved: 1–2 days of focused work per child issue, assuming Ollama and Python toolchain are already on the host (they are — both are used by other agents).

## 10. Approval

Spec freeze requested via `request_confirmation` interaction on WEI-255 with idempotency key `confirmation:WEI-255:plan:v1`. Child issues will be created **only after acceptance** (per CTO operating contract).
