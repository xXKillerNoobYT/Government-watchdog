"""Tests for scripts/embed.py (WEI-262).

Network-free: mocks the Ollama embed call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import embed as embed_mod  # noqa: E402


def test_chunk_text_overlap():
    text = "x" * 2000
    chunks = embed_mod.chunk_text(text, size=800, overlap=100)
    assert len(chunks) == 3
    assert all(len(c) <= 800 for c in chunks)
    assert chunks[0][-100:] == chunks[1][:100]


def test_chunk_text_short_input():
    assert embed_mod.chunk_text("hello") == ["hello"]
    assert embed_mod.chunk_text("") == []


def test_vector_round_trip():
    vec = [0.0, 1.5, -2.25, 3.5e-3]
    blob = embed_mod.vector_to_blob(vec)
    assert len(blob) == len(vec) * 4
    out = embed_mod.blob_to_vector(blob)
    for a, b in zip(vec, out):
        assert abs(a - b) < 1e-6


def test_embed_pass_inserts_and_is_idempotent(tmp_path: Path):
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, raw_text) "
        "VALUES (?, ?, ?, ?, ?)",
        ("https://x.gov/a.pdf", "Raw-PDFs/2026/x/a.pdf", "0" * 64,
         "2026-05-04T00:00:00.000+00:00", "x" * 2000),
    )
    conn.commit()

    calls = []

    def fake_embed(text, **_):
        calls.append(text)
        return [0.1] * embed_mod.DIM

    rows = conn.execute("SELECT id, raw_text FROM documents").fetchall()
    inserted = embed_mod.embed_pass(
        conn,
        object_type="document",
        rows=[(r["id"], r["raw_text"]) for r in rows],
        embed_fn=fake_embed,
    )
    assert inserted == 3  # 2000-char text → 3 chunks at 800/100
    assert len(calls) == 3

    # Re-run is idempotent: zero new inserts, no new embed calls.
    calls.clear()
    inserted2 = embed_mod.embed_pass(
        conn,
        object_type="document",
        rows=[(r["id"], r["raw_text"]) for r in rows],
        embed_fn=fake_embed,
    )
    assert inserted2 == 0
    assert calls == []

    # Vectors decode correctly and have expected dim.
    blobs = conn.execute("SELECT vector, dim FROM embeddings").fetchall()
    for row in blobs:
        assert row["dim"] == embed_mod.DIM
        vec = embed_mod.blob_to_vector(row["vector"])
        assert len(vec) == embed_mod.DIM
        assert abs(vec[0] - 0.1) < 1e-6
