"""Tests for scripts/fetch_transcripts.py (WEI-260).

Network-free: mocks yt-dlp subprocess + youtube-transcript-api.
Live ≥5 transcripts acceptance lives in WEI-262 closeout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import fetch_transcripts as ft  # noqa: E402


def test_normalize_upload_date():
    assert ft._normalize_upload_date("20260315") == "2026-03-15"
    assert ft._normalize_upload_date(None) is None
    assert ft._normalize_upload_date("bad") is None
    assert ft._normalize_upload_date("2026-03-15") is None  # only YYYYMMDD form


def test_yt_dlp_enumerate_parses_jsonl(monkeypatch):
    payload = (
        json.dumps({"id": "abc1234567A", "title": "Council 2026-03-01",
                    "uploader": "Alpine Town", "duration": 4200,
                    "upload_date": "20260301"}) + "\n" +
        json.dumps({"id": "def1234567B", "title": "Planning"}) + "\n"
    )
    fake = MagicMock(returncode=0, stdout=payload, stderr="")
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: fake)
    out = ft._yt_dlp_enumerate("ytsearch10:alpine", 10)
    assert [v["video_id"] for v in out] == ["abc1234567A", "def1234567B"]
    assert out[0]["channel_title"] == "Alpine Town"
    assert out[0]["upload_date"] == "20260301"


def test_yt_dlp_failure_returns_empty(monkeypatch):
    fake = MagicMock(returncode=2, stdout="", stderr="boom")
    monkeypatch.setattr(ft.subprocess, "run", lambda *a, **k: fake)
    assert ft._yt_dlp_enumerate("ytsearch1:x", 1) == []


def test_run_inserts_transcripts_and_idempotent(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "t.db"

    discovered = [
        {"video_id": "vid0000001A", "video_url": "https://youtu.be/vid0000001A",
         "title": "Council 2026-03-01", "channel_id": "UCxx", "channel_title": "Alpine",
         "upload_date": "20260301", "duration_seconds": 4200},
        {"video_id": "vid0000002B", "video_url": "https://youtu.be/vid0000002B",
         "title": "Planning", "channel_id": "UCxx", "channel_title": "Alpine",
         "upload_date": None, "duration_seconds": None},
    ]
    monkeypatch.setattr(ft, "discover_via_channel", lambda cid, lim: discovered)
    monkeypatch.setattr(ft, "discover_via_search", lambda q, lim: [])

    def fake_fetch(video_id):
        if video_id == "vid0000002B":
            return None  # transcript miss
        return {"full_text": "hello world",
                "timestamped_text": "00:00 hello\n00:02 world",
                "language": "en",
                "segment_count": 2}
    monkeypatch.setattr(ft, "fetch_transcript", fake_fetch)

    new, scanned, misses = ft.run(
        channel_id="UCxx", queries=[], limit=10, dry_run=False, db_path=db_path,
    )
    assert new == 1
    assert scanned == 2
    assert misses == ["vid0000002B"]

    with db.open_db(db_path) as conn:
        rows = conn.execute("SELECT video_id, language, segment_count, sha256 "
                            "FROM transcripts").fetchall()
    assert len(rows) == 1
    assert rows[0]["video_id"] == "vid0000001A"
    assert rows[0]["segment_count"] == 2

    # Re-run = idempotent (existing video_id is skipped before fetch).
    fetch_calls = []
    monkeypatch.setattr(ft, "fetch_transcript",
                        lambda v: fetch_calls.append(v) or fake_fetch(v))
    new2, _, _ = ft.run(
        channel_id="UCxx", queries=[], limit=10, dry_run=False, db_path=db_path,
    )
    assert new2 == 0
    assert "vid0000001A" not in fetch_calls  # short-circuited via existing set


def test_run_requires_channel_or_query(tmp_path: Path):
    # Direct CLI parse — main() exits via parser.error.
    with pytest.raises(SystemExit):
        with patch.object(sys, "argv", ["fetch_transcripts.py", "--db", str(tmp_path / "x.db")]):
            ft.main()
