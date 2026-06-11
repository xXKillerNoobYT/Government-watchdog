"""Deterministic 1.07 structuring over the REAL Alpine corpus (GOV-125, Option B).

Chain item #2 of 4 (CEO sequencing on GOV-123), goal `927f07dc`. CTO ruling:
**Option B** (ADR in plan §2 rev 2). This is the RUN of the existing, proven 1.07
pipeline over the real GOV-124 corpus — NOT a re-crawl, NOT AI, NOT a fixture.

What it does, oldest→newest, reusing the proven primitives unchanged:
  §4.0  ensure the GOV-124 ingest has populated `documents` + `sources` (idempotent).
  spine  one `meetings` row per real meeting folder (124; `mayor-investigation/`
         is already excluded by manifest_local_corpus — non-date-named).
  §4.1  materialize a `transcripts` row per transcript document (the GOV-125 bridge,
         already-preserved bytes only); for each TIMED transcript, deterministically
         segment it (`segment_transcript`) and create one verbatim statement per
         segment — segment-anchored (`statement_from_segment`) AND carrying a
         complete §2 timestamp evidence pointer to the corpus source (no orphan).
  §4.2  completeness gaps, first-class + surfaced, never papered over:
         - meeting folder with no source-of-record file  -> `no_primary_source`
         - folder with a primary source but no transcript -> `missing_transcript`
         - each PDF document (no deterministic extractor exists) -> `pdf_text_unextracted`
         - untimed transcript -> `missing_timestamps` (emitted by the bridge)
  §5.3  `concept_map.assert_acyclic` at the end (serve-time invariant). The
         deterministic pass creates no generic `concept_edges` (topic/thread
         inference is the gated AI pass GOV-126), so the concept graph is the
         typed relational spine; acyclicity holds trivially and is asserted.

Conservative speaker attribution (Option B / §5.4): the deterministic pass has NO
speaker-identification source (no diarization, no AI), so it binds **zero** names
— the maximally conservative outcome ("no name beats a wrong name"). Speaker
identity is deferred to the gated AI pass. Reported as 0 bound / 0 attributions.

Boundary: Alpine-only, reviewer-internal/vault-only, NO network, NO AI; never
flips `publication_state`; statements default `not_publishable`. Raw bytes + the
DB stay gitignored.

Usage:
    python scripts/structure_real_corpus.py --report
    python scripts/structure_real_corpus.py --source-dir /path --db Database/gov.db --report
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import completeness  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import ingest_local_corpus as ingest  # noqa: E402
import manifest_local_corpus as mlc  # noqa: E402
import segment_transcript as seg  # noqa: E402
import statements as stmt  # noqa: E402
import transcript_from_documents as bridge  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = ingest.DEFAULT_CORPUS
CORPUS_SOURCE_ID = ingest.CORPUS_SOURCE_ID  # 'alpine_local_corpus'
# A jurisdiction-level body that does not fabricate a specific government body
# (Council vs Planning Commission) we cannot determine without agenda parsing.
DEFAULT_BODY = "Town of Alpine"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ensure_meeting(conn: sqlite3.Connection, meeting_date: str, *,
                    body: str = DEFAULT_BODY) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO meetings (meeting_date, body, title, fetch_time_utc) "
        "VALUES (?, ?, ?, ?)",
        (meeting_date, body, None, _now()),
    )
    row = conn.execute(
        "SELECT id FROM meetings WHERE meeting_date = ? AND body = ?",
        (meeting_date, body),
    ).fetchone()
    return int(row[0])


def _link_transcript_to_meeting(conn: sqlite3.Connection, transcript_id: int,
                                meeting_date: str) -> int | None:
    """Link the meeting's `transcript_id` (first transcript per date wins). Returns
    the meeting id so segments resolve to the meeting spine."""
    meeting_id = _ensure_meeting(conn, meeting_date)
    existing = conn.execute(
        "SELECT transcript_id FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if existing is not None and existing[0] is None:
        conn.execute(
            "UPDATE meetings SET transcript_id = ? WHERE id = ?",
            (transcript_id, meeting_id),
        )
    return meeting_id


def _timestamp_pointer(conn: sqlite3.Connection, transcript_id: int,
                       segment: dict) -> dict:
    """A complete §2 exact-source pointer into the meeting recording at a timestamp.

    `to_source_id` is the transcript's registry source (the corpus source row),
    so the pointer resolves; the locator is the segment's REAL timestamp (never
    fabricated — only timed segments reach here)."""
    tr = conn.execute(
        "SELECT video_url, source_id, local_path FROM transcripts WHERE id = ?",
        (transcript_id,),
    ).fetchone()
    return {
        "to_source_id": tr["source_id"] or CORPUS_SOURCE_ID,
        "relation": "substantiates",
        "locator_kind": "timestamp",
        "timestamp_seconds": segment["timestamp_seconds"],
        "timestamp_human": segment["timestamp_human"],
        "original_url": tr["video_url"],          # file:// provenance (vault-only)
        "archive_status": "not_checked",
        "scan_date": segment.get("scan_date") or _now()[:10],
        "captured_at_utc": _now(),
        "verification_status": "machine_extracted_unreviewed",
        "confidence": "medium",
        "transcript_path": tr["local_path"],       # vault-only provenance
    }


def _statements_for_timed_transcript(conn: sqlite3.Connection, transcript_id: int,
                                     segments: list[dict]) -> int:
    """One verbatim statement per timestamped segment, segment-anchored + with a
    complete timestamp pointer (non-orphan via BOTH disjuncts). Commits once."""
    created = 0
    for segment in segments:
        statement = {
            "statement_id": f"stmt:{segment['segment_id']}",
            "segment_id": segment["segment_id"],
            "statement_text": segment["segment_text"],  # verbatim, never paraphrased
            "is_verbatim": 1,
            "produced_by": "automation",
            "layer": "known_then",
        }
        stmt.insert_statement(
            conn, statement, [_timestamp_pointer(conn, transcript_id, segment)],
            commit=False,
        )
        created += 1
    conn.commit()
    return created


def structure(corpus_root: Path, db_path: Path, *, skip_ingest: bool = False) -> dict:
    """Run the full deterministic structuring pass. Returns a summary dict."""
    corpus_root = Path(corpus_root).resolve()

    # §4.0 — ensure documents + sources exist (idempotent GOV-124 ingest).
    detected_run_id = None
    if not skip_ingest:
        isum = ingest.ingest(corpus_root, db_path, dry_run=False)
        detected_run_id = isum.get("run_id")
    else:
        db.apply_migrations(db_path)

    folders = mlc.iter_meeting_folders(corpus_root)              # 124, oldest→newest
    selection = mlc.iter_source_of_record_files(corpus_root)
    folders_with_primary = {
        sf.meeting_date for sf in selection if sf.origin == "meeting_folder"
    }

    with db.open_db(db_path) as conn:
        # spine: one meeting per real folder (accounts for ALL 124).
        for date_str, _ in folders:
            _ensure_meeting(conn, date_str)
        conn.commit()

        # §4.1 — materialize transcripts from transcript documents (the bridge).
        bsum = bridge.materialize_transcripts(
            conn, raw_store_root=REPO_ROOT, detected_run_id=detected_run_id
        )

        # link + segment + statements for each materialized transcript.
        total_segments = 0
        total_statements = 0
        meetings_with_transcript: set[str] = set()
        for item in bsum["items"]:
            tid = item.get("transcript_id")
            if tid is None:
                continue
            _link_transcript_to_meeting(conn, tid, item["doc_date"])
            meetings_with_transcript.add(item["doc_date"])
            if item["timed"]:
                segments = seg.segment_transcript(conn, tid, source_id=CORPUS_SOURCE_ID)
                total_segments += len(segments)
                total_statements += _statements_for_timed_transcript(conn, tid, segments)
            # untimed: bridge already recorded missing_timestamps; no segments/statements.

        # §4.2 — coverage gaps (first-class, surfaced). detail anchors on ids/dates,
        # never raw titles (B3): a meeting date / doc id is not human-name PII.
        for date_str, _ in folders:
            if date_str not in folders_with_primary:
                completeness.record_gap(
                    conn, subject_node_id=date_str, subject_node_type="meeting",
                    gap_type="no_primary_source", source_id=CORPUS_SOURCE_ID,
                    detected_run_id=detected_run_id,
                    detail=f"meeting folder {date_str} has only derived (.md) material; "
                           "no source-of-record primary document",
                    commit=False,
                )
            elif date_str not in meetings_with_transcript:
                completeness.record_gap(
                    conn, subject_node_id=date_str, subject_node_type="meeting",
                    gap_type="missing_transcript", source_id=CORPUS_SOURCE_ID,
                    detected_run_id=detected_run_id,
                    detail=f"meeting folder {date_str} has a primary source but no "
                           "transcript document to structure",
                    commit=False,
                )
        # each PDF document: no deterministic text extractor exists (plan §4.2).
        pdf_docs = conn.execute(
            "SELECT id, doc_date FROM documents WHERE LOWER(source_url) LIKE '%.pdf'"
        ).fetchall()
        for doc in pdf_docs:
            completeness.record_gap(
                conn, subject_node_id=str(doc["id"]), subject_node_type="document",
                gap_type="pdf_text_unextracted", source_id=CORPUS_SOURCE_ID,
                detected_run_id=detected_run_id,
                detail=f"document id={doc['id']} ({doc['doc_date']}) is a PDF; no "
                       "deterministic text extractor in scope — text left unextracted",
                commit=False,
            )
        conn.commit()

        # §5.3 — serve-time acyclicity invariant (no generic edges created here).
        cm.assert_acyclic(conn)

        summary = _collect_counts(conn)
        summary.update({
            "meeting_folders": len(folders),
            "folders_with_primary": len(folders_with_primary),
            "transcripts_materialized": bsum["materialized"],
            "transcripts_timed": bsum["timed"],
            "transcripts_untimed": bsum["untimed"],
            "segments_created": total_segments,
            "statements_created": total_statements,
            "run_id": detected_run_id,
        })
    return summary


def _collect_counts(conn: sqlite3.Connection) -> dict:
    def n(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0])
    return {
        "rows": {
            "meetings": n("SELECT COUNT(*) FROM meetings"),
            "transcripts": n("SELECT COUNT(*) FROM transcripts"),
            "transcript_segments": n("SELECT COUNT(*) FROM transcript_segments"),
            "statements": n("SELECT COUNT(*) FROM statements"),
            "evidence_links": n("SELECT COUNT(*) FROM evidence_links"),
            "agenda_items": n("SELECT COUNT(*) FROM agenda_items"),
            "concept_edges": n("SELECT COUNT(*) FROM concept_edges"),
            "speaker_attributions": n("SELECT COUNT(*) FROM speaker_attributions"),
            "made_statement_edges": n("SELECT COUNT(*) FROM made_statement"),
            "completeness_gaps": n("SELECT COUNT(*) FROM completeness_gaps"),
        },
        "no_orphan_statements": n(
            "SELECT COUNT(*) FROM statements s WHERE s.segment_id IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM evidence_links e WHERE e.from_node_id = s.statement_id "
            "AND e.from_node_type = 'statement')"
        ),  # MUST be 0 — every statement resolves to a segment edge or a pointer
        "statements_not_publishable": n(
            "SELECT COUNT(*) FROM statements WHERE publication_state = 'not_publishable'"
        ),
        "gap_report": completeness.gap_report(conn),
    }


def _sample_subgraph(db_path: Path) -> dict | None:
    """A sample real concept-map subgraph for one meeting (evidence §5.6).

    Prefers a meeting that produced timestamped segments+statements; if none exist
    (the real Alpine reality — untimed transcripts), falls back to the real typed
    spine that DOES exist: jurisdiction → body → meeting → its documents + its
    first-class completeness gaps. Either way the subgraph is real and typed."""
    with db.open_db(db_path) as conn:
        row = conn.execute(
            "SELECT m.id, m.meeting_date, m.body, m.transcript_id FROM meetings m "
            "JOIN transcript_segments s ON s.meeting_id = m.id LIMIT 1"
        ).fetchone()
        if row is None:
            # Fallback: a meeting with documents + gaps (the statement-less spine).
            mrow = conn.execute(
                "SELECT id, meeting_date, body, transcript_id FROM meetings "
                "ORDER BY meeting_date LIMIT 1"
            ).fetchone()
            if mrow is None:
                return None
            docs = conn.execute(
                "SELECT id, doc_type FROM documents WHERE doc_date = ? ORDER BY id",
                (mrow["meeting_date"],),
            ).fetchall()
            gaps = conn.execute(
                "SELECT gap_type, severity FROM completeness_gaps "
                "WHERE subject_node_id = ? ORDER BY gap_type",
                (mrow["meeting_date"],),
            ).fetchall()
            return {
                "jurisdiction": "Alpine",
                "government_body": mrow["body"],
                "meeting": {"id": mrow["id"], "date": mrow["meeting_date"]},
                "transcript_id": mrow["transcript_id"],
                "segments_in_meeting": 0,
                "sample_statement": None,
                "documents": [dict(d) for d in docs],
                "gaps": [dict(g) for g in gaps],
                "typed_edges": [
                    "jurisdiction -[held_meeting(body)]-> meeting",
                    "meeting -[references_source]-> document",
                    "meeting -[has_completeness_gap]-> completeness_gap (first-class)",
                ],
            }
        seg_count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE meeting_id = ?", (row["id"],)
        ).fetchone()[0]
        stmt_row = conn.execute(
            "SELECT s.statement_id, s.segment_id, e.to_source_id, e.locator_kind, "
            "e.timestamp_human FROM statements s JOIN evidence_links e "
            "ON e.from_node_id = s.statement_id JOIN transcript_segments t "
            "ON t.segment_id = s.segment_id WHERE t.meeting_id = ? LIMIT 1",
            (row["id"],),
        ).fetchone()
        return {
            "jurisdiction": "Alpine",
            "government_body": row["body"],
            "meeting": {"id": row["id"], "date": row["meeting_date"]},
            "transcript_id": row["transcript_id"],
            "segments_in_meeting": seg_count,
            "sample_statement": dict(stmt_row) if stmt_row else None,
            "typed_edges": [
                "jurisdiction -[held_meeting(body)]-> meeting",
                "meeting -[has_transcript]-> transcript",
                "transcript -[transcript_segment]-> segment",
                "statement -[statement_from_segment]-> segment",
                "statement -[evidence_link:substantiates]-> source_record",
            ],
        }


def render_report(summary: dict, subgraph: dict | None) -> str:
    r = summary["rows"]
    g = summary["gap_report"]
    lines = ["# GOV-125 real-data 1.07 structuring — run report (Option B)", ""]
    lines.append("## §5.1 row counts (REAL Alpine data)")
    for k, v in r.items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## §5.2 no-orphan invariant")
    lines.append(f"- orphan statements (MUST be 0): **{summary['no_orphan_statements']}**")
    lines.append(f"- statements default not_publishable: {summary['statements_not_publishable']}/{r['statements']}")
    lines.append("")
    lines.append("## coverage (oldest→newest)")
    lines.append(
        f"- meeting folders: {summary['meeting_folders']} · with primary source: "
        f"{summary['folders_with_primary']} · transcripts materialized: "
        f"{summary['transcripts_materialized']} (timed {summary['transcripts_timed']} / "
        f"untimed {summary['transcripts_untimed']})"
    )
    lines.append(f"- segments: {summary['segments_created']} · statements: {summary['statements_created']}")
    lines.append("")
    lines.append("## §5.4 conservative speaker attribution")
    lines.append(
        f"- speaker_attributions: {r['speaker_attributions']} · names bound "
        f"(made_statement edges): {r['made_statement_edges']} — deterministic pass "
        "binds ZERO names (no speaker-id source); identity deferred to gated AI pass"
    )
    lines.append("")
    lines.append("## §5.5 completeness-gap report (first-class, surfaced)")
    lines.append(f"- total gaps: {g['total']}")
    for t, c in g["by_type"].items():
        lines.append(f"  - {t}: {c}")
    lines.append(f"- by severity: {g['by_severity']}")
    lines.append("")
    lines.append("## §5.6 sample real concept-map subgraph")
    if subgraph:
        lines.append(f"- jurisdiction: {subgraph['jurisdiction']} → body: {subgraph['government_body']} "
                     f"→ meeting {subgraph['meeting']['date']} (id {subgraph['meeting']['id']})")
        lines.append(f"- segments in meeting: {subgraph['segments_in_meeting']}")
        if subgraph["sample_statement"]:
            ss = subgraph["sample_statement"]
            lines.append(f"- sample statement {ss['statement_id']} → segment {ss['segment_id']} "
                         f"→ evidence({ss['locator_kind']} {ss.get('timestamp_human')}) → source {ss['to_source_id']}")
        if subgraph.get("documents"):
            lines.append(f"- documents on this meeting: "
                         + ", ".join(f"{d['doc_type']}#{d['id']}" for d in subgraph["documents"]))
        if subgraph.get("gaps"):
            lines.append(f"- first-class gaps on this meeting: "
                         + ", ".join(f"{g['gap_type']}({g['severity']})" for g in subgraph["gaps"]))
        for e in subgraph["typed_edges"]:
            lines.append(f"  - {e}")
    else:
        lines.append("- (no timed meeting with segments — all transcripts untimed or absent)")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOV-125 deterministic real-corpus structuring.")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--skip-ingest", action="store_true",
                        help="assume documents/sources already ingested")
    parser.add_argument("--report", action="store_true", help="print the full markdown report")
    args = parser.parse_args(argv)

    summary = structure(args.source_dir, args.db, skip_ingest=args.skip_ingest)
    subgraph = _sample_subgraph(args.db)
    if args.report:
        print(render_report(summary, subgraph))
    else:
        r = summary["rows"]
        print(f"meetings={r['meetings']} segments={r['transcript_segments']} "
              f"statements={r['statements']} evidence={r['evidence_links']} "
              f"gaps={r['completeness_gaps']} orphans={summary['no_orphan_statements']}")
    return 1 if summary["no_orphan_statements"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
