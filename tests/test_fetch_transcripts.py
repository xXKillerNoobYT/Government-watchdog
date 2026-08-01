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


# --- GOV-1691 (C4): the three callables `run`'s tests MONKEYPATCH AWAY ---------
#
# The file above already covers `_yt_dlp_enumerate` (the shared parser) and `run`
# end-to-end. But `test_run_inserts_transcripts_and_idempotent` replaces
# `discover_via_channel`, `discover_via_search` and `fetch_transcript` with stubs
# — so those three never execute anywhere in the suite. What was untested is
# precisely the part each one adds ON TOP of the shared parser: the URL it builds,
# and the snippet mapping.
#
# The mocking approach is the one this file already established (mock the yt-dlp
# subprocess + youtube-transcript-api); nothing new is introduced.


def _spy_run(monkeypatch, *, stdout: str = "", returncode: int = 0):
    """Record the argv handed to the ONLY network seam."""
    calls: list[list[str]] = []

    def fake(cmd, **kwargs):
        calls.append(list(cmd))
        return MagicMock(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(ft.subprocess, "run", fake)
    return calls


def test_a_UC_channel_becomes_the_UU_uploads_playlist(monkeypatch):
    """Breaking this is SILENT civic data loss, which is why it needs a test.

    The docstring records the reason: *"The /videos tab URL only returns
    featured/highlighted videos; use the auto-generated uploads playlist
    (UC... -> UU...) for the full upload feed."*

    Revert it and discovery still succeeds, still returns rows, still looks
    healthy — it just quietly stops seeing most of a town's meetings. There is no
    error to notice.
    """
    calls = _spy_run(monkeypatch)
    ft.discover_via_channel("UCabc123", 5)
    assert calls[0][-1] == "https://www.youtube.com/playlist?list=UUabc123", (
        "a UC... channel id must become the UU... uploads playlist; the /videos "
        "tab returns only FEATURED videos and silently under-reports the feed")


def test_a_non_UC_channel_id_falls_back_to_the_videos_tab(monkeypatch):
    calls = _spy_run(monkeypatch)
    ft.discover_via_channel("@alpinetown", 5)
    assert calls[0][-1] == "https://www.youtube.com/channel/@alpinetown/videos"


def test_search_discovery_builds_ytsearchN_and_passes_the_limit(monkeypatch):
    calls = _spy_run(monkeypatch)
    ft.discover_via_search("alpine town council", 7)
    cmd = calls[0]
    assert cmd[-1] == "ytsearch7:alpine town council"
    assert cmd[cmd.index("--playlist-end") + 1] == "7"


def test_malformed_and_id_less_lines_are_skipped_not_fatal(monkeypatch):
    """One bad line from an external tool must not discard the good rows."""
    calls = _spy_run(monkeypatch, stdout="\n".join([
        json.dumps({"id": "vid1"}), "not json at all", "",
        json.dumps({"title": "no id here"}), json.dumps({"id": "vid2"}),
    ]))
    out = ft.discover_via_search("council", 10)
    assert [r["video_id"] for r in out] == ["vid1", "vid2"]
    assert out[0]["video_url"] == "https://www.youtube.com/watch?v=vid1", (
        "a row with no url must have one synthesised from the video id")
    assert calls, "the yt-dlp seam must actually have been exercised"


def test_a_missing_yt_dlp_binary_gives_an_actionable_error(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file")
    monkeypatch.setattr(ft.subprocess, "run", boom)
    with pytest.raises(RuntimeError, match="yt-dlp not installed"):
        ft.discover_via_search("q", 1)


# --- fetch_transcript: a miss is None, never an exception ---------------------


class _Snippet:
    def __init__(self, text, start):
        self.text, self.start = text, start


def _stub_transcript_api(monkeypatch, *, snippets=None, raises=None):
    """`fetch_transcript` imports the library INSIDE the function, so a stub in
    sys.modules is picked up at call time."""
    import types
    module = types.ModuleType("youtube_transcript_api")

    class FakeApi:
        def fetch(self, video_id, languages=None):
            if raises is not None:
                raise raises
            return types.SimpleNamespace(snippets=snippets or [], language_code="en")

    module.YouTubeTranscriptApi = FakeApi
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)


def test_a_transcript_miss_returns_None_and_never_raises(monkeypatch):
    """The broad `except Exception` is deliberate — pin it.

    The library raises many subclasses (private video, no captions, age-gated).
    A bulk pull must survive every one of them; a future narrowing that lets one
    escape would abort a whole crawl on a single unavailable video.
    """
    _stub_transcript_api(monkeypatch, raises=RuntimeError("no captions"))
    assert ft.fetch_transcript("vid-private") is None


def test_snippets_become_full_text_and_MM_SS_timestamps(monkeypatch):
    _stub_transcript_api(monkeypatch, snippets=[
        _Snippet("  Call to order.  ", 0.0),
        _Snippet("Item four A.", 65.4),
        _Snippet("   ", 70.0),            # whitespace-only: absent from full_text
    ])
    got = ft.fetch_transcript("vid-ok")
    assert got["full_text"] == "Call to order. Item four A."
    assert got["timestamped_text"].splitlines()[:2] == [
        "00:00 Call to order.", "01:05 Item four A."], (
        "timestamps are MM:SS derived from snippet start seconds")
    assert got["segment_count"] == 3, "segment_count counts SNIPPETS, not words"
    assert got["language"] == "en"


def test_empty_captions_are_a_RESULT_not_a_miss(monkeypatch):
    """Zero snippets means "captions exist and are empty" — distinct from a miss.

    Collapsing the two would make an empty-caption video indistinguishable from
    one that was never fetched, and the second is a coverage gap while the first
    is not.
    """
    _stub_transcript_api(monkeypatch, snippets=[])
    got = ft.fetch_transcript("vid-empty")
    assert got is not None
    assert got["segment_count"] == 0 and got["full_text"] == ""
