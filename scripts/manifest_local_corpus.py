"""Selection manifest for the on-disk Town-of-Alpine corpus (GOV-124, Stage 1).

Chain item #1 of 4 (CEO sequencing on GOV-123); the actual Stage 1 goal
`927f07dc` — Alpine source inventory + raw preservation on the REAL corpus.

WHY THIS EXISTS (read first)
---------------------------
The existing Lane-1 scripts are *online acquirers* — `crawl_pdfs.py` fetches over
HTTP, `fetch_transcripts.py` pulls from YouTube, `source_inventory.py` seeds a
hardcoded 4-source list. None of them walks a local folder. The corpus at
`/Users/IA/Documents/TOA/TownOfAlpine` is already-downloaded artifacts on disk,
so GOV-124 is a *build*: a new on-disk ingest adapter that reuses the existing
`raw_preservation` / `crawl_runs` / `sources`+`documents` primitives.

Before any bulk ingest writes a single row, the corpus has to be *selected*:
which folders are meeting source-records, which top-level dirs are operational
scratch (and must be excluded), and how each file type is classified. That
selection is a provenance/coverage decision SourceArchivist owns. This module
produces the **selection manifest** for that sign-off checkpoint (GOV-124 plan
§2) — and it is the SAME deterministic walk + classification the ingest adapter
will import, so the selection that gets signed off is byte-identical to the
selection that gets ingested. (No silent drift between "what the reviewer
approved" and "what the run actually touched".)

SAFETY / DATA BOUNDARY
----------------------
Read-only. Walks the tree and counts; it never reads file *contents*, never
copies bytes, never writes to the DB, never reaches the network. Output is
sanitized metadata only — per-folder *counts* per type, byte footprint, and the
excluded-dir list — so it is committable (the data-publication boundary keeps
raw bytes local/vault-only; this emits no raw bytes). `mayor-investigation/` and
every non-meeting dir are reported in the exclusion list with a reason, so a
reviewer can confirm nothing source-bearing was dropped.

Usage:
    python scripts/manifest_local_corpus.py \
        --source-dir /Users/IA/Documents/TOA/TownOfAlpine \
        --json Docs/GOV-124-selection-manifest.json \
        --markdown Docs/GOV-124-selection-manifest.md

Output paths note: the manifest is sanitized counts only, so it lives under the
committed `Docs/` tree — NOT `Docs/Source-Data/` (which is reserved for raw
vault-only data and, on a case-insensitive FS, is the same dir as `source-data`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# A meeting folder is named exactly YYYY-MM-DD (the meeting date == provenance
# scan/meeting date + the "section" grouping). Anything else at top level is
# operational/scratch and is excluded by default.
MEETING_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Top-level dirs we explicitly know are NOT meeting source records. Reported in
# the exclusion list so the reviewer sees them by name. `mayor-investigation` is
# a defamation/privacy risk surface (RISK cat 3/4) — never ingested as
# "source/fact" in this pass; any future inclusion is a SecurityPrivacyAgent +
# CEO gated decision, out of scope here.
KNOWN_NON_MEETING_DIRS = {
    "directives": "operational — agent directives, not a meeting record",
    "master": "operational — master/index notes, not a meeting record",
    "execution": "operational — execution logs, not a meeting record",
    "reports": "derived — generated reports, not a primary source record",
    "weekly-digests": "derived — weekly digests, not a primary source record",
    "mayor-investigation": (
        "EXCLUDED (RISK cat 3/4) — defamation/privacy surface; not ingested as "
        "source/fact this pass. Any future inclusion = SecurityPrivacyAgent + CEO gate"
    ),
}

# File-type classification. `selection` is a PROPOSED default the manifest puts
# in front of SourceArchivist; `open_question` flags the ones that genuinely need
# a reviewer decision rather than a guess (GOV-124 plan §2.4 / spec-first rule).
#   - source_of_record=True  -> a primary artifact ingested as a `documents` row
#   - source_of_record=False -> provenance recorded, marked derived/secondary
@dataclass(frozen=True)
class FileClass:
    source_type: str
    source_of_record: bool
    note: str
    open_question: str | None = None


FILE_CLASSES: dict[str, FileClass] = {
    ".pdf": FileClass(
        source_type="document",
        source_of_record=True,
        note="official document (agenda/minutes/notice/ordinance) — primary source-of-record",
    ),
    ".txt": FileClass(
        source_type="transcript_text",
        source_of_record=True,
        note="plain-text meeting record (likely transcript)",
        open_question="Confirm .txt are meeting transcripts vs scratch notes "
        "(affects source-of-record vs derived).",
    ),
    ".md": FileClass(
        source_type="derived_note",
        source_of_record=False,
        note="markdown — DEFAULT treat as derived/secondary (record provenance, "
        "not source-of-record)",
        open_question="Are any in-folder .md primary source artifacts rather than "
        "derived notes/digests? Default = derived unless SourceArchivist names specifics.",
    ),
    ".json": FileClass(
        source_type="metadata_sidecar",
        source_of_record=False,
        note="likely metadata sidecar — provenance only, not source-of-record",
        open_question="Inspect: metadata sidecars vs primary data?",
    ),
}

# Anything not in FILE_CLASSES (e.g. .DS_Store, images, .docx) is reported under
# "other" and excluded by default; the reviewer can promote a type if needed.
OTHER_CLASS = FileClass(
    source_type="other",
    source_of_record=False,
    note="unclassified file type — excluded by default, listed for reviewer",
)


def classify_file(path: Path) -> FileClass:
    """Deterministic file-type classification (the adapter imports this)."""
    return FILE_CLASSES.get(path.suffix.lower(), OTHER_CLASS)


def iter_meeting_folders(root: Path) -> list[tuple[str, Path]]:
    """Date-named meeting folders, sorted **oldest→newest** (Isaac June-6 directive).

    Lexical sort on YYYY-MM-DD == chronological sort, so a plain `sorted` is the
    ingest order. Returns (date_str, path) pairs. The adapter walks this exact list.
    """
    folders = [
        (p.name, p)
        for p in root.iterdir()
        if p.is_dir() and MEETING_DIR_RE.match(p.name)
    ]
    folders.sort(key=lambda pair: pair[0])
    return folders


def _excluded_top_level(root: Path) -> list[dict]:
    """Every top-level entry that is NOT a meeting folder, with a reason."""
    excluded: list[dict] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_dir() and MEETING_DIR_RE.match(name):
            continue  # included — handled elsewhere
        if name.startswith("."):
            reason = "dot dir/file — operational/scratch, excluded"
        elif entry.is_dir():
            reason = KNOWN_NON_MEETING_DIRS.get(
                name, "non-date top-level dir — not a meeting record, excluded by default"
            )
        else:
            reason = "loose top-level file — not inside a meeting folder, excluded"
        excluded.append({"name": name, "is_dir": entry.is_dir(), "reason": reason})
    return excluded


@dataclass
class FolderReport:
    date: str
    type_counts: dict[str, int] = field(default_factory=dict)
    type_bytes: dict[str, int] = field(default_factory=dict)
    source_of_record_count: int = 0
    total_files: int = 0


def build_manifest(root: Path) -> dict:
    """Produce the full sanitized selection manifest (counts only, no contents)."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"corpus root not found: {root}")

    folders = iter_meeting_folders(root)
    folder_reports: list[FolderReport] = []
    totals_count: dict[str, int] = {}
    totals_bytes: dict[str, int] = {}
    sor_footprint_bytes = 0  # bytes that WOULD be preserved (source-of-record only)
    # File types found INSIDE meeting folders that are not in FILE_CLASSES
    # (e.g. extensionless, .err) — neither source-of-record nor a known derived
    # type. Surfaced explicitly so the reviewer can confirm none are source-bearing.
    unclassified: dict[str, dict[str, int]] = {}

    for date_str, folder in folders:
        rep = FolderReport(date=date_str)
        for f in sorted(folder.rglob("*")):
            if not f.is_file():
                continue
            ext = f.suffix.lower() or "<none>"
            fc = classify_file(f)
            size = f.stat().st_size
            rep.type_counts[ext] = rep.type_counts.get(ext, 0) + 1
            rep.type_bytes[ext] = rep.type_bytes.get(ext, 0) + size
            rep.total_files += 1
            totals_count[ext] = totals_count.get(ext, 0) + 1
            totals_bytes[ext] = totals_bytes.get(ext, 0) + size
            if fc.source_of_record:
                rep.source_of_record_count += 1
                sor_footprint_bytes += size
            elif fc is OTHER_CLASS:
                bucket = unclassified.setdefault(ext, {"files": 0, "bytes": 0})
                bucket["files"] += 1
                bucket["bytes"] += size
        folder_reports.append(rep)

    excluded = _excluded_top_level(root)

    classification = {
        ext: {
            "source_type": fc.source_type,
            "source_of_record": fc.source_of_record,
            "note": fc.note,
            "open_question": fc.open_question,
            "files": totals_count.get(ext, 0),
            "bytes": totals_bytes.get(ext, 0),
        }
        for ext, fc in FILE_CLASSES.items()
    }

    open_questions = [
        f"{ext}: {fc.open_question}"
        for ext, fc in FILE_CLASSES.items()
        if fc.open_question
    ]
    if unclassified:
        kinds = ", ".join(
            f"{ext}×{v['files']}" for ext, v in sorted(unclassified.items())
        )
        open_questions.append(
            f"UNCLASSIFIED in meeting folders ({kinds}): excluded by default — "
            "confirm none are source-bearing, or name a type to promote."
        )
    # Build-level decisions that also belong to the sign-off (GOV-124 plan §4 step 2/3).
    open_questions.append(
        "RAW STORAGE: copy bytes into a managed gitignored raw store (recommended; "
        "true preservation, ~{} footprint) vs reference-in-place by absolute path "
        "(no duplication; original already vault-only). raw_preservation.verify works "
        "for both.".format(_human_bytes(sor_footprint_bytes))
    )
    open_questions.append(
        "SOURCE GRANULARITY: one `sources` row for the whole local corpus "
        "(`alpine_local_corpus`) vs one per meeting folder. Recommend one corpus-level "
        "source; meeting grouping is modeled later by the `meetings` table (GOV-125/1.07)."
    )

    return {
        "issue": "GOV-124",
        "corpus_root": str(root),
        "generated_by": "scripts/manifest_local_corpus.py (read-only, sanitized counts)",
        "meeting_folders": {
            "count": len(folders),
            "oldest": folders[0][0] if folders else None,
            "newest": folders[-1][0] if folders else None,
            "order": "oldest->newest (Isaac June-6 directive)",
        },
        "totals": {
            "files_by_type": totals_count,
            "bytes_by_type": totals_bytes,
            "source_of_record_footprint_bytes": sor_footprint_bytes,
            "source_of_record_footprint_human": _human_bytes(sor_footprint_bytes),
        },
        "classification": classification,
        "unclassified_in_meeting_folders": unclassified,
        "excluded_top_level": excluded,
        "open_questions": open_questions,
        "folders": [
            {
                "date": r.date,
                "total_files": r.total_files,
                "source_of_record_files": r.source_of_record_count,
                "type_counts": r.type_counts,
            }
            for r in folder_reports
        ],
    }


