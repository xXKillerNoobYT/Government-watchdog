"""documents -> transcripts bridge adapter (GOV-125, Stage 1, plan §2).

The integration gap the CTO feasibility pass identified: GOV-124 landed the 44
real Alpine transcripts as **`documents`** rows (source-of-record `.txt`, raw
bytes sha-addressed in the gitignored `Raw-Corpus` store). The 1.07 segmenter
(`scripts/segment_transcript.py`) consumes **`transcripts.timestamped_text`**.
Nothing bridges the two.

CTO-preferred decision (plan §2, "do not widen the segmenter's table coupling"):
materialize a `transcripts` row from each transcript `document`, reading the
**already-preserved** bytes only (NO re-fetch, NO re-crawl), then let the
unchanged segmenter consume it. This keeps `documents` as the source-of-record and
leaves the segmenter contract intact.

Timed vs untimed (plan §2.2 — **never fabricate a timestamp**):
- If the preserved text has parseable ``MM:SS``/``HH:MM:SS`` lines, the segmenter
  will produce real ``timestamp_seconds`` rows downstream — no gap.
- If it does NOT, this adapter records a first-class ``missing_timestamps``
  completeness gap against the materialized transcript and leaves the timestamp
  absent. The untimed-segment representation itself (NULL-timestamp segment rows
  vs source-pointer-anchored statements) is a SEPARATE migration decision flagged
  to the CTO; this adapter does not pre-empt it — it only materializes + labels.

Idempotent: `transcripts.video_id` is UNIQUE and derived from the document id, so
re-running materializes nothing new and re-records no gap (INSERT OR IGNORE).

Scope: NO network, NO AI, Alpine-only, local/vault-only.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import completeness  # noqa: E402
import db  # noqa: E402
from segment_transcript import _LINE_RE  # noqa: E402  (THE timed-line contract)

REPO_ROOT = Path(__file__).resolve().parent.parent

# doc_type values GOV-124's ingest assigns to transcript material (ingest_local_
# corpus.doc_type_for: filename contains "transcript" -> 'transcript'; a bare .txt
# -> 'transcript_text'). Mirror that set here so the bridge picks up exactly the
# transcript documents and nothing else.
TRANSCRIPT_DOC_TYPES = frozenset({"transcript", "transcript_text"})


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def has_parseable_timestamps(text: str) -> bool:
    """True iff at least one line carries a parseable MM:SS / HH:MM:SS locator.

    Uses THE segmenter's own line regex so this classification can never disagree
    with what the segmenter will actually parse (single source of truth).
    """
    for line in (text or "").splitlines():
        if _LINE_RE.match(line.strip()):
            return True
    return False


def _read_preserved_text(raw_store_root: Path, local_path: str) -> str:
    """Read the already-preserved bytes (NO re-fetch). Decoded lossily so a stray
    non-UTF-8 byte in a real transcript never crashes the run; the canonical raw
    bytes remain the sha-addressed store copy, untouched."""
    path = (raw_store_root / local_path).resolve()
    return path.read_text(encoding="utf-8", errors="replace")


def _transcript_documents(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, source_url, title, doc_type, doc_date, local_path, sha256, source_id "
        "FROM documents WHERE doc_type IN (%s) ORDER BY doc_date, id"
        % ",".join("?" * len(TRANSCRIPT_DOC_TYPES)),
        tuple(sorted(TRANSCRIPT_DOC_TYPES)),
    ).fetchall()
    return [dict(r) for r in rows]


def materialize_transcripts(
    conn: sqlite3.Connection,
    *,
    raw_store_root: Path = REPO_ROOT,
    detected_run_id: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Materialize a `transcripts` row per transcript `document`, oldest→newest.

    For each row: read preserved text, classify timed/untimed, INSERT OR IGNORE a
    `transcripts` row (video_id = ``localdoc-<document_id>``, deterministic +
    UNIQUE), and for untimed text record a ``missing_timestamps`` gap. Returns a
    summary (counts + per-transcript classification). Never fabricates a timestamp.
    """
    docs = _transcript_documents(conn)
    materialized = 0
    timed = 0
    untimed = 0
    items: list[dict] = []

    for doc in docs:
        try:
            text = _read_preserved_text(raw_store_root, doc["local_path"])
        except OSError as exc:
            # Preserved bytes unreadable: record a gap, never invent content.
            items.append({"document_id": doc["id"], "error": str(exc)})
            if not dry_run:
                completeness.record_gap(
                    conn,
                    subject_node_id=str(doc["id"]),
                    subject_node_type="document",
                    gap_type="missing_transcript",
                    detail=f"preserved transcript bytes unreadable: {exc}",
                    source_id=doc["source_id"],
                    detected_run_id=detected_run_id,
                    commit=False,
                )
            continue

        is_timed = has_parseable_timestamps(text)
        video_id = f"localdoc-{doc['id']}"

        if not dry_run:
            conn.execute(
                "INSERT OR IGNORE INTO transcripts ("
                "video_id, video_url, meeting_date, full_text, timestamped_text, "
                "local_path, sha256, source_id, fetch_time_utc"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    video_id,
                    doc["source_url"],          # file:// provenance URI (vault-only)
                    doc["doc_date"],
                    text,                        # full_text (verbatim preserved text)
                    text,                        # timestamped_text: segmenter parses
                    #                              only the timestamped lines; untimed
                    #                              text yields zero segments by design
                    doc["local_path"],
                    doc["sha256"],
                    doc["source_id"],
                    _now_utc_iso(),
                ),
            )

        tid_row = conn.execute(
            "SELECT id FROM transcripts WHERE video_id = ?", (video_id,)
        ).fetchone()
        transcript_id = int(tid_row[0]) if tid_row else None

        if not is_timed and not dry_run and transcript_id is not None:
            completeness.record_gap(
                conn,
                subject_node_id=str(transcript_id),
                subject_node_type="transcript",
                gap_type="missing_timestamps",
                # B3: anchor detail on the stable doc id + doc_type, NOT the raw
                # human-readable title (a public-comment-derived title can carry a
                # member-of-public name). The title is resolvable from the doc id
                # in the reviewer-internal store; it never lands in the gap field.
                detail=(
                    f"transcript document id={doc['id']} (doc_type={doc['doc_type']}) "
                    "has no parseable timestamp locators (none of the deterministic "
                    "family: bracketed/bare decimal-seconds or MM:SS/HH:MM:SS); "
                    "timestamps left absent (never fabricated)"
                ),
                source_id=doc["source_id"],
                detected_run_id=detected_run_id,
                commit=False,
            )

        materialized += 1
        timed += int(is_timed)
        untimed += int(not is_timed)
        items.append({
            "document_id": doc["id"],
            "transcript_id": transcript_id,
            "video_id": video_id,
            "doc_date": doc["doc_date"],
            "timed": is_timed,
        })

    if not dry_run:
        conn.commit()

    return {
        "transcript_documents": len(docs),
        "materialized": materialized,
        "timed": timed,
        "untimed": untimed,
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize transcripts rows from transcript documents (GOV-125 §2)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--raw-store-root", type=Path, default=REPO_ROOT,
                        help="root the documents.local_path is resolved against")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    db.apply_migrations(args.db)
    with db.open_db(args.db) as conn:
        summary = materialize_transcripts(
            conn, raw_store_root=args.raw_store_root, dry_run=args.dry_run
        )
    tag = "[dry-run] " if args.dry_run else ""
    print(
        f"{tag}transcript_documents={summary['transcript_documents']} "
        f"materialized={summary['materialized']} timed={summary['timed']} "
        f"untimed={summary['untimed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
