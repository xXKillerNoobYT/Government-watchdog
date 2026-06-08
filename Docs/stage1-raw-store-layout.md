# Stage 1 Raw-Store Layout & Reproducibility (Contract 1.04)

Issue: GOV-75 (`[Stage 1 Impl][Backend] C — Raw preservation & reproducibility hardening`)
Stage: Stage 1 Alpine implementation — Slice 1, Issue C. Blocked-by: GOV-74 (source registry).
Source: GOV-72 gap analysis §3.3 / §4 (Issue C); contract 1.04.
Scope: **Town of Alpine only. Local/vault-only. No public surface.**

This document is the committed record of **where raw bytes live, what guarantees
hold over them, and how a reviewer reproduces a hash check**. The raw bytes
themselves are never committed (see §4).

---

## 1. Raw-store layout

| Artifact class | On-disk location | Hash recorded as | Written by |
|---|---|---|---|
| Crawled PDFs / HTML | `Raw-PDFs/<YYYY>/<source-key>/<safe-name>.pdf` | `documents.sha256` = SHA-256 of the **stored file bytes** | `scripts/crawl_pdfs.py` |
| Meeting-video transcripts | `Transcripts/<YYYY>/<video_id>.json` | `transcripts.sha256` = SHA-256 of the transcript **text** (not the JSON file) | `scripts/fetch_transcripts.py` |
| SQLite inventory + provenance | `Database/gov_watchdog.db` | — | `scripts/db.py` migrations |
| Run logs | `Logs/crawl-YYYY-MM-DD.log` | — | crawler `logging` |

Path roots are relative to the backend repo root. `documents.local_path` /
`transcripts.local_path` store the repo-relative path so a row resolves to its
file as `repo_root / local_path`.

### Per-document provenance columns (`documents`)
`source_url`, `referer_url`, `local_path`, `sha256`, `size_bytes`,
`fetch_time_utc` (ISO-8601 ms, UTC), `wayback_url`, `cms_signature`,
`robots_status`, `source_id` (FK → `sources`, GOV-74). The registry row
(`sources`) carries source-level archive/preservation/verification status.

---

## 2. Guarantees (what "raw-preserved" means)

Implemented in `scripts/raw_preservation.py`:

1. **Raw-before-parse gate** (1.04-a/b) — `assert_raw_preserved(conn, object_type, id)`.
   Before any extraction/derivation reads an artifact, the gate proves the raw
   predecessor is **present on disk** and its bytes **re-hash to the recorded
   `sha256`**. `scripts/embed.py` calls this gate before populating
   `documents.raw_text`; a missing or tampered/corrupted raw artifact **blocks
   extraction** (raises `RawPreservationError`) — no parsed/derived record can
   exist without a hash-verifiable raw predecessor.

2. **Reproducibility check** (1.04-b/e) — `verify_reproducibility(conn)`.
   Re-hashes every stored raw document and compares to the recorded `sha256`,
   classifying each as `ok` / `missing` / `mismatch`. This is the
   tamper/corruption detector. A mismatch never silently passes downstream.

> **Transcript caveat.** Transcript rows hash the transcript *text*, not the
> stored JSON file, so file-bytes re-hashing would false-positive. The
> reproducibility verifier therefore covers **documents** by default; transcript
> reproducibility is a later transcript-preservation hardening pass. The
> `object_types` argument keeps the structure ready for it.

---

## 3. Reviewer replay (manual reproducibility)

To independently reproduce the hash check for one document:

```bash
# 1. read the recorded hash + path from the inventory
sqlite3 Database/gov_watchdog.db \
  "SELECT local_path, sha256 FROM documents WHERE id = <ID>;"

# 2. re-hash the stored raw file
shasum -a 256 <local_path>

# 3. compare — the two SHA-256 values must match exactly
```

Or run the automated check over the whole store (exits non-zero on any failure):

```bash
python scripts/raw_preservation.py verify
# reproducibility: checked=<n> ok=<n> missing=<n> mismatch=<n>
```

Run cadence: this `verify` command belongs in the run-log review checklist
(BACKEND_CRAWLER_WORKFLOWS "Run-log review") after every crawl run and before any
record is considered for review/publication. A non-zero exit is an
issue-creation trigger (possible tamper/corruption), not a silent log line.

---

## 4. Data-publication boundary (non-negotiable)

Raw bytes, the SQLite DB, and run logs are **local/vault-only and are never
committed to GitHub** (WORKFLOW_GOVERNANCE data-publication boundary; 1.04-g).
`.gitignore` enforces this:

```
Database/*.db
Raw-PDFs/
Transcripts/
```

Only **tooling, migrations, tests, and this documentation** are versioned. This
change set adds no raw data to the repo. If a raw artifact were ever found to
contain private identity/address/voter-registry data beyond the boundary rules,
stop and escalate to CEO / SecurityPrivacyAgent (1.15 §4; risk workflow cat. 3).

---

## 5. `crawl_runs` as the AI-gateway Lane 1 run log (1.04-f)

`crawl_runs` is the **Lane 1 (deterministic ingest)** run log of the AI-gateway
processing model (AI_GATEWAY_PROCESSING_WORKFLOW lane 1: fetch / archive / hash /
version / extract-text / store-metadata / log-run). Migration
`0004_crawl_runs_lane1.sql` adds the contract-required fields:

| Column | Meaning |
|---|---|
| `lane` | `lane1_deterministic_ingest` (the gateway lane this run belongs to) |
| `source_set` | JSON array — the **input source set** for the run (registry `source_id`s / crawler target keys) |
| `retry_count` | retries performed during the run |
| *(existing)* `started_utc`, `finished_utc`, `status`, `targets`, `new_documents`, `new_transcripts`, `notes` | run timing, status, and counts |

Written via `raw_preservation.record_crawl_run()`; `scripts/crawl_pdfs.py` routes
its run-log insert through it.
