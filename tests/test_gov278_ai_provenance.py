"""GOV-278 Stage 2.05 — `produced_by='ai'` write-time provenance binding.

Successor half of the Stage 2.05 slice (GOV-275 landed the deterministic
`transcript_class` half). Asserts the AI-provenance bar from GOV-233 §2.05 /
GOV-230:

- **write-time rejection (fail-closed)** — `statements.insert_statement` rejects a
  `produced_by='ai'` row unless it names a non-NULL `ai_extraction_runs.run_id`
  that resolves to a ledger row with `error_status='ok'`. NULL, unresolved,
  `failed`, and `partial` runs are all rejected, and nothing is written;
- **non-AI rows are unaffected** — `automation`/`human` rows need no run binding;
- **no-orphan provenance integrity** — `ai_provenance.audit_ai_provenance` finds
  zero orphans on a DB built only through the guarded writer, and DOES flag
  orphans created by a raw-SQL bypass (the exact gap the write-time gate closes);
- **rule is not re-invented** — the producer enum is the SSOT
  `publication.ALLOWED_PRODUCED_BY`; the binding keys on `error_status='ok'`;
- **additive / vault-only** — `ai_extraction_run_id` is not web-safe (allowlist
  unchanged); no Stage-1 column/CHECK change is required by this slice.

No network, no AI model, no real corpus — pure sqlite + the committed fixture.
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
import ai_provenance as prov  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import statements as stmt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
SOURCE_ID = "alpine:video:2026-05-08-regular"
SEGMENT_ID = "alpine-sample-0001:seg-0000"
OK_RUN = "alpine:ai-extract:2026-06-19:gov278"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _migrated(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "gov278.db"
    db.apply_migrations(db_path)
    return db.open_db(db_path)


def _seed_source(conn, source_id: str = SOURCE_ID) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "Alpine Council 2026-05-08 video", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _seed_segment(conn, *, source_id: str = SOURCE_ID) -> str:
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
    seg.segment_transcript(conn, int(cur.lastrowid), source_id=source_id)
    return SEGMENT_ID


def _base_conn(tmp_path: Path, *, run_status: str | None = "ok") -> sqlite3.Connection:
    """Migrated DB with a source, a real segment, and (optionally) one run."""
    conn = _migrated(tmp_path)
    _seed_source(conn)
    _seed_segment(conn)
    if run_status is not None:
        ai.create_run(conn, run_id=OK_RUN, input_source_ids=[SOURCE_ID])
        if run_status != "ok":
            ai.finalize_run(
                conn, OK_RUN, output_statement_ids=[], output_evidence_link_ids=[],
                orphan_rejected_count=0, error_status=run_status,
            )
    return conn


def _ai_statement(statement_id: str = "alpine:ai:stmt-0278", **over) -> dict:
    s = {
        "statement_id": statement_id,
        "segment_id": SEGMENT_ID,  # resolving edge -> not an orphan *claim*
        "statement_text": "The council approved the water system capital project.",
        "produced_by": "ai",
        "ai_extraction_run_id": OK_RUN,
    }
    s.update(over)
    return s


# --- write-time accept path ---------------------------------------------------

def test_ai_row_with_ok_run_is_written(tmp_path: Path) -> None:
    with _base_conn(tmp_path) as conn:
        result = stmt.insert_statement(conn, _ai_statement())
        row = conn.execute(
            "SELECT produced_by, ai_extraction_run_id FROM statements "
            "WHERE statement_id=?", (result["statement_id"],)
        ).fetchone()
    assert row["produced_by"] == "ai"
    assert row["ai_extraction_run_id"] == OK_RUN


def test_non_ai_rows_need_no_run_binding(tmp_path: Path) -> None:
    """automation/human rows must still write with a NULL run id (unaffected)."""
    with _base_conn(tmp_path, run_status=None) as conn:
        for producer in ("automation", "human"):
            res = stmt.insert_statement(
                conn,
                _ai_statement(
                    statement_id=f"alpine:{producer}:stmt",
                    produced_by=producer,
                    ai_extraction_run_id=None,
                ),
            )
            run_id = conn.execute(
                "SELECT ai_extraction_run_id FROM statements WHERE statement_id=?",
                (res["statement_id"],),
            ).fetchone()[0]
            assert run_id is None


# --- write-time reject path (fail-closed) -------------------------------------

@pytest.mark.parametrize(
    "run_id_value, run_status, label",
    [
        (None, "ok", "null-run"),
        ("", "ok", "blank-run"),
        ("alpine:ai:does-not-exist", "ok", "unresolved-run"),
        (OK_RUN, "failed", "failed-run"),
        (OK_RUN, "partial", "partial-run"),
    ],
)
def test_ai_row_without_valid_ok_run_is_rejected(
    tmp_path: Path, run_id_value, run_status, label
) -> None:
    with _base_conn(tmp_path, run_status=run_status) as conn:
        before = conn.execute("SELECT count(*) FROM statements").fetchone()[0]
        with pytest.raises(stmt.AiProvenanceError):
            stmt.insert_statement(
                conn, _ai_statement(ai_extraction_run_id=run_id_value)
            )
        after = conn.execute("SELECT count(*) FROM statements").fetchone()[0]
    assert before == after == 0, f"{label}: a rejected AI write must persist nothing"


def test_rejection_happens_before_any_evidence_link_write(tmp_path: Path) -> None:
    """Fail-closed: an invalid AI row writes neither the statement nor its links."""
    with _base_conn(tmp_path, run_status="failed") as conn:
        link = {
            "to_source_id": SOURCE_ID,
            "relation": "references",
            "locator_kind": "timestamp",
            "timestamp_seconds": 12,
            "timestamp_human": "00:00:12",
            "original_url": "https://example.gov/v",
            "archive_status": "available",
            "scan_date": "2026-05-10",
            "captured_at_utc": "2026-05-10T17:04:22Z",
            "verification_status": "machine_extracted_unreviewed",
            "confidence": "high",
        }
        with pytest.raises(stmt.AiProvenanceError):
            stmt.insert_statement(conn, _ai_statement(), [link])
        assert conn.execute("SELECT count(*) FROM statements").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM evidence_links").fetchone()[0] == 0


# --- resolve_ok_run unit ------------------------------------------------------

def test_resolve_ok_run_truth_table(tmp_path: Path) -> None:
    with _base_conn(tmp_path) as conn:
        assert stmt.resolve_ok_run(conn, OK_RUN) is True
        assert stmt.resolve_ok_run(conn, None) is False
        assert stmt.resolve_ok_run(conn, "") is False
        assert stmt.resolve_ok_run(conn, "nope") is False
        ai.finalize_run(
            conn, OK_RUN, output_statement_ids=[], output_evidence_link_ids=[],
            orphan_rejected_count=1, error_status="partial",
        )
        assert stmt.resolve_ok_run(conn, OK_RUN) is False  # no longer 'ok'


# --- no-orphan provenance integrity audit -------------------------------------

def test_audit_clean_on_guarded_writes(tmp_path: Path) -> None:
    """A DB built only through insert_statement has zero provenance orphans."""
    with _base_conn(tmp_path) as conn:
        stmt.insert_statement(conn, _ai_statement("alpine:ai:stmt-a"))
        stmt.insert_statement(conn, _ai_statement("alpine:ai:stmt-b"))
        report = prov.audit_ai_provenance(conn)
    assert report["ai_statement_count"] == 2
    assert report["orphan_count"] == 0
    assert report["clean"] is True


def test_audit_detects_raw_sql_bypass_orphans(tmp_path: Path) -> None:
    """The exact gap the write-time gate closes: a raw INSERT can plant an orphan
    AI row (NULL run id) or a dangling run id; the auditor catches both."""
    db_path = tmp_path / "gov278.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    _seed_source(conn)
    _seed_segment(conn)
    ai.create_run(conn, run_id=OK_RUN, input_source_ids=[SOURCE_ID])
    # NULL run id: the FK always permits NULL, so only the app-layer write gate
    # stands here — a raw INSERT plants it.
    conn.execute(
        "INSERT INTO statements (statement_id, statement_text, produced_by, "
        "ai_extraction_run_id) VALUES ('orphan-null', 'x', 'ai', NULL)"
    )
    conn.commit()
    conn.close()
    # Dangling run id: the FK would reject this on an FK-ON connection, but SQLite
    # defaults FK enforcement OFF — a plain connection simulates a load path that
    # never opted in. This is exactly why a read-only auditor earns its keep.
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO statements (statement_id, statement_text, produced_by, "
        "ai_extraction_run_id) VALUES ('orphan-dangling', 'x', 'ai', 'ghost-run')"
    )
    raw.commit()
    raw.close()
    with db.open_db(db_path) as conn:
        report = prov.audit_ai_provenance(conn)
    assert report["clean"] is False
    assert report["null_run_statement_ids"] == ["orphan-null"]
    assert report["unresolved_run"] == [["orphan-dangling", "ghost-run"]]
    assert report["orphan_count"] == 2


def test_audit_reports_non_ok_run_as_info_not_orphan(tmp_path: Path) -> None:
    """A row whose run later finalized 'partial' is informational, not an orphan
    (it was 'ok' at write time; its orphan-rejected siblings made the run partial)."""
    with _base_conn(tmp_path) as conn:
        stmt.insert_statement(conn, _ai_statement("alpine:ai:stmt-ok"))
        ai.finalize_run(
            conn, OK_RUN, output_statement_ids=["alpine:ai:stmt-ok"],
            output_evidence_link_ids=[], orphan_rejected_count=1, error_status="partial",
        )
        report = prov.audit_ai_provenance(conn)
    assert report["orphan_count"] == 0
    assert report["clean"] is True
    assert report["non_ok_run"] == [["alpine:ai:stmt-ok", OK_RUN, "partial"]]


def test_audit_empty_db_is_clean(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        report = prov.audit_ai_provenance(conn)
    assert report == {
        "ai_statement_count": 0,
        "null_run_statement_ids": [],
        "unresolved_run": [],
        "unresolved_evidence_run": [],
        "non_ok_run": [],
        "orphan_count": 0,
        "clean": True,
    }


def test_real_db_has_no_provenance_orphans(tmp_path: Path) -> None:
    """No-orphan integrity on the canonical DB. The real Alpine build is rebuilt
    per-run and git-ignored; when present it MUST be clean. When absent (fresh
    worktree), assert the same invariant on a DB built from the identical schema."""
    canonical = db.DEFAULT_DB_PATH
    if canonical.exists():
        with db.open_db(canonical) as conn:
            report = prov.audit_ai_provenance(conn)
        assert report["clean"] is True, f"canonical DB has orphans: {report}"
    with _base_conn(tmp_path) as conn:
        stmt.insert_statement(conn, _ai_statement("alpine:ai:real-shape"))
        assert prov.audit_ai_provenance(conn)["clean"] is True


# --- rule provenance: not re-invented -----------------------------------------

def test_binding_uses_ssot_producer_enum() -> None:
    """'ai' is the SSOT producer value; the app set mirrors publication (no shadow)."""
    assert stmt.ALLOWED_STATEMENT_PRODUCED_BY == pub.ALLOWED_PRODUCED_BY
    assert "ai" in pub.ALLOWED_PRODUCED_BY
    assert "ok" in ai.ALLOWED_RUN_ERROR_STATUS


def test_run_id_is_not_web_safe() -> None:
    """Provenance run id is vault-only: ai_extraction_run_id never reaches a web
    projection. (produced_by IS web-safe — it is a label, not an identifier — so
    this slice adds NO new web-safe field; the allowlist is unchanged.)"""
    assert "ai_extraction_run_id" not in pub.WEB_SAFE_FIELD_ALLOWLIST
    projected = pub.to_web_safe({
        "source_id": SOURCE_ID,
        "name": "Town of Alpine",
        "ai_extraction_run_id": OK_RUN,
        "produced_by": "ai",
    })
    assert "ai_extraction_run_id" not in projected
