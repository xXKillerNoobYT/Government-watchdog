"""Alpine source registry seed loader (GOV-74, Stage 1 Slice 1 Issue B).

Contracts 1.02 / 1.03. Source: GOV-72 gap analysis §4 (Issue B).

This is the single committed generator for the `sources` table — the inventory
of what Government Watchdog crawls/ingests. It:

1. Registers the known Alpine source set (the same set `crawl_pdfs.py` /
   `fetch_transcripts.py` already crawl), porting the hand-maintained vault
   registry into the DB.
2. Validates scope **before** insert: every seed must be `scope == 'alpine'`,
   with a usable locator. Non-Alpine scope is rejected (1.02-g / 1.05-a). The
   DB also CHECK-constrains scope == 'alpine' as a second line of defence.
3. Is idempotent — re-running upserts the same rows (no duplicates).
4. Reconciles existing crawled artifacts: back-fills `documents.source_id` /
   `transcripts.source_id` by matching each artifact's URL host to a registered
   source, so every existing document/transcript resolves to a `source_id`
   (GOV-74 success criterion).

Scope lock: Alpine-only, local/vault-only, no AI extraction, no public surface.
Seed rows default to raw_preservation_status='seed_only' and
verification_status='source_recorded' (registered, not yet preserved/reviewed).

Usage:
    python scripts/source_inventory.py [--dry-run] [--no-reconcile] [--db PATH]
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

# Env var the transcript fetcher reads (scripts/fetch_transcripts.py).
ENV_CHANNEL_ID = "GOV_WATCHDOG_ALPINE_CHANNEL_ID"

# Columns we write on a seed row. Anything omitted falls back to the schema
# default (which keeps records not-publishable / unreviewed).
_SEED_COLUMNS = (
    "source_id", "name", "scope", "url", "original_url", "source_type",
    "source_class", "source_authority_level", "jurisdiction",
    "expected_artifacts", "robots_policy", "owner_agent", "scan_date",
    "archive_status", "raw_preservation_status", "local_note_path",
    "verification_status", "correction_status", "topic_tags", "notes",
    "registered_utc",
)


def _alpine_youtube_url() -> tuple[str | None, str, str]:
    """Resolve the Alpine YouTube channel locator from config.

    Returns (url, raw_preservation_status, note). The concrete channel id is
    config-provided (not hard-coded / fabricated): if GOV_WATCHDOG_ALPINE_CHANNEL_ID
    is set we build the channel videos URL, otherwise the row is registered as a
    seed awaiting channel-id configuration.
    """
    channel_id = os.environ.get(ENV_CHANNEL_ID, "").strip()
    if channel_id:
        return (
            f"https://www.youtube.com/channel/{channel_id}/videos",
            "seed_only",
            f"Channel id from {ENV_CHANNEL_ID}.",
        )
    return (
        None,
        "seed_only_unconfigured",
        f"Channel id supplied at crawl time via {ENV_CHANNEL_ID} "
        "(SourceArchivist owns the verified channel id).",
    )


def alpine_sources() -> list[dict]:
    """The committed Alpine seed set (ports the vault source inventory).

    Mirrors the targets in scripts/crawl_pdfs.py (TARGETS) plus the Alpine
    YouTube channel consumed by scripts/fetch_transcripts.py.
    """
    yt_url, yt_status, yt_note = _alpine_youtube_url()
    return [
        {
            "source_id": "alpinewy_gov",
            "name": "Town of Alpine official website",
            "url": "https://www.alpinewy.gov/",
            "original_url": "https://www.alpinewy.gov/",
            "source_type": "website",
            "source_class": "municipal_primary",
            "source_authority_level": "primary",
            "jurisdiction": "Alpine",
            "expected_artifacts": "agendas,minutes,notices,ordinances,resolutions,pdfs",
            "robots_policy": "respect",
            "local_note_path": "Docs/Source-Data/source-registry/alpinewy_gov.md",
            "topic_tags": "town,council,government",
        },
        {
            "source_id": "lincolncountywy_gov_alpine",
            "name": "Lincoln County WY — Alpine-relevant pages",
            "url": "https://www.lincolncountywy.gov/",
            "original_url": "https://www.lincolncountywy.gov/",
            "source_type": "website",
            "source_class": "county_relevant",
            "source_authority_level": "secondary",
            "jurisdiction": "Lincoln County (Alpine-relevant)",
            "expected_artifacts": "agendas,minutes,land_records,notices",
            "robots_policy": "respect",
            "local_note_path": "Docs/Source-Data/source-registry/lincolncountywy_alpine.md",
            "topic_tags": "county,alpine-relevant",
            "notes": "Alpine-relevant pages only (alpine_filter applied at crawl time).",
        },
        {
            "source_id": "municode_alpine",
            "name": "Municode — Town of Alpine code of ordinances",
            "url": "https://library.municode.com/wy/alpine",
            "original_url": "https://library.municode.com/wy/alpine",
            "source_type": "legal_code",
            "source_class": "codified_ordinances",
            "source_authority_level": "primary",
            "jurisdiction": "Alpine",
            "expected_artifacts": "municipal_code,ordinances",
            "robots_policy": "respect",
            "local_note_path": "Docs/Source-Data/source-registry/municode_alpine.md",
            "topic_tags": "code,ordinances",
        },
        {
            "source_id": "alpine_youtube_channel",
            "name": "Town of Alpine YouTube channel (meeting videos)",
            "url": yt_url,
            "original_url": yt_url,
            "source_type": "video_channel",
            "source_class": "meeting_video",
            "source_authority_level": "primary",
            "jurisdiction": "Alpine",
            "expected_artifacts": "meeting_videos,transcripts",
            "robots_policy": "respect",
            "raw_preservation_status": yt_status,
            "local_note_path": "Docs/Source-Data/source-registry/alpine_youtube_channel.md",
            "topic_tags": "video,meetings,transcripts",
            "notes": yt_note,
        },
    ]


def _validate(seed: dict) -> None:
    """Reject a seed that is not Alpine-scoped or lacks identity (1.02-g/1.05-a)."""
    scope = seed.get("scope", "alpine")
    if scope != "alpine":
        raise ValueError(
            f"source {seed.get('source_id')!r} has non-Alpine scope {scope!r}; "
            "rejected (Alpine-only scope lock)"
        )
    if not seed.get("source_id"):
        raise ValueError("seed missing source_id")
    if not seed.get("name"):
        raise ValueError(f"source {seed['source_id']!r} missing name")


def _host(url: str | None) -> str | None:
    if not url:
        return None
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or None


def upsert_sources(conn, seeds: list[dict]) -> int:
    """Idempotently upsert seed rows. Returns rows written."""
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    written = 0
    for seed in seeds:
        _validate(seed)
        row = {"scope": "alpine", "registered_utc": now, **seed}
        cols = [c for c in _SEED_COLUMNS if c in row]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "source_id")
        conn.execute(
            f"INSERT INTO sources ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(source_id) DO UPDATE SET {updates}",
            [row[c] for c in cols],
        )
        written += 1
    conn.commit()
    return written


def reconcile(conn, dry_run: bool = False) -> dict:
    """Back-fill documents.source_id / transcripts.source_id by URL host.

    Returns counts: {linked_documents, linked_transcripts, unresolved}.
    """
    sources = conn.execute(
        "SELECT source_id, url, source_type FROM sources"
    ).fetchall()
    host_to_id: dict[str, str] = {}
    for sid, url, source_type in sources:
        host = _host(url)
        if host:
            host_to_id.setdefault(host, sid)
        # A video_channel owns all its videos regardless of whether the concrete
        # channel URL is configured yet (the channel id is env-supplied) — map
        # the platform host so transcript rows reconcile.
        if source_type == "video_channel":
            host_to_id.setdefault("youtube.com", sid)

    counts = {"linked_documents": 0, "linked_transcripts": 0, "unresolved": 0}

    def link(table: str, url_col: str, key: str) -> None:
        rows = conn.execute(
            f"SELECT id, {url_col} FROM {table} WHERE source_id IS NULL"
        ).fetchall()
        for rid, url in rows:
            host = _host(url)
            sid = host_to_id.get(host) if host else None
            if sid is None:
                counts["unresolved"] += 1
                continue
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET source_id = ? WHERE id = ?", (sid, rid)
                )
            counts[key] += 1

    link("documents", "source_url", "linked_documents")
    link("transcripts", "video_url", "linked_transcripts")
    if not dry_run:
        conn.commit()
    return counts


def load(db_path: Path = db.DEFAULT_DB_PATH, *, dry_run: bool = False,
         do_reconcile: bool = True) -> dict:
    db.apply_migrations(db_path)
    seeds = alpine_sources()
    for seed in seeds:  # fail fast before touching the DB
        _validate(seed)
    with db.open_db(db_path) as conn:
        if dry_run:
            written = len(seeds)
        else:
            written = upsert_sources(conn, seeds)
        recon = reconcile(conn, dry_run=dry_run) if do_reconcile else {}
    return {"sources_written": written, "reconcile": recon, "dry_run": dry_run}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the Alpine source registry seed set.")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate + report without writing")
    parser.add_argument("--no-reconcile", action="store_true",
                        help="skip back-filling documents/transcripts source_id")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH,
                        help="SQLite DB path (default: Database/gov_watchdog.db)")
    args = parser.parse_args(argv)

    result = load(args.db, dry_run=args.dry_run, do_reconcile=not args.no_reconcile)
    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}sources registered: {result['sources_written']}")
    if result["reconcile"]:
        r = result["reconcile"]
        print(f"{tag}reconciled: documents={r['linked_documents']} "
              f"transcripts={r['linked_transcripts']} unresolved={r['unresolved']}")
        if r["unresolved"]:
            print(f"{tag}WARNING: {r['unresolved']} artifact(s) did not match a "
                  "registered source host — review the registry.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