def _human_bytes(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f}{unit}" if unit != "B" else f"{int(val)}B"
        val /= 1024
    return f"{n}B"


def render_markdown(manifest: dict) -> str:
    mf = manifest["meeting_folders"]
    tot = manifest["totals"]
    lines: list[str] = []
    lines.append("# GOV-124 — Town-of-Alpine local corpus selection manifest")
    lines.append("")
    lines.append(
        "_Generated by `scripts/manifest_local_corpus.py` — read-only, sanitized "
        "counts only (no raw contents, no bytes published). Awaiting SourceArchivist "
        "provenance/coverage sign-off before bulk ingest (GOV-124 plan §2)._"
    )
    lines.append("")
    lines.append(f"- **Corpus root:** `{manifest['corpus_root']}` (local/vault-only)")
    lines.append(
        f"- **Meeting folders (included):** {mf['count']} — "
        f"oldest `{mf['oldest']}` → newest `{mf['newest']}`, ingested {mf['order']}"
    )
    lines.append(
        f"- **Source-of-record preservation footprint:** "
        f"{tot['source_of_record_footprint_human']} "
        f"({tot['source_of_record_footprint_bytes']} bytes)"
    )
    lines.append("")
    lines.append("## File-type classification (PROPOSED — confirm/override)")
    lines.append("")
    lines.append("| ext | source_type | source-of-record? | files | bytes | note |")
    lines.append("|-----|-------------|-------------------|------:|------:|------|")
    for ext, c in manifest["classification"].items():
        lines.append(
            f"| `{ext}` | {c['source_type']} | "
            f"{'**yes**' if c['source_of_record'] else 'no (derived)'} | "
            f"{c['files']} | {c['bytes']} | {c['note']} |"
        )
    lines.append("")
    unclassified = manifest.get("unclassified_in_meeting_folders") or {}
    if unclassified:
        lines.append("## Unclassified files inside meeting folders (excluded by default)")
        lines.append("")
        lines.append("| ext | files | bytes |")
        lines.append("|-----|------:|------:|")
        for ext, v in sorted(unclassified.items()):
            lines.append(f"| `{ext}` | {v['files']} | {v['bytes']} |")
        lines.append("")
    lines.append("## Open questions for sign-off")
    lines.append("")
    for q in manifest["open_questions"]:
        lines.append(f"- {q}")
    lines.append("")
    lines.append("## Excluded top-level entries (confirm nothing source-bearing dropped)")
    lines.append("")
    lines.append("| entry | dir? | reason |")
    lines.append("|-------|------|--------|")
    for e in manifest["excluded_top_level"]:
        lines.append(f"| `{e['name']}` | {'yes' if e['is_dir'] else 'no'} | {e['reason']} |")
    lines.append("")
    lines.append("## Per-folder coverage (oldest→newest)")
    lines.append("")
    lines.append("| meeting date | total files | source-of-record | type counts |")
    lines.append("|--------------|------------:|-----------------:|-------------|")
    for fr in manifest["folders"]:
        tc = ", ".join(f"{k}:{v}" for k, v in sorted(fr["type_counts"].items()))
        lines.append(
            f"| {fr['date']} | {fr['total_files']} | "
            f"{fr['source_of_record_files']} | {tc} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the GOV-124 Alpine local-corpus selection manifest (read-only)."
    )
    parser.add_argument(
        "--source-dir", type=Path,
        default=Path("/Users/IA/Documents/TOA/TownOfAlpine"),
        help="corpus root (local/vault-only)",
    )
    parser.add_argument("--json", type=Path, help="write the JSON manifest here")
    parser.add_argument("--markdown", type=Path, help="write the human manifest here")
    args = parser.parse_args(argv)

    manifest = build_manifest(args.source_dir)
    mf = manifest["meeting_folders"]
    print(
        f"manifest: {mf['count']} meeting folders {mf['oldest']}..{mf['newest']} | "
        f"types={manifest['totals']['files_by_type']} | "
        f"source-of-record footprint={manifest['totals']['source_of_record_footprint_human']} | "
        f"excluded top-level={len(manifest['excluded_top_level'])}"
    )
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.json}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(manifest), encoding="utf-8")
        print(f"wrote {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
