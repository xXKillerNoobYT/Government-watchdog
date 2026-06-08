"""Tests for raw preservation & reproducibility hardening (GOV-75, Issue C).

Covers the 1.04 acceptance criteria:
- reproducibility check: re-hash of stored raw matches recorded sha256 (green),
  and a tampered/corrupted file is detected (does not silently pass);
- raw-before-parse gate: extraction is blocked when raw is missing or its hash
  mismatches — no derived record without a hash-verifiable predecessor;
- `crawl_runs` formalized as the Lane 1 run log: records source set + status +
  retry; migration 0004 is additive + idempotent.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import embed as embed_mod  # noqa: E402
import raw_preservation as rp  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _write_raw(repo_root: Path, rel_path: str, content: bytes) -> str:
    """Write a raw artifact under a fake repo root; return its sha256."""
    path = repo_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return rp.sha256_file(path)


def _insert_document(conn, *, source_url: str, local_path: str, sha256: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
        "VALUES (?, ?, ?, ?)",
        (source_url, local_path, sha256, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- migration 0004: crawl_runs Lane 1 fields -----------------------------

def test_migration_adds_lane1_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        cols = _columns(conn, "crawl_runs")
    for required in ("lane", "source_set", "retry_count"):
        assert required in cols, f"crawl_runs.{required} missing"


def test_migration_idempotent_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # must not raise (duplicate-column hazard)
    with db.open_db(db_path) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(crawl_runs)")]
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert cols.count("retry_count") == 1
    assert "0004_crawl_runs_lane1" in ledger


# --- crawl_runs as Lane 1 deterministic-ingest run log (1.04-f) ------------

def test_record_crawl_run_records_source_set_status_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        run_id = rp.record_crawl_run(
            conn,
            started_utc=_now(),
            finished_utc=_now(),
            status="ok",
            source_set=["alpinewy_gov", "municode_alpine"],
            retry_count=2,
            new_documents=5,
            notes="smoke",
        )
        row = conn.execute(
            "SELECT lane, source_set, status, retry_count, new_documents "
            "FROM crawl_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert row["lane"] == "lane1_deterministic_ingest"
    assert json.loads(row["source_set"]) == ["alpinewy_gov", "municode_alpine"]
    assert row["status"] == "ok"
    assert row["retry_count"] == 2
    assert row["new_documents"] == 5


# --- reproducibility check (1.04-b/e) -------------------------------------

def test_verify_reproducibility_passes_on_intact_store(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/a.pdf", b"%PDF-1.4 alpine bytes")
    with db.open_db(db_path) as conn:
        _insert_document(
            conn, source_url="https://www.alpinewy.gov/a.pdf",
            local_path="Raw-PDFs/2026/alpinewy/a.pdf", sha256=sha,
        )
        result = rp.verify_reproducibility(conn, repo_root=repo_root)
    assert result["checked"] == 1
    assert result["ok"] == 1
    assert result["missing"] == []
    assert result["mismatch"] == []


def test_verify_reproducibility_detects_tamper(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)

    good_sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/good.pdf", b"intact bytes")
    tampered_sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/bad.pdf", b"original bytes")
    with db.open_db(db_path) as conn:
        _insert_document(
            conn, source_url="https://www.alpinewy.gov/good.pdf",
            local_path="Raw-PDFs/2026/alpinewy/good.pdf", sha256=good_sha,
        )
        bad_id = _insert_document(
            conn, source_url="https://www.alpinewy.gov/bad.pdf",
            local_path="Raw-PDFs/2026/alpinewy/bad.pdf", sha256=tampered_sha,
        )
        # tamper with the stored file AFTER its hash was recorded
        (repo_root / "Raw-PDFs/2026/alpinewy/bad.pdf").write_bytes(b"corrupted bytes!!")
        result = rp.verify_reproducibility(conn, repo_root=repo_root)

    assert result["checked"] == 2
    assert result["ok"] == 1
    assert [e["id"] for e in result["mismatch"]] == [bad_id]
    assert result["missing"] == []


def test_verify_reproducibility_flags_missing_file(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        mid = _insert_document(
            conn, source_url="https://www.alpinewy.gov/gone.pdf",
            local_path="Raw-PDFs/2026/alpinewy/gone.pdf", sha256="0" * 64,
        )
        result = rp.verify_reproducibility(conn, repo_root=repo_root)
    assert result["ok"] == 0
    assert [e["id"] for e in result["missing"]] == [mid]


# --- raw-before-parse gate (1.04-a/b) -------------------------------------

def test_assert_raw_preserved_returns_hash_on_match(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/ok.pdf", b"good")
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/ok.pdf",
            local_path="Raw-PDFs/2026/alpinewy/ok.pdf", sha256=sha,
        )
        assert rp.assert_raw_preserved(conn, "document", did, repo_root) == sha


def test_assert_raw_preserved_raises_on_missing(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/x.pdf",
            local_path="Raw-PDFs/2026/alpinewy/x.pdf", sha256="0" * 64,
        )
        with pytest.raises(rp.RawPreservationError, match="missing"):
            rp.assert_raw_preserved(conn, "document", did, repo_root)


def test_assert_raw_preserved_raises_on_hash_mismatch(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/m.pdf", b"actual bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/m.pdf",
            local_path="Raw-PDFs/2026/alpinewy/m.pdf", sha256="9" * 64,  # wrong
        )
        with pytest.raises(rp.RawPreservationError, match="hash mismatch"):
            rp.assert_raw_preserved(conn, "document", did, repo_root)


def test_raw_before_parse_gate_blocks_extraction_on_tamper(tmp_path: Path) -> None:
    """A tampered raw artifact must not produce a derived raw_text record."""
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/t.pdf", b"some bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/t.pdf",
            local_path="Raw-PDFs/2026/alpinewy/t.pdf", sha256="a" * 64,  # mismatch
        )
        # The gate blocks before _extract_pdf_text is ever called, so no pypdf
        # dependency / real PDF is needed to prove extraction is blocked.
        extracted = embed_mod.extract_missing_document_text(conn, repo_root)
        raw_text = conn.execute(
            "SELECT raw_text FROM documents WHERE id = ?", (did,)
        ).fetchone()[0]
    assert extracted == 0
    assert raw_text is None
