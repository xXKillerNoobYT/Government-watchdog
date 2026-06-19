"""Tests for the statement + evidence_link model (GOV-82, Slice 2 C).

Covers the Contract 1.07 §2/§1.4 acceptance criteria:
- migration 0007 (statements + evidence_links) is additive + idempotent;
- **orphan-claim rejection** — a statement with no segment edge AND no complete
  evidence_link pointer is rejected (1.07 §2.3);
- **pointer validity** — every evidence_link must carry a complete, valid pointer
  (required fields, locator matching locator_kind, resolving to_source_id);
- **default not-publishable** — every freshly inserted statement defaults
  publication_state=not_publishable and a gated (non-allowlisted) uiStatus;
- **enum reuse** — the 6-value verificationStatus enum, the publication
  allowlist, and compute_ui_status are imported from publication.py (same object,
  no shadow copy), and the SQL CHECK literals match the Python enum (no drift).

Note (GOV-89, Slice 3 B): the 0007 ``produced_by`` scope lock has been lifted —
``ai`` is now a permitted producer (widened in migration 0009 + the app-layer set
in lockstep, CTO D-1). The two tests below were updated from the slice-2
"no 'ai'" assertions to the slice-3 reality; the comprehensive AI-path coverage
lives in ``tests/test_ai_extraction.py``.

No AI, no network: pure sqlite + the committed Alpine fixture.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import statements as stmt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
SOURCE_ID = "alpine:video:2026-05-08-regular"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _check_clause(conn, table: str) -> str:
    """Return the CREATE TABLE SQL (carries the CHECK literals) for parity tests."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0]


