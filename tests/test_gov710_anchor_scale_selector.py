"""GOV-710 — agenda-anchoring scale selector tests (scale leg 2 of 6).

Proves the additive ``--meeting-id`` / ``--agenda-doc-id`` / ``--transcript-id``
parameterization of ``scripts/agenda_anchor_batch.py`` (GOV-709 §3) generalizes the
pinned pilot constants correctly, keeps every fail-closed cross-check, and stays
pilot-preserving — WITHOUT touching any frozen surface:

* the agenda-item id is keyed on the meeting + document, collision-free across
  meetings (test 1);
* ``build_manifest`` on a NON-pilot meeting (108 / 2026-05-05 / doc 68 / tx14)
  extracts items keyed to that meeting+doc and scopes the reviewed statements to
  that transcript (test 2);
* the generalized fail-closed cross-checks all refuse: an agenda doc whose
  ``doc_date`` disagrees with the meeting date, a transcript dated differently from
  the meeting, and an unknown meeting id (tests 3–5);
* ``apply --commit`` anchors the non-pilot batch by containment, files the
  ``agenda_items`` under THAT meeting, and the board yields real cards for it
  (test 6);
* the target-statement selector is transcript-scoped: a reviewed row on a DIFFERENT
  transcript is not pulled into this meeting's batch (test 7).

Pure sqlite + tmp files; the agenda fixture is synthetic but reproduces the real
strict-increment grammar.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agenda_anchor_batch as aab  # noqa: E402  (under test)
import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import stage5_agenda_board as board_mod  # noqa: E402
import statements as st  # noqa: E402

SOURCE_ID = aab.AGENDA_DOC_SOURCE_ID
# Non-pilot scope: 2026-05-05 council, meeting 108, timed transcript tx14, agenda 68.
DATE = "2026-05-05"
MEETING_ID = 108
TX14 = 14
AGENDA_DOC_ID = 68
OTHER_TX = 15            # a differently-dated transcript, for the cross-check test
OTHER_DATE = "2026-05-12"
ORIGINAL_URL = "https://www.youtube.com/watch?v=tx14"

AGENDA_TEXT = """TOWN OF ALPINE — REGULAR MEETING (synthetic fixture)
May 5, 2026

1. CALL TO ORDER

2. PUBLIC HEARING

