"""Adapter contract + stdlib SQLite backend + canonical export model.

DEPLOY-2026 (GOV-722 plan §3). This module is backend-agnostic: it defines *what*
a portable export is (retention-class column allowlists + canonical hashing),
*how* the frozen access gates are probed, and a reference SQLite implementation.
The PostgreSQL backend lives in :mod:`.postgres_adapter`; both produce byte-for-
byte identical :class:`CanonicalExport` objects for the same fixture — that
equality is the migration proof (PORT-1/PORT-2, AM-6).

Key design choices:

* **Column allowlist, not table dump.** Only the web-safe / derived columns of
  §5 classes (b) derived civic records, (c) AI/provider outputs, (d) audit/cost
  ledger are exported. Raw-snapshot columns (class a) and reviewer-note free text
  (class g) are *structurally* excluded — a synthetic fixture may hold them, but
  they can never reach a drill artifact (PORT-4, RET-2). :func:`scan_export_for_leaks`
  is the belt-and-braces second layer.
* **Frozen gates are imported, never re-typed.** :func:`access_decisions` runs
  the real ``read_api`` publishability filters and the real ``mcp_service``
  allowlist/redaction over a backend's rows, so "access decision" here is
  identical to production by construction (no drift surface).
* **stdlib only.** No third-party driver at runtime; the Postgres adapter shells
  to ``psql`` (see that module). This keeps INV-7 / the stdlib runtime intact.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import db  # noqa: E402  (stdlib sqlite migration runner — the local backend)
import read_api  # noqa: E402  (frozen serving surface — imported, never modified)
from mcp_service import allowlists as _mcp_allowlists  # noqa: E402
from mcp_service import redaction as _mcp_redaction  # noqa: E402


# ---------------------------------------------------------------------------
# Retention-class export spec (REQ-2026-COMM §5 b/c/d — column allowlists).
# ---------------------------------------------------------------------------
# Only these columns of these tables ever leave a backend. Ordered by the table's
# primary key on export so the byte stream is deterministic. Raw-path / free-text
# columns are deliberately absent (PORT-4); adding one here would trip the
# forbidden-column guard below and the artifact-leak test.

EXPORT_SPEC: dict[str, dict[str, list[str]]] = {
    # (b) derived / structured civic records — the evidence trail, web-safe cols.
    "b_derived_civic": {
        "sources": [
            "source_id", "name", "scope", "source_class", "jurisdiction",
            "scan_date", "archive_url", "verification_status",
        ],
        "transcript_segments": [
            "segment_id", "transcript_id", "segment_index", "timestamp_seconds",
            "timestamp_human", "segment_text",
        ],
        "statements": [
            "statement_id", "segment_id", "statement_text", "verification_status",
            "publication_state", "ai_extraction_run_id",
        ],
        "evidence_links": [
            "evidence_link_id", "from_node_id", "from_node_type", "to_source_id",
            "relation", "locator_kind", "page",
        ],
    },
    # (c) AI / provider outputs incl. prompts — retained with hashes + versions
    # (RET-2 c). Free-text bodies are NOT exported into the drill artifact.
    "c_ai_outputs": {
        "ai_extraction_runs": [
            "run_id", "lane", "model_name", "model_version", "tool_version",
            "prompt_id", "output_count", "error_status", "dry_run",
            "started_utc", "finished_utc",
        ],
        "mcp_job_outputs": [
            "output_id", "job_id", "output_kind", "policy_pack_id",
            "policy_pack_version", "output_schema_id", "review_state",
            "created_utc",
        ],
    },
    # (d) audit / cost ledger — decisions + metering, no reviewer free text.
    "d_audit_ledger": {
        "reviewer_decisions": [
            "decision_id", "statement_id", "reviewer_id", "decision",
            "from_verification_status", "to_verification_status",
            "reason_category", "promoted", "decided_utc", "created_utc",
        ],
        "mcp_audit_events": [
            "audit_id", "grant_id", "seq", "job_id", "area_id", "kind", "name",
            "outcome", "error_code", "created_at",
        ],
    },
}

# Class (a) raw snapshots and class (g) reviewer notes: local/vault-only forever
# (RET-2). A drill artifact that names any of these tables is a PORT-4 failure.
EXCLUDED_TABLES: frozenset[str] = frozenset(
    {"transcripts", "documents", "crawl_runs", "meeting_documents", "embeddings"}
)

# Column *names* that must never appear in an export stream — a static tripwire so
# a future spec edit that adds a raw/secret column fails the test, not production.
_FORBIDDEN_COLUMN_RE = re.compile(
    r"(raw_local_path|local_note_path|transcript_path|full_text|raw_sha256"
    r"|secret|password|token|hmac|private_key|api_key|\breason\b)",
    re.IGNORECASE,
)

# Value-level secret shapes (belt-and-braces over the read_api raw-path scanner,
# which catches filesystem paths / raw markers but not opaque secrets).
_SECRET_VALUE_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[0-9A-Za-z-]+"
    r"|password\s*[:=]|secret\s*[:=])",
    re.IGNORECASE,
)


def _table_order() -> list[tuple[str, str, list[str]]]:
    """(retention_class, table, columns) in a fixed, dependency-safe order."""
    out: list[tuple[str, str, list[str]]] = []
    for cls in ("b_derived_civic", "c_ai_outputs", "d_audit_ledger"):
        for table, cols in EXPORT_SPEC[cls].items():
            out.append((cls, table, cols))
    return out


# ---------------------------------------------------------------------------
# Canonical serialization + hashing.
# ---------------------------------------------------------------------------

def canonical_bytes(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact, ASCII-safe."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CanonicalExport:
    """A retention-class-partitioned, deterministically hashed row export.

    ``streams[class][table]`` is a list of row dicts (allowlisted columns only,
    ordered by primary key). ``class_hashes[class]`` and :attr:`manifest_hash`
    are sha256 over the canonical bytes — equal hashes across two backends prove
    a lossless, semantics-preserving round-trip (AM-6).
    """

    def __init__(self, streams: dict[str, dict[str, list[dict[str, Any]]]]):
        self.streams = streams
        self.class_hashes: dict[str, str] = {
            cls: sha256_hex(canonical_bytes(tables))
            for cls, tables in streams.items()
        }
        self.manifest_hash: str = sha256_hex(canonical_bytes(self.class_hashes))

    def tables(self) -> set[str]:
        return {t for tables in self.streams.values() for t in tables}

    def row_count(self) -> int:
        return sum(
            len(rows) for tables in self.streams.values() for rows in tables.values()
        )

    def to_manifest(self) -> dict[str, Any]:
        """A hash-only manifest safe to log/report (no row payloads)."""
        return {
            "retention_classes": sorted(self.streams),
            "tables": sorted(self.tables()),
            "row_count": self.row_count(),
            "class_hashes": self.class_hashes,
            "manifest_hash": self.manifest_hash,
        }

    def to_json(self) -> str:
        return canonical_bytes(self.streams).decode("utf-8")


# ---------------------------------------------------------------------------
# Shared export / restore over a sqlite3 connection (both adapters reuse this
# for the SQLite side; the Postgres adapter overrides the storage half).
# ---------------------------------------------------------------------------

def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def export_from_conn(conn: sqlite3.Connection) -> CanonicalExport:
    """Read the allowlisted retention streams out of a live SQLite connection."""
    streams: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for cls, table, cols in _table_order():
        present = _existing_columns(conn, table)
        use = [c for c in cols if c in present]
        pk = use[0]  # spec lists the primary key first — deterministic order.
        rows = [
            {c: row[c] for c in use}
            for row in conn.execute(
                f"SELECT {', '.join(use)} FROM {table} ORDER BY {pk}"
            )
        ]
        streams.setdefault(cls, {})[table] = rows
    return CanonicalExport(streams)


def _required_defaults(conn: sqlite3.Connection, table: str,
                       have: set[str]) -> dict[str, Any]:
    """Sentinels for NOT-NULL columns absent from the export.

    Raw/free-text columns (e.g. ``reviewer_decisions.reason``, class g) are
    deliberately not exported (PORT-4). The production schema still marks some of
    them NOT NULL, so a restore fills a deterministic, content-free sentinel — it
    is never re-exported (not on the allowlist) and never read by an access gate,
    so it affects neither hash parity nor access decisions.
    """
    defaults: dict[str, Any] = {}
    for cid, name, ctype, notnull, dflt, pk in conn.execute(
        f"PRAGMA table_info({table})"
    ):
        if notnull and dflt is None and not pk and name not in have:
            defaults[name] = 0 if (ctype or "").upper() in {"INTEGER", "REAL"} else ""
    return defaults


def restore_into_conn(conn: sqlite3.Connection, export: CanonicalExport) -> None:
    """Load an export's rows into a freshly-migrated SQLite connection.

    Foreign keys are disabled for the bulk load (a standard restore posture:
    class (a) parent rows such as ``transcripts`` are intentionally absent from
    the export, so referential inserts would otherwise fail). The access-gate
    probe never depends on those excluded parents.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    for cls, table, _cols in _table_order():
        rows = export.streams.get(cls, {}).get(table, [])
        for row in rows:
            merged = dict(_required_defaults(conn, table, set(row)))
            merged.update(row)
            keys = list(merged)
            placeholders = ", ".join("?" for _ in keys)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})",
                [merged[k] for k in keys],
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Leak scanner (PORT-4 / RED: "secrets or private data present in drill artifacts").
# ---------------------------------------------------------------------------

