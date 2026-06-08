"""Phase 1 smoke test: schema apply, idempotency, sample insert.

See Docs/phase1-spec.md §4.8.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _table_names(conn) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def test_apply_creates_all_tables(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        tables = _table_names(conn)
    assert tables == {
        "documents",
        "transcripts",
        "meetings",
        "meeting_documents",
        "embeddings",
        "crawl_runs",
        "sources",  # GOV-74: source registry
        "schema_migrations",  # GOV-74 §6: idempotent migration ledger
        "agenda_items",  # GOV-81: Slice 2 B — 1.07 §1 agenda_item node
        "transcript_segments",  # GOV-81: Slice 2 B — 1.07 §1 addressable segment rows
        "statements",  # GOV-82: Slice 2 C — 1.07 §1/§2 statement node
        "evidence_links",  # GOV-82: Slice 2 C — 1.07 §1.4/§2 exact-source pointer
    }


def test_apply_is_idempotent(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    db.apply_migrations(fresh_db)  # must not raise
    with db.open_db(fresh_db) as conn:
        tables = _table_names(conn)
    assert "documents" in tables


def test_sample_insert_round_trip(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?)",
            ("https://alpinewy.gov/example.pdf", "Raw-PDFs/2026/alpinewy/example.pdf", "0" * 64, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT source_url, sha256 FROM documents WHERE source_url = ?",
            ("https://alpinewy.gov/example.pdf",),
        ).fetchone()
    assert row["source_url"] == "https://alpinewy.gov/example.pdf"
    assert row["sha256"] == "0" * 64


def test_unique_source_url(fresh_db: Path) -> None:
    import sqlite3

    db.apply_migrations(fresh_db)
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?)",
            ("https://alpinewy.gov/dup.pdf", "Raw-PDFs/2026/alpinewy/dup.pdf", "1" * 64, now),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
                "VALUES (?, ?, ?, ?)",
                ("https://alpinewy.gov/dup.pdf", "Raw-PDFs/2026/alpinewy/dup.pdf", "1" * 64, now),
            )
            conn.commit()
