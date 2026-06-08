"""DB helpers for the Government Watchdog Phase 1 pipeline.

See Docs/phase1-spec.md §5 for the schema.

Migration runner (GOV-74 §6 fix): ``apply_migrations`` is re-run safe. Two
guarantees layered together:

1. A ``schema_migrations`` ledger records which migration files have been
   applied (by filename stem). Already-applied versions are skipped on the
   next run — an audit trail and a fast-path.
2. Every statement is applied through an idempotent executor. ``CREATE ... IF
   NOT EXISTS`` is naturally re-runnable, but SQLite has no
   ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``; the executor guards each
   ``ADD COLUMN`` with a ``PRAGMA table_info`` check and skips columns that
   already exist. This makes additive ``ALTER`` migrations (0002, 0003+)
   safe even on a legacy DB that pre-dates the ledger.

Constraint: migration ``.sql`` files use one statement per ``;`` with no
semicolons embedded in string literals or triggers (true for all current
migrations). The simple splitter below relies on that.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "Database" / "gov_watchdog.db"
MIGRATIONS_DIR = REPO_ROOT / "Database" / "migrations"

_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>[\"\w]+)\s+ADD\s+COLUMN\s+(?P<col>[\"\w]+)",
    re.IGNORECASE,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _statements(sql: str) -> list[str]:
    """Split a migration file into individual statements.

    Strips full-line ``--`` comments, then splits on ``;``. Adequate for the
    project's simple, trigger-free migration files (see module docstring).
    """
    no_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    return [stmt.strip() for stmt in no_comments.split(";") if stmt.strip()]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    table = table.strip('"')
    column = column.strip('"')
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _apply_statement(conn: sqlite3.Connection, stmt: str) -> None:
    """Execute one statement idempotently.

    Only special-cases ``ADD COLUMN`` (not re-runnable in SQLite); everything
    else is expected to use ``IF NOT EXISTS`` and is passed through.
    """
    match = _ADD_COLUMN_RE.match(stmt)
    if match and _column_exists(conn, match["table"], match["col"]):
        return
    conn.execute(stmt)


def apply_migrations(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        raise RuntimeError(f"no migrations found in {MIGRATIONS_DIR}")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_utc TEXT NOT NULL)"
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sql_files:
            version = path.stem
            if version in applied:
                continue
            for stmt in _statements(path.read_text(encoding="utf-8")):
                _apply_statement(conn, stmt)
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_utc) VALUES (?, ?)",
                (version, _utcnow()),
            )
        conn.commit()


def open_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    apply_migrations()
    print(f"applied migrations to {DEFAULT_DB_PATH}")
