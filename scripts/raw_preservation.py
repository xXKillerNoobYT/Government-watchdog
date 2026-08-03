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

Stage 2.04 preservation-replay (GOV-262): `preservation_replay()` / the
`preservation-replay` CLI subcommand layer the FULL Stage-2 preservation-validity
gate on top of these primitives — document reproducibility + transcript-text
reconcile + `sources` seed_only→preserved validity — writing one `crawl_runs` row
tagged `preservation_replay`. It is the precondition for any Stage 2.05 extraction:
no unit may be read until this pass is green. Drift is a preservation DEFECT — never
a completeness_gap, never a re-fetch, the recorded `sha256` is NEVER overwritten.

Usage:
    python scripts/raw_preservation.py verify [--db PATH] [--object-type document]
    python scripts/raw_preservation.py preservation-replay [--db PATH] [--apply] \
        [--gap-exception SOURCE_ID ...]
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

# --- Stage 2.04 preservation-validity vocabulary (GOV-262) -----------------
# A source is preservation-valid when its raw is preserved (own bytes OR all of
# its child documents). `raw_preserved` is the value the GOV-124 ingest writes
# (ingest_local_corpus.py); `preserved` is the Stage 2.04 contract word. Both are
# accepted as preserved-states; `preserved` is the canonical upgrade target so the
# stored corpus never carries the Stage-2-INVALID `seed_only` family.
PRESERVED_STATES = frozenset({"preserved", "raw_preserved"})
INVALID_SOURCE_STATES = frozenset({"seed_only", "seed_only_unconfigured"})
CANONICAL_PRESERVED = "preserved"

# crawl_runs.status values for the preservation-replay pass (issue §4).
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_FAILED = "failed"
PRESERVATION_REPLAY_TARGET = "preservation_replay"


class RawPreservationError(Exception):
    """Raised when a raw artifact is missing or fails its hash check.

    This is the raw-before-parse gate's failure signal: callers MUST treat it as
    "do not extract/derive from this artifact" (1.04 failure definition).
    """


class RawPathEscape(RawPreservationError):
    """A STORED artifact path resolved outside the repository root (GOV-1693)."""


def _contained(repo_root: Path, stored: str) -> Path:
    """Join a stored artifact path under ``repo_root``, refusing any escape.

    `Path(root) / value` **silently discards `root` when `value` is absolute** —
    measured: ``Path("/repo") / "/etc/passwd"`` is ``/etc/passwd``, not an error.
    A `..` segment walks out just as quietly. Either way the caller would then
    read, hash, or report a file outside the preservation store while believing
    it was inside.

    **Not a live vulnerability today, and that is stated rather than implied.**
    Every writer of these columns constructs a contained relative path —
    `crawl_pdfs` uses `_safe_name()` then `local_path.relative_to(repo_root)`
    (which raises on escape), and `ingest_local_corpus` builds
    ``<store>/<sha[:2]>/<sha><ext>`` from a hex digest plus `Path.suffix`, which
    cannot contain a separator.

    The reason to check anyway: that invariant is enforced at **six-plus write
    sites and verified at none**, so every future writer has to re-derive it
    correctly. One read-side check covers all of them at once.
    """
    candidate = repo_root / stored
    root = repo_root.resolve()
    if not candidate.resolve().is_relative_to(root):
        raise RawPathEscape(
            f"stored path {stored!r} resolves outside the repository root "
            f"({candidate.resolve()}); refusing to read it"
        )
    return candidate



