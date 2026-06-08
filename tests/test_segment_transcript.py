"""Tests for the deterministic transcript segmenter (GOV-81, Slice 2 B).

Covers the Contract 1.07 §1 acceptance criteria:
- additive + idempotent migration 0006 (agenda_items + transcript_segments) —
  re-run safe (GOV-72 §6 / db.py ledger + IF NOT EXISTS);
- the segmenter produces timestamped segments from a real (sanitized) Alpine
  fixture, with every contract field populated;
- FK linkage of transcript_segments to transcripts / meetings / sources, and of
  agenda_items to meetings / sources, holds (PRAGMA foreign_key_check clean);
- segmentation is deterministic and idempotent (re-run inserts no duplicates).

No AI, no network: pure sqlite + the committed fixture.
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
import segment_transcript as seg  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _seed_source(conn, source_id: str = "alpine:video:2026-05-08-regular") -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "Alpine Council 2026-05-08 video", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _insert_transcript(conn, fixture: dict, *, source_id: str | None = None) -> int:
    meta = fixture["meta"]
    tr = fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, channel_id, channel_title, "
        "upload_date, meeting_date, duration_seconds, language, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], meta.get("channel_id"),
            meta.get("channel_title"), "2026-05-08", "2026-05-08",
            meta.get("duration_seconds"), tr["language"], tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            # vault-only provenance path (never committed; transcript_path comes from here)
            "Transcripts/2026/alpine-sample-0001.json",
            "0" * 64, _now(), source_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_meeting(conn, transcript_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO meetings (meeting_date, body, title, transcript_id, fetch_time_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        ("2026-05-08", "Alpine Town Council", "Regular Meeting", transcript_id, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- pure parser (deterministic core) -------------------------------------

def test_parse_timestamp_two_part_is_total_minutes() -> None:
    # fetch_transcripts.py emits MM:SS where MM is total minutes (may exceed 59)
    assert seg.parse_timestamp("00:00") == 0
    assert seg.parse_timestamp("01:33") == 93
    assert seg.parse_timestamp("72:15") == 72 * 60 + 15


def test_parse_timestamp_three_part_is_hms() -> None:
    assert seg.parse_timestamp("01:12:15") == 3600 + 12 * 60 + 15


def test_format_human_is_hh_mm_ss() -> None:
    assert seg.format_human(0) == "00:00:00"
    assert seg.format_human(93) == "00:01:33"
    assert seg.format_human(4335) == "01:12:15"


def test_parse_timestamped_text_skips_non_timestamp_lines() -> None:
    text = "HEADER LINE (no timestamp)\n\n00:05 first\n00:10 second\n"
    parsed = seg.parse_timestamped_text(text)
    assert parsed == [(5, "first"), (10, "second")]


def test_parse_timestamped_text_is_deterministic() -> None:
    text = _load_fixture()["transcript"]["timestamped_text"]
    assert seg.parse_timestamped_text(text) == seg.parse_timestamped_text(text)


# --- migration 0006: additive + idempotent --------------------------------

def test_migration_creates_agenda_and_segment_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        agenda_cols = _columns(conn, "agenda_items")
        seg_cols = _columns(conn, "transcript_segments")
    for required in ("agenda_item_id", "meeting_id", "item_order", "title", "agenda_doc_source_id"):
        assert required in agenda_cols, f"agenda_items.{required} missing"
    for required in (
        "segment_id", "transcript_id", "meeting_id", "source_id", "segment_index",
        "timestamp_seconds", "timestamp_human", "segment_text", "is_verbatim",
        "confidence", "transcript_path",
    ):
        assert required in seg_cols, f"transcript_segments.{required} missing"


def test_migration_idempotent_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # must not raise
    with db.open_db(db_path) as conn:
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        # table still single + intact
        seg_cols = [r[1] for r in conn.execute("PRAGMA table_info(transcript_segments)")]
    assert "0006_agenda_transcript_segments" in ledger
    assert seg_cols.count("segment_id") == 1


# --- segmenter on the real (sanitized) Alpine fixture ----------------------

def test_segmenter_produces_segments_from_alpine_fixture(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    fixture = _load_fixture()
    with db.open_db(db_path) as conn:
        source_id = _seed_source(conn)
        tid = _insert_transcript(conn, fixture, source_id=source_id)
        records = seg.segment_transcript(conn, tid)

        rows = conn.execute(
            "SELECT segment_id, segment_index, timestamp_seconds, timestamp_human, "
            "segment_text, is_verbatim, confidence, transcript_path, source_id, transcript_id "
            "FROM transcript_segments WHERE transcript_id = ? ORDER BY segment_index",
            (tid,),
        ).fetchall()

    expected_count = fixture["transcript"]["segment_count"]
    assert len(records) == expected_count
    assert len(rows) == expected_count

    # every contract field populated, timestamps strictly increasing, verbatim
    last_ts = -1
    for i, row in enumerate(rows):
        assert row["segment_id"] == f"alpine-sample-0001:seg-{i:04d}"
        assert row["segment_index"] == i
        assert isinstance(row["timestamp_seconds"], int)
        assert row["timestamp_seconds"] > last_ts  # monotonic, deterministic order
        last_ts = row["timestamp_seconds"]
        assert row["timestamp_human"] == seg.format_human(row["timestamp_seconds"])
        assert row["segment_text"].strip() != ""
        assert row["is_verbatim"] == 1  # deterministic slice -> verbatim
        assert row["confidence"] in seg.ALLOWED_CONFIDENCE
        # transcript_path is the vault-only provenance path, carried for source linking
        assert row["transcript_path"] == "Transcripts/2026/alpine-sample-0001.json"
        assert row["source_id"] == source_id
        assert row["transcript_id"] == tid

    # spot-check the first/last known segment text + timestamp from the fixture
    assert rows[0]["timestamp_seconds"] == 0
    assert rows[0]["segment_text"].startswith("Call to order")
    assert rows[-1]["segment_text"].endswith("the meeting is adjourned.")


# --- FK linkage to transcripts / meetings / sources ------------------------

def test_segments_link_to_meeting_and_source(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    fixture = _load_fixture()
    with db.open_db(db_path) as conn:
        source_id = _seed_source(conn)
        tid = _insert_transcript(conn, fixture, source_id=source_id)
        mid = _insert_meeting(conn, tid)  # links meetings.transcript_id -> tid
        seg.segment_transcript(conn, tid)  # meeting_id auto-resolved from the link

        # meeting_id back-filled from meetings.transcript_id, FK integrity holds
        meeting_ids = {r[0] for r in conn.execute(
            "SELECT DISTINCT meeting_id FROM transcript_segments WHERE transcript_id = ?", (tid,)
        )}
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()

        # joins across the spine resolve
        joined = conn.execute(
            "SELECT ts.segment_id, m.body, s.source_class "
            "FROM transcript_segments ts "
            "JOIN meetings m ON m.id = ts.meeting_id "
            "JOIN sources s ON s.source_id = ts.source_id "
            "WHERE ts.transcript_id = ?",
            (tid,),
        ).fetchall()

    assert meeting_ids == {mid}
    assert fk_violations == []
    assert len(joined) == fixture["transcript"]["segment_count"]
    assert joined[0]["body"] == "Alpine Town Council"
    assert joined[0]["source_class"] == "alpine-official"


def test_agenda_item_links_to_meeting_and_source(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    fixture = _load_fixture()
    with db.open_db(db_path) as conn:
        source_id = _seed_source(conn, "alpine:agenda:2026-05-08")
        tid = _insert_transcript(conn, fixture)
        mid = _insert_meeting(conn, tid)
        conn.execute(
            "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title, "
            "agenda_doc_source_id, created_utc) VALUES (?, ?, ?, ?, ?, ?)",
            ("alpine:2026-05-08:item-7", mid, 7, "WWTP financing update", source_id, _now()),
        )
        conn.commit()
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        row = conn.execute(
            "SELECT ai.title, m.body, s.source_id FROM agenda_items ai "
            "JOIN meetings m ON m.id = ai.meeting_id "
            "JOIN sources s ON s.source_id = ai.agenda_doc_source_id "
            "WHERE ai.agenda_item_id = ?",
            ("alpine:2026-05-08:item-7",),
        ).fetchone()
    assert fk_violations == []
    assert row["title"] == "WWTP financing update"
    assert row["body"] == "Alpine Town Council"
    assert row["source_id"] == source_id


# --- determinism + idempotency of segmentation -----------------------------

def test_segmentation_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    fixture = _load_fixture()
    with db.open_db(db_path) as conn:
        tid = _insert_transcript(conn, fixture)
        first = seg.segment_transcript(conn, tid)
        seg.segment_transcript(conn, tid)  # re-run must not duplicate
        count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?", (tid,)
        ).fetchone()[0]
    assert count == len(first) == fixture["transcript"]["segment_count"]


def test_unknown_transcript_id_raises(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        with pytest.raises(ValueError, match="no transcript"):
            seg.segment_transcript(conn, 9999)


def test_bad_confidence_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    fixture = _load_fixture()
    with db.open_db(db_path) as conn:
        tid = _insert_transcript(conn, fixture)
        with pytest.raises(ValueError, match="confidence"):
            seg.segment_transcript(conn, tid, confidence="superb")
