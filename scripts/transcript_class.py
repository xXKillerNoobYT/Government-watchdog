"""`transcript_class` SSOT + deterministic Alpine backfill (GOV-275, Stage 2.05).

First Stage 2.05 backend slice. Owner: BackendCrawlerEngineer. Builds on the
Stage 2.04 contract (GOV-230 goal 7e4434b1) which froze `transcript_class` as a
CLOSED enum + fail-closed default and deferred the schema migration to Stage 2.05.
Migration 0018 (Database/migrations/0018_transcript_class.sql) adds the column;
this module is its single source of truth and the deterministic (no-AI) backfill.

SSOT PARITY: :data:`TRANSCRIPT_CLASSES` mirrors the `transcript_class` CHECK
literal in migration 0018 EXACTLY. A parity test
(tests/test_gov275_transcript_class.py) asserts the two cannot drift — the same
guard 0015/completeness.py uses for `gap_type`. The enum is FROZEN by GOV-230: a
Stage 2.x child MAY NOT add a value here without patching the GOV-230 contract
first (inheritance-by-reference).

WHY the classifier only emits two values: a row in the `transcripts` table is, by
schema (`full_text NOT NULL`), an actual transcript artifact — so `minutes_only`,
`derived_md_only`, and `no_transcript` (which describe NON-transcript inventory:
minutes PDFs, derived-md-only folders, registered-but-absent meetings) are not
reachable from a transcript row and are owned by the completeness-gap / inventory
layer, not this backfill. `official_transcript` is NEVER auto-assigned: GOV-230
states the official-vs-auto-caption ORIGIN cannot be determined from the artifact
alone, so the deterministic pass fails closed away from the highest-confidence
class — only a reviewer or an evidence-backed re-classification may upgrade to it.
The deterministic pass therefore reduces to: parseable MM:SS markers present ->
`auto_caption_timed`; otherwise the fail-closed default `auto_caption_untimed`.

Scope: NO network, NO AI, Alpine-only, local/vault-only. The default MAY NOT be
silently overridden by an AI lane (GOV-230 §default).
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import DEFAULT_DB_PATH, apply_migrations, open_db  # noqa: E402

# --- the controlled class vocabulary (SSOT — mirrors the 0018 CHECK) ---------

# GOV-230 closed enum. Order documents confidence (highest -> gap-only). Adding a
# value here means patching GOV-230 + the 0018 CHECK in lockstep (parity test).
TRANSCRIPT_CLASSES = frozenset({
    "official_transcript",    # timed, official body output; highest confidence
    "auto_caption_timed",     # auto-caption retaining MM:SS; medium confidence
    "auto_caption_untimed",   # ASR-only, no MM:SS; lower confidence; FAIL-CLOSED DEFAULT
    "minutes_only",           # official minutes (paraphrase); no quoted_text projection
    "derived_md_only",        # derived markdown, no primary source; blocks statements
    "no_transcript",          # registered meeting, no transcript artifact; gap-only
})

# GOV-230 §default: ambiguous classification fails closed to this value. It forces
# locator = segment_id (no fabricated timestamps), a `missing_timestamps`
# completeness gap, and a lower confidence label downstream. Reversible by a
# reviewer or a deterministic re-classification pass; NOT silently by an AI lane.
DEFAULT_TRANSCRIPT_CLASS = "auto_caption_untimed"

# GOV-230 confidence-label mapping rule (operative; feeds the Stage 2.06 frontend
# surface contract). Recorded here as SSOT so downstream consumers derive the
# label from the class rather than re-deriving it. derived_md_only additionally
# BLOCKS statement production; no_transcript produces no statements at all.
CONFIDENCE_LABEL_BY_CLASS: Mapping[str, str] = {
    "official_transcript": "source_anchored_timed",
    "auto_caption_timed": "auto_caption_timed",
    "auto_caption_untimed": "auto_caption_untimed",
    "minutes_only": "minutes_summary",
    "derived_md_only": "derived_summary",
    # no_transcript intentionally absent: no statement is produced from it.
}

# Classes the deterministic transcripts-table backfill is permitted to assign.
# See module docstring: official requires reviewer evidence; the three non-row
# classes belong to the inventory / completeness-gap layer, not a transcript row.
DETERMINISTIC_ASSIGNABLE = frozenset({"auto_caption_timed", "auto_caption_untimed"})

# Import-time self-asserts: the SSOT must stay internally consistent.
assert DEFAULT_TRANSCRIPT_CLASS in TRANSCRIPT_CLASSES, (
    f"fail-closed default {DEFAULT_TRANSCRIPT_CLASS!r} not in TRANSCRIPT_CLASSES"
)
assert len(TRANSCRIPT_CLASSES) == 6, (
    f"GOV-230 froze a 6-value enum; found {len(TRANSCRIPT_CLASSES)}"
)
assert DETERMINISTIC_ASSIGNABLE <= TRANSCRIPT_CLASSES
assert set(CONFIDENCE_LABEL_BY_CLASS) <= TRANSCRIPT_CLASSES


class TranscriptClassError(ValueError):
    """Raised when a transcript_class value is outside the SSOT."""


# A timestamp marker: MM:SS or HH:MM:SS, e.g. "12:34" / "1:02:33" / "[00:05]".
# Anchored on a colon-separated numeric run; the surrounding `\b` keeps it from
# matching bare integers. Deterministic and origin-blind by design.
_TIMESTAMP_MARKER_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")

# How many distinct timestamp markers must appear before we treat a transcript as
# genuinely "timed". One stray "7:30" in prose (a meeting start time) must not flip
# an ASR-only transcript to timed; a real caption track carries many markers. Two
# is the minimal fail-closed threshold (a single match is ambiguous -> default).
_MIN_TIMESTAMP_MARKERS = 2


def _has_timestamp_markers(text: str | None) -> bool:
    """True iff *text* carries enough MM:SS markers to be a timed caption track.

    Deterministic: same text -> same answer. Fail-closed: ``None``/empty/too-few
    markers -> False (caller then assigns the untimed default).
    """
    if not text:
        return False
    seen: set[str] = set()
    for m in _TIMESTAMP_MARKER_RE.finditer(text):
        seen.add(m.group(0))
        if len(seen) >= _MIN_TIMESTAMP_MARKERS:
            return True
    return False


def classify_transcript(row: Mapping[str, Any]) -> str:
    """Deterministically classify one transcript row from observable structure.

    Derived ONLY from the row's own preserved fields (no AI, no network):
    `timestamped_text` is the timed-caption signal; absent reliable timestamps the
    class fails closed to :data:`DEFAULT_TRANSCRIPT_CLASS`. Origin (official vs
    auto-caption) is NOT inferable from the artifact, so this pass never returns
    `official_transcript` — only a reviewer/evidence-backed pass may upgrade.

    Returns a value in :data:`DETERMINISTIC_ASSIGNABLE`.
    """
    timestamped = row["timestamped_text"] if "timestamped_text" in row.keys() else None
    if _has_timestamp_markers(timestamped):
        return "auto_caption_timed"
    return DEFAULT_TRANSCRIPT_CLASS


def backfill_transcript_class(
    conn: sqlite3.Connection, *, apply: bool = False
) -> dict[str, Any]:
    """Deterministically backfill `transcript_class` for unclassified rows.

    Only rows where `transcript_class IS NULL` are touched (NULL == not yet
    classified; an already-set value is never re-derived/overwritten — fail-closed
    against clobbering a reviewer upgrade). Dry-run by default: pass ``apply=True``
    to write. Returns a byte-stable count summary (ordered by id) for evidence.
    """
    rows = conn.execute(
        "SELECT id, timestamped_text FROM transcripts "
        "WHERE transcript_class IS NULL ORDER BY id"
    ).fetchall()
    by_class: dict[str, int] = {}
    assignments: list[tuple[int, str]] = []
    for row in rows:
        cls = classify_transcript(row)
        if cls not in DETERMINISTIC_ASSIGNABLE:  # defensive; cannot happen today
            raise TranscriptClassError(
                f"classifier produced non-deterministic-assignable {cls!r}"
            )
        by_class[cls] = by_class.get(cls, 0) + 1
        assignments.append((row["id"], cls))

    if apply:
        conn.executemany(
            "UPDATE transcripts SET transcript_class = ? WHERE id = ?",
            [(cls, tid) for tid, cls in assignments],
        )
        conn.commit()

    return {
        "mode": "apply" if apply else "dry-run",
        "scanned": len(rows),
        "updated": len(assignments) if apply else 0,
        "by_class": dict(sorted(by_class.items())),
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic transcript_class backfill (GOV-275, Alpine-only)."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--apply", action="store_true",
        help="write the backfill (default is dry-run, the fail-closed posture)",
    )
    args = parser.parse_args(argv)

    apply_migrations(args.db)  # ensure 0018 is present before we touch the column
    with open_db(args.db) as conn:
        summary = backfill_transcript_class(conn, apply=args.apply)
    print(
        f"transcript_class backfill [{summary['mode']}]: "
        f"scanned={summary['scanned']} updated={summary['updated']} "
        f"by_class={summary['by_class']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
