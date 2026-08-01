"""GOV-1688 (C4, ingest-provenance): `extract_metadata` had ZERO test references.

198 lines, two public functions, and — measured at C1 (GOV-1686) — the only
document in `Docs/` that mentions it is a *gap analysis*, which records a finding
rather than governing behaviour. So its docstring was the whole specification and
nothing checked it.

The docstring makes three claims. These tests pin all three, because the third is
a **provenance** promise inside the provenance area:

1. *"Idempotent."*
2. *"Writes only when the existing column is NULL or a placeholder
   (doc_type='other', title equal to the URL slug)."*
3. *"Never touches raw_text, source_url, fetch_time_utc, or sha256."*

Claim 3 is the load-bearing one. `sha256` and `source_url` are how a served
civic record is traced back to what was actually fetched; a backfill that
overwrote either would corrupt provenance **silently and irreversibly**, because
the original value is gone and nothing downstream would report a mismatch.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import extract_metadata as em  # noqa: E402

#: A PDF body that trips exactly one type rule and one date rule.
ORDINANCE_TEXT = "TOWN OF ALPINE\nORDINANCE NO. 2026-04\nREPORT YEAR: 2026\n"

#: Columns the module promises never to write. `documents` and `transcripts`
#: name them slightly differently, so each table carries its own tuple.
DOC_PROVENANCE = ("raw_text", "source_url", "fetch_time_utc", "sha256")
TX_PROVENANCE = ("full_text", "video_url", "fetch_time_utc", "sha256", "local_path")


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "em.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


def _insert_document(conn, **over):
    row = dict(
        id=1, source_url="https://alpinewy.gov/files/ord-2026-04.pdf",
        local_path="/vault/a.pdf", sha256="a" * 64,
        fetch_time_utc="2026-06-01T00:00:00Z", raw_text=ORDINANCE_TEXT,
        title=None, doc_type=None, doc_date=None,
    )
    row.update(over)
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO documents ({cols}) VALUES ({', '.join('?' * len(row))})",
                 tuple(row.values()))
    conn.commit()
    return row


def _snapshot(conn, table, cols, rid=1):
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} WHERE id = ?", (rid,)).fetchone()
    return dict(zip(cols, row))


# --- claim 2: only NULL or placeholder is written ------------------------------

class TestWritesOnlyNullOrPlaceholder:

    def test_it_fills_a_null_type_date_and_title(self, conn):
        _insert_document(conn)
        stats = em.update_documents(conn)
        assert stats == {"doc_type": 1, "doc_date": 1, "title": 1}
        got = _snapshot(conn, "documents", ("doc_type", "doc_date", "title"))
        assert got == {"doc_type": "ordinance", "doc_date": "2026-12-31",
                       "title": "Ordinance (2026-12-31)"}

    def test_it_does_NOT_overwrite_a_reviewed_value(self, conn):
        """The whole point: a human-set field must survive a backfill."""
        _insert_document(conn, doc_type="minutes", doc_date="2026-01-02",
                         title="Council minutes, January 2")
        stats = em.update_documents(conn)
        assert stats == {"doc_type": 0, "doc_date": 0, "title": 0}
        got = _snapshot(conn, "documents", ("doc_type", "doc_date", "title"))
        assert got["doc_type"] == "minutes", "a reviewed doc_type was overwritten"
        assert got["doc_date"] == "2026-01-02", "a reviewed doc_date was overwritten"
        assert got["title"] == "Council minutes, January 2"

    def test_doc_type_other_and_a_url_slug_title_count_as_placeholders(self, conn):
        """`other` and "title == the URL's last segment" are explicitly writable."""
        _insert_document(conn, doc_type="other", title="ord-2026-04.pdf")
        stats = em.update_documents(conn)
        assert stats["doc_type"] == 1 and stats["title"] == 1
        assert _snapshot(conn, "documents", ("doc_type",))["doc_type"] == "ordinance"


# --- claim 3: provenance columns are never written -----------------------------

class TestNeverTouchesProvenance:
    """`raw_text`, `source_url`, `fetch_time_utc`, `sha256` — hands off.

    These are how a served record is traced back to the bytes actually fetched.
    A backfill that rewrote one would corrupt provenance **silently**: the prior
    value is gone and nothing downstream reports a mismatch.
    """

    def test_update_documents_leaves_every_provenance_column_byte_identical(self, conn):
        _insert_document(conn)
        before = _snapshot(conn, "documents", DOC_PROVENANCE)
        em.update_documents(conn)
        after = _snapshot(conn, "documents", DOC_PROVENANCE)
        assert after == before, (
            "extract_metadata wrote a provenance column. Its docstring promises "
            "'Never touches raw_text, source_url, fetch_time_utc, or sha256' — "
            f"changed: {[k for k in before if before[k] != after[k]]}")

    def test_update_transcripts_leaves_every_provenance_column_byte_identical(
            self, conn, tmp_path, monkeypatch):
        cache = tmp_path / "tx"
        cache.mkdir()
        (cache / "vid123.json").write_text(json.dumps(
            {"meta": {"title": "Town Council 6/23/2026", "upload_date": "20260624"}}),
            encoding="utf-8")
        monkeypatch.setattr(em, "TRANSCRIPTS_DIR", cache)
        conn.execute(
            "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path,"
            " sha256, fetch_time_utc, title) VALUES"
            " (1, 'vid123', 'https://y/watch?v=vid123', 'text', '/vault/t.json',"
            " 'b', '2026-06-24T00:00:00Z', NULL)")
        conn.commit()

        before = _snapshot(conn, "transcripts", TX_PROVENANCE)
        stats = em.update_transcripts(conn)
        after = _snapshot(conn, "transcripts", TX_PROVENANCE)

        assert stats["meeting_date"] == 1 and stats["upload_date"] == 1
        assert after == before, (
            "extract_metadata wrote a transcript provenance column — "
            f"changed: {[k for k in before if before[k] != after[k]]}")
        got = _snapshot(conn, "transcripts", ("meeting_date", "upload_date"))
        assert got == {"meeting_date": "2026-06-23", "upload_date": "2026-06-24"}


# --- claim 1: idempotent -------------------------------------------------------

def test_a_second_run_changes_nothing(conn, tmp_path, monkeypatch):
    """"Idempotent" is the first word of the docstring; nothing checked it.

    A backfill that is not idempotent is dangerous precisely because it looks
    fine on the first run — the damage appears only when an operator re-runs it.
    """
    cache = tmp_path / "tx"
    cache.mkdir()
    (cache / "v1.json").write_text(json.dumps(
        {"meta": {"title": "Council 6/23/2026", "upload_date": "20260624"}}),
        encoding="utf-8")
    monkeypatch.setattr(em, "TRANSCRIPTS_DIR", cache)
    _insert_document(conn)
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path,"
        " sha256, fetch_time_utc, title) VALUES"
        " (1, 'v1', 'u', 't', '/p', 'h', '2026-06-24T00:00:00Z', NULL)")
    conn.commit()

    first_docs = em.update_documents(conn)
    first_tx = em.update_transcripts(conn)
    assert sum(first_docs.values()) > 0 and sum(first_tx.values()) > 0

    assert em.update_documents(conn) == {"doc_type": 0, "doc_date": 0, "title": 0}, (
        "update_documents is not idempotent — a re-run rewrote fields")
    assert em.update_transcripts(conn) == {
        "meeting_date": 0, "upload_date": 0, "title_meta_used": 0}, (
        "update_transcripts is not idempotent — a re-run rewrote fields")


def test_a_transcript_with_no_cached_meta_file_is_skipped_not_crashed(
        conn, tmp_path, monkeypatch):
    """The cache is a local artifact of `fetch_transcripts`; it may be absent."""
    monkeypatch.setattr(em, "TRANSCRIPTS_DIR", tmp_path / "does-not-exist")
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path,"
        " sha256, fetch_time_utc) VALUES"
        " (1, 'gone', 'u', 't', '/p', 'h', '2026-06-24T00:00:00Z')")
    conn.commit()
    assert em.update_transcripts(conn) == {
        "meeting_date": 0, "upload_date": 0, "title_meta_used": 0}
