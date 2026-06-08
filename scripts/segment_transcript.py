"""Deterministic transcript segmenter (Stage 1 Slice 2 B / GOV-81).

Contract 1.07 §1 (meeting -> agenda_item -> transcript_segment) and §2
(exact-source timestamp locator). Turns a *preserved* timestamped transcript
(the `transcripts.timestamped_text` blob written by scripts/fetch_transcripts.py)
into addressable `transcript_segments` rows.

Deterministic by construction — NO AI, no network, no model:
- The same `timestamped_text` always yields byte-identical segment rows.
- Segment ids are derived (`<video_id>:seg-NNNN`), so re-running is idempotent
  (PK + `INSERT OR IGNORE`; the `(transcript_id, segment_index)` UNIQUE is the
  backstop).
- Because the segmenter only *slices* an already-preserved verbatim transcript
  (it never paraphrases), every emitted segment is `is_verbatim = 1`. A
  paraphrase/AI-summary path is explicitly out of scope (1.07 §5.4 — that is the
  later `statements` layer).

Input format (matches fetch_transcripts.py output): one line per snippet,
``MM:SS text`` where ``MM`` is *total minutes* (``int(start // 60)``, so it may
exceed 59), e.g. ``72:15`` = 1h12m15s. ``HH:MM:SS text`` is also accepted. Lines
that do not start with a timestamp (headers, blanks) are skipped deterministically.

Data boundary (1.07 §7): `transcript_path` is the vault-only local path to the
preserved transcript; it is stored for provenance and must never reach a web-safe
projection. This module writes only to the local SQLite DB — never to any public
surface.

CLI:
    python scripts/segment_transcript.py --transcript-id 12
    python scripts/segment_transcript.py --all
    python scripts/segment_transcript.py --all --db Database/gov_watchdog.db --dry-run
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

logger = logging.getLogger("segment_transcript")

ALLOWED_CONFIDENCE = ("high", "medium", "low")
DEFAULT_CONFIDENCE = "medium"

# A timestamp token (MM:SS or HH:MM:SS) followed by at least one space and text.
_LINE_RE = re.compile(r"^\s*(\d{1,2}(?::\d{2}){1,2})\s+(.*\S)\s*$")


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_timestamp(token: str) -> int:
    """Parse a ``MM:SS`` or ``HH:MM:SS`` token into integer seconds.

    Two-part tokens follow the fetch_transcripts.py convention where the first
    field is *total minutes* (may exceed 59); three-part tokens are H:M:S.
    """
    parts = token.split(":")
    if len(parts) == 2:
        minutes, seconds = int(parts[0]), int(parts[1])
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"unparseable timestamp token: {token!r}")


def format_human(total_seconds: int) -> str:
    """Format integer seconds as ``HH:MM:SS`` (contract 1.07 §2 timestamp_human)."""
    hours, rem = divmod(int(total_seconds), 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_timestamped_text(text: str) -> list[tuple[int, str]]:
    """Deterministically parse a timestamped transcript blob.

    Returns ``[(timestamp_seconds, segment_text), ...]`` in file order. Blank
    lines and lines without a leading timestamp are skipped; no segment text is
    ever invented or merged.
    """
    segments: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _LINE_RE.match(stripped)
        if not match:
            continue
        segments.append((parse_timestamp(match.group(1)), match.group(2).strip()))
    return segments


def _resolve_meeting_id(conn: sqlite3.Connection, transcript_id: int) -> int | None:
    """A transcript is linked to a meeting via meetings.transcript_id (0001)."""
    row = conn.execute(
        "SELECT id FROM meetings WHERE transcript_id = ?", (transcript_id,)
    ).fetchone()
    return int(row[0]) if row else None


def segment_transcript(
    conn: sqlite3.Connection,
    transcript_id: int,
    *,
    source_id: str | None = None,
    meeting_id: int | None = None,
    confidence: str = DEFAULT_CONFIDENCE,
    dry_run: bool = False,
) -> list[dict]:
    """Slice one preserved transcript into transcript_segments rows.

    Reads `transcripts.timestamped_text` (already preserved at ingest), parses it
    deterministically, and inserts one row per segment. `source_id` / `meeting_id`
    default to the transcript's own FK / its linked meeting so segments resolve to
    the Slice-1 `sources` registry and the `meetings` spine. Idempotent: re-running
    inserts nothing new (deterministic PK + `INSERT OR IGNORE`).

    Returns the list of segment records (whether or not they were freshly inserted).
    """
    if confidence not in ALLOWED_CONFIDENCE:
        raise ValueError(f"confidence must be one of {ALLOWED_CONFIDENCE}, got {confidence!r}")

    row = conn.execute(
        "SELECT id, video_id, timestamped_text, local_path, source_id "
        "FROM transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no transcript with id={transcript_id}")

    video_id = row["video_id"]
    transcript_path = row["local_path"]  # vault-only provenance path (1.07 §7)
    resolved_source_id = source_id if source_id is not None else row["source_id"]
    resolved_meeting_id = (
        meeting_id if meeting_id is not None else _resolve_meeting_id(conn, transcript_id)
    )

    parsed = parse_timestamped_text(row["timestamped_text"])
    records: list[dict] = []
    for index, (timestamp_seconds, segment_text) in enumerate(parsed):
        record = {
            "segment_id": f"{video_id}:seg-{index:04d}",
            "transcript_id": transcript_id,
            "meeting_id": resolved_meeting_id,
            "source_id": resolved_source_id,
            "segment_index": index,
            "timestamp_seconds": timestamp_seconds,
            "timestamp_human": format_human(timestamp_seconds),
            "segment_text": segment_text,
            "is_verbatim": 1,  # deterministic verbatim slice — never a paraphrase
            "confidence": confidence,
            "transcript_path": transcript_path,
        }
        records.append(record)
        if dry_run:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO transcript_segments ("
            "segment_id, transcript_id, meeting_id, source_id, segment_index, "
            "timestamp_seconds, timestamp_human, segment_text, is_verbatim, "
            "confidence, transcript_path, created_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["segment_id"],
                record["transcript_id"],
                record["meeting_id"],
                record["source_id"],
                record["segment_index"],
                record["timestamp_seconds"],
                record["timestamp_human"],
                record["segment_text"],
                record["is_verbatim"],
                record["confidence"],
                record["transcript_path"],
                _now_utc_iso(),
            ),
        )
    if not dry_run:
        conn.commit()
    return records


def _unsegmented_transcript_ids(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT t.id FROM transcripts t "
        "WHERE t.timestamped_text IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM transcript_segments s WHERE s.transcript_id = t.id) "
        "ORDER BY t.id"
    ).fetchall()
    return [int(r[0]) for r in rows]


def run(*, transcript_id: int | None, all_transcripts: bool, dry_run: bool, db_path: Path) -> int:
    """Segment one or all transcripts. Returns total segments produced."""
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    if all_transcripts:
        ids = _unsegmented_transcript_ids(conn)
    elif transcript_id is not None:
        ids = [transcript_id]
    else:
        raise ValueError("need --transcript-id or --all")

    total = 0
    for tid in ids:
        records = segment_transcript(conn, tid, dry_run=dry_run)
        total += len(records)
        logger.info("transcript %s -> %d segments%s", tid, len(records), " (dry-run)" if dry_run else "")
    logger.info("DONE: %d transcripts, %d segments total", len(ids), total)
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic transcript segmenter (GOV-81).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--transcript-id", type=int, help="segment a single transcript by id")
    group.add_argument("--all", action="store_true", help="segment all not-yet-segmented transcripts")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run(
        transcript_id=args.transcript_id,
        all_transcripts=args.all,
        dry_run=args.dry_run,
        db_path=Path(args.db),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
