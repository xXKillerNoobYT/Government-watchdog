"""YouTube transcript bulk-pull for the Government Watchdog Phase 1 (WEI-260).

Behaviour (per Docs/phase1-spec.md §2.2, §6, open question Q1):
- Discover videos via yt-dlp `--flat-playlist` against an Alpine town channel
  (channel ID via --channel-id / GOV_WATCHDOG_ALPINE_CHANNEL_ID env), OR
  against one or more search queries (--query / repeated) when no official
  channel exists.
- Pull transcripts with `youtube-transcript-api`. Log misses (no captions,
  private, unlisted) for Phase 1.5 follow-up.
- Save under Transcripts/<YYYY>/<video_id>.json (one JSON per video).
- Insert into `transcripts` (video_id UNIQUE so re-run is idempotent).
- Phase 1 is naive on date filtering: prioritize the most recent N videos
  (default 30) and let downstream Phase-2 enrichment derive `meeting_date`.

CLI:
    python scripts/fetch_transcripts.py --channel-id UCxxxxxxxxxxxxxxxxxxxx
    python scripts/fetch_transcripts.py --query "Alpine WY town council"
    python scripts/fetch_transcripts.py --channel-id UCxxx --limit 50 --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

logger = logging.getLogger("fetch_transcripts")

DEFAULT_LIMIT = 30
ENV_CHANNEL_ID = "GOV_WATCHDOG_ALPINE_CHANNEL_ID"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_video_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT video_id FROM transcripts").fetchall()
    return {r[0] for r in rows}


def discover_via_channel(channel_id: str, limit: int) -> list[dict]:
    """Use yt-dlp --flat-playlist to enumerate channel videos (newest first)."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    return _yt_dlp_enumerate(url, limit)


def discover_via_search(query: str, limit: int) -> list[dict]:
    """Use yt-dlp ytsearchN: search to enumerate matching videos."""
    return _yt_dlp_enumerate(f"ytsearch{limit}:{query}", limit)


def _yt_dlp_enumerate(target: str, limit: int) -> list[dict]:
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-end", str(limit),
        target,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("yt-dlp not installed; pip install yt-dlp") from exc
    if proc.returncode != 0:
        logger.warning("yt-dlp failed (%d): %s", proc.returncode, proc.stderr.strip()[:300])
        return []
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid = obj.get("id")
        if not vid:
            continue
        out.append({
            "video_id": vid,
            "video_url": obj.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "title": obj.get("title"),
            "channel_id": obj.get("channel_id") or obj.get("uploader_id"),
            "channel_title": obj.get("channel") or obj.get("uploader"),
            "upload_date": obj.get("upload_date"),  # YYYYMMDD or None
            "duration_seconds": obj.get("duration"),
        })
    return out


def fetch_transcript(video_id: str) -> dict | None:
    """Return {full_text, timestamped_text, language, segments} or None on miss."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError as exc:
        raise RuntimeError("youtube-transcript-api not installed; pip install -r requirements.txt") from exc

    try:
        segments = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-US"])
    except Exception as exc:  # noqa: BLE001 — library raises various subclasses
        logger.info("transcript-miss %s: %s", video_id, exc.__class__.__name__)
        return None

    full = " ".join(seg.get("text", "").strip() for seg in segments if seg.get("text"))
    timestamped = "\n".join(
        f"{int(seg['start'] // 60):02d}:{int(seg['start'] % 60):02d} {seg.get('text', '').strip()}"
        for seg in segments
    )
    return {
        "full_text": full,
        "timestamped_text": timestamped,
        "language": "en",
        "segment_count": len(segments),
    }


def _normalize_upload_date(value: str | None) -> str | None:
    if not value or len(value) != 8 or not value.isdigit():
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _insert_transcript(
    conn: sqlite3.Connection,
    *,
    video_meta: dict,
    transcript: dict,
    local_path: str,
) -> bool:
    try:
        conn.execute(
            "INSERT INTO transcripts (video_id, video_url, channel_id, channel_title, "
            "upload_date, meeting_date, duration_seconds, language, segment_count, "
            "full_text, timestamped_text, local_path, sha256, fetch_time_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                video_meta["video_id"],
                video_meta["video_url"],
                video_meta.get("channel_id"),
                video_meta.get("channel_title"),
                _normalize_upload_date(video_meta.get("upload_date")),
                None,  # meeting_date — Phase-2 enrichment per spec §8 Q3
                video_meta.get("duration_seconds"),
                transcript["language"],
                transcript["segment_count"],
                transcript["full_text"],
                transcript["timestamped_text"],
                local_path,
                _sha256(transcript["full_text"]),
                _now_utc_iso(),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def run(
    *,
    channel_id: str | None,
    queries: list[str],
    limit: int,
    dry_run: bool,
    db_path: Path,
) -> tuple[int, int, list[str]]:
    """Returns (new_transcripts, scanned_videos, miss_log)."""
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    existing = _existing_video_ids(conn)

    repo_root = Path(__file__).resolve().parent.parent
    year = datetime.now(timezone.utc).strftime("%Y")
    out_dir = repo_root / "Transcripts" / year
    out_dir.mkdir(parents=True, exist_ok=True)

    discovered: list[dict] = []
    seen_ids: set[str] = set()
    if channel_id:
        for v in discover_via_channel(channel_id, limit):
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                discovered.append(v)
    for q in queries:
        for v in discover_via_search(q, limit):
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                discovered.append(v)

    logger.info("discovered %d candidate videos", len(discovered))

    new_count = 0
    misses: list[str] = []
    started = _now_utc_iso()
    for meta in discovered:
        vid = meta["video_id"]
        if vid in existing:
            continue
        transcript = fetch_transcript(vid)
        if transcript is None:
            misses.append(vid)
            continue
        local_path = out_dir / f"{vid}.json"
        if dry_run:
            logger.info("[dry-run] would save %s (%d segments)", local_path, transcript["segment_count"])
            continue
        local_path.write_text(
            json.dumps({"meta": meta, "transcript": transcript}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        rel = local_path.relative_to(repo_root).as_posix()
        if _insert_transcript(conn, video_meta=meta, transcript=transcript, local_path=rel):
            new_count += 1
            existing.add(vid)

    if not dry_run:
        conn.execute(
            "INSERT INTO crawl_runs (started_utc, finished_utc, status, targets, "
            "new_transcripts, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (started, _now_utc_iso(), "ok",
             json.dumps({"channel_id": channel_id, "queries": queries}),
             new_count,
             f"discovered={len(discovered)} new={new_count} misses={len(misses)}"),
        )
        conn.commit()

    return new_count, len(discovered), misses


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel-id", default=os.environ.get(ENV_CHANNEL_ID),
                        help=f"YouTube channel ID (or set {ENV_CHANNEL_ID})")
    parser.add_argument("--query", action="append", default=[],
                        help="search query (repeatable); used in addition to or instead of --channel-id")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"max videos per source (default {DEFAULT_LIMIT})")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not args.channel_id and not args.query:
        parser.error(f"need --channel-id (or {ENV_CHANNEL_ID}) or at least one --query")

    new, scanned, misses = run(
        channel_id=args.channel_id,
        queries=args.query,
        limit=args.limit,
        dry_run=args.dry_run,
        db_path=Path(args.db),
    )
    logger.info("DONE: %d new transcripts, %d scanned, %d misses", new, scanned, len(misses))
    if misses:
        logger.info("miss video_ids: %s", ",".join(misses[:20]) + (",..." if len(misses) > 20 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
