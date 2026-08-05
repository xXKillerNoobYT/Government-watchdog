"""Civic source-version preservation writer (GOV-1684, Stage 5 R1/Slice 1).

The deterministic, **no-model-in-the-loop** writer over the `source_versions`
table (migration 0033). It binds `{original, changed}` retrievals of the *same*
crawled civic source URL with a typed supersession/correction lineage edge, so a
later slice can diff them (Slice 2 / 5.16) and resolve what they affect (Slice 3).

This is the crawled-source analogue of the supplied-file supersede pattern
(0030 / B5): a changed retrieval is a NEW row that points a typed edge back at the
prior version — a prior row is never updated or deleted. History is append-only.

Determinism law (Directive 7 / slot .09). Every fact this module stores is
computed in code:

* ``content_hash`` is ``sha256`` of the retrieved bytes (:func:`raw_preservation.sha256_file`
  for a preserved file, or a direct digest of in-memory bytes) — never a model's
  guess at whether two documents "look the same";
* the supersession edge is derived from the content hash alone: identical hash for
  the same URL is a **no-op**, a new hash is a **new version + edge**.

Fail-closed house style:

* ``lineage_type`` is validated against the closed set :data:`LINEAGE_TYPES`; an
  unknown type is **rejected** (``UnknownLineageType``), never stored — matched by
  the DB-level CHECK as a backstop;
* the writer **refuses** a version it cannot hash (no content) or cannot
  provenance (empty ``provenance``);
* a stored ``snapshot_path`` must be repo-relative and contained — an absolute or
  ``..``-escaping path is refused (``RawPathEscape``), at write and again at read.

Concurrency. The invariant "the latest version of this URL" is read, a decision is
made, then a row is written — the exact read-then-write shape ``CLAUDE.md`` warns
opens a race under SQLite's DML-only implicit transaction. So the writer takes an
explicit ``BEGIN IMMEDIATE`` **before** the first read; the ``UNIQUE
(source_url, version_ordinal)`` constraint is the DB-level backstop if two writers
ever slip through. The writer manages its own transaction and expects an idle
connection (open it via :func:`db.open_db`).

Usage (library):

    import source_version_store as svs
    result = svs.preserve_source_version(
        conn,
        source_url="https://www.alpinewy.gov/agenda.pdf",
        retrieval_time="2026-08-04T17:00:00+00:00",
        content=raw_bytes,                      # or content_path=<repo-relative>
        provenance={"crawl_run_id": 42, "fetch_method": "http_get"},
        source_id="alpine-town-agendas",        # optional registry link
        lineage_type="supersedes",              # or "corrects"
    )
    # result["action"] is "created" or "noop"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import raw_preservation  # noqa: E402  (reused: sha256_file + contained-path guard)

REPO_ROOT = Path(__file__).resolve().parent.parent

# The closed lineage vocabulary — the same the 5.05 corrections ledger uses. A
# frozenset so any future value is a conscious, reviewed change, never drift.
LINEAGE_SUPERSEDES = "supersedes"
LINEAGE_CORRECTS = "corrects"
LINEAGE_TYPES: frozenset[str] = frozenset({LINEAGE_SUPERSEDES, LINEAGE_CORRECTS})

# Re-export so callers/tests share one path-escape type with raw_preservation.
RawPathEscape = raw_preservation.RawPathEscape


class SourceVersionError(Exception):
    """A source-version preservation request that fail-closed refuses."""


class UnknownLineageType(SourceVersionError):
    """The requested lineage type is outside the closed :data:`LINEAGE_TYPES` set."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _version_id(source_url: str, content_hash: str) -> str:
    """Deterministic version id from the URL + content hash.

    Content-addressed (no randomness, no wall-clock), so the same retrieval always
    yields the same id and tests are reproducible. Unique by construction: two
    versions of one URL differ in ``content_hash``, and the ``UNIQUE
    (source_url, content_hash)`` constraint enforces it at the DB.
    """
    digest = hashlib.sha256(f"{source_url}\n{content_hash}".encode("utf-8")).hexdigest()
    return f"srcver:{digest[:24]}"


