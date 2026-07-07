"""On-disk Alpine corpus ingest adapter (GOV-124, Stage 1 goal `927f07dc`).

Chain item #1 of 4 (CEO sequencing on GOV-123). This is the BUILD the CTO
feasibility pass identified: the existing Lane-1 scripts only acquire over
HTTP/YouTube, so none ingests a local folder. This adapter is the on-disk
acquisition front-end that REUSES the existing downstream primitives:

- `manifest_local_corpus.iter_source_of_record_files` — THE signed selection
  (GOV-133 SourceArchivist sign-off). Driving ingest from this single function
  is sign-off condition **C1 (no drift)**: the run can never touch anything other
  than what was approved, byte-identical, including the binding out-of-folder
  allowlist.
- `raw_preservation` — sha256 + the Lane-1 `crawl_runs` ledger (`record_crawl_run`)
  + the reproducibility verifier.
- the `sources` / `documents` schema (migrations 0001 / 0003) — no new migration;
  provenance fits existing columns (sign-off **C4**).

Sign-off conditions honored here:
- **C1 no drift** — selection comes only from the shared walk.
- **C2 COPY, not reference-in-place** — `/Users/IA/Documents/TOA/TownOfAlpine` is a
  LIVE agent scratch dir (files get rewritten), so reference-in-place would break
  the sha256-reproducibility AC the moment an upstream file changes. We snapshot
  each file's bytes into a managed, gitignored, sha-addressed raw store at ingest
  and hash the snapshot. Reproducibility is then a property of OUR store.
- **C4 per-document provenance** — one corpus-level `sources` row
  (`alpine_local_corpus`); each `documents` row carries the meeting/folder date
  (`doc_date`), the original absolute path (`source_url` as a `file://` URI), the
  file type (`doc_type`), and the sha256. Meeting grouping is left to GOV-125's
  `meetings` table, not flattened away here.

Data boundary: raw bytes, the SQLite DB, and the raw store stay local/vault-only
and are never committed (`.gitignore` covers `Database/*.db` and `Raw-Corpus/`).
Only the tooling + sanitized counts/logs are committable.

Idempotent: `documents.source_url` is UNIQUE, so re-running upserts the same rows;
the raw store is sha-addressed, so identical bytes resolve to the same path and a
second run copies nothing and changes no hash.

Usage:
    python scripts/ingest_local_corpus.py --source-dir /Users/IA/Documents/TOA/TownOfAlpine --report
    python scripts/ingest_local_corpus.py --dry-run            # plan only, no writes
    python scripts/raw_preservation.py verify --object-type document   # reproducibility
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import manifest_local_corpus as mlc  # noqa: E402
import source_inventory as si  # noqa: E402
from raw_preservation import (  # noqa: E402
    record_crawl_run,
    sha256_file,
    verify_reproducibility,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = Path("/Users/IA/Documents/TOA/TownOfAlpine")
# Managed, gitignored raw store (C2). Relative paths under REPO_ROOT so
# raw_preservation.verify resolves `REPO_ROOT / local_path`.
RAW_STORE_DIRNAME = "Raw-Corpus"
CORPUS_SOURCE_ID = "alpine_local_corpus"
CORPUS_NOTE_PATH = "Docs/Source-Data/source-registry/alpine_local_corpus.md"

# Deterministic doc_type sub-typing by filename (SourceArchivist: "MEET-Agenda* ->
# agenda, youtube_transcript* -> transcript"). Ordered; first match wins. This is
# a label only — it never changes WHICH files are ingested (that is the signed
# selection); it gives GOV-125 structuring a typed starting point.
def doc_type_for(sf: "mlc.SelectedFile") -> str:
    if sf.origin == "allowlist" or sf.file_class.source_type == "notice":
        return "notice"
    name = sf.path.name.lower()
    if "transcript" in name:
        return "transcript"
    if name.startswith("meet-agenda") or "_agenda" in name or "agenda" in name:
        return "agenda"
    if "packet" in name:
        return "meeting_packet"
    if "minutes" in name:
        return "minutes"
    if name.startswith("ord") or "ordinance" in name or "emergord" in name:
        return "ordinance"
    if name.startswith("res") or "resolution" in name:
        return "resolution"
    if "staff" in name and "report" in name or name.startswith("staffreport"):
        return "staff_report"
    if "report" in name or "recap" in name:
        return "report"
    if "press-release" in name or "press_release" in name:
        return "press_release"
    if sf.path.suffix.lower() == ".txt":
        return "transcript_text"
    return "document"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def ensure_corpus_source(conn, corpus_root: Path, *, scan_date: str) -> None:
    """Upsert the single corpus-level `sources` row (C4) via the validated path."""
    seed = {
        "source_id": CORPUS_SOURCE_ID,
        "name": "Town of Alpine — local meeting corpus (on-disk archive)",
        "scope": "alpine",
        "url": corpus_root.resolve().as_uri(),
        "original_url": corpus_root.resolve().as_uri(),
        "source_type": "local_archive",
        "source_class": "alpine-official",
        "source_authority_level": "primary",
        "jurisdiction": "Alpine",
        "expected_artifacts": "agendas,packets,minutes,ordinances,resolutions,"
        "staff_reports,transcripts,notices",
        "robots_policy": "n/a-local",
        "owner_agent": "BackendCrawlerEngineer",
        "scan_date": scan_date,
        "raw_preservation_status": "raw_preserved",
        "local_note_path": CORPUS_NOTE_PATH,
        "topic_tags": "alpine,meetings,local-corpus",
        "notes": "On-disk curated corpus ingested by ingest_local_corpus.py "
        "(GOV-124). Selection signed off GOV-133. .md = derived (not ingested). "
        "Raw bytes COPIED into the gitignored Raw-Corpus store; reference-in-place "
        "rejected (live scratch dir).",
    }
    si.upsert_sources(conn, [seed])


def _upsert_document(conn, *, source_url: str, title: str, doc_type: str,
                     doc_date: str, local_path: str, sha256: str,
                     size_bytes: int, fetch_time_utc: str) -> bool:
    """Idempotent upsert keyed on the UNIQUE source_url. Returns True if new.

    fetch_time_utc is preserved on conflict (stable first-seen provenance), so a
    re-run changes no row meaningfully — supports the reproducibility AC.
    """
    existing = conn.execute(
        "SELECT id FROM documents WHERE source_url = ?", (source_url,)
    ).fetchone()
    conn.execute(
        "INSERT INTO documents (source_url, title, doc_type, doc_date, local_path, "
        "sha256, size_bytes, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_url) DO UPDATE SET "
        "title=excluded.title, doc_type=excluded.doc_type, doc_date=excluded.doc_date, "
        "local_path=excluded.local_path, sha256=excluded.sha256, "
        "size_bytes=excluded.size_bytes, source_id=excluded.source_id",
        (source_url, title, doc_type, doc_date, local_path, sha256, size_bytes,
         fetch_time_utc, CORPUS_SOURCE_ID),
    )
    return existing is None


def _apply_only_date(selection: list, only_date: str | None) -> list:
    """Narrow the SIGNED selection to a single meeting date (GOV-621 pilot scope).

    Post-walk, exclude-ONLY: the signed GOV-133 walk + classification run first and
    unchanged; this only DROPS files whose `meeting_date` != `only_date`. It can never
    add or reclassify a file, so sign-off condition C1 (no drift) holds. Provenance
    (`source_url` from the real path) is untouched. Allowlisted out-of-folder notices
    carry their own parsed date and are dropped unless they match the window.
    """
    if not only_date:
        return selection
    return [sf for sf in selection if sf.meeting_date == only_date]


def ingest(corpus_root: Path, db_path: Path, *, dry_run: bool = False,
           only_date: str | None = None) -> dict:
    """Ingest the signed source-of-record selection, oldest→newest.

    `only_date` (YYYY-MM-DD), when set, narrows the run to one meeting folder
    (GOV-621 Option-C pilot) via a post-walk exclude-only filter — see
    `_apply_only_date`.

    Returns a summary dict (counts, doc_type breakdown, coverage, run id).
    """
    corpus_root = corpus_root.resolve()
    selection = _apply_only_date(
        mlc.iter_source_of_record_files(corpus_root), only_date
    )
    raw_store = REPO_ROOT / RAW_STORE_DIRNAME
    started = _now_utc()
    scan_date = started[:10]

    by_doc_type: dict[str, int] = {}
    new_rows = 0
    copied = 0
    failures: list[dict] = []
    folders_with_primary: set[str] = set()

    if dry_run:
        for sf in selection:
            by_doc_type[doc_type_for(sf)] = by_doc_type.get(doc_type_for(sf), 0) + 1
            if sf.origin == "meeting_folder":
                folders_with_primary.add(sf.meeting_date)
        return _summary(corpus_root, selection, by_doc_type, new_rows=0,
                        copied=0, failures=failures,
                        folders_with_primary=folders_with_primary,
                        run_id=None, dry_run=True, only_date=only_date)

    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        ensure_corpus_source(conn, corpus_root, scan_date=scan_date)
        for sf in selection:
            try:
                sha = sha256_file(sf.path)
                ext = sf.path.suffix.lower()
                rel = f"{RAW_STORE_DIRNAME}/{sha[:2]}/{sha}{ext}"
                dest = REPO_ROOT / rel
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sf.path, dest)
                    if sha256_file(dest) != sha:  # copy-integrity gate
                        dest.unlink(missing_ok=True)
                        raise OSError(f"copy hash mismatch for {sf.path}")
                    copied += 1
                doc_type = doc_type_for(sf)
                is_new = _upsert_document(
                    conn,
                    source_url=sf.path.resolve().as_uri(),
                    title=sf.path.name,
                    doc_type=doc_type,
                    doc_date=sf.meeting_date,
                    local_path=rel,
                    sha256=sha,
                    size_bytes=sf.path.stat().st_size,
                    fetch_time_utc=_now_utc(),
                )
                new_rows += int(is_new)
                by_doc_type[doc_type] = by_doc_type.get(doc_type, 0) + 1
                if sf.origin == "meeting_folder":
                    folders_with_primary.add(sf.meeting_date)
            except Exception as exc:  # record, continue; surfaced in the run log
                failures.append({"path": str(sf.path), "error": str(exc)})
        conn.commit()

        # Orphan check: every document must resolve to its source row.
        orphans = conn.execute(
            "SELECT COUNT(*) FROM documents d LEFT JOIN sources s "
            "ON d.source_id = s.source_id WHERE s.source_id IS NULL"
        ).fetchone()[0]

        finished = _now_utc()
        run_id = record_crawl_run(
            conn,
            started_utc=started,
            finished_utc=finished,
            status="succeeded" if not failures else "partial",
            source_set=[CORPUS_SOURCE_ID],
            new_documents=new_rows,
            targets=[str(corpus_root)],
            notes=f"local-corpus ingest: {len(selection)} selected, {new_rows} new, "
            f"{copied} copied to raw store, {len(failures)} failures, orphans={orphans}",
        )

    summary = _summary(corpus_root, selection, by_doc_type, new_rows=new_rows,
                       copied=copied, failures=failures,
                       folders_with_primary=folders_with_primary,
                       run_id=run_id, dry_run=False, only_date=only_date)
    summary["orphans"] = orphans
    return summary


def _summary(corpus_root, selection, by_doc_type, *, new_rows, copied, failures,
             folders_with_primary, run_id, dry_run, only_date=None) -> dict:
    all_folders = [d for d, _ in mlc.iter_meeting_folders(corpus_root)]
    if only_date:  # scope coverage to the pilot window so denominators stay coherent
        all_folders = [d for d in all_folders if d == only_date]
    md_only = [d for d in all_folders if d not in folders_with_primary]
    return {
        "dry_run": dry_run,
        "run_id": run_id,
        "selected": len(selection),
        "new_documents": new_rows,
        "copied_to_raw_store": copied,
        "by_doc_type": dict(sorted(by_doc_type.items())),
        "failures": failures,
        "coverage": {
            "meeting_folders_total": len(all_folders),
            "with_primary_source": len(folders_with_primary),
            "derived_md_only": len(md_only),
            "earliest_primary": min(folders_with_primary) if folders_with_primary else None,
            "md_only_dates": md_only,
        },
    }


def render_report(summary: dict) -> str:
    cov = summary["coverage"]
    lines = ["# GOV-124 local-corpus ingest — run report", ""]
    tag = "[DRY-RUN] " if summary["dry_run"] else ""
    lines.append(f"{tag}selected source-of-record files: **{summary['selected']}**")
    lines.append(f"- new `documents` rows: {summary['new_documents']}")
    lines.append(f"- copied to gitignored raw store: {summary['copied_to_raw_store']}")
    if "orphans" in summary:
        lines.append(f"- orphan documents (must be 0): **{summary['orphans']}**")
    if summary["run_id"] is not None:
        lines.append(f"- `crawl_runs` run id: {summary['run_id']}")
    lines.append("")
    lines.append("## doc_type breakdown")
    for dt, n in summary["by_doc_type"].items():
        lines.append(f"- {dt}: {n}")
    lines.append("")
    lines.append("## Coverage (oldest→newest) — carry the gap to GOV-125/GOV-129")
    lines.append(
        f"- meeting folders: {cov['meeting_folders_total']} total · "
        f"**{cov['with_primary_source']} with primary source** · "
        f"{cov['derived_md_only']} derived-md-only (no primary)"
    )
    lines.append(
        f"- earliest folder with primary source: {cov['earliest_primary']} "
        "(earlier dates are lead-only → must render with a gap/low-confidence label)"
    )
    if summary["failures"]:
        lines.append("")
        lines.append(f"## Failures ({len(summary['failures'])})")
        for f in summary["failures"]:
            lines.append(f"- {f['path']}: {f['error']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest the on-disk Alpine corpus (GOV-124, signed selection)."
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_CORPUS,
                        help="corpus root (local/vault-only)")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH,
                        help="SQLite DB path (default: Database/gov_watchdog.db)")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan the selection without writing rows/copying bytes")
    parser.add_argument("--only-date", metavar="YYYY-MM-DD", default=None,
                        help="narrow the run to ONE meeting date (post-walk, "
                        "exclude-only; GOV-621 pilot scope)")
    parser.add_argument("--report", action="store_true",
                        help="print the full markdown run/coverage report")
    args = parser.parse_args(argv)

    summary = ingest(args.source_dir, args.db, dry_run=args.dry_run,
                     only_date=args.only_date)
    if args.report:
        print(render_report(summary))
    else:
        tag = "[dry-run] " if summary["dry_run"] else ""
        print(
            f"{tag}selected={summary['selected']} new={summary['new_documents']} "
            f"copied={summary['copied_to_raw_store']} "
            f"with_primary={summary['coverage']['with_primary_source']}/"
            f"{summary['coverage']['meeting_folders_total']} "
            f"failures={len(summary['failures'])}"
        )
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
