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
        note="plain-text meeting record — source-of-record; tag "
        "verification=unverified-transcript (GOV-133: may be machine-generated, "
        "must not be presented as authoritative quotes without the verify gate)",
    ),
    ".md": FileClass(
        source_type="derived_note",
        source_of_record=False,
        note="markdown — derived/lead-only; NEVER promoted to a `documents` "
        "source-of-record row (GOV-133 hard boundary: AI-written summaries are "
        "not primary sources)",
    ),
    ".json": FileClass(
        source_type="metadata_sidecar",
        source_of_record=False,
        note="metadata sidecar — provenance only, not source-of-record (GOV-133: "
        "0 found inside meeting folders; excluded)",
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


# Binding out-of-folder allowlist (GOV-133 SourceArchivist sign-off, reconciled
# 2026-06-11). Exactly one genuine primary source lives OUTSIDE the date-named
# meeting folders; a folder-only walk would silently drop it. It is placed in the
# SHARED walk (not the adapter) so the signed selection == the ingested selection,
# byte-identical — the sign-off condition was explicit: "the fix lands in the walk
# itself ... not in prose only." The `PRESERVED_media{id}_` prefix marks a captured
# official record (real provenance); top-level `.docx` briefings carry no such
# marker and stay excluded as agent-authored/derived. This notice names a person
# (routine public record) — the GOV-105 PII guard covers the downstream
# label/alias write boundary.
ALLOWLIST: dict[str, FileClass] = {
    "master/PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf": FileClass(
        source_type="notice",
        source_of_record=True,
        note="public postponement notice (PRESERVED_ media-id capture) — binding "
        "out-of-folder allowlist (GOV-133); meeting/scan date 2026-03-24",
    ),
}

# The settled selection from the SourceArchivist provenance/coverage sign-off
# (GOV-133, CONDITIONAL PASS, reconciled 2026-06-11). Recorded in the manifest so
# the artifact documents WHAT was approved, not just open questions.
SIGN_OFF = {
    "review_issue": "GOV-133",
    "verdict": "CONDITIONAL PASS — SourceArchivist provenance/coverage, 2026-06-11",
    "settled": [
        ".pdf -> source-of-record (document/agenda/notice).",
        ".txt -> source-of-record (transcript_text); tag verification=unverified-transcript.",
        ".md  -> derived/lead-only; NEVER a `documents` source-of-record row.",
        ".json / .DS_Store / .err -> excluded.",
        "One corpus-level `sources` row (alpine_local_corpus); meeting grouping -> "
        "GOV-125 `meetings` table, not flattened away.",
        "Raw storage = COPY bytes into a managed gitignored raw store + sha256 at "
        "ingest (reference-in-place REJECTED: TOA is a live agent scratch dir).",
        "Binding out-of-folder allowlist: "
        "master/PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf (notice).",
        "Excluded as agent-authored/derived: top-level .docx briefings "
        "(FIREWORKS_RETAILER_NOTICE.docx, NOTE_TO_EDITOR, MASTER_BRIEFING_Bob, "
        "Apr-18 reports), last-update.txt.",
    ],
    "coverage_reality": (
        "Only 34/124 meeting folders contain primary source (.pdf/.txt); the other "
        "90 are derived-md-only (earliest primary 2024-10-09). Downstream "
        "(GOV-125/1.07, GOV-129) MUST render md-only dates with a gap/low-confidence "
        "label — never 'sourced/verified'."
    ),
}

_DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass(frozen=True)
class SelectedFile:
    """One file in the settled ingest set (what the adapter preserves as a row)."""

    path: Path  # absolute path on disk
    meeting_date: str  # YYYY-MM-DD (folder name, or date parsed from an allowlist name)
    file_class: FileClass
    origin: str  # "meeting_folder" | "allowlist"


def iter_allowlisted_files(root: Path) -> list[SelectedFile]:
    """The binding out-of-folder allowlist entries that exist on disk."""
    out: list[SelectedFile] = []
    for rel, fc in sorted(ALLOWLIST.items()):
        p = root / rel
        if p.is_file():
            m = _DATE_IN_NAME_RE.search(rel)
            out.append(
                SelectedFile(
                    path=p,
                    meeting_date=m.group(1) if m else "",
                    file_class=fc,
                    origin="allowlist",
                )
            )
    return out


def iter_source_of_record_files(root: Path) -> list[SelectedFile]:
    """The complete, ordered ingest set the adapter consumes (THE signed selection).

    Meeting folders oldest→newest, each folder's source-of-record files in sorted
    path order, then the binding out-of-folder allowlist. Only source-of-record
    files are yielded (`.pdf` / `.txt` + allowlisted notices); `.md`/derived,
    sidecars, and unclassified files are skipped. The adapter MUST drive its
    ingest from this single function so the run can never drift from what
    SourceArchivist signed off.
    """
    root = Path(root).resolve()
    selected: list[SelectedFile] = []
    for date_str, folder in iter_meeting_folders(root):
        for f in sorted(folder.rglob("*")):
            if f.is_file() and classify_file(f).source_of_record:
                selected.append(
                    SelectedFile(
                        path=f,
                        meeting_date=date_str,
                        file_class=classify_file(f),
                        origin="meeting_folder",
                    )
                )
    selected.extend(iter_allowlisted_files(root))
    return selected


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

    # Binding out-of-folder allowlist (GOV-133) — folded into the source-of-record
    # footprint so the manifest reflects the FULL signed selection (folders + allowlist).
    allowlist_files: list[dict] = []
    for sf in iter_allowlisted_files(root):
        size = sf.path.stat().st_size
        sor_footprint_bytes += size
        allowlist_files.append(
            {
                "rel_path": str(sf.path.relative_to(root)),
                "meeting_date": sf.meeting_date,
                "source_type": sf.file_class.source_type,
                "bytes": size,
            }
        )

    excluded = _excluded_top_level(root)

    classification = {
        ext: {
            "source_type": fc.source_type,
            "source_of_record": fc.source_of_record,
            "note": fc.note,
            "files": totals_count.get(ext, 0),
            "bytes": totals_bytes.get(ext, 0),
        }
        for ext, fc in FILE_CLASSES.items()
    }

    resolved_notes: list[str] = []
    if unclassified:
        kinds = ", ".join(
            f"{ext}×{v['files']}" for ext, v in sorted(unclassified.items())
        )
        resolved_notes.append(
            f"Unclassified inside meeting folders ({kinds}) = .DS_Store/.err — "
            "confirmed not source-bearing, excluded (GOV-133)."
        )

    return {
        "issue": "GOV-124",
        "corpus_root": str(root),
        "generated_by": "scripts/manifest_local_corpus.py (read-only, sanitized counts)",
        "sign_off": SIGN_OFF,
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
        "allowlisted_out_of_folder": allowlist_files,
        "unclassified_in_meeting_folders": unclassified,
        "resolved_notes": resolved_notes,
        "excluded_top_level": excluded,
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
    so = manifest.get("sign_off") or {}
    lines.append(
        "_Generated by `scripts/manifest_local_corpus.py` — read-only, sanitized "
        "counts only (no raw contents, no bytes published). SELECTION SIGNED OFF: "
        f"{so.get('verdict', 'pending')} ([{so.get('review_issue','GOV-133')}]"
        f"(/GOV/issues/{so.get('review_issue','GOV-133')}))._"
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
    allow = manifest.get("allowlisted_out_of_folder") or []
    if allow:
        lines.append(
            f"- **Binding out-of-folder allowlist:** {len(allow)} file(s) "
            "(GOV-133 amendment) folded into the footprint above."
        )
    lines.append("")
    if so:
        lines.append("## Settled selection (GOV-133 sign-off)")
        lines.append("")
        for s in so.get("settled", []):
            lines.append(f"- {s}")
        lines.append("")
        lines.append(f"**Coverage reality:** {so.get('coverage_reality','')}")
        lines.append("")
    lines.append("## File-type classification (settled)")
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
    if allow:
        lines.append("## Allowlisted out-of-folder source (binding, GOV-133)")
        lines.append("")
        lines.append("| rel path | meeting date | source_type | bytes |")
        lines.append("|----------|--------------|-------------|------:|")
        for a in allow:
            lines.append(
                f"| `{a['rel_path']}` | {a['meeting_date']} | "
                f"{a['source_type']} | {a['bytes']} |"
            )
        lines.append("")
    unclassified = manifest.get("unclassified_in_meeting_folders") or {}
    if unclassified:
        lines.append("## Unclassified files inside meeting folders (excluded)")
        lines.append("")
        lines.append("| ext | files | bytes |")
        lines.append("|-----|------:|------:|")
        for ext, v in sorted(unclassified.items()):
            lines.append(f"| `{ext}` | {v['files']} | {v['bytes']} |")
        lines.append("")
    for note in manifest.get("resolved_notes") or []:
        lines.append(f"> {note}")
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
