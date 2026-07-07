"""GOV-637 pre-flight behaviors (GOV-636 §4) for the FULL-corpus ingest run.

Two additive, fail-closed behaviors the full 134-folder run depends on:

- §4.2  `referer_url` capture at ingest — the ONLY derivable public URL inside the
        signed selection is the YouTube video id in `youtube_transcript_{VIDEOID}.txt`
        filenames. Derivation is pure string work (no network); everything else stays
        NULL fail-closed. Video ids are case-sensitive and preserved verbatim.
- §4.1  per-transcript structuring isolation — one malformed transcript must not abort
        the whole pass. The failure is recorded in `structuring_failures[]`, the run
        continues over the remaining transcripts, and the CLI exits 1 (never a silent
        clean report over a partial run).

No network, no AI, pure sqlite + tmp files.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import ingest_local_corpus as ing  # noqa: E402
import structure_real_corpus as struct  # noqa: E402

# A realistic timed transcript (MM:SS lines — segmenter yields real segments).
TIMED_TEXT = (
    "Alpine Town Council Regular Meeting\n"
    "00:00 Mayor calls the meeting to order.\n"
    "00:12 Roll call of the council members.\n"
    "01:30 Discussion of the water system capital project.\n"
)


@pytest.fixture()
def patched_repo(tmp_path: Path, monkeypatch) -> Path:
    """Point BOTH the ingest raw store and the structuring bridge at a tmp repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(ing, "REPO_ROOT", repo)
    monkeypatch.setattr(struct, "REPO_ROOT", repo)
    return repo


# --- §4.2 referer_url derivation ----------------------------------------------

def _fake_sf(name: str):
    class _SF:  # referer_url_for only reads sf.path.name
        path = Path("/tmp") / name
    return _SF()


def test_referer_url_for_youtube_transcript_derives_public_url() -> None:
    # Case-sensitive video id preserved verbatim (YouTube ids are case-sensitive).
    assert (ing.referer_url_for(_fake_sf("youtube_transcript_dQw4w9WgXcQ.txt"))
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert (ing.referer_url_for(_fake_sf("youtube_transcript_abc.txt"))
            == "https://www.youtube.com/watch?v=abc")


def test_referer_url_for_non_youtube_stays_null_fail_closed() -> None:
    # No guessed URL for anything without a derivable public signal.
    assert ing.referer_url_for(_fake_sf("Council_Meeting_Packet.pdf")) is None
    assert ing.referer_url_for(_fake_sf("MEET-Agenda-2024-10-09.pdf")) is None
    assert ing.referer_url_for(
        _fake_sf("PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf")
    ) is None
    # A transcript that is not the youtube naming convention: no derivation.
    assert ing.referer_url_for(_fake_sf("meeting_transcript.txt")) is None


def test_ingest_writes_referer_url_only_for_youtube(patched_repo, tmp_path) -> None:
    corpus = tmp_path / "corpus"
    (corpus / "2024-10-09").mkdir(parents=True)
    (corpus / "2024-10-09" / "youtube_transcript_XyZ123.txt").write_text("hello")
    (corpus / "2024-10-09" / "Council_Meeting_Packet.pdf").write_bytes(b"%PDF x")
    db_path = tmp_path / "t.db"

    summary = ing.ingest(corpus, db_path, dry_run=False)
    assert summary["referer_url_derived"] == 1

    with db.open_db(db_path) as conn:
        rows = {
            r["title"]: r["referer_url"]
            for r in conn.execute("SELECT title, referer_url FROM documents").fetchall()
        }
    assert rows["youtube_transcript_XyZ123.txt"] == "https://www.youtube.com/watch?v=XyZ123"
    assert rows["Council_Meeting_Packet.pdf"] is None


def test_ingest_referer_url_survives_reingest_idempotently(patched_repo, tmp_path) -> None:
    corpus = tmp_path / "corpus"
    (corpus / "2024-10-09").mkdir(parents=True)
    (corpus / "2024-10-09" / "youtube_transcript_abc.txt").write_text("hello")
    db_path = tmp_path / "t.db"

    ing.ingest(corpus, db_path, dry_run=False)
    # Second run: hash-skip path (unchanged bytes) — the stored referer_url persists.
    second = ing.ingest(corpus, db_path, dry_run=False)
    assert second["new_documents"] == 0 and second["copied_to_raw_store"] == 0
    with db.open_db(db_path) as conn:
        val = conn.execute("SELECT referer_url FROM documents").fetchone()[0]
    assert val == "https://www.youtube.com/watch?v=abc"


# --- §4.1 per-transcript structuring isolation --------------------------------

def _timed_corpus(root: Path) -> Path:
    (root / "2024-10-09").mkdir(parents=True)
    (root / "2024-10-09" / "youtube_transcript_aaa.txt").write_text(TIMED_TEXT)
    (root / "2024-11-13").mkdir(parents=True)
    (root / "2024-11-13" / "youtube_transcript_bbb.txt").write_text(TIMED_TEXT)
    return root


def test_structuring_isolates_one_bad_transcript_and_continues(
    patched_repo, tmp_path, monkeypatch
) -> None:
    corpus = _timed_corpus(tmp_path / "corpus")
    db_path = tmp_path / "t.db"

    real_segment = struct.seg.segment_transcript
    state = {"calls": 0}

    def flaky_segment(conn, tid, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:  # first timed transcript blows up
            raise ValueError("simulated malformed transcript")
        return real_segment(conn, tid, *args, **kwargs)

    monkeypatch.setattr(struct.seg, "segment_transcript", flaky_segment)

    summary = struct.structure(corpus, db_path, skip_ingest=False)

    # One transcript isolated, the run did NOT abort, the other still structured.
    failures = summary["structuring_failures"]
    assert len(failures) == 1
    assert "simulated malformed transcript" in failures[0]["error"]
    assert failures[0]["transcript_id"] is not None
    # The second transcript produced real segments/statements (proves continuation).
    assert summary["segments_created"] > 0
    # Both meetings still exist (spine is independent of the per-transcript failure).
    assert summary["meeting_folders"] == 2


def test_structuring_cli_exits_1_on_isolated_failure(
    patched_repo, tmp_path, monkeypatch
) -> None:
    corpus = _timed_corpus(tmp_path / "corpus")
    db_path = tmp_path / "t.db"

    def always_raise(conn, tid, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(struct.seg, "segment_transcript", always_raise)

    code = struct.main([
        "--source-dir", str(corpus), "--db", str(db_path), "--report",
    ])
    assert code == 1  # partial structuring run must never report as clean (exit 0)


def test_structuring_clean_run_exits_0(patched_repo, tmp_path) -> None:
    corpus = _timed_corpus(tmp_path / "corpus")
    db_path = tmp_path / "t.db"
    code = struct.main(["--source-dir", str(corpus), "--db", str(db_path)])
    assert code == 0
