"""GOV-125 real-data 1.07 structuring — checkpoint invariants (plan §5.2–§5.5, §6).

This is the CTO's "first checkpoint before bulk structuring" gate: it does NOT run
the full 124-folder corpus. It seeds a small, realistic sandbox (transcript
`documents` rows, exactly the shape GOV-124 writes) and asserts the contract
invariants the bulk run must uphold:

- §5.5 / §3  completeness-gap SSOT parity (Python frozenset == 0015 CHECK literal),
             fail-closed vocab, and the documents->transcripts bridge surfacing a
             first-class `missing_timestamps` gap for an untimed transcript with
             **no fabricated timestamp**.
- §2.2       a TIMED transcript materializes + segments with real timestamps;
             an UNTIMED transcript materializes + segments to ZERO rows (never an
             invented timestamp).
- §5.2       no-orphan invariant: a statement anchored to a complete evidence
             pointer (the untimed path) is accepted; one with neither segment nor
             pointer is rejected.
- §5.3       concept edges stay within the SSOT allowed types + acyclic at serve.
- §5.4       conservative attribution: a low-confidence speaker never binds a
             person and never writes a `made_statement` edge (no name > wrong name).

No network, no AI, no dependency on the real local corpus — pure sqlite + tmp files.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import completeness  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import segment_transcript as seg  # noqa: E402
import speakers as spk  # noqa: E402
import statements as st  # noqa: E402
import transcript_from_documents as bridge  # noqa: E402

MIGRATION_0015 = (
    Path(__file__).resolve().parent.parent
    / "Database" / "migrations" / "0015_completeness_gaps.sql"
)
SOURCE_ID = "alpine_local_corpus"

# A realistic timed transcript (MM:SS lines, fetch_transcripts convention).
TIMED_TEXT = (
    "Alpine Town Council Regular Meeting\n"
    "00:00 Mayor calls the meeting to order.\n"
    "00:12 Roll call of the council members.\n"
    "01:30 Discussion of the water system capital project.\n"
)
# A realistic UNTIMED transcript (prose, no MM:SS — the common real-.txt case).
UNTIMED_TEXT = (
    "Town of Alpine work session.\n"
    "The council discussed the proposed budget amendment at length.\n"
    "No formal vote was taken; the item was tabled to the next regular meeting.\n"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _seed_source(conn) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (SOURCE_ID, "Town of Alpine — local meeting corpus", "local_archive",
         "alpine-official", "alpine"),
    )


def _seed_transcript_document(conn, raw_root: Path, *, doc_id_hint: str, doc_date: str,
                              text: str, doc_type: str = "transcript_text") -> int:
    """Write a tmp raw-store file + a `documents` row exactly as GOV-124 would."""
    rel = f"Raw-Corpus/sample/{doc_id_hint}.txt"
    dest = raw_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    cur = conn.execute(
        "INSERT INTO documents (source_url, title, doc_type, doc_date, local_path, "
        "sha256, size_bytes, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f"file:///tmp/{doc_id_hint}.txt", f"{doc_id_hint}.txt", doc_type, doc_date,
         rel, "0" * 64, len(text.encode()), _now(), SOURCE_ID),
    )
    conn.commit()
    return int(cur.lastrowid)


def _migrated(tmp_path: Path):
    db_path = tmp_path / "gov125.db"
    db.apply_migrations(db_path)
    return db.open_db(db_path)


# --- §3 / §5.5 : completeness SSOT --------------------------------------------

def test_migration_0015_applies_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must not raise
    with db.open_db(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(completeness_gaps)")}
    assert {"gap_id", "subject_node_id", "subject_node_type", "gap_type",
            "severity", "resolved_status", "produced_by"} <= cols


def test_gap_type_ssot_matches_migration_check() -> None:
    """The Python SSOT frozenset must equal the 0015 `gap_type` CHECK literal."""
    sql = MIGRATION_0015.read_text(encoding="utf-8")
    block = re.search(r"gap_type\s+TEXT\s+NOT NULL\s+CHECK \(gap_type IN \((.*?)\)\)",
                      sql, re.DOTALL)
    assert block, "could not find gap_type CHECK literal in 0015"
    sql_types = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert sql_types == set(completeness.GAP_TYPES), (
        f"gap_type drift — SQL={sorted(sql_types)} SSOT={sorted(completeness.GAP_TYPES)}"
    )


def test_record_gap_is_fail_closed_and_idempotent(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        with pytest.raises(completeness.GapError):
            completeness.record_gap(conn, subject_node_id="x", subject_node_type="meeting",
                                    gap_type="not_a_real_gap")
        gid1 = completeness.record_gap(conn, subject_node_id="m1", subject_node_type="meeting",
                                       gap_type="no_primary_source")
        gid2 = completeness.record_gap(conn, subject_node_id="m1", subject_node_type="meeting",
                                       gap_type="no_primary_source")
        assert gid1 == gid2
        assert conn.execute("SELECT COUNT(*) FROM completeness_gaps").fetchone()[0] == 1


# --- §2.2 : documents->transcripts bridge, timed vs untimed --------------------

def test_bridge_timed_transcript_materializes_and_segments(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        _seed_source(conn)
        _seed_transcript_document(conn, tmp_path, doc_id_hint="timed",
                                  doc_date="2023-04-26", text=TIMED_TEXT)
        summary = bridge.materialize_transcripts(conn, raw_store_root=tmp_path)
        assert summary["materialized"] == 1
        assert summary["timed"] == 1 and summary["untimed"] == 0
        # No missing_timestamps gap for a timed transcript.
        assert completeness.gaps_for(conn, gap_type="missing_timestamps") == []
        # The unchanged segmenter consumes the materialized row -> real timestamps.
        tid = summary["items"][0]["transcript_id"]
        records = seg.segment_transcript(conn, tid, source_id=SOURCE_ID)
        assert len(records) == 3  # three MM:SS lines (header skipped)
        assert all(r["timestamp_seconds"] is not None for r in records)
        assert records[2]["timestamp_seconds"] == 90  # 01:30


def test_bridge_untimed_transcript_gaps_without_fabricating(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        _seed_source(conn)
        _seed_transcript_document(conn, tmp_path, doc_id_hint="untimed",
                                  doc_date="2023-05-10", text=UNTIMED_TEXT)
        summary = bridge.materialize_transcripts(conn, raw_store_root=tmp_path)
        assert summary["materialized"] == 1
        assert summary["untimed"] == 1 and summary["timed"] == 0
        # First-class missing_timestamps gap surfaced, against the transcript.
        gaps = completeness.gaps_for(conn, gap_type="missing_timestamps")
        assert len(gaps) == 1
        assert gaps[0]["subject_node_type"] == "transcript"
        # Never fabricated: the segmenter yields ZERO timed rows from untimed text.
        tid = summary["items"][0]["transcript_id"]
        assert seg.segment_transcript(conn, tid, source_id=SOURCE_ID) == []


def test_bridge_is_idempotent(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        _seed_source(conn)
        _seed_transcript_document(conn, tmp_path, doc_id_hint="untimed",
                                  doc_date="2023-05-10", text=UNTIMED_TEXT)
        bridge.materialize_transcripts(conn, raw_store_root=tmp_path)
        bridge.materialize_transcripts(conn, raw_store_root=tmp_path)  # re-run
        assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM completeness_gaps").fetchone()[0] == 1


# --- §5.2 : no orphan claims (untimed path anchors to a source pointer) --------

def _complete_pointer(**overrides) -> dict:
    pointer = {
        "to_source_id": SOURCE_ID,
        "relation": "references",
        "locator_kind": "section",
        "section": "Budget amendment discussion",
        "original_url": "file:///tmp/untimed.txt",
        "final_url": "file:///tmp/untimed.txt",
        "archive_url": None,
        "archive_status": "not_checked",
        "scan_date": "2023-05-10",
        "captured_at_utc": "2023-05-10T17:04:22Z",
        "is_verbatim": 1,
        "verification_status": "machine_extracted_unreviewed",
        "correction_status": "none",
        "confidence": "low",
    }
    pointer.update(overrides)
    return pointer


def test_untimed_statement_anchors_to_source_pointer_not_orphan(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        _seed_source(conn)
        # An untimed-meeting claim has no segment edge, so it MUST carry a complete
        # evidence pointer to the source (locator_kind=section, no timestamp).
        result = st.insert_statement(
            conn,
            {"statement_id": "alpine:2023-05-10:s1",
             "statement_text": "The budget amendment was tabled to the next meeting."},
            [_complete_pointer()],
        )
        assert result["publication_state"] == "not_publishable"  # fail-closed default


def test_orphan_claim_rejected(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        _seed_source(conn)
        with pytest.raises(st.OrphanClaimError):
            st.insert_statement(
                conn,
                {"statement_id": "orphan", "statement_text": "no segment, no pointer"},
                [],
            )


# --- §5.3 : concept edges within SSOT + acyclic at serve -----------------------

def test_concept_edges_allowed_types_and_acyclic(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        cm.insert_topic(conn, "topic:water", "Water system", "Water system",
                        jurisdiction_id="alpine")
        cm.insert_topic(conn, "topic:capital", "Capital projects", "Capital projects",
                        jurisdiction_id="alpine")
        cm.insert_edge(conn, "topic_rollup", "topic:water", "topic:capital")
        cm.assert_acyclic(conn)  # serve-time invariant holds
        # Out-of-vocab edge type is rejected.
        with pytest.raises(cm.EdgeError):
            cm.insert_edge(conn, "totally_made_up_edge", "topic:water", "topic:capital")
        # A cycle is rejected at insert.
        with pytest.raises(cm.TopicTreeCycleError):
            cm.insert_edge(conn, "topic_rollup", "topic:capital", "topic:water")


# --- §5.4 : conservative speaker attribution (no name > wrong name) ------------

def test_low_confidence_speaker_never_binds_a_name(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        _seed_source(conn)
        st.insert_statement(
            conn,
            {"statement_id": "alpine:2023-05-10:s2",
             "statement_text": "An attendee raised a concern about drainage."},
            [_complete_pointer(section="Public comment")],
        )
        spk.attribute_speaker(
            conn,
            {"speaker_attribution_id": "alpine:2023-05-10:s2:spk",
             "statement_id": "alpine:2023-05-10:s2",
             "attribution_state": "attributed",          # requested a name...
             "speaker_class": "unidentified",            # ...but speaker is unknown
             "confidence": "low"},
        )
        row = conn.execute(
            "SELECT attribution_state, person_id FROM speaker_attributions "
            "WHERE speaker_attribution_id = ?", ("alpine:2023-05-10:s2:spk",),
        ).fetchone()
        # Both `uncertain` and `unattributed` are NON-naming withheld states — the
        # guarantee that matters is that no name is shown and no person is bound.
        assert row["attribution_state"] in {"uncertain", "unattributed"}  # never `attributed`
        assert row["person_id"] is None                    # no person bind
        assert conn.execute("SELECT COUNT(*) FROM made_statement").fetchone()[0] == 0


def test_gap_report_surfaces_counts(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        completeness.record_gap(conn, subject_node_id="2023-04-26", subject_node_type="meeting",
                                gap_type="no_primary_source")
        completeness.record_gap(conn, subject_node_id="2023-05-10", subject_node_type="meeting",
                                gap_type="no_primary_source")
        report = completeness.gap_report(conn)
        assert report["total"] == 2
        assert report["by_type"]["no_primary_source"] == 2