def scan_export_for_leaks(export: CanonicalExport) -> list[str]:
    """Return a list of leak findings; empty means the artifact is clean.

    Three independent layers:
      1. no excluded (class a/g) table may appear;
      2. no forbidden column name (raw path / note / secret-shaped) may appear;
      3. every string value passes the frozen ``read_api`` raw-path scanner and a
         secret-shape regex.
    """
    findings: list[str] = []
    for table in export.tables():
        if table in EXCLUDED_TABLES:
            findings.append(f"excluded table present in export: {table}")
    for tables in export.streams.values():
        for table, rows in tables.items():
            for row in rows:
                for col in row:
                    if _FORBIDDEN_COLUMN_RE.search(col):
                        findings.append(f"forbidden column exported: {table}.{col}")
    # Value scan: reuse the frozen transport-leak guard, then a secret regex.
    try:
        read_api.assert_no_raw_paths(export.streams)
    except Exception as exc:  # read_api raises on a raw path / raw marker.
        findings.append(f"raw-path scanner tripped: {exc}")
    for match in _SECRET_VALUE_RE.finditer(export.to_json()):
        findings.append(f"secret-shaped value in artifact: {match.group(0)[:24]}")
    return findings


# ---------------------------------------------------------------------------
# Synthetic-only guard (PORT-4 / INV-7: real registry never touched by drill).
# ---------------------------------------------------------------------------

