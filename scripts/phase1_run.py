"""Phase 1 entrypoint: db init → crawl PDFs → fetch transcripts → embed (WEI-262).

Per Docs/phase1-spec.md §6 / §9. Single command end-to-end with no
human-in-the-loop. Sub-stage flags allow partial re-runs.

CLI:
    python scripts/phase1_run.py
    python scripts/phase1_run.py --crawl-only
    python scripts/phase1_run.py --transcripts-only --channel-id UCxxx
    python scripts/phase1_run.py --embed-only
    python scripts/phase1_run.py --as-of 2026-04-01    # backtest mode
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import crawl_pdfs  # noqa: E402
import db  # noqa: E402
import embed as embed_mod  # noqa: E402
import fetch_transcripts as ft  # noqa: E402

logger = logging.getLogger("phase1")

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCEPTANCE_LOG = REPO_ROOT / "Logs" / "acceptance.log"


def log_acceptance(line: str) -> None:
    ACCEPTANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    with ACCEPTANCE_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{stamp}\t{line}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--crawl-only", action="store_true")
    parser.add_argument("--transcripts-only", action="store_true")
    parser.add_argument("--embed-only", action="store_true")
    parser.add_argument("--as-of", help="ISO date for backtest mode (advisory; "
                                        "individual stages may ignore)")
    parser.add_argument("--channel-id",
                        default=os.environ.get(ft.ENV_CHANNEL_ID),
                        help=f"YouTube channel ID (or set {ft.ENV_CHANNEL_ID})")
    parser.add_argument("--query", action="append", default=[],
                        help="transcript search query (repeatable)")
    parser.add_argument("--limit", type=int, default=ft.DEFAULT_LIMIT)
    parser.add_argument("--target", choices=list(crawl_pdfs.TARGETS), action="append",
                        help="restrict crawler to one or more targets")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    db_path = Path(args.db)
    db.apply_migrations(db_path)

    flags = (args.crawl_only, args.transcripts_only, args.embed_only)
    do_all = not any(flags)
    do_crawl = do_all or args.crawl_only
    do_transcripts = do_all or args.transcripts_only
    do_embed = do_all or args.embed_only

    summary: dict = {"started": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "as_of": args.as_of}

    if do_crawl:
        targets = [crawl_pdfs.TARGETS[k] for k in (args.target or list(crawl_pdfs.TARGETS))]
        rate = crawl_pdfs.RateLimiter()
        conn = db.open_db(db_path)
        crawl_summary = []
        for t in targets:
            try:
                new, scanned = crawl_pdfs.crawl_target(t, conn, dry_run=False, rate=rate)
                crawl_summary.append({"target": t.key, "new_pdfs": new, "scanned": scanned})
            except Exception as exc:
                logger.exception("crawl failed for %s", t.key)
                crawl_summary.append({"target": t.key, "error": str(exc)})
        summary["crawl"] = crawl_summary

    if do_transcripts:
        if not args.channel_id and not args.query:
            logger.warning("transcripts: no --channel-id and no --query; skipping discovery")
            summary["transcripts"] = {"skipped": "no channel-id or query"}
        else:
            new, scanned, misses = ft.run(
                channel_id=args.channel_id,
                queries=args.query,
                limit=args.limit,
                dry_run=False,
                db_path=db_path,
            )
            summary["transcripts"] = {"new": new, "scanned": scanned, "misses": len(misses)}

    if do_embed:
        if not embed_mod.check_ollama():
            logger.error("Ollama not reachable / %s not pulled — embed stage skipped. "
                         "Run: ollama pull %s", embed_mod.MODEL, embed_mod.MODEL)
            summary["embed"] = {"skipped": "ollama unavailable"}
        else:
            summary["embed"] = embed_mod.run(db_path=db_path)

    # Acceptance check (spec §4.3): ≥10 PDFs, ≥5 transcripts, embeddings populated.
    conn = db.open_db(db_path)
    docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    transcripts = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    docs_with_emb = conn.execute(
        "SELECT COUNT(DISTINCT object_id) FROM embeddings WHERE object_type='document'"
    ).fetchone()[0]
    tx_with_emb = conn.execute(
        "SELECT COUNT(DISTINCT object_id) FROM embeddings WHERE object_type='transcript'"
    ).fetchone()[0]

    summary["acceptance"] = {
        "documents": docs,
        "documents_target": 10,
        "transcripts": transcripts,
        "transcripts_target": 5,
        "documents_with_embeddings": docs_with_emb,
        "transcripts_with_embeddings": tx_with_emb,
        "passed_4_3_pdfs": docs >= 10,
        "passed_4_3_transcripts": transcripts >= 5,
    }
    summary["finished"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    line = json.dumps(summary, ensure_ascii=False)
    logger.info("PHASE1 SUMMARY: %s", line)
    log_acceptance(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