def _seed_source(conn, source_id: str = SOURCE_ID) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "Alpine Council 2026-05-08 video", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _seed_segment(conn, *, source_id: str = SOURCE_ID, segment_id: str = "alpine-sample-0001:seg-0000") -> str:
    """Seed a transcript + one real segment via the GOV-81 segmenter."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], "2026-05-08", tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            "Transcripts/2026/alpine-sample-0001.json", "0" * 64, _now(), source_id,
        ),
    )
    tid = int(cur.lastrowid)
    seg.segment_transcript(conn, tid, source_id=source_id)
    return segment_id


def _good_pointer(**overrides) -> dict:
    pointer = {
        "to_source_id": SOURCE_ID,
        "relation": "references",
        "locator_kind": "timestamp",
        "timestamp_seconds": 2533,
        "timestamp_human": "00:42:13",
        "original_url": "https://example.gov/video",
        "final_url": "https://example.gov/video",
        "archive_url": "https://web.archive.org/web/x",
        "archive_status": "available",
        "scan_date": "2026-05-10",
        "captured_at_utc": "2026-05-10T17:04:22Z",
        "is_verbatim": 1,
        "verification_status": "machine_extracted_unreviewed",
        "correction_status": "none",
        "confidence": "high",
        "deep_link": "https://example.gov/video?t=2533",
    }
    pointer.update(overrides)
    return pointer


def _migrated(tmp_path: Path) -> Path:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    return db_path


# --- migration 0007: additive + idempotent ---------------------------------

def test_migration_creates_statement_and_evidence_tables(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        stmt_cols = _columns(conn, "statements")
        ev_cols = _columns(conn, "evidence_links")
    for required in (
        "statement_id", "segment_id", "agenda_item_id", "speaker_attribution_id",
        "statement_text", "is_verbatim", "layer", "produced_by", "verification_status",
        "correction_status", "review_state", "publication_state", "source_changed",
        "ui_status", "confidence", "updates_statement_id",
    ):
        assert required in stmt_cols, f"statements.{required} missing"
    for required in (
        "evidence_link_id", "from_node_id", "from_node_type", "to_source_id", "relation",
        "layer", "locator_kind", "timestamp_seconds", "timestamp_human", "page", "section",
        "paragraph", "original_url", "final_url", "archive_url", "archive_status",
        "scan_date", "captured_at_utc", "is_verbatim", "verification_status",
        "correction_status", "confidence", "transcript_path", "deep_link",
    ):
        assert required in ev_cols, f"evidence_links.{required} missing"


def test_migration_0007_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must not raise
    with db.open_db(db_path) as conn:
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        stmt_cols = [r[1] for r in conn.execute("PRAGMA table_info(statements)")]
    assert "0007_statements_evidence" in ledger
    assert stmt_cols.count("statement_id") == 1


# --- enum reuse (no re-typed enums) ----------------------------------------

def test_six_value_enum_is_the_same_object_not_a_copy() -> None:
    # The validator must reuse the SSOT enum, not shadow it.
    assert stmt.pub.ALLOWED_VERIFICATION_STATUSES is pub.ALLOWED_VERIFICATION_STATUSES
    assert stmt.pub.compute_ui_status is pub.compute_ui_status


def test_produced_by_matches_ssot_and_includes_ai() -> None:
    # Slice 3 B (GOV-89) widened the app-layer set to the full SSOT producer set.
    assert stmt.ALLOWED_STATEMENT_PRODUCED_BY == pub.ALLOWED_PRODUCED_BY
    assert "ai" in stmt.ALLOWED_STATEMENT_PRODUCED_BY
    assert stmt.ALLOWED_STATEMENT_PRODUCED_BY == {"automation", "ai", "human"}


def test_sql_check_matches_python_verification_enum(tmp_path: Path) -> None:
    # The migration's CHECK literals must equal the authoritative 6-value enum —
    # a drift guard so the SQL constraint and publication.py cannot diverge.
    with db.open_db(_migrated(tmp_path)) as conn:
        for table in ("statements", "evidence_links"):
            sql = _check_clause(conn, table)
            for value in pub.ALLOWED_VERIFICATION_STATUSES:
                assert f"'{value}'" in sql, f"{table} CHECK missing {value!r}"


def test_relation_enum_matches_contract() -> None:
    assert stmt.ALLOWED_EVIDENCE_RELATIONS == {
        "references", "supports", "contradicts", "corrects", "substantiates"
    }


# --- pointer validity (1.07 §2.2/§2.3) -------------------------------------

def test_complete_pointer_passes(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        stmt.validate_pointer(_good_pointer(), conn=conn)  # must not raise


@pytest.mark.parametrize("missing", [
    "to_source_id", "relation", "original_url", "archive_status",
    "scan_date", "captured_at_utc", "locator_kind", "verification_status", "confidence",
])
def test_pointer_missing_required_field_rejected(tmp_path: Path, missing: str) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        bad = _good_pointer()
        bad[missing] = None
        with pytest.raises(stmt.PointerError):
            stmt.validate_pointer(bad, conn=conn)


def test_pointer_timestamp_locator_without_seconds_rejected(tmp_path: Path) -> None:
    # §2.3: locator_kind set but the matching locator field absent -> reject.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        bad = _good_pointer(locator_kind="timestamp", timestamp_seconds=None)
        with pytest.raises(stmt.PointerError, match="timestamp_seconds"):
            stmt.validate_pointer(bad, conn=conn)


def test_pointer_page_locator_requires_page(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        ok = _good_pointer(locator_kind="page", page=12, timestamp_seconds=None, timestamp_human=None)
        stmt.validate_pointer(ok, conn=conn)  # page present -> ok
        bad = _good_pointer(locator_kind="page", page=None, timestamp_seconds=None)
        with pytest.raises(stmt.PointerError, match="page"):
            stmt.validate_pointer(bad, conn=conn)


def test_pointer_unresolvable_source_rejected(tmp_path: Path) -> None:
    # §2.2: a pointer whose source_id does not resolve to a registry row is rejected.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        bad = _good_pointer(to_source_id="alpine:video:does-not-exist")
        with pytest.raises(stmt.PointerError, match="does not resolve"):
            stmt.validate_pointer(bad, conn=conn)


def test_pointer_bad_relation_rejected(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        with pytest.raises(stmt.PointerError, match="relation"):
            stmt.validate_pointer(_good_pointer(relation="implies"), conn=conn)


# --- orphan-claim rejection (1.07 §2.3) ------------------------------------

def test_statement_with_segment_edge_is_not_orphan(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        result = stmt.insert_statement(
            conn,
            {"statement_id": "alpine:2026-05-08:stmt-0001",
             "segment_id": seg_id,
             "statement_text": "The financing gap is $X."},
        )
        row = conn.execute(
            "SELECT segment_id, publication_state FROM statements WHERE statement_id = ?",
            ("alpine:2026-05-08:stmt-0001",),
        ).fetchone()
    assert row["segment_id"] == seg_id
    assert result["ui_status"] in pub.ALLOWED_UI_STATUSES


def test_statement_with_evidence_pointer_only_is_not_orphan(tmp_path: Path) -> None:
    # A non-transcript statement (no segment) anchored by a complete pointer.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        stmt.insert_statement(
            conn,
            {"statement_id": "alpine:2026-05-08:stmt-doc-1",
             "statement_text": "Ordinance 2026-04 sets the rate."},
            [_good_pointer(relation="references", locator_kind="page", page=3,
                           timestamp_seconds=None, timestamp_human=None)],
        )
        n_links = conn.execute(
            "SELECT COUNT(*) FROM evidence_links WHERE from_node_id = ?",
            ("alpine:2026-05-08:stmt-doc-1",),
        ).fetchone()[0]
    assert n_links == 1


def test_orphan_statement_rejected(tmp_path: Path) -> None:
    # No segment edge AND no evidence_link -> orphan -> rejected, nothing written.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        with pytest.raises(stmt.OrphanClaimError):
            stmt.insert_statement(
                conn,
                {"statement_id": "alpine:2026-05-08:orphan",
                 "statement_text": "Unsourced claim."},
            )
        count = conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
    assert count == 0  # rejected before any write


def test_dangling_segment_id_is_orphan(tmp_path: Path) -> None:
    # A segment_id that does not resolve does not satisfy the edge requirement.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        with pytest.raises(stmt.OrphanClaimError):
            stmt.insert_statement(
                conn,
                {"statement_id": "alpine:2026-05-08:dangling",
                 "segment_id": "no-such-segment:seg-9999",
                 "statement_text": "Points at nothing."},
            )


def test_statement_with_invalid_pointer_rejected_before_write(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        with pytest.raises(stmt.PointerError):
            stmt.insert_statement(
                conn,
                {"statement_id": "alpine:2026-05-08:badptr",
                 "statement_text": "Has an incomplete pointer."},
                [_good_pointer(timestamp_seconds=None)],  # timestamp locator, no seconds
            )
        assert conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 0


# --- default not-publishable (fail-closed) ---------------------------------

def test_new_statement_defaults_not_publishable(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        stmt.insert_statement(
            conn,
            {"statement_id": "alpine:2026-05-08:stmt-pub",
             "segment_id": seg_id,
             "statement_text": "Default posture check."},
        )
        row = conn.execute(
            "SELECT produced_by, verification_status, review_state, publication_state, ui_status "
            "FROM statements WHERE statement_id = ?",
            ("alpine:2026-05-08:stmt-pub",),
        ).fetchone()
    assert row["publication_state"] == pub.DEFAULT_PUBLICATION_STATE == "not_publishable"
    assert row["produced_by"] == "automation"
    assert row["verification_status"] == "machine_extracted_unreviewed"
    assert row["review_state"] == "unreviewed"
    # the computed uiStatus is gated — NOT on the publication allowlist.
    assert row["ui_status"] not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES
    assert row["ui_status"] == "unverified"


def test_ai_produced_by_accepted_when_anchored(tmp_path: Path) -> None:
    # Slice 3 B: 'ai' is now a permitted producer. It still enters fail-closed
    # (machine_extracted_unreviewed / not_publishable) and still obeys no-orphan.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        # GOV-278: an AI row must name an `ok` gateway run (write-time binding).
        ai.create_run(conn, run_id="alpine:ai:run", input_source_ids=[SOURCE_ID])
        result = stmt.insert_statement(
            conn,
            {"statement_id": "alpine:2026-05-08:ai",
             "segment_id": seg_id,
             "statement_text": "AI paraphrase anchored to a real segment.",
             "is_verbatim": 0,
             "layer": "ai_thought_then",
             "produced_by": "ai",
             "ai_extraction_run_id": "alpine:ai:run"},
        )
        row = conn.execute(
            "SELECT produced_by, verification_status, publication_state "
            "FROM statements WHERE statement_id = ?",
            ("alpine:2026-05-08:ai",),
        ).fetchone()
    assert result["ui_status"] in pub.ALLOWED_UI_STATUSES
    assert row["produced_by"] == "ai"
    assert row["verification_status"] == "machine_extracted_unreviewed"
    assert row["publication_state"] == "not_publishable"


def test_publication_state_check_rejects_bad_value(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO statements (statement_id, statement_text, publication_state) "
                "VALUES ('x', 'y', 'definitely_publish_it')"
            )


def test_evidence_link_relation_check_rejects_bad_value(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO evidence_links (evidence_link_id, from_node_id, to_source_id, "
                "relation, locator_kind) VALUES ('e', 's', ?, 'implies', 'timestamp')",
                (SOURCE_ID,),
            )


# --- FK integrity across the spine -----------------------------------------

def test_statement_and_evidence_fk_integrity(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        stmt.insert_statement(
            conn,
            {"statement_id": "alpine:2026-05-08:stmt-fk",
             "segment_id": seg_id,
             "statement_text": "FK check."},
            [_good_pointer()],
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        joined = conn.execute(
            "SELECT st.statement_id, ts.segment_text, ev.relation, s.source_class "
            "FROM statements st "
            "JOIN transcript_segments ts ON ts.segment_id = st.segment_id "
            "JOIN evidence_links ev ON ev.from_node_id = st.statement_id "
            "JOIN sources s ON s.source_id = ev.to_source_id "
            "WHERE st.statement_id = ?",
            ("alpine:2026-05-08:stmt-fk",),
        ).fetchall()
    assert violations == []
    assert len(joined) == 1
    assert joined[0]["source_class"] == "alpine-official"
    assert joined[0]["relation"] == "references"