class RealRegistryRefused(RuntimeError):
    """Raised when a drill path is aimed at the real registry or raw store."""


_REAL_MARKERS = ("gov_watchdog.db", "obsidian vault", "townofalpine", "source-data")


def assert_synthetic_path(path: str | Path) -> Path:
    """Fail closed unless ``path`` is clearly a synthetic/scratch fixture.

    Refuses the real registry filename, the repo ``Database/`` dir, and known
    raw-vault path fragments. The drill builds its own fixture in a scratch dir;
    this guard makes "point it at the real DB" impossible by code, not by
    convention.
    """
    p = Path(path).expanduser()
    lowered = str(p).lower()
    for marker in _REAL_MARKERS:
        if marker in lowered:
            raise RealRegistryRefused(
                f"refusing non-synthetic path (matched {marker!r}): {p}"
            )
    # The repo registry dir is off-limits even under a different filename.
    try:
        db_dir = (db.REPO_ROOT / "Database").resolve()
        if db_dir in p.resolve().parents or p.resolve() == db_dir:
            raise RealRegistryRefused(f"refusing path under the repo Database/ dir: {p}")
    except RealRegistryRefused:
        raise
    except OSError:
        pass
    return p


# ---------------------------------------------------------------------------
# Access-decision probe — runs the FROZEN gates over a backend's rows.
# ---------------------------------------------------------------------------