def _content_hash(
    *,
    content: bytes | None,
    content_path: str | Path | None,
    repo_root: Path,
) -> str:
    """sha256 of the retrieved content, computed in code (fail-closed).

    Exactly one of ``content`` (in-memory bytes) or ``content_path`` (a
    repo-relative path to preserved bytes) must be given. A ``content_path`` is
    containment-checked before it is read — an absolute/escaping path is refused
    (``RawPathEscape``), because ``Path(root) / value`` silently discards ``root``
    when ``value`` is absolute.
    """
    if (content is None) == (content_path is None):
        raise SourceVersionError(
            "exactly one of content (bytes) or content_path must be provided — "
            "refusing to record a version I cannot hash"
        )
    if content is not None:
        if not isinstance(content, (bytes, bytearray)):
            raise SourceVersionError("content must be bytes")
        return hashlib.sha256(bytes(content)).hexdigest()
    path = raw_preservation._contained(repo_root, str(content_path))
    if not path.exists():
        raise SourceVersionError(
            f"content_path {content_path!r} does not exist under the repo root — "
            "refusing to record a version I cannot hash"
        )
    return raw_preservation.sha256_file(path)


def _normalize_provenance(provenance: Any) -> str:
    """Canonical JSON provenance text, or refuse (fail-closed).

    A mapping is serialized deterministically (``sort_keys``); a non-empty string is
    accepted verbatim (assumed already a provenance token/JSON). Anything empty or
    ``None`` is refused — a version with no provenance is not preservable.
    """
    if provenance is None:
        raise SourceVersionError("provenance is required — refusing an unprovenanced version")
    if isinstance(provenance, str):
        if not provenance.strip():
            raise SourceVersionError("provenance string is empty — refusing")
        return provenance
    if isinstance(provenance, dict):
        if not provenance:
            raise SourceVersionError("provenance dict is empty — refusing")
        return json.dumps(provenance, sort_keys=True, separators=(",", ":"))
    raise SourceVersionError(f"provenance must be a dict or non-empty string, got {type(provenance).__name__}")


def _validate_snapshot_path(snapshot_path: str | None, repo_root: Path) -> str | None:
    """Refuse an absolute/escaping snapshot path at write time (defense in depth).

    The load-bearing check is at the READ site (the inventory), but a well-behaved
    writer never STORES an escaping path in the first place.
    """
    if snapshot_path is None:
        return None
    raw_preservation._contained(repo_root, snapshot_path)  # raises RawPathEscape on escape
    return snapshot_path


