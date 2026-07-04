"""GOV-605 agenda-board projection tests (GOV-601 §2 contract, over reviewed Alpine).

Proves ``scripts/stage5_agenda_board.py`` closes exactly the GOV-601 PROJECTION GAP so
GOV-599's shipped agenda-Kanban renders REAL reviewed Alpine data:

* cards are keyed on ``agenda_item + meeting + thread`` (not just ``statementId``), one
  card per agenda item, carrying meeting identity + thread label/status (AC1, test 1);
* the two named gaps are composed: ``videoRef`` = ``transcripts.video_url`` + the segment
  ``timestamp_seconds`` (earliest segment wins), and typed ``lineage`` = agenda lifecycle
  edges + ``updates_statement_id`` correction refs (AC2, tests 2 + 3);
* latent fields are emitted empty + disclosed, never fabricated — ``decisions: []`` and
  ``categoryAnchor.kind = agenda_thread`` with honest ``gapBadges`` (AC3, test 4);
* fail-closed: an unreviewed statement never reaches the board, and a card mixing an
  ``ai_presented`` statement is never labelled ``Verified`` (AC4, tests 5 + 6);
* empty-state honesty: no reviewed Alpine agenda records -> a well-formed empty board with
  disclosure, not an error (AC5, test 7);
* the single fail-closed gate is shared with ``reviewer_internal_records`` — a row the
  reviewer serve drops is absent from the board too (AC4, test 8);
* determinism / no-leak: re-projection is byte-identical and no raw vault/FS path crosses
  the body (test 9 + 10).

Pure sqlite + tmp files: no network, no real-corpus dependency. Seed mirrors the GOV-520 /
GOV-524 reviewer-internal Alpine corpus.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402
import stage3_card_feed as card_feed  # noqa: E402
import stage5_agenda_board as ab  # noqa: E402  (under test)

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-05-11.pdf"
VIDEO_URL = "https://www.youtube.com/watch?v=alpine0413"
MEETING_URL = "https://www.alpinewy.gov/meetings/2026-04-13"
MEETING_DATE = "2026-04-13"

ITEM_BUDGET = "alpine:2026-04-13:item-budget"
ITEM_ZONING = "alpine:2026-04-13:item-zoning"
THREAD_BUDGET = "thr-budget"


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


@pytest.fixture()
def empty_conn(tmp_path: Path):
    """A migrated but unseeded DB — no reviewed Alpine agenda records exist yet."""
    db_path = tmp_path / "empty.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    yield connection
    connection.close()


def _promote(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    agenda_item_id: str,
    segment_id: str | None = None,
    produced_by: str = "human",
    run_id: str | None = None,
    updates_statement_id: str | None = None,
) -> None:
    record = {
        "statement_id": statement_id,
        "agenda_item_id": agenda_item_id,
        "segment_id": segment_id,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": produced_by,
        "updates_statement_id": updates_statement_id,
    }
    if produced_by == "ai":
        if conn.execute(
            "SELECT 1 FROM ai_extraction_runs WHERE run_id=?", (run_id,)
        ).fetchone() is None:
            ai.create_run(conn, run_id=run_id, input_source_ids=[])
        record["ai_extraction_run_id"] = run_id
    link = {
        "to_source_id": MINUTES_SOURCE,
        "relation": "substantiates",
        "original_url": ORIGINAL_URL,
        "final_url": ORIGINAL_URL,
        "archive_status": "not_checked",
        "scan_date": MEETING_DATE,
        "captured_at_utc": "2026-04-15T12:00:00Z",
        "locator_kind": "page",
        "page": 1,
        "verification_status": "human_verified",
        "confidence": "high",
    }
    st.insert_statement(conn, record, [link])
    gate.promote_statement(
        conn,
        statement_id,
        reviewer_id="reviewer:isaac",
        decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Town Council Minutes', "
        "'alpine', 'minutes', 'official', 'official', ?)",
        (MINUTES_SOURCE, ORIGINAL_URL),
    )
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Council Agenda', "
        "'alpine', 'agenda', 'official', 'official', ?)",
        (AGENDA_SOURCE, AGENDA_URL),
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-605 agenda-board seed",
    )
    # transcript + segments (the videoRef backbone) ---------------------------
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc) VALUES (1, 'alpine0413', ?, 'full', 'x', 'y', "
        "'2026-04-13T00:00:00Z')",
        (VIDEO_URL,),
    )
    for seg_id, idx, ts in (("seg-budget-late", 1, 300), ("seg-budget-early", 2, 45)):
        conn.execute(
            "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
            "timestamp_seconds, timestamp_human, segment_text) VALUES (?, 1, ?, ?, ?, 'seg')",
            (seg_id, idx, ts, f"00:0{ts}"),
        )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, title, source_url, transcript_id, "
        "fetch_time_utc) VALUES (1, ?, 'Town Council', 'Regular Meeting', ?, 1, "
        "'2026-04-13T00:00:00Z')",
        (MEETING_DATE, MEETING_URL),
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES (?, 1, 1, 'FY27 Budget')",
        (ITEM_BUDGET,),
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES (?, 1, 2, 'Zoning Variance')",
        (ITEM_ZONING,),
    )
    # thread + typed edges ----------------------------------------------------
    conn.execute(
        "INSERT INTO agenda_threads (agenda_thread_id, title, jurisdiction_id, status, "
        "canonical_human_label) VALUES (?, 'Budget thread', 'alpine', 'open', 'Town budget')",
        (THREAD_BUDGET,),
    )
    conn.execute(
        "INSERT INTO concept_edges (edge_id, edge_type, from_node_id, from_node_type, "
        "to_node_id, to_node_type) VALUES ('e-in-thread', 'agenda_item_in_thread', ?, "
        "'agenda_item', ?, 'agenda_thread')",
        (ITEM_BUDGET, THREAD_BUDGET),
    )
    conn.execute(
        "INSERT INTO concept_edges (edge_id, edge_type, from_node_id, from_node_type, "
        "to_node_id, to_node_type) VALUES ('e-super', 'agenda_item_supersedes', ?, "
        "'agenda_item', ?, 'agenda_item')",
        (ITEM_ZONING, ITEM_BUDGET),
    )
    conn.commit()

    # statements (all reviewer-cleared except s-unreviewed) -------------------
    _promote(conn, "s-budget-1", agenda_item_id=ITEM_BUDGET, segment_id="seg-budget-late")
    _promote(conn, "s-budget-2", agenda_item_id=ITEM_BUDGET, segment_id="seg-budget-early")
    _promote(
        conn, "s-budget-super", agenda_item_id=ITEM_BUDGET,
        updates_statement_id="s-budget-1",
    )
    _promote(
        conn, "s-budget-ai", agenda_item_id=ITEM_BUDGET, produced_by="ai", run_id="run-ai-1",
    )
    _promote(conn, "s-zoning-1", agenda_item_id=ITEM_ZONING)

    # an unreviewed statement — inserted but NEVER promoted (fail-closed check).
    st.insert_statement(
        conn,
        {
            "statement_id": "s-unreviewed",
            "agenda_item_id": ITEM_BUDGET,
            "statement_text": "Unreviewed claim that must never reach the board.",
            "verification_status": "machine_extracted_unreviewed",
            "produced_by": "automation",
        },
        [
            {
                "to_source_id": MINUTES_SOURCE,
                "relation": "substantiates",
                "original_url": ORIGINAL_URL,
                "final_url": ORIGINAL_URL,
                "archive_status": "not_checked",
                "scan_date": MEETING_DATE,
                "captured_at_utc": "2026-04-15T12:00:00Z",
                "locator_kind": "page",
                "page": 1,
                "verification_status": "human_verified",
                "confidence": "high",
            }
        ],
    )
    conn.commit()


def _all_cards(board: dict) -> dict[str, dict]:
    return {c["agendaItemId"]: c for lane in board["lanes"] for c in lane["cards"]}


# --- test 1: card keying on agenda_item + meeting + thread (AC1) ---------------


def test_cards_keyed_on_agenda_item_meeting_thread(conn: sqlite3.Connection) -> None:
    board = ab.agenda_board(conn)
    cards = _all_cards(board)
    # exactly one card per agenda item that has a reviewed statement (NOT per statement).
    assert set(cards) == {ITEM_BUDGET, ITEM_ZONING}
    assert board["cardCount"] == 2

    budget = cards[ITEM_BUDGET]
    # meeting identity projected onto the card.
    assert budget["meetingId"] == 1
    assert budget["meetingDate"] == MEETING_DATE
    assert budget["meetingBody"] == "Town Council"
    assert budget["meetingTitle"] == "Regular Meeting"
    assert budget["meetingSourceUrl"] == MEETING_URL
    assert budget["agendaItemTitle"] == "FY27 Budget"
    assert budget["itemOrder"] == 1
    # thread projected (label + status).
    assert budget["agendaThreadId"] == THREAD_BUDGET
    assert budget["threadLabel"] == "Town budget"
    assert budget["threadStatus"] == "open"
    # the card aggregates its four reviewed statements (not one card each).
    assert budget["statementIds"] == [
        "s-budget-1", "s-budget-2", "s-budget-ai", "s-budget-super",
    ]
    assert budget["recordCount"] == 4


# --- test 2: videoRef composition — earliest resolvable segment (AC2) ----------


def test_video_ref_composed_from_earliest_segment(conn: sqlite3.Connection) -> None:
    budget = _all_cards(ab.agenda_board(conn))[ITEM_BUDGET]
    # video_url + the EARLIEST segment timestamp (45s beats 300s) — start of discussion.
    assert budget["videoRef"] == {"url": VIDEO_URL, "timestampSeconds": 45}


# --- test 3: typed lineage — lifecycle edges + updates_statement (AC2) ---------


def test_typed_lineage_composed(conn: sqlite3.Connection) -> None:
    cards = _all_cards(ab.agenda_board(conn))
    # budget: the correction ref (s-budget-super updates s-budget-1).
    assert {"relation": "updates_statement", "ref": "s-budget-1"} in cards[ITEM_BUDGET]["lineage"]
    # zoning: the typed agenda lifecycle edge (never an untyped "related").
    assert cards[ITEM_ZONING]["lineage"] == [
        {"relation": "agenda_item_supersedes", "ref": ITEM_BUDGET}
    ]


# --- test 4: latent fields empty + disclosed, honest gaps (AC3) ---------------


def test_latent_fields_empty_and_disclosed(conn: sqlite3.Connection) -> None:
    board = ab.agenda_board(conn)
    cards = _all_cards(board)
    for card in cards.values():
        assert card["decisions"] == []  # never fabricated
        assert card["categoryAnchor"]["kind"] == "agenda_thread"
        assert card["categoryAnchor"]["disclosure"]  # non-empty honest disclosure
    # zoning has no thread edge and no resolvable segment -> both gaps surfaced visibly.
    zoning_gaps = cards[ITEM_ZONING]["gapBadges"]
    assert any("Agenda thread not yet linked" in g for g in zoning_gaps)
    assert any("Video deep-link unavailable" in g for g in zoning_gaps)
    # board-level disclosures name the latent-by-data reality.
    assert "decisions" in board["disclosures"]
    assert board["disclosures"]["emptyState"] is False


# --- test 5: unreviewed statement never reaches the board (AC4) ----------------


def test_unreviewed_statement_excluded(conn: sqlite3.Connection) -> None:
    board = ab.agenda_board(conn)
    all_ids = [
        sid for card in _all_cards(board).values() for sid in card["statementIds"]
    ]
    assert "s-unreviewed" not in all_ids


# --- test 6: a card mixing an ai_presented statement is never "Verified" (AC4) -


def test_card_never_falsely_verified(conn: sqlite3.Connection) -> None:
    budget = _all_cards(ab.agenda_board(conn))[ITEM_BUDGET]
    # budget aggregates an ai_presented statement -> conservative status, never "Verified".
    assert budget["statusBadge"] != "Verified"


# --- test 7: empty-state honesty — well-formed empty board, not an error (AC5) -


def test_empty_state_is_well_formed(empty_conn: sqlite3.Connection) -> None:
    board = ab.agenda_board(empty_conn)  # must NOT raise
    assert board["scope"] == "alpine"
    assert board["access"] == "reviewer_internal"
    assert board["cardCount"] == 0
    # all six frozen lanes present, each an empty column (a board never hides a lane).
    assert [lane["lane"] for lane in board["lanes"]] == list(ab.surface.LANE_ORDER)
    assert all(lane["cardCount"] == 0 and lane["cards"] == [] for lane in board["lanes"])
    assert board["disclosures"]["emptyState"] is True


# --- test 8: the board shares the single fail-closed gate (AC4) ----------------


def test_board_shares_reviewer_internal_gate(conn: sqlite3.Connection) -> None:
    served_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    board_ids = {
        sid for card in _all_cards(ab.agenda_board(conn)).values()
        for sid in card["statementIds"]
    }
    # every statement on the board is a reviewer-cleared served row (subset, no drift).
    assert board_ids <= served_ids
    # and the reviewer serve is unchanged by the gate refactor (all cleared rows present).
    assert served_ids == {
        "s-budget-1", "s-budget-2", "s-budget-super", "s-budget-ai", "s-zoning-1",
    }


# --- test 9: deterministic + idempotent re-projection ------------------------


def test_deterministic_reprojection(conn: sqlite3.Connection) -> None:
    first = json.dumps(ab.agenda_board(conn), sort_keys=True)
    second = json.dumps(ab.agenda_board(conn), sort_keys=True)
    assert first == second


# --- test 10: no raw vault / FS path crosses the body (transport sweep) --------


def test_no_raw_path_leak(conn: sqlite3.Connection) -> None:
    board = ab.agenda_board(conn)  # agenda_board runs assert_no_raw_paths internally
    blob = json.dumps(board)
    for marker in read_api.RAW_PATH_MARKERS:
        assert marker not in blob
    # segment_id is web-UNSAFE and must never appear, even though videoRef is derived from it.
    assert "seg-budget-early" not in blob
    assert "seg-budget-late" not in blob
