"""Raw preservation & reproducibility hardening (GOV-75, Stage 1 Slice 1 Issue C).

Contract 1.04. Source: GOV-72 gap analysis §3.3 / §4 (Issue C).

The crawler already writes the raw-preservation primitives at fetch time —
`sha256`, `local_path`, `fetch_time_utc` (crawl_pdfs.py:337-359). What it did
NOT enforce, and what 1.04 requires, are the two guarantees this module adds:

1. RAW-BEFORE-PARSE GATE (1.04-a/b) — `assert_raw_preserved()`.
   Proves a raw artifact is present on disk AND its bytes re-hash to the
   recorded `sha256` *before* any extraction/derivation step is allowed to read
   it. embed.py calls this gate before populating `documents.raw_text`, so no
   parsed/derived record can exist without a hash-verifiable raw predecessor.
   A hash mismatch (tamper/corruption) BLOCKS extraction rather than silently
   feeding a corrupted artifact downstream.

2. REPRODUCIBILITY CHECK (1.04-b/e) — `verify_reproducibility()`.
   Re-hashes every stored raw artifact and compares to the recorded `sha256`.
   This is the tamper/corruption detector that automates the reviewer replay
   step (`shasum -a 256 <file>` → compare to inventory). The CLI `verify`
   subcommand exits non-zero on any mismatch/missing artifact so CI catches it.

It also formalizes `crawl_runs` as the AI-gateway Lane 1 (deterministic ingest)
run log (1.04-f) via `record_crawl_run()`: input source set + status + retry.

Reproducibility scope: documents store the raw *file bytes* (sha256 == hash of
the stored file), so re-hashing is exact. Transcript rows hash the transcript
*text* (not the JSON cache file), so file-bytes re-hashing would false-positive;
transcript-level reproducibility is therefore out of this verifier's default
scope and is left to a later transcript-preservation hardening pass. The
`object_types` argument keeps the structure ready for it without over-building.

Data boundary: raw bytes, the SQLite DB, and logs stay local/vault-only and are
never committed to GitHub (`.gitignore` excludes `Database/*.db`, `Raw-PDFs/`,
`Transcripts/`). This module reads and verifies; it never publishes.

Usage:
    python scripts/raw_preservation.py verify [--db PATH] [--object-type document]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Per object type: which table/columns hold the raw locator + recorded hash.
# Only "document" is verified by default (raw == stored file bytes). See module
# docstring for the transcript caveat.
_RAW_TABLES: dict[str, dict[str, str]] = {
    "document": {"table": "documents", "path_col": "local_path", "hash_col": "sha256"},
    "transcript": {"table": "transcripts", "path_col": "local_path", "hash_col": "sha256"},
}

_HASH_CHUNK = 1 << 20  # 1 MiB streaming read — don't load whole PDFs into memory


class RawPreservationError(Exception):
    """Raised when a raw artifact is missing or fails its hash check.

    This is the raw-before-parse gate's failure signal: callers MUST treat it as
    "do not extract/derive from this artifact" (1.04 failure definition).
    """


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed (memory-safe for large PDFs)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_row(conn: sqlite3.Connection, object_type: str, object_id: int) -> sqlite3.Row:
    spec = _RAW_TABLES.get(object_type)
    if spec is None:
        raise ValueError(f"unknown object_type {object_type!r}")
    row = conn.execute(
        f"SELECT id, {spec['path_col']} AS local_path, {spec['hash_col']} AS sha256 "
        f"FROM {spec['table']} WHERE id = ?",
        (object_id,),
    ).fetchone()
    if row is None:
        raise RawPreservationError(f"{object_type} id={object_id} not found")
    return row


def assert_raw_preserved(
    conn: sqlite3.Connection,
    object_type: str,
    object_id: int,
    repo_root: Path = REPO_ROOT,
) -> str:
    """Raw-before-parse gate (1.04-a/b).

    Verifies the artifact's raw predecessor is present on disk AND its bytes
    re-hash to the recorded `sha256`. Returns the verified hash on success;
    raises `RawPreservationError` (with a precise reason) otherwise. Callers
    must NOT extract/derive from an artifact that fails this gate.
    """
    row = _raw_row(conn, object_type, object_id)
    recorded = row["sha256"]
    rel_path = row["local_path"]
    if not recorded:
        raise RawPreservationError(
            f"{object_type} id={object_id}: no recorded sha256 — raw not preserved"
        )
    if not rel_path:
        raise RawPreservationError(
            f"{object_type} id={object_id}: no local_path — raw not preserved"
        )
    path = repo_root / rel_path
    if not path.exists():
        raise RawPreservationError(
            f"{object_type} id={object_id}: raw artifact missing at {rel_path}"
        )
    actual = sha256_file(path)
    if actual != recorded:
        raise RawPreservationError(
            f"{object_type} id={object_id}: hash mismatch for {rel_path} "
            f"(recorded {recorded[:12]}…, stored {actual[:12]}…) — "
            "tamper/corruption; extraction blocked"
        )
    return actual


def verify_reproducibility(
    conn: sqlite3.Connection,
    repo_root: Path = REPO_ROOT,
    object_types: tuple[str, ...] = ("document",),
) -> dict:
    """Re-hash every stored raw artifact vs its recorded `sha256` (1.04-b/e).

    Returns a summary:
        {checked, ok, missing: [...], mismatch: [...]}
    where `missing`/`mismatch` list `{object_type, id, local_path}` entries.
    A clean store has empty `missing` and `mismatch`.
    """
    summary: dict = {"checked": 0, "ok": 0, "missing": [], "mismatch": []}
    for object_type in object_types:
        spec = _RAW_TABLES[object_type]
        rows = conn.execute(
            f"SELECT id, {spec['path_col']} AS local_path, {spec['hash_col']} AS sha256 "
            f"FROM {spec['table']} "
            f"WHERE {spec['hash_col']} IS NOT NULL AND {spec['path_col']} IS NOT NULL"
        ).fetchall()
        for row in rows:
            summary["checked"] += 1
            entry = {
                "object_type": object_type,
                "id": row["id"],
                "local_path": row["local_path"],
            }
            path = repo_root / row["local_path"]
            if not path.exists():
                summary["missing"].append(entry)
                continue
            if sha256_file(path) != row["sha256"]:
                summary["mismatch"].append(entry)
                continue
            summary["ok"] += 1
    return summary


def record_crawl_run(
    conn: sqlite3.Connection,
    *,
    started_utc: str,
    finished_utc: str | None,
    status: str,
    source_set: list[str] | None = None,
    retry_count: int = 0,
    lane: str = "lane1_deterministic_ingest",
    targets: list[str] | None = None,
    new_documents: int = 0,
    new_transcripts: int = 0,
    notes: str | None = None,
) -> int:
    """Write a Lane 1 (deterministic ingest) `crawl_runs` row (1.04-f).

    Formalizes the run log with the contract-required fields: input `source_set`,
    `status`, and `retry_count` (plus the existing timing/target/count fields).
    Returns the new run id.
    """
    cur = conn.execute(
        "INSERT INTO crawl_runs (started_utc, finished_utc, status, targets, "
        "new_documents, new_transcripts, notes, lane, source_set, retry_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            started_utc,
            finished_utc,
            status,
            json.dumps(targets if targets is not None else (source_set or [])),
            new_documents,
            new_transcripts,
            notes,
            lane,
            json.dumps(source_set if source_set is not None else []),
            retry_count,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Raw preservation & reproducibility tooling (1.04)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser("verify", help="re-hash stored raw vs recorded sha256")
    v.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    v.add_argument(
        "--object-type", action="append", choices=list(_RAW_TABLES),
        help="restrict verification to one or more object types (default: document)",
    )
    args = parser.parse_args(argv)

    if args.command == "verify":
        db.apply_migrations(args.db)
        types = tuple(args.object_type or ("document",))
        with db.open_db(args.db) as conn:
            result = verify_reproducibility(conn, object_types=types)
        bad = len(result["missing"]) + len(result["mismatch"])
        print(
            f"reproducibility: checked={result['checked']} ok={result['ok']} "
            f"missing={len(result['missing'])} mismatch={len(result['mismatch'])}"
        )
        for kind in ("missing", "mismatch"):
            for e in result[kind]:
                print(f"  {kind.upper()}: {e['object_type']} id={e['id']} {e['local_path']}",
                      file=sys.stderr)
        if bad:
            print(f"FAIL: {bad} artifact(s) failed reproducibility", file=sys.stderr)
            return 1
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