def preserve_source_version(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    retrieval_time: str,
    provenance: Any,
    content: bytes | None = None,
    content_path: str | Path | None = None,
    source_id: str | None = None,
    lineage_type: str = LINEAGE_SUPERSEDES,
    snapshot_path: str | None = None,
    repo_root: Path = REPO_ROOT,
    now: str | None = None,
) -> dict[str, Any]:
    """Preserve one retrieved version of a civic source URL. Idempotent.

    * **Unchanged** — a version with this exact ``(source_url, content_hash)`` is
      already stored: **no-op**, no new row, no new edge (AC-3). Returns the
      existing version's identity with ``action='noop'``.
    * **Changed / first** — a new ``content_hash`` for this URL: writes exactly one
      new version row. If a prior version exists it also writes the typed
      supersession edge back at the immediately-prior version and increments the
      ordinal; the prior row is left **untouched** (AC-1/AC-2/AC-4).

    ``lineage_type`` (``supersedes`` | ``corrects``) is validated up front; an
    unknown type is refused (AC-2). The whole read-decide-write runs under
    ``BEGIN IMMEDIATE``.

    Returns ``{action, version_id, version_ordinal, content_hash,
    supersedes_version_id, lineage_type}``.
    """
    if not source_url:
        raise SourceVersionError("source_url is required")
    if not retrieval_time:
        raise SourceVersionError("retrieval_time is required")
    # Fail-closed vocabulary check BEFORE any write (matched by the DB CHECK).
    if lineage_type not in LINEAGE_TYPES:
        raise UnknownLineageType(
            f"lineage_type {lineage_type!r} is not in the closed set "
            f"{sorted(LINEAGE_TYPES)} — refusing"
        )
    provenance_text = _normalize_provenance(provenance)
    snapshot_rel = _validate_snapshot_path(snapshot_path, repo_root)
    content_hash = _content_hash(
        content=content, content_path=content_path, repo_root=repo_root
    )
    created = now or _now_utc_iso()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT version_id, version_ordinal, lineage_type, supersedes_version_id "
            "FROM source_versions WHERE source_url = ? AND content_hash = ?",
            (source_url, content_hash),
        ).fetchone()
        if existing is not None:
            # Identical content already preserved for this URL — a no-op (AC-3).
            conn.commit()
            return {
                "action": "noop",
                "version_id": existing["version_id"],
                "version_ordinal": existing["version_ordinal"],
                "content_hash": content_hash,
                "supersedes_version_id": existing["supersedes_version_id"],
                "lineage_type": existing["lineage_type"],
            }

        prior = conn.execute(
            "SELECT version_id, version_ordinal FROM source_versions "
            "WHERE source_url = ? ORDER BY version_ordinal DESC LIMIT 1",
            (source_url,),
        ).fetchone()
        if prior is None:
            ordinal = 1
            supersedes_id: str | None = None
            edge_type: str | None = None
        else:
            ordinal = int(prior["version_ordinal"]) + 1
            supersedes_id = prior["version_id"]
            edge_type = lineage_type

        version_id = _version_id(source_url, content_hash)
        conn.execute(
            "INSERT INTO source_versions (version_id, source_id, source_url, "
            "retrieval_time, content_hash, provenance, snapshot_path, "
            "version_ordinal, supersedes_version_id, lineage_type, created_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version_id,
                source_id,
                source_url,
                retrieval_time,
                content_hash,
                provenance_text,
                snapshot_rel,
                ordinal,
                supersedes_id,
                edge_type,
                created,
            ),
        )
        conn.commit()
        return {
            "action": "created",
            "version_id": version_id,
            "version_ordinal": ordinal,
            "content_hash": content_hash,
            "supersedes_version_id": supersedes_id,
            "lineage_type": edge_type,
        }
    except Exception:
        conn.rollback()
        raise


def list_versions(conn: sqlite3.Connection, source_url: str) -> list[dict[str, Any]]:
    """Every preserved version of ``source_url``, oldest-first (ordinal order).

    A read helper the inventory builds on. Returns the raw rows as dicts (including
    ``snapshot_path``); the reviewer-internal inventory decides what to project.
    """
    return [
        dict(row)
        for row in conn.execute(
            "SELECT version_id, source_id, source_url, retrieval_time, content_hash, "
            "provenance, snapshot_path, version_ordinal, supersedes_version_id, "
            "lineage_type, created_utc FROM source_versions "
            "WHERE source_url = ? ORDER BY version_ordinal ASC",
            (source_url,),
        )
    ]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Civic source-version preservation writer (GOV-1684)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--source-url", required=True)
    parser.add_argument(
        "--content-path", required=True,
        help="repo-relative path to the preserved raw bytes to hash",
    )
    parser.add_argument("--retrieval-time", required=True)
    parser.add_argument("--source-id", default=None)
    parser.add_argument(
        "--lineage-type", default=LINEAGE_SUPERSEDES, choices=sorted(LINEAGE_TYPES)
    )
    parser.add_argument(
        "--provenance", required=True,
        help="provenance JSON text or token recorded verbatim",
    )
    parser.add_argument("--snapshot-path", default=None)
    args = parser.parse_args(argv)

    db.apply_migrations(args.db)
    with db.open_db(args.db) as conn:
        result = preserve_source_version(
            conn,
            source_url=args.source_url,
            retrieval_time=args.retrieval_time,
            provenance=args.provenance,
            content_path=args.content_path,
            source_id=args.source_id,
            lineage_type=args.lineage_type,
            snapshot_path=args.snapshot_path,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