def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, streamed (memory-safe for large PDFs)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    """SHA-256 of a UTF-8 string.

    Mirrors ``fetch_transcripts._sha256`` exactly: transcript rows record the hash
    of their ``full_text`` (the preserved *text*), NOT the JSON cache file's bytes.
    The transcript-text reconcile pass MUST re-hash with this, not ``sha256_file``,
    or every transcript would false-positive as a mismatch (module docstring caveat).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    path = _contained(repo_root, rel_path)
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
            path = _contained(repo_root, row["local_path"])
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


def reconcile_transcript_text(
    conn: sqlite3.Connection,
    repo_root: Path = REPO_ROOT,  # accepted for call-site symmetry; text hash is path-free
) -> dict:
    """Transcript-text reconcile leg of the preservation-replay pass (GOV-262 §1).

    Re-hashes every transcript's stored ``full_text`` against its recorded
    ``sha256`` (a TEXT hash — see :func:`sha256_text`). A row whose text no longer
    re-hashes is a preservation DEFECT (drift), reported in ``mismatch``; a row
    with NULL/empty ``full_text`` but a recorded hash is reported in ``missing_text``.
    Both are fail-closed conditions — never silently dropped, never re-fetched, and
    the recorded ``sha256`` is NEVER overwritten (absolute drift rule, 2.04 contract).

    Returns ``{checked, ok, mismatch: [...], missing_text: [...]}`` where each entry
    is ``{object_type, id, local_path}`` (ids/paths only — no transcript text leaves).
    """
    summary: dict = {"checked": 0, "ok": 0, "mismatch": [], "missing_text": []}
    rows = conn.execute(
        "SELECT id, full_text, sha256, local_path FROM transcripts "
        "WHERE sha256 IS NOT NULL"
    ).fetchall()
    for row in rows:
        summary["checked"] += 1
        entry = {"object_type": "transcript", "id": row["id"], "local_path": row["local_path"]}
        text = row["full_text"]
        if not text:
            summary["missing_text"].append(entry)
            continue
        if sha256_text(text) != row["sha256"]:
            summary["mismatch"].append(entry)
            continue
        summary["ok"] += 1
    return summary


def preservation_manifest(
    conn: sqlite3.Connection,
    object_types: tuple[str, ...] = ("document", "transcript"),
) -> dict:
    """Column-stable aggregate-hash manifest of the preservation set (2.04 contract).

    Produces a deterministic digest over ``(object_type, id, sha256)`` for every
    preserved unit, sorted so the value depends only on identity + recorded hash
    (column-stable: independent of row order, scan time, or any mutable field). Two
    runs over an unchanged corpus yield the SAME ``aggregate_sha256`` — that equality
    is the reviewer's one-line proof the preservation set did not drift between runs.
    """
    lines: list[str] = []
    for object_type in object_types:
        spec = _RAW_TABLES[object_type]
        rows = conn.execute(
            f"SELECT id, {spec['hash_col']} AS sha256 FROM {spec['table']} "
            f"WHERE {spec['hash_col']} IS NOT NULL"
        ).fetchall()
        for row in rows:
            lines.append(f"{object_type}|{row['id']}|{row['sha256']}")
    lines.sort()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"unit_count": len(lines), "aggregate_sha256": digest}


def _source_children_state(
    conn: sqlite3.Connection,
    source_id: str,
    bad_document_ids: set[int],
) -> str:
    """Classify a source's document children for preservation validity.

    Returns ``"valid"`` (>=1 child, none in the failed-document set), ``"defect"``
    (has children but >=1 failed reproducibility), or ``"none"`` (no children).
    Drives "preserved-by-children": an umbrella corpus source (no own raw bytes)
    is preservation-valid exactly when every document under it re-hashes.
    """
    child_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM documents WHERE source_id = ?", (source_id,)
        ).fetchall()
    ]
    if not child_ids:
        return "none"
    if any(cid in bad_document_ids for cid in child_ids):
        return "defect"
    return "valid"


def validate_sources(
    conn: sqlite3.Connection,
    repo_root: Path,
    *,
    bad_document_ids: set[int],
    apply: bool,
    gap_exceptions: tuple[str, ...],
    run_id: int | None,
) -> dict:
    """Make every ``sources`` row preservation-valid for Stage 2 (GOV-262 §3).

    For each source:
      * already-preserved (own bytes that re-hash, OR all child documents valid) →
        ``preserved``;
      * ``seed_only``/``seed_only_unconfigured`` with own bytes that re-hash, or with
        all child documents valid → UPGRADED to the canonical ``preserved`` state
        (only when ``apply``); ``last_validated_utc`` is refreshed, immutable as-of
        fields are untouched;
      * ``seed_only`` family with no preserved bytes/children, but named in
        ``gap_exceptions`` → a deliberate-exception ``no_primary_source`` gap is
        recorded against the source (only when ``apply``);
      * everything else (undocumented ``seed_only``, a "preserved"-marked source whose
        bytes are missing/mismatch) → ``invalid`` (a fail-closed defect).

    NEVER overwrites ``raw_sha256`` and NEVER re-fetches (absolute drift rule).
    Returns lists keyed ``preserved / upgraded / exception_documented / invalid``.
    """
    out: dict = {"preserved": [], "upgraded": [], "exception_documented": [], "invalid": []}
    now = _now_utc_iso()
    gap_set = set(gap_exceptions)
    rows = conn.execute(
        "SELECT source_id, raw_preservation_status, raw_local_path, raw_sha256 "
        "FROM sources"
    ).fetchall()
    for row in rows:
        sid = row["source_id"]
        status = row["raw_preservation_status"]
        own_path = row["raw_local_path"]
        own_hash = row["raw_sha256"]
        own_state = "none"
        if own_path and own_hash:
            disk = _contained(repo_root, own_path)
            if not disk.exists():
                own_state = "missing"
            elif sha256_file(disk) != own_hash:
                own_state = "defect"  # drift — defect, not a gap, never overwritten
            else:
                own_state = "valid"
        child_state = _source_children_state(conn, sid, bad_document_ids)
        preserved_now = own_state == "valid" or child_state == "valid"
        has_defect = own_state in ("missing", "defect") or child_state == "defect"

        if status in PRESERVED_STATES:
            if preserved_now:
                out["preserved"].append(sid)
            else:
                out["invalid"].append(
                    {"source_id": sid, "status": status, "own_state": own_state,
                     "child_state": child_state,
                     "reason": "marked-preserved-but-no-valid-raw"}
                )
            continue

        # seed_only family — INVALID for Stage 2 unless upgraded or documented.
        if preserved_now and not has_defect:
            if apply:
                conn.execute(
                    "UPDATE sources SET raw_preservation_status = ?, "
                    "last_validated_utc = ? WHERE source_id = ?",
                    (CANONICAL_PRESERVED, now, sid),
                )
            out["upgraded"].append(sid)
            continue
        if sid in gap_set and not has_defect:
            if apply:
                import completeness  # local import: avoid import-time coupling
                completeness.record_gap(
                    conn,
                    subject_node_id=sid,
                    subject_node_type="source",
                    gap_type="no_primary_source",
                    detail=f"source {sid}: deliberate Stage-2.04 preservation exception "
                           "(registry seed with no preserved raw artifact)",
                    source_id=sid,
                    detected_run_id=run_id,
                    severity="warn",
                    commit=False,
                )
            out["exception_documented"].append(sid)
            continue
        out["invalid"].append(
            {"source_id": sid, "status": status, "own_state": own_state,
             "child_state": child_state,
             "reason": "seed_only-without-preserved-raw-or-documented-exception"}
        )
    if apply:
        conn.commit()
    return out


def preservation_replay(
    conn: sqlite3.Connection,
    repo_root: Path = REPO_ROOT,
    *,
    object_types: tuple[str, ...] = ("document",),
    apply: bool = False,
    gap_exceptions: tuple[str, ...] = (),
    strict: bool = True,
) -> dict:
    """Deterministic preservation-replay pass over the Alpine corpus (GOV-262).

    Runs the mandatory document reproducibility leg (``verify_reproducibility`` —
    ``document`` is ALWAYS included, never disabled), the transcript-text reconcile
    leg, and the ``sources`` preservation-validity pass, then writes ONE ``crawl_runs``
    row tagged ``preservation_replay``:

      * ANY missing/mismatch document, transcript drift/missing-text, or invalid
        source ⇒ ``status='failed'`` with every offending ``{object_type, id,
        local_path}`` (and invalid source) listed verbatim in ``notes``; on a clean
        corpus ⇒ ``status='success'`` carrying the aggregate-hash manifest.
      * The run row is written regardless of ``apply`` (it is append-only audit, not
        corpus mutation). ``apply`` gates only source-status UPGRADES and deliberate-
        exception gap writes.
      * When ``strict`` and the pass failed, raises :class:`RawPreservationError` AFTER
        the failed run row is durably written (fail-closed: the unit is refused, the
        failure is recorded, nothing downstream may read the corpus).

    Returns a result dict: ``{run_id, status, documents, transcripts, sources,
    manifest, miss_count, apply}``.
    """
    started = _now_utc_iso()
    # 'document' reproducibility is the mandatory scope and can never be disabled.
    doc_types = tuple(dict.fromkeys(("document",) + tuple(object_types)))
    documents = verify_reproducibility(conn, repo_root=repo_root, object_types=doc_types)
    transcripts = reconcile_transcript_text(conn, repo_root=repo_root)

    bad_document_ids = {e["id"] for e in documents["missing"] + documents["mismatch"]}

    # Write the run row first (status provisional) so source gaps can reference its
    # id and so a crash mid-validation still leaves an audit trail.
    run_id = record_crawl_run(
        conn,
        started_utc=started,
        finished_utc=None,
        status="running",
        targets=[PRESERVATION_REPLAY_TARGET, *doc_types, "transcript", "source"],
        source_set=[PRESERVATION_REPLAY_TARGET],
        notes=None,
    )

    sources = validate_sources(
        conn,
        repo_root,
        bad_document_ids=bad_document_ids,
        apply=apply,
        gap_exceptions=gap_exceptions,
        run_id=run_id,
    )

    misses: list[dict] = (
        list(documents["missing"])
        + list(documents["mismatch"])
        + list(transcripts["mismatch"])
        + list(transcripts["missing_text"])
    )
    miss_count = len(misses) + len(sources["invalid"])
    status = RUN_STATUS_FAILED if miss_count else RUN_STATUS_SUCCESS

    manifest = preservation_manifest(conn)
    notes_payload = {
        "pass": PRESERVATION_REPLAY_TARGET,
        "apply": apply,
        "documents": {k: documents[k] for k in ("checked", "ok", "missing", "mismatch")},
        "transcripts": transcripts,
        "sources": {
            "preserved": len(sources["preserved"]),
            "upgraded": sources["upgraded"],
            "exception_documented": sources["exception_documented"],
            "invalid": sources["invalid"],
        },
        "manifest": manifest,
    }
    conn.execute(
        "UPDATE crawl_runs SET finished_utc = ?, status = ?, notes = ? WHERE id = ?",
        (_now_utc_iso(), status, json.dumps(notes_payload, sort_keys=True), run_id),
    )
    conn.commit()

    result = {
        "run_id": run_id,
        "status": status,
        "documents": documents,
        "transcripts": transcripts,
        "sources": sources,
        "manifest": manifest,
        "miss_count": miss_count,
        "apply": apply,
    }
    if strict and status == RUN_STATUS_FAILED:
        raise RawPreservationError(
            f"preservation-replay FAILED: {miss_count} unit(s) not preservation-valid "
            f"(documents missing={len(documents['missing'])} mismatch={len(documents['mismatch'])}, "
            f"transcripts mismatch={len(transcripts['mismatch'])} "
            f"missing_text={len(transcripts['missing_text'])}, "
            f"sources invalid={len(sources['invalid'])}); "
            f"crawl_runs id={run_id} status=failed — corpus refused for Stage 2.05"
        )
    return result


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

    pr = sub.add_parser(
        "preservation-replay",
        help="Stage 2.04 deterministic preservation-validity pass over the corpus "
             "(documents + transcripts + sources); writes a crawl_runs row",
    )
    pr.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    pr.add_argument(
        "--apply", action="store_true",
        help="persist source-status upgrades + deliberate-exception gaps "
             "(default: dry-run — validate + write the run-log row only)",
    )
    pr.add_argument(
        "--gap-exception", action="append", default=[], metavar="SOURCE_ID",
        help="deliberately document this seed_only source as a no_primary_source "
             "preservation exception instead of failing on it (repeatable)",
    )
    args = parser.parse_args(argv)

    if args.command == "preservation-replay":
        db.apply_migrations(args.db)
        with db.open_db(args.db) as conn:
            try:
                result = preservation_replay(
                    conn,
                    apply=args.apply,
                    gap_exceptions=tuple(args.gap_exception),
                    strict=False,  # CLI reports via exit code; no traceback noise
                )
            except RawPreservationError as exc:  # defensive: strict=False shouldn't raise
                print(f"FAIL: {exc}", file=sys.stderr)
                return 1
        doc, tr, src = result["documents"], result["transcripts"], result["sources"]
        print(
            f"preservation-replay: status={result['status']} run_id={result['run_id']} "
            f"apply={result['apply']}"
        )
        print(
            f"  documents: checked={doc['checked']} ok={doc['ok']} "
            f"missing={len(doc['missing'])} mismatch={len(doc['mismatch'])}"
        )
        print(
            f"  transcripts: checked={tr['checked']} ok={tr['ok']} "
            f"mismatch={len(tr['mismatch'])} missing_text={len(tr['missing_text'])}"
        )
        print(
            f"  sources: preserved={len(src['preserved'])} upgraded={len(src['upgraded'])} "
            f"exception_documented={len(src['exception_documented'])} invalid={len(src['invalid'])}"
        )
        print(
            f"  manifest: unit_count={result['manifest']['unit_count']} "
            f"aggregate_sha256={result['manifest']['aggregate_sha256']}"
        )
        for e in doc["missing"] + doc["mismatch"] + tr["mismatch"] + tr["missing_text"]:
            print(f"  MISS: {e['object_type']} id={e['id']} {e['local_path']}", file=sys.stderr)
        for e in src["invalid"]:
            print(f"  INVALID SOURCE: {e['source_id']} ({e['reason']})", file=sys.stderr)
        if result["status"] == RUN_STATUS_FAILED:
            print(
                f"FAIL: {result['miss_count']} unit(s) not preservation-valid — "
                f"crawl_runs id={result['run_id']} status=failed",
                file=sys.stderr,
            )
            return 1
        return 0

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
