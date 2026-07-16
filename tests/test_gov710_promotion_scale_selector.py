"""GOV-710 — reviewer-promotion scale selector tests (scale leg 2 of 6).

Proves the additive ``--meeting-date`` / ``--transcript-id`` parameterization of
``scripts/reviewer_promotion_batch.py`` (GOV-709 §3) is a *transcript-scoped*
selector, fail-closed, and pilot-preserving — WITHOUT touching any frozen surface:

* the §1a hazard is real and handled: 2026-05-07 carries TWO timed transcripts that
  ``UNIQUE(meeting_date, body)`` collapses into one ``meetings`` row and whose
  ``segment_index`` sequences both restart at 0; a date-scoped slice interleaves
  them, a transcript-scoped slice returns exactly one transcript's rows in order
  (tests 1 + 2);
* ``--meeting-date`` is a fail-closed cross-check against ``transcripts.meeting_date``
  and an unknown transcript id refuses (tests 3 + 4);
* the manifest records the selector and tags the batch id with ``tx<id>`` so the
  leg-4 card key convention (GOV-709 §2) is derivable, and re-proposal is
  byte-identical (test 5);
* ``apply`` re-derives its scope-gate allowlist from the manifest selector: a clean
  transcript batch promotes through the frozen gate, and a row from the SIBLING
  transcript of the same date is out-of-slice and refused with exit 2 (tests 6 + 7);
* the pilot default (no selector) still date-scopes exactly as GOV-648 (test 8).

Pure sqlite + tmp files: no network, no real-corpus dependency.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import reviewer_promotion_batch as rpb  # noqa: E402  (under test)
import statements as st  # noqa: E402

SOURCE_ID = "toa_youtube"
# 2026-05-07 meeting 109 has two timed transcripts (GOV-709 §1a).
DATE = "2026-05-07"
MEETING_ID = 109
TX16 = 16          # first timed transcript of the date
TX17 = 17          # second timed transcript of the SAME date
OTHER_DATE = "2026-05-05"
ORIGINAL_URL = "https://www.youtube.com/watch?v=tx16"


def _link() -> dict:
    return {
        "to_source_id": SOURCE_ID,
        "relation": "substantiates",
        "original_url": ORIGINAL_URL,
        "final_url": ORIGINAL_URL,
        "archive_status": "not_checked",
        "scan_date": DATE,
        "captured_at_utc": "2026-05-08T12:00:00Z",
        "locator_kind": "page",
        "page": 1,
        "verification_status": "human_verified",
        "confidence": "high",
    }


def _add_transcript(conn: sqlite3.Connection, tid: int, meeting_date: str, sha: str) -> None:
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc) VALUES (?, ?, ?, ?, 'full', ?, ?, "
        "'2026-05-08T00:00:00Z')",
        (tid, f"vid{tid}", f"http://x/{tid}", meeting_date, f"Docs/tx{tid}.txt", sha),
    )


def _add_statement(conn: sqlite3.Connection, sid: str, tid: int, idx: int) -> None:
    seg = f"seg-{tid}-{idx}"
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES (?, ?, ?, ?, ?, 'seg')",
        (seg, tid, idx, idx * 60, f"00:0{idx}:00"),
    )
    st.insert_statement(
        conn,
        {
            "statement_id": sid,
            "segment_id": seg,
            "statement_text": f"Verbatim civic claim {sid}.",
            "verification_status": "machine_extracted_unreviewed",
            "produced_by": "automation",
            "is_verbatim": 1,
        },
        [_link()],
    )


def _seed(conn: sqlite3.Connection, *, register: bool = True) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'TOA council video', "
        "'alpine', 'video_transcript', 'official', 'official', ?)",
        (SOURCE_ID, ORIGINAL_URL),
    )
    if register:
        gate.register_reviewer(
            conn, rpb.REVIEWER_ID, display_name="Isaac",
            registered_by="owner:isaac (card 26562fe6 / GOV-702)",
            note="GOV-710 scale seed",
        )
    _add_transcript(conn, TX16, DATE, "a" * 64)
    _add_transcript(conn, TX17, DATE, "b" * 64)
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, title, source_url, transcript_id, "
        "fetch_time_utc) VALUES (?, ?, 'Town Council', 'Regular Meeting', ?, ?, "
        "'2026-05-08T00:00:00Z')",
        (MEETING_ID, DATE, ORIGINAL_URL, TX16),
    )
    # Each transcript's segment_index restarts at 0 (the §1a interleave hazard).
    _add_statement(conn, "s16-0", TX16, 0)
    _add_statement(conn, "s16-1", TX16, 1)
    _add_statement(conn, "s17-0", TX17, 0)
    _add_statement(conn, "s17-1", TX17, 1)
    conn.commit()


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "reg.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


# --- test 1: date-scoped slice interleaves the two transcripts (the hazard) ---

def test_date_scope_interleaves_two_transcripts(conn: sqlite3.Connection) -> None:
    rows = rpb.pilot_slice(conn, meeting_date=DATE)
    # ordered by (segment_index, statement_id): idx0 rows first, then idx1 rows.
    assert [r["statement_id"] for r in rows] == ["s16-0", "s17-0", "s16-1", "s17-1"]


# --- test 2: transcript-scoped slice returns exactly one transcript, in order --

def test_transcript_scope_isolates_one_transcript(conn: sqlite3.Connection) -> None:
    # the operator passes BOTH the date and the transcript (GOV-709 §3); the date is
    # a cross-check, the transcript is the actual scope.
    assert [r["statement_id"] for r in
            rpb.pilot_slice(conn, meeting_date=DATE, transcript_id=TX16)] == \
        ["s16-0", "s16-1"]
    assert rpb.pilot_slice_ids(conn, meeting_date=DATE, transcript_id=TX17) == \
        {"s17-0", "s17-1"}
    # the two slices are disjoint — no cross-transcript leakage.
    assert rpb.pilot_slice_ids(conn, meeting_date=DATE, transcript_id=TX16) & \
        rpb.pilot_slice_ids(conn, meeting_date=DATE, transcript_id=TX17) == set()


# --- test 3: --meeting-date is a fail-closed cross-check ----------------------

def test_meeting_date_crosscheck_refuses_mismatch(conn: sqlite3.Connection) -> None:
    with pytest.raises(rpb.PromotionScopeError):
        rpb.pilot_slice(conn, meeting_date=OTHER_DATE, transcript_id=TX16)


# --- test 4: an unknown transcript id refuses --------------------------------

def test_unknown_transcript_id_refuses(conn: sqlite3.Connection) -> None:
    with pytest.raises(rpb.PromotionScopeError):
        rpb.pilot_slice(conn, transcript_id=9999)


# --- test 5: manifest records the selector + tx-tagged batch id, deterministic -

def test_manifest_records_selector_and_tx_batch_id(conn: sqlite3.Connection) -> None:
    m1 = rpb.build_manifest(conn, transcript_id=TX16, meeting_date=DATE)
    m2 = rpb.build_manifest(conn, transcript_id=TX16, meeting_date=DATE)
    assert m1["transcript_id"] == TX16
    assert m1["meeting_date"] == DATE
    assert m1["batch_id"] == "promotion-batch:tx16:0000-0002"   # tx tag per GOV-709 §2
    assert [s["statement_id"] for s in m1["statements"]] == ["s16-0", "s16-1"]
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)


# --- test 6: apply promotes a clean transcript batch through the frozen gate ---

def test_apply_promotes_transcript_batch(conn: sqlite3.Connection) -> None:
    m = rpb.build_manifest(conn, transcript_id=TX16, meeting_date=DATE)
    summary = rpb.apply_manifest(conn, m, card_id="int-m109-tx16-b1", commit=True)
    assert summary["counts"]["promoted"] == 2
    for sid in ("s16-0", "s16-1"):
        row = conn.execute(
            "SELECT verification_status, publication_state FROM statements "
            "WHERE statement_id = ?", (sid,)
        ).fetchone()
        assert row["verification_status"] == "reviewed_source_linked"
        assert row["publication_state"] == "not_publishable"   # never flipped
    # the sibling transcript is untouched.
    for sid in ("s17-0", "s17-1"):
        row = conn.execute(
            "SELECT verification_status FROM statements WHERE statement_id = ?", (sid,)
        ).fetchone()
        assert row["verification_status"] == "machine_extracted_unreviewed"


# --- test 7: a sibling-transcript row is out-of-slice for a tx16 batch (exit 2) -

def test_sibling_transcript_row_is_out_of_slice(conn: sqlite3.Connection,
                                                tmp_path: Path) -> None:
    m = rpb.build_manifest(conn, transcript_id=TX16, meeting_date=DATE)
    m["statements"].append(
        {"statement_id": "s17-0", "decision": "approved", "agenda_item_id": None}
    )
    with pytest.raises(rpb.PromotionScopeError):
        rpb.apply_manifest(conn, m, card_id="int-1", commit=True)
    assert conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0] == 0

    # the CLI surfaces it as the no-bypass scope exit.
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(m), encoding="utf-8")
    rc = rpb.main(["--db", db_path, "apply", "--manifest", str(mpath),
                   "--card", "int-1", "--commit"])
    assert rc == rpb.EXIT_SCOPE


# --- test 8: the pilot default (no selector) still date-scopes (GOV-648) ------

def test_pilot_default_still_date_scopes(conn: sqlite3.Connection) -> None:
    # No selector => defaults to the pilot 2026-06-23 date, which this fixture has
    # no rows for, so the slice is empty (the pilot behaviour, unchanged).
    assert rpb.pilot_slice(conn) == []
    assert rpb.pilot_slice_ids(conn) == set()
