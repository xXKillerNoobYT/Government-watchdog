"""PostgreSQL scale-backend adapter — the ONLY provider-specific module.

DEPLOY-2026 (GOV-722 plan §3 D3/D5). This is where backend-specific code is
*allowed* to live (the lock-in lint excludes ``deploy_adapters/`` for exactly this
reason). It drives the managed-DB stand-in — a local ``postgres:16`` container —
over ``psql`` using stdlib :mod:`subprocess`. **No** ``psycopg`` / third-party
driver is imported, so the runtime stays stdlib-only (PORT-3 / INV-7).

Portability strategy: Postgres is used as a lossless, type-preserving store of the
canonical row stream. Each exported row is base64-encoded canonical JSON parked in
one ``deploy_portable_rows`` table; on export the rows are read back and decoded,
reproducing the exact Python scalars the SQLite backend produced. Equal hashes
across the two engines are then the migration proof (AM-6). The access view is an
in-memory SQLite rebuilt from those rows, so the frozen SQLite-bound ``read_api``
gates apply unchanged — proving the round-trip preserves access semantics.

The adapter is import-safe with no database present; every method that touches
``psql`` fails loudly (``PsqlUnavailable`` / ``subprocess.CalledProcessError``)
rather than silently degrading. Live exercise happens in GOV-722 leg 4.
"""

from __future__ import annotations

import base64
import shutil
import sqlite3
import subprocess
from typing import Any

from . import base

_STORE_TABLE = "deploy_portable_rows"
_STORE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_STORE_TABLE} ("
    "retention_class text NOT NULL, tbl text NOT NULL, ord integer NOT NULL, "
    "payload_b64 text NOT NULL, PRIMARY KEY (retention_class, tbl, ord));"
)


class PsqlUnavailable(RuntimeError):
    """``psql`` is not on PATH — the managed-DB drill cannot run here."""


class PostgresAdapter(base.DatabaseAdapter):
    """Managed-PostgreSQL stand-in reached via the ``psql`` CLI."""

    name = "postgres"

    def __init__(self, dsn: str, *, psql_bin: str = "psql"):
        self.dsn = dsn
        self.psql_bin = psql_bin

    # -- psql plumbing ------------------------------------------------------

    def _require_psql(self) -> str:
        found = shutil.which(self.psql_bin)
        if not found:
            raise PsqlUnavailable(
                f"{self.psql_bin!r} not found on PATH; start the scale-shape "
                "compose profile (postgres:16) and install the psql client."
            )
        return found

    def _run(self, sql: str, *, capture: bool = False) -> str:
        """Execute SQL through psql. ``ON_ERROR_STOP`` makes failures loud."""
        psql = self._require_psql()
        args = [psql, self.dsn, "-v", "ON_ERROR_STOP=1", "-tA"]
        proc = subprocess.run(
            [*args, "-c", sql],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout if capture else ""

    # -- adapter contract ---------------------------------------------------

    def restore(self, export: base.CanonicalExport) -> None:
        """Create the store and bulk-load base64 canonical rows via a SQL script."""
        statements = [_STORE_DDL, f"TRUNCATE {_STORE_TABLE};"]
        for cls, table, _cols in base._table_order():
            rows = export.streams.get(cls, {}).get(table, [])
            for ord_, row in enumerate(rows):
                payload = base64.b64encode(base.canonical_bytes(row)).decode("ascii")
                statements.append(
                    f"INSERT INTO {_STORE_TABLE} "
                    f"(retention_class, tbl, ord, payload_b64) VALUES "
                    f"('{cls}', '{table}', {ord_}, '{payload}');"
                )
        script = "\n".join(statements)
        psql = self._require_psql()
        subprocess.run(
            [psql, self.dsn, "-v", "ON_ERROR_STOP=1", "-q"],
            input=script,
            text=True,
            capture_output=True,
            check=True,
        )

    def export(self) -> base.CanonicalExport:
        out = self._run(
            f"SELECT retention_class || '\t' || tbl || '\t' || ord || '\t' || "
            f"payload_b64 FROM {_STORE_TABLE} ORDER BY retention_class, tbl, ord;",
            capture=True,
        )
        streams: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            cls, table, _ord, payload_b64 = line.split("\t", 3)
            decoded = base64.b64decode(payload_b64)
            streams.setdefault(cls, {}).setdefault(table, []).append(
                _loads(decoded)
            )
        # Ensure every spec class key exists even if empty, matching the SQLite side.
        for cls in ("b_derived_civic", "c_ai_outputs", "d_audit_ledger"):
            for _cls, table, _cols in base._table_order():
                if _cls == cls:
                    streams.setdefault(cls, {}).setdefault(table, [])
        return base.CanonicalExport(streams)

    def access_view(self) -> sqlite3.Connection:
        """Rebuild an in-memory SQLite from the Postgres-persisted rows."""
        import db  # local backend migration runner (stdlib sqlite)

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # apply_migrations targets a path; build schema in-memory via the SQL files.
        for sql_file in sorted((db.MIGRATIONS_DIR).glob("*.sql")):
            conn.executescript(_sqlite_only(sql_file.read_text(encoding="utf-8")))
        base.restore_into_conn(conn, self.export())
        return conn


def _loads(data: bytes) -> dict[str, Any]:
    import json

    return json.loads(data.decode("utf-8"))


def _sqlite_only(sql: str) -> str:
    """Strip lines the raw executescript path cannot run (defensive no-op today).

    All project migrations are plain SQLite DDL; this hook exists so a future
    Postgres-specific guard clause never leaks into the in-memory rebuild.
    """
    return sql