3. ADJOURNMENT
"""

# (statement_id, transcript_id, segment_index, timestamp_seconds, timestamp_human)
STATEMENTS = [
    ("stmt:tx14:seg-0000", TX14, 0, 5, "00:00:05"),
    ("stmt:tx14:seg-0001", TX14, 1, 20, "00:00:20"),
    ("stmt:tx14:seg-0002", TX14, 2, 40, "00:00:40"),
]

# reviewer-confirmed half-open ranges filled onto the manifest before apply.
RANGES = {
    aab.agenda_item_id_for(1, meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID): (0, 10),
    aab.agenda_item_id_for(2, meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID): (10, 30),
    aab.agenda_item_id_for(3, meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID): (30, 50),
}


def _link() -> dict:
    return {
        "to_source_id": SOURCE_ID,
        "relation": "substantiates",
        "original_url": ORIGINAL_URL,
        "final_url": ORIGINAL_URL,
        "archive_status": "not_checked",
        "scan_date": DATE,
        "captured_at_utc": "2026-05-06T12:00:00Z",
        "locator_kind": "page",
        "page": 1,
        "verification_status": "human_verified",
        "confidence": "high",
    }


def _write_agenda(corpus_root: Path) -> tuple[str, str]:
    sha = hashlib.sha256(AGENDA_TEXT.encode("utf-8")).hexdigest()
    rel = f"Raw-Corpus/{sha[:2]}/{sha}.txt"
    abspath = corpus_root / rel
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_text(AGENDA_TEXT, encoding="utf-8")
    return rel, sha


def _promote(conn: sqlite3.Connection, sid: str) -> None:
    gate.promote_statement(
        conn, sid, reviewer_id=aab.REVIEWER_ID, decision="approved",
        reason="GOV-710 scale promotion", to_verification_status="reviewed_source_linked",
        reason_category="promotion-card:test-scale", commit=False,
    )


def _seed(conn: sqlite3.Connection, corpus_root: Path) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Alpine local corpus', "
        "'alpine', 'video_transcript', 'official', 'official', ?)",
        (SOURCE_ID, ORIGINAL_URL),
    )
    gate.register_reviewer(
        conn, aab.REVIEWER_ID, display_name="Isaac",
        registered_by="owner:isaac (card 26562fe6 / GOV-702)", note="GOV-710 scale seed",
    )
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc) VALUES (?, 'tx14', ?, ?, 'full', "
        "'Raw-Corpus/tx14.txt', ?, '2026-05-06T00:00:00Z')",
        (TX14, ORIGINAL_URL, DATE, "t" * 64),
    )
    # a second transcript on a DIFFERENT date, for the transcript cross-check test.
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc) VALUES (?, 'tx15', 'http://x/15', ?, 'full', "
        "'Raw-Corpus/tx15.txt', ?, '2026-05-13T00:00:00Z')",
        (OTHER_TX, OTHER_DATE, "u" * 64),
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, title, source_url, transcript_id, "
        "fetch_time_utc) VALUES (?, ?, 'Town of Alpine', 'Regular Meeting', ?, ?, "
        "'2026-05-06T00:00:00Z')",
        (MEETING_ID, DATE, ORIGINAL_URL, TX14),
    )
    rel, sha = _write_agenda(corpus_root)
    conn.execute(
        "INSERT INTO documents (id, source_url, title, doc_type, doc_date, local_path, "
        "sha256, fetch_time_utc, source_id) VALUES "
        "(?, 'https://alpine/agenda-0505', 'MEET-Agenda_may05_council.txt', "
        "'agenda', ?, ?, ?, '2026-05-06T00:00:00Z', ?)",
        (AGENDA_DOC_ID, DATE, rel, sha, SOURCE_ID),
    )
    for sid, tid, idx, ts, human in STATEMENTS:
        seg = f"{sid.split(':', 1)[1]}"
        conn.execute(
            "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
            "timestamp_seconds, timestamp_human, segment_text) VALUES (?, ?, ?, ?, ?, ?)",
            (seg, tid, idx, ts, human, f"segment text for {seg}"),
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
        _promote(conn, sid)
    conn.commit()
    return sha


@pytest.fixture()
def env(tmp_path: Path):
    corpus_root = tmp_path / "repo"
    corpus_root.mkdir()
    db_path = tmp_path / "reg.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    sha = _seed(conn, corpus_root)
    yield conn, corpus_root, sha
    conn.close()


def _filled_manifest(conn, corpus_root) -> dict:
    m = aab.build_manifest(
        conn, corpus_root=corpus_root,
        meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID, transcript_id=TX14,
    )
    for it in m["agenda_items"]:
        rng = RANGES.get(it["agenda_item_id"])
        if rng:
            it["range_start_s"], it["range_end_s"] = rng
    return m


# --- test 1: agenda_item_id keyed on meeting + document -----------------------

def test_agenda_item_id_generalized() -> None:
    assert aab.agenda_item_id_for(1, meeting_id=108, agenda_doc_id=68) == \
        "agi:m108:doc-68:item-01"
    # keying on meeting+doc keeps ids collision-free across meetings.
    assert aab.agenda_item_id_for(1, meeting_id=108, agenda_doc_id=68) != \
        aab.agenda_item_id_for(1, meeting_id=129, agenda_doc_id=137)


# --- test 2: build_manifest on a NON-pilot meeting ----------------------------

def test_build_manifest_non_pilot_meeting(env) -> None:
    conn, corpus_root, _ = env
    m = aab.build_manifest(
        conn, corpus_root=corpus_root,
        meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID, transcript_id=TX14,
    )
    assert m["meeting_id"] == MEETING_ID
    assert m["meeting_date"] == DATE
    assert m["transcript_id"] == TX14
    assert [it["agenda_item_id"] for it in m["agenda_items"]] == [
        "agi:m108:doc-68:item-01",
        "agi:m108:doc-68:item-02",
        "agi:m108:doc-68:item-03",
    ]
    for it in m["agenda_items"]:
        assert it["source_document_id"] == AGENDA_DOC_ID
    assert [s["statement_id"] for s in m["statements"]] == [
        "stmt:tx14:seg-0000", "stmt:tx14:seg-0001", "stmt:tx14:seg-0002",
    ]


# --- test 3: doc_date != meeting date refuses ---------------------------------

def test_doc_date_mismatch_refuses(env) -> None:
    conn, corpus_root, _ = env
    conn.execute("UPDATE documents SET doc_date = ? WHERE id = ?",
                 ("2026-05-04", AGENDA_DOC_ID))
    conn.commit()
    with pytest.raises(aab.AnchorRefusedError):
        aab.build_manifest(
            conn, corpus_root=corpus_root,
            meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID, transcript_id=TX14,
        )


# --- test 4: transcript dated differently from the meeting refuses ------------

def test_transcript_date_mismatch_refuses(env) -> None:
    conn, corpus_root, _ = env
    with pytest.raises(aab.AnchorRefusedError):
        aab.build_manifest(
            conn, corpus_root=corpus_root,
            meeting_id=MEETING_ID, agenda_doc_id=AGENDA_DOC_ID, transcript_id=OTHER_TX,
        )


# --- test 5: an unknown meeting id refuses ------------------------------------

def test_unknown_meeting_id_refuses(env) -> None:
    conn, corpus_root, _ = env
    with pytest.raises(aab.AnchorRefusedError):
        aab.build_manifest(
            conn, corpus_root=corpus_root,
            meeting_id=9999, agenda_doc_id=AGENDA_DOC_ID, transcript_id=TX14,
        )


# --- test 6: apply anchors the non-pilot batch; board yields real cards --------

def test_apply_anchors_non_pilot_and_board_cards(env) -> None:
    conn, corpus_root, _ = env
    report = aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                                card_id="int-m108-tx14-b1", commit=True)
    assert report["counts"]["anchored"] == 3
    assert report["counts"]["unanchored_remaining"] == 0

    got = dict(conn.execute(
        "SELECT statement_id, agenda_item_id FROM statements "
        "WHERE agenda_item_id IS NOT NULL"
    ).fetchall())
    assert got == {
        "stmt:tx14:seg-0000": "agi:m108:doc-68:item-01",
        "stmt:tx14:seg-0001": "agi:m108:doc-68:item-02",
        "stmt:tx14:seg-0002": "agi:m108:doc-68:item-03",
    }
    # agenda_items are filed under THIS meeting (108), not the pilot 129.
    meetings = {r["meeting_id"] for r in conn.execute(
        "SELECT meeting_id FROM agenda_items").fetchall()}
    assert meetings == {MEETING_ID}

    board = board_mod.agenda_board(conn)
    assert board["cardCount"] == 3
    assert board["unanchoredStatementCount"] == 0


# --- test 7: target statements are transcript-scoped --------------------------

def test_target_statements_transcript_scoped(env) -> None:
    conn, corpus_root, _ = env
    # a reviewed row on the OTHER transcript must NOT enter meeting 108's batch.
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('tx15:seg-0000', ?, 0, 7, '00:00:07', 'x')", (OTHER_TX,),
    )
    st.insert_statement(conn, {
        "statement_id": "stmt:tx15:seg-0000", "segment_id": "tx15:seg-0000",
        "statement_text": "other transcript", "verification_status": "reviewed_source_linked",
        "produced_by": "automation", "is_verbatim": 1,
    }, [_link()])
    conn.commit()

    ids = [s["statement_id"] for s in aab.target_statements(conn, transcript_id=TX14)]
    assert "stmt:tx15:seg-0000" not in ids
    assert ids == ["stmt:tx14:seg-0000", "stmt:tx14:seg-0001", "stmt:tx14:seg-0002"]
