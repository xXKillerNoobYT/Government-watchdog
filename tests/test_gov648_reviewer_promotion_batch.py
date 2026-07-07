"""GOV-648 — reviewer-promotion batch CLI tests (Option-A pilot, leg 2 of 6).

Proves ``scripts/reviewer_promotion_batch.py`` drives the frozen Lane-5 gate over
the 2026-06-23 Town of Alpine pilot slice, fail-closed and idempotent, WITHOUT
touching any frozen surface:

* the date-scoped slice selector returns exactly the pilot statements, in
  ``segment_index`` order, excluding out-of-slice rows (test 1);
* ``propose`` emits a read-only review packet (verbatim text + timestamp + source
  anchor) and writes zero ledger rows; re-proposal is byte-identical (test 2);
* ``apply --commit`` of an approved batch flips ``verification_status`` ->
  ``reviewed_source_linked``, writes one promoting ledger row per statement, and
  never touches ``publication_state`` (test 3);
* dry-run (the default) writes nothing but reports the plan (test 4);
* ``hold`` / ``rejected`` never promote and never reach the reviewer serve (test 5);
* an unregistered reviewer (empty registry) and a forbidden automation/AI id are
  both refused, writing nothing (tests 6 + 7);
* the scope gate refuses an out-of-slice statement (exit 2) and an oversized batch
  (tests 8 + 9);
* re-applying an already-applied batch writes 0 new rows and exits 0 (test 10);
* promoted rows pass ``read_api.reviewer_internal_records`` and, once anchored on
  apply, the ``agenda_board`` ``cardCount`` increments; unanchored promoted rows
  are disclosed, never dropped (test 11);
* the CLI is additive-only — it neither re-implements a frozen serve/promotion
  function nor issues a bare status UPDATE, so the frozen surfaces stay untouched
  (test 12; byte-0-diff vs main is re-checked at the GOV-649 merge gate).

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
import read_api  # noqa: E402
import reviewer_promotion_batch as rpb  # noqa: E402  (under test)
import stage5_agenda_board as ab  # noqa: E402
import statements as st  # noqa: E402

SOURCE_ID = "toa_youtube"
TRANSCRIPT_ID = 33            # the timed 2026-06-23 transcript (mirrors ops registry)
OTHER_TRANSCRIPT_ID = 90     # an out-of-slice transcript (different meeting_date)
LOCAL_PATH = "Docs/Source-Data/alpine/youtube_transcript_JZA89mC7Oj8.txt"
SHA256 = "a" * 64
ORIGINAL_URL = "https://www.youtube.com/watch?v=JZA89mC7Oj8"
MEETING_ID = 129
AGENDA_ITEM_ID = "alpine:2026-06-23:item-1"


def _link() -> dict:
    return {
        "to_source_id": SOURCE_ID,
        "relation": "substantiates",
        "original_url": ORIGINAL_URL,
        "final_url": ORIGINAL_URL,
        "archive_status": "not_checked",
        "scan_date": rpb.PILOT_MEETING_DATE,
        "captured_at_utc": "2026-06-24T12:00:00Z",
        "locator_kind": "page",
        "page": 1,
        "verification_status": "human_verified",
        "confidence": "high",
    }


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
            registered_by="owner:isaac (card 64a4c200 / GOV-646)",
            note="GOV-648 pilot seed",
        )
    # the pilot transcript (meeting_date is the normative slice selector, §1) ---
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc) VALUES (?, 'JZA89mC7Oj8', ?, ?, 'full', "
        "?, ?, '2026-06-24T00:00:00Z')",
        (TRANSCRIPT_ID, ORIGINAL_URL, rpb.PILOT_MEETING_DATE, LOCAL_PATH, SHA256),
    )
    # an out-of-slice transcript (different meeting_date) ----------------------
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc) VALUES (?, 'OTHER', 'http://x', "
        "'2026-05-01', 'full', 'other.txt', ?, '2026-05-02T00:00:00Z')",
        (OTHER_TRANSCRIPT_ID, "b" * 64),
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, title, source_url, transcript_id, "
        "fetch_time_utc) VALUES (?, ?, 'Town Council', 'Regular Meeting', ?, ?, "
        "'2026-06-24T00:00:00Z')",
        (MEETING_ID, rpb.PILOT_MEETING_DATE, ORIGINAL_URL, TRANSCRIPT_ID),
    )
    # three in-slice statements (segment_index order 3,1,2 -> proves ordering) --
    for sid, seg, idx, ts in (
        ("s-a", "seg-a", 3, "00:05:00"),
        ("s-b", "seg-b", 1, "00:01:00"),
        ("s-c", "seg-c", 2, "00:03:00"),
    ):
        conn.execute(
            "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
            "timestamp_seconds, timestamp_human, segment_text) VALUES (?, ?, ?, ?, ?, 'seg')",
            (seg, TRANSCRIPT_ID, idx, idx * 60, ts),
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
    # one out-of-slice statement (segment on the 2026-05-01 transcript) --------
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-out', ?, 1, 60, '00:01:00', 'seg')",
        (OTHER_TRANSCRIPT_ID,),
    )
    st.insert_statement(
        conn,
        {
            "statement_id": "s-out",
            "segment_id": "seg-out",
            "statement_text": "Out-of-slice claim (2026-05-01).",
            "verification_status": "machine_extracted_unreviewed",
            "produced_by": "automation",
            "is_verbatim": 1,
        },
        [_link()],
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "reg.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


@pytest.fixture()
def db_path_registered(tmp_path: Path) -> Path:
    p = tmp_path / "reg_cli.db"
    db.apply_migrations(p)
    c = db.open_db(p)
    _seed(c)
    c.close()
    return p


def _manifest(conn, *, decision="approved", anchor=False, ids=("s-a", "s-b", "s-c")):
    m = rpb.build_manifest(conn, offset=0, limit=rpb.MAX_BATCH)
    m["statements"] = [e for e in m["statements"] if e["statement_id"] in ids]
    for e in m["statements"]:
        e["decision"] = decision
        if anchor:
            e["agenda_item_id"] = AGENDA_ITEM_ID
    if anchor:
        m["agenda_items"] = [{
            "agenda_item_id": AGENDA_ITEM_ID, "meeting_id": MEETING_ID,
            "item_order": 1, "title": "Call to Order and Roll Call",
        }]
    return m


# --- test 1: date-scoped slice selector (GOV-647 §1) --------------------------

def test_pilot_slice_selector_ordered_and_scoped(conn: sqlite3.Connection) -> None:
    rows = rpb.pilot_slice(conn)
    # only the three 2026-06-23 statements; s-out (2026-05-01) is excluded.
    assert [r["statement_id"] for r in rows] == ["s-b", "s-c", "s-a"]  # segment_index 1,2,3
    assert rpb.pilot_slice_ids(conn) == {"s-a", "s-b", "s-c"}
    assert "s-out" not in rpb.pilot_slice_ids(conn)


# --- test 2: propose is read-only + byte-identical (GOV-647 §3) ---------------

def test_propose_readonly_and_deterministic(conn: sqlite3.Connection) -> None:
    before = conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0]
    m1 = rpb.build_manifest(conn, offset=0, limit=2)
    m2 = rpb.build_manifest(conn, offset=0, limit=2)
    after = conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0]

    assert before == after == 0            # read-only: no ledger rows written
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)  # deterministic
    assert len(m1["statements"]) == 2
    first = m1["statements"][0]
    assert first["statement_id"] == "s-b"  # window follows the slice order
    assert first["text"] == "Verbatim civic claim s-b."       # verbatim
    assert first["timestamp_human"] == "00:01:00"
    assert first["source"] == {"transcript_local_path": LOCAL_PATH, "sha256": SHA256}
    assert m1["reviewer_id"] == "reviewer:isaac"
    assert m1["to_verification_status"] == "reviewed_source_linked"


# --- test 3: apply --commit promotes + freezes publication_state (GOV-647 §2) -

def test_apply_commit_flips_status_only(conn: sqlite3.Connection) -> None:
    summary = rpb.apply_manifest(conn, _manifest(conn), card_id="int-1", commit=True)
    assert summary["counts"]["promoted"] == 3

    for sid in ("s-a", "s-b", "s-c"):
        row = conn.execute(
            "SELECT verification_status, review_state, publication_state FROM statements "
            "WHERE statement_id = ?", (sid,)
        ).fetchone()
        assert row["verification_status"] == "reviewed_source_linked"
        assert row["review_state"] == "reviewed"
        assert row["publication_state"] == "not_publishable"   # NEVER flipped
        dec = gate.latest_decision(conn, sid)
        assert dec["decision"] == "approved" and dec["promoted"] == 1
        assert dec["to_verification_status"] == "reviewed_source_linked"
        assert dec["reason_category"] == "promotion-card:int-1"  # card recorded for audit


# --- test 4: dry-run is the default and writes nothing (GOV-647 §7.2) ---------

def test_dry_run_default_writes_nothing(conn: sqlite3.Connection) -> None:
    summary = rpb.apply_manifest(conn, _manifest(conn), card_id="int-1", commit=False)
    assert summary["dry_run"] is True
    assert summary["counts"]["promoted"] == 3          # reports the plan
    # but nothing is persisted.
    assert conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0] == 0
    row = conn.execute(
        "SELECT verification_status FROM statements WHERE statement_id='s-a'"
    ).fetchone()
    assert row["verification_status"] == "machine_extracted_unreviewed"


# --- test 5: hold / reject never promote, never served (GOV-647 §4) ----------

def test_hold_and_reject_do_not_promote_or_serve(conn: sqlite3.Connection) -> None:
    rpb.apply_manifest(conn, _manifest(conn, decision="hold"), card_id="int-1", commit=True)
    row = conn.execute(
        "SELECT verification_status, review_state FROM statements WHERE statement_id='s-a'"
    ).fetchone()
    assert row["verification_status"] == "machine_extracted_unreviewed"  # unchanged
    assert row["review_state"] == "in_review"
    assert gate.latest_decision(conn, "s-a")["promoted"] == 0

    rpb.apply_manifest(conn, _manifest(conn, decision="rejected", ids=("s-b",)),
                       card_id="int-1", commit=True)
    rb = conn.execute(
        "SELECT verification_status FROM statements WHERE statement_id='s-b'"
    ).fetchone()
    assert rb["verification_status"] == "do_not_publish"                 # terminal downgrade

    # neither reaches the reviewer-internal serve.
    served = {r["statementId"] if "statementId" in r else r.get("statement_id")
              for r in read_api.reviewer_internal_records(conn)}
    assert "s-a" not in served and "s-b" not in served


# --- test 6: unregistered reviewer refused, nothing written (GOV-647 §2 P1) --

def test_unregistered_reviewer_refused(tmp_path: Path) -> None:
    p = tmp_path / "noreg.db"
    db.apply_migrations(p)
    c = db.open_db(p)
    _seed(c, register=False)          # reviewer:isaac NOT in the registry
    manifest = _manifest(c)
    with pytest.raises(gate.ReviewerGateError):
        rpb.apply_manifest(c, manifest, card_id="int-1", commit=True)
    assert c.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0] == 0
    c.close()

    # and the CLI surfaces it as a fail-closed non-zero exit.
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    rc = rpb.main(["--db", str(p), "apply", "--manifest", str(mpath),
                   "--card", "int-1", "--commit"])
    assert rc == rpb.EXIT_REFUSED


# --- test 7: forbidden automation/AI reviewer id refused pre-DB (GOV-647 §2) --

def test_forbidden_reviewer_id_refused(conn: sqlite3.Connection) -> None:
    for bad in ("automation", "ai", "gateway", ""):
        with pytest.raises(gate.ReviewerGateError):
            rpb.apply_manifest(conn, _manifest(conn), card_id="int-1",
                               commit=True, reviewer_id=bad)
    assert conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0] == 0


# --- test 8: scope gate refuses an out-of-slice statement, exit 2 (§7.3) ------

def test_scope_gate_refuses_out_of_slice(conn: sqlite3.Connection, db_path_registered,
                                         tmp_path: Path) -> None:
    manifest = _manifest(conn)
    manifest["statements"].append(
        {"statement_id": "s-out", "decision": "approved", "agenda_item_id": None}
    )
    with pytest.raises(rpb.PromotionScopeError):
        rpb.apply_manifest(conn, manifest, card_id="int-1", commit=True)
    assert conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0] == 0

    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    rc = rpb.main(["--db", str(db_path_registered), "apply", "--manifest", str(mpath),
                   "--card", "int-1", "--commit"])
    assert rc == rpb.EXIT_SCOPE       # no bypass flag exists


# --- test 9: oversized batch refused (GOV-647 §3, ≤50) -----------------------

def test_batch_over_ceiling_refused(conn: sqlite3.Connection) -> None:
    manifest = _manifest(conn)
    manifest["statements"] = [
        {"statement_id": f"x{i}", "decision": "approved"} for i in range(rpb.MAX_BATCH + 1)
    ]
    with pytest.raises(rpb.PromotionScopeError):
        rpb.apply_manifest(conn, manifest, card_id="int-1", commit=True)


# --- test 10: idempotent re-apply — 0 new rows, exit 0 (GOV-647 §4) ----------

def test_idempotent_reapply(conn: sqlite3.Connection) -> None:
    rpb.apply_manifest(conn, _manifest(conn), card_id="int-1", commit=True)
    n1 = conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0]
    assert n1 == 3

    summary = rpb.apply_manifest(conn, _manifest(conn), card_id="int-1", commit=True)
    n2 = conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0]
    assert n2 == n1                                   # 0 new ledger rows
    assert summary["counts"]["skipped_idempotent"] == 3
    assert summary["counts"]["promoted"] == 0


# --- test 11: promoted rows serve + anchoring increments cardCount (§5) -------

def test_board_renders_promoted_and_cardcount_increments(conn: sqlite3.Connection) -> None:
    # baseline: empty board, nothing reviewed.
    assert ab.agenda_board(conn)["cardCount"] == 0
    assert read_api.reviewer_internal_records(conn) == []

    # anchor s-a + s-b to one agenda item; leave s-c unanchored.
    manifest = _manifest(conn, anchor=True, ids=("s-a", "s-b"))
    manifest["statements"].append(
        {"statement_id": "s-c", "decision": "approved", "agenda_item_id": None}
    )
    rpb.apply_manifest(conn, manifest, card_id="int-1", commit=True)

    served_ids = {r.get("statementId", r.get("statement_id"))
                  for r in read_api.reviewer_internal_records(conn)}
    assert {"s-a", "s-b", "s-c"} <= served_ids        # all three promoted rows serve

    board = ab.agenda_board(conn)
    assert board["cardCount"] == 1                    # the one anchored agenda item
    assert board["unanchoredStatementCount"] == 1     # s-c disclosed, never dropped
    card = next(c for lane in board["lanes"] for c in lane["cards"])
    assert card["agendaItemId"] == AGENDA_ITEM_ID
    assert card["agendaItemTitle"] == "Call to Order and Roll Call"  # verbatim
    assert sorted(card["statementIds"]) == ["s-a", "s-b"]


# --- test 12: additive-only — no frozen surface re-implemented / bypassed -----

def test_cli_is_additive_only_no_bypass() -> None:
    src = (ROOT / "scripts" / "reviewer_promotion_batch.py").read_text(encoding="utf-8")
    # never re-implements a frozen serve/promotion function.
    for frozen_def in (
        "def reviewer_internal_records", "def promote_statement",
        "def agenda_board", "def compute_ui_status", "def build_cards",
    ):
        assert frozen_def not in src
    # never issues a bare status/publication UPDATE — only the gate transitions those.
    lowered = src.lower()
    assert "update statements set verification_status" not in lowered
    assert "update statements set review_state" not in lowered
    assert "publishable" not in lowered  # never flips publication_state
