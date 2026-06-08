"""Tests for the Alpine source registry + seed loader (GOV-74, Issue B).

Covers the acceptance criteria:
- migration 0003 idempotent (apply twice — re-run safe);
- seed populates the Alpine source set;
- FK linkage documents/transcripts -> sources;
- non-Alpine scope rejected;
- loader idempotent (no duplicate rows);
- reconciliation back-fills source_id for existing artifacts.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import source_inventory as si  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# --- migration 0003 -------------------------------------------------------

def test_migration_creates_sources_table(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        cols = _columns(conn, "sources")
    # spot-check the contract-required 1.02-b fields
    for required in (
        "source_id", "name", "scope", "url", "original_url", "source_class",
        "source_authority_level", "jurisdiction", "robots_policy", "scan_date",
        "last_validated_utc", "archive_url", "archive_status", "raw_sha256",
        "raw_preservation_status", "local_note_path", "verification_status",
        "correction_status", "topic_tags", "notes",
    ):
        assert required in cols, f"sources.{required} missing"


def test_documents_and_transcripts_gain_source_id_fk(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        assert "source_id" in _columns(conn, "documents")
        assert "source_id" in _columns(conn, "transcripts")


def test_migration_idempotent_applied_twice(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    db.apply_migrations(fresh_db)  # must not raise (the §6 fix)
    with db.open_db(fresh_db) as conn:
        # exactly one source_id column on documents (no duplicate ADD COLUMN)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(documents)")]
        assert cols.count("source_id") == 1
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert "0003_sources" in ledger


def test_scope_check_rejects_non_alpine(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sources (source_id, name, scope) VALUES (?, ?, ?)",
                ("bad", "Statewide source", "wyoming"),
            )
            conn.commit()


# --- seed loader ----------------------------------------------------------

def test_loader_populates_alpine_set(fresh_db: Path) -> None:
    si.load(fresh_db)
    with db.open_db(fresh_db) as conn:
        ids = {r[0] for r in conn.execute("SELECT source_id FROM sources")}
    assert {
        "alpinewy_gov",
        "lincolncountywy_gov_alpine",
        "municode_alpine",
        "alpine_youtube_channel",
    } <= ids


def test_every_seed_is_alpine_scope(fresh_db: Path) -> None:
    si.load(fresh_db)
    with db.open_db(fresh_db) as conn:
        scopes = {r[0] for r in conn.execute("SELECT DISTINCT scope FROM sources")}
    assert scopes == {"alpine"}


def test_seed_defaults_not_publishable(fresh_db: Path) -> None:
    si.load(fresh_db)
    with db.open_db(fresh_db) as conn:
        row = conn.execute(
            "SELECT raw_preservation_status, verification_status, archive_status "
            "FROM sources WHERE source_id = 'alpinewy_gov'"
        ).fetchone()
    assert row["raw_preservation_status"] == "seed_only"
    assert row["verification_status"] == "source_recorded"
    assert row["archive_status"] == "not_checked"


def test_loader_idempotent_no_duplicates(fresh_db: Path) -> None:
    si.load(fresh_db)
    first = si.load(fresh_db)  # re-run
    with db.open_db(fresh_db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert total == len(si.alpine_sources())
    assert first["sources_written"] == len(si.alpine_sources())


def test_validate_rejects_non_alpine_seed() -> None:
    with pytest.raises(ValueError, match="non-Alpine scope"):
        si._validate({"source_id": "x", "name": "x", "scope": "wyoming"})


# --- FK linkage + reconciliation -----------------------------------------

def test_fk_linkage_enforced(fresh_db: Path) -> None:
    """A document may reference a registered source; an unregistered one fails."""
    si.load(fresh_db)
    now = _now()
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, source_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://www.alpinewy.gov/a.pdf", "raw/a.pdf", "0" * 64, now, "alpinewy_gov"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT d.source_id, s.name FROM documents d "
            "JOIN sources s ON s.source_id = d.source_id "
            "WHERE d.source_url = 'https://www.alpinewy.gov/a.pdf'"
        ).fetchone()
        assert row["source_id"] == "alpinewy_gov"
        assert row["name"] == "Town of Alpine official website"

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, source_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("https://www.alpinewy.gov/b.pdf", "raw/b.pdf", "1" * 64, now, "ghost_source"),
            )
            conn.commit()


def test_reconcile_backfills_source_id(fresh_db: Path) -> None:
    # migrate + insert artifacts BEFORE the registry exists (source_id NULL)
    db.apply_migrations(fresh_db)
    now = _now()
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?)",
            ("https://www.alpinewy.gov/old.pdf", "raw/old.pdf", "2" * 64, now),
        )
        conn.execute(
            "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?)",
            ("https://library.municode.com/wy/alpine/x", "raw/x", "3" * 64, now),
        )
        conn.execute(
            "INSERT INTO transcripts (video_id, video_url, full_text, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("vid1", "https://www.youtube.com/watch?v=vid1", "t", "raw/t", "4" * 64, now),
        )
        conn.commit()

    # now load the registry — reconciliation should back-fill source_id
    result = si.load(fresh_db)
    assert result["reconcile"]["linked_documents"] == 2
    assert result["reconcile"]["linked_transcripts"] == 1

    with db.open_db(fresh_db) as conn:
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source_id IS NULL"
        ).fetchone()[0]
        assert unresolved == 0
        yt = conn.execute(
            "SELECT source_id FROM transcripts WHERE video_id = 'vid1'"
        ).fetchone()[0]
        assert yt == "alpine_youtube_channel"


def test_dry_run_writes_nothing(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    si.load(fresh_db, dry_run=True)
    with db.open_db(fresh_db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    assert total == 0
