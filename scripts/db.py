"""DB helpers for the Government Watchdog Phase 1 pipeline.

See Docs/phase1-spec.md §5 for the schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "Database" / "gov_watchdog.db"
MIGRATIONS_DIR = REPO_ROOT / "Database" / "migrations"


def apply_migrations(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        raise RuntimeError(f"no migrations found in {MIGRATIONS_DIR}")
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for path in sql_files:
            conn.executescript(path.read_text(encoding="utf-8"))
        conn.commit()


def open_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    apply_migrations()
    print(f"applied migrations to {DEFAULT_DB_PATH}")