def access_decisions(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute the deterministic access-decision set for a backend.

    Uses only frozen serving surfaces, so this is production's notion of access:
      * ``read_api.reviewer_internal_records`` — reviewer-cleared, not-yet-public
        served set (the meaningful non-empty surface for a reviewed fixture);
      * ``read_api.published_records`` — the public lane (empty until the owner
        gate; proving it stays 0 is itself a decision);
      * MCP ``allowlists.project`` + ``redaction.scan_findings`` — the least-
        privilege field projection and leak scan applied to each served row.

    Returned value is JSON-canonicalisable; callers hash it and compare backends.
    """
    reviewer = read_api.reviewer_internal_records(conn)
    published = read_api.published_records(conn)

    mcp_projections: list[dict[str, Any]] = []
    for rec in reviewer:
        candidate = {
            "statement_id": rec.get("statementId") or rec.get("statement_id"),
            "text": rec.get("text") or rec.get("statementText"),
            "segment_id": rec.get("segmentId") or rec.get("segment_id"),
            "verification_status": rec.get("verificationStatus"),
            "publication_state": rec.get("publicationState"),
        }
        projected = _mcp_allowlists.project("evidence.statement", candidate)
        findings = _mcp_redaction.scan_findings(projected)
        mcp_projections.append(
            {"projection": projected, "redaction_findings": findings}
        )

    return {
        "reviewer_internal_ids": sorted(
            (r.get("statementId") or r.get("statement_id")) for r in reviewer
        ),
        "reviewer_internal_count": len(reviewer),
        "published_count": len(published),
        "mcp_projections": mcp_projections,
    }


def decisions_hash(decisions: dict[str, Any]) -> str:
    return sha256_hex(canonical_bytes(decisions))


# ---------------------------------------------------------------------------
# The adapter interface + the reference SQLite implementation.
# ---------------------------------------------------------------------------

class DatabaseAdapter:
    """Backend contract. Subclasses map a storage backend onto the portable
    export/restore/access-view triple. Civic-domain code depends only on this."""

    name = "abstract"

    def restore(self, export: CanonicalExport) -> None:
        raise NotImplementedError

    def export(self) -> CanonicalExport:
        raise NotImplementedError

    def access_view(self) -> sqlite3.Connection:
        """A read-only SQLite connection the frozen gates can run against.

        Local backend: the live DB. Scale backend: an in-memory SQLite rebuilt
        from the backend's rows, so ``read_api`` (SQLite-bound) still applies —
        proving the round-trip preserves access semantics."""
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - trivial
        pass


class SqliteAdapter(DatabaseAdapter):
    """The local backend: a real, migrated SQLite database file."""

    name = "sqlite"

    def __init__(self, db_path: str | Path, *, migrate: bool = False):
        self.db_path = Path(db_path)
        if migrate:
            db.apply_migrations(self.db_path)

    def restore(self, export: CanonicalExport) -> None:
        db.apply_migrations(self.db_path)  # schema first, then rows.
        conn = db.open_db(self.db_path)
        try:
            restore_into_conn(conn, export)
        finally:
            conn.close()

    def export(self) -> CanonicalExport:
        conn = db.open_db(self.db_path)
        try:
            return export_from_conn(conn)
        finally:
            conn.close()

    def access_view(self) -> sqlite3.Connection:
        return db.open_db(self.db_path)
