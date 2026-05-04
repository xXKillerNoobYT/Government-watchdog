"""Local Ollama embedding pass for the Government Watchdog Phase 1 (WEI-262).

Per Docs/phase1-spec.md §5–§6:
- Model: Ollama `nomic-embed-text` (768-dim).
- Chunking: token-naive 800-char windows, 100-char overlap.
- Targets: PDF `documents.raw_text` and transcript `full_text`.
- Storage: `embeddings` row per chunk; vector is float32 LE BLOB.
- Idempotent via UNIQUE (object_type, object_id, chunk_index, model).
- No paid API calls (Ollama at http://localhost:11434).
"""

from __future__ import annotations

import argparse
import logging
import struct
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

logger = logging.getLogger("embed")

OLLAMA_URL = "http://localhost:11434/api/embeddings"
MODEL = "nomic-embed-text"
DIM = 768
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def chunk_text(text: str, *, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if not text:
        return []
    if size <= overlap:
        raise ValueError("size must be > overlap")
    chunks: list[str] = []
    step = size - overlap
    for start in range(0, len(text), step):
        piece = text[start:start + size]
        if piece.strip():
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks


def vector_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def blob_to_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def ollama_embed(text: str, *, model: str = MODEL, url: str = OLLAMA_URL,
                 session: requests.Session | None = None) -> list[float]:
    sess = session or requests.Session()
    resp = sess.post(url, json={"model": model, "prompt": text}, timeout=120)
    resp.raise_for_status()
    data = resp.json()
    vec = data.get("embedding")
    if not isinstance(vec, list):
        raise RuntimeError(f"unexpected ollama response: {data!r:.200}")
    if len(vec) != DIM:
        raise RuntimeError(f"expected dim {DIM}, got {len(vec)} for model {model}")
    return vec


def _existing_chunks(conn: sqlite3.Connection, object_type: str, model: str) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT object_id, chunk_index FROM embeddings WHERE object_type = ? AND model = ?",
        (object_type, model),
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def embed_pass(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    rows: list[tuple[int, str]],
    model: str = MODEL,
    embed_fn=ollama_embed,
) -> int:
    """Embed any chunks not yet present. Returns # of new rows inserted."""
    existing = _existing_chunks(conn, object_type, model)
    inserted = 0
    for obj_id, text in rows:
        chunks = chunk_text(text or "")
        for idx, chunk in enumerate(chunks):
            if (obj_id, idx) in existing:
                continue
            try:
                vec = embed_fn(chunk)
            except Exception:
                logger.exception("embed failed for %s id=%s chunk=%s", object_type, obj_id, idx)
                continue
            try:
                conn.execute(
                    "INSERT INTO embeddings (object_type, object_id, chunk_index, chunk_text, "
                    "model, dim, vector, embed_time_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (object_type, obj_id, idx, chunk, model, DIM,
                     vector_to_blob(vec), _now_utc_iso()),
                )
                conn.commit()
                inserted += 1
                existing.add((obj_id, idx))
            except sqlite3.IntegrityError:
                continue
    return inserted


def _extract_pdf_text(local_path: Path) -> str:
    # pypdf is pure-Python, no system deps. Fail soft: return "" so the
    # row stays in the DB without raw_text and we can re-try in Phase 2.
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed; PDFs will not be embedded")
        return ""
    try:
        reader = PdfReader(str(local_path))
        return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf extract failed for %s: %s", local_path, exc)
        return ""


def extract_missing_document_text(conn, repo_root: Path) -> int:
    """Populate documents.raw_text for rows that don't have it yet.

    Returns count of newly-extracted rows.
    """
    rows = conn.execute(
        "SELECT id, local_path FROM documents "
        "WHERE raw_text IS NULL OR length(raw_text) = 0"
    ).fetchall()
    extracted = 0
    for row in rows:
        path = repo_root / row["local_path"]
        if not path.exists():
            logger.warning("missing local pdf %s", path)
            continue
        text = _extract_pdf_text(path)
        if not text:
            continue
        conn.execute("UPDATE documents SET raw_text = ? WHERE id = ?",
                     (text, row["id"]))
        conn.commit()
        extracted += 1
    return extracted


def run(*, db_path: Path, model: str = MODEL, embed_fn=ollama_embed) -> dict:
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)

    repo_root = Path(__file__).resolve().parent.parent
    extracted = extract_missing_document_text(conn, repo_root)

    docs = conn.execute(
        "SELECT id, raw_text FROM documents WHERE raw_text IS NOT NULL AND length(raw_text) > 0"
    ).fetchall()
    transcripts = conn.execute(
        "SELECT id, full_text FROM transcripts WHERE full_text IS NOT NULL AND length(full_text) > 0"
    ).fetchall()

    new_doc = embed_pass(conn, object_type="document",
                         rows=[(r["id"], r["raw_text"]) for r in docs],
                         model=model, embed_fn=embed_fn)
    new_tx = embed_pass(conn, object_type="transcript",
                        rows=[(r["id"], r["full_text"]) for r in transcripts],
                        model=model, embed_fn=embed_fn)
    return {"new_document_chunks": new_doc, "new_transcript_chunks": new_tx,
            "documents_scanned": len(docs), "transcripts_scanned": len(transcripts),
            "pdf_text_extracted": extracted}


def check_ollama() -> bool:
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(MODEL in m for m in models)
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not check_ollama():
        logger.error("Ollama is not reachable or %s is not pulled. "
                     "Run: ollama pull %s", MODEL, MODEL)
        return 2
    summary = run(db_path=Path(args.db), model=args.model)
    logger.info("DONE: %s", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
