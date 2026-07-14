"""GOV-698 — agenda-anchoring batch CLI tests (Option-A pilot, anchoring leg 2 of 6).

Proves ``scripts/agenda_anchor_batch.py`` (a) extracts top-level agenda items from
a meeting's own revised agenda via the deterministic strict-increment marker
grammar and (b) anchors the pre-promoted ``reviewed_source_linked`` statements to
those items by pure reviewer-confirmed timestamp containment — fail-closed,
write-once, and idempotent, WITHOUT touching any frozen surface:

* the marker grammar accepts strictly incrementing top-level markers, excludes
  nested restarting enumerations, handles inline + marker-alone layouts, and emits
  exact line-span citations (tests 1a–1c);
* ``propose`` re-verifies the agenda-doc sha256 and emits a read-only manifest with
  a *null* range table; a drifted raw file fails closed (tests 2 + 3);
* the range table is validated half-open / non-overlapping / monotonic, and bad
  tables abort (test 4);
* ``apply --commit`` anchors by containment, inserts agenda_items with full
  provenance (source_document_id + citation_target), and discloses (never guesses)
  unanchored statements (test 5);
* the statement write is a WRITE-ONCE narrow ``agenda_item_id`` UPDATE — every
  other column is identical pre/post (test 6);
* dry-run (the default) writes nothing (test 7);
* re-running is idempotent (already_anchored), and re-anchoring to a different item
  is a hard error (tests 8 + 9);
* an empty / forbidden / wrong reviewer id is refused (test 10);
* source drift (doc sha256 / statement-set change) and a batch over the ceiling and
  a manifest-sha256 mismatch are all refused (tests 11–13);
* once anchored, ``read_api.reviewer_internal_records`` still serves the rows and
  ``stage5_agenda_board`` yields real cards keyed on the anchored agenda_item_id,
  with unanchored rows disclosed in ``unanchoredStatementCount`` (test 14);
* the CLI is additive-only: it re-implements no frozen serve/gate function and
  issues no bare status/publication UPDATE (test 15).

Pure sqlite + tmp files: no network, no real-corpus text committed. The agenda
fixture is synthetic but reproduces the exact grammar of the real 2026-06-23 doc.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agenda_anchor_batch as aab  # noqa: E402  (under test)
import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402
import stage5_agenda_board as ab  # noqa: E402
import statements as st  # noqa: E402

SOURCE_ID = aab.AGENDA_DOC_SOURCE_ID
TRANSCRIPT_ID = 33
MEETING_ID = aab.PILOT_MEETING_ID
DATE = aab.PILOT_MEETING_DATE
ORIGINAL_URL = "https://www.youtube.com/watch?v=localdoc142"

# A synthetic agenda that reproduces the real grammar: marker-alone titles, a
# nested restarting enumeration that MUST be excluded, and inline markers.
AGENDA_TEXT = """TOWN OF ALPINE — REGULAR MEETING (synthetic fixture)
June 23, 2026

1.

CALL TO ORDER - Mayor Green

2.

ROLL CALL – Monica Chenault

3.

ACTION ITEMS
a. Consider adopting the amended budget, including:
1. - The use of Six Hundred Thousand Dollars from the Water Fund; and
2. - The use of Five Hundred Dollars from the Sewer Fund.
b. Consider a liquor license application.
4. PUBLIC COMMENT
5. ADJOURNMENT
"""

# ts -> (expected item after ranges below). Statement s3 (ts 999) is unanchorable.
STATEMENTS = [
    ("stmt:localdoc-142:seg-0000", "localdoc-142:seg-0000", 0, 5, "00:00:05"),
    ("stmt:localdoc-142:seg-0001", "localdoc-142:seg-0001", 1, 20, "00:00:20"),
    ("stmt:localdoc-142:seg-0002", "localdoc-142:seg-0002", 2, 40, "00:00:40"),
    ("stmt:localdoc-142:seg-0003", "localdoc-142:seg-0003", 3, 999, "00:16:39"),
]

# Reviewer-confirmed ranges (half-open) filled onto the manifest before apply.
RANGES = {
    "agi:m129:doc-137:item-01": (0, 10),
    "agi:m129:doc-137:item-02": (10, 30),
    "agi:m129:doc-137:item-03": (30, 50),
}


def _link() -> dict:
    return {
        "to_source_id": SOURCE_ID,
        "relation": "substantiates",
        "original_url": ORIGINAL_URL,
        "final_url": ORIGINAL_URL,
        "archive_status": "not_checked",
        "scan_date": DATE,
        "captured_at_utc": "2026-06-24T12:00:00Z",
        "locator_kind": "page",
        "page": 1,
        "verification_status": "human_verified",
        "confidence": "high",
    }


def _write_agenda(corpus_root: Path) -> tuple[str, str]:
    """Write the synthetic agenda under a content-addressed path; return path+sha."""
    sha = hashlib.sha256(AGENDA_TEXT.encode("utf-8")).hexdigest()
    rel = f"Raw-Corpus/{sha[:2]}/{sha}.txt"
    abspath = corpus_root / rel
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_text(AGENDA_TEXT, encoding="utf-8")
    return rel, sha


def _seed(conn: sqlite3.Connection, corpus_root: Path) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Alpine local corpus', "
        "'alpine', 'video_transcript', 'official', 'official', ?)",
        (SOURCE_ID, ORIGINAL_URL),
    )
    gate.register_reviewer(
        conn, aab.REVIEWER_ID, display_name="Isaac",
        registered_by="owner:isaac (card 7b606128 / GOV-652)", note="GOV-698 pilot seed",
    )
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc) VALUES (?, 'localdoc-142', ?, ?, 'full', "
        "'Raw-Corpus/tx.txt', ?, '2026-06-24T00:00:00Z')",
        (TRANSCRIPT_ID, ORIGINAL_URL, DATE, "t" * 64),
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, title, source_url, transcript_id, "
        "fetch_time_utc) VALUES (?, ?, 'Town of Alpine', 'Regular Meeting', ?, ?, "
        "'2026-06-24T00:00:00Z')",
        (MEETING_ID, DATE, ORIGINAL_URL, TRANSCRIPT_ID),
    )
    rel, sha = _write_agenda(corpus_root)
    conn.execute(
        "INSERT INTO documents (id, source_url, title, doc_type, doc_date, local_path, "
        "sha256, fetch_time_utc, source_id) VALUES "
        "(?, 'https://alpine/agenda-revised', 'MEET-Agenda_jun23_council_REVISED-MON.txt', "
        "'agenda', ?, ?, ?, '2026-06-24T00:00:00Z', ?)",
        (aab.AGENDA_DOC_ID, DATE, rel, sha, SOURCE_ID),
    )
    for sid, seg, idx, ts, human in STATEMENTS:
        conn.execute(
            "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
            "timestamp_seconds, timestamp_human, segment_text) VALUES (?, ?, ?, ?, ?, ?)",
            (seg, TRANSCRIPT_ID, idx, ts, human, f"segment text for {seg}"),
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
        # Promote to reviewed_source_linked via the frozen gate (mirrors GOV-650).
        gate.promote_statement(
            conn, sid, reviewer_id=aab.REVIEWER_ID, decision="approved",
            reason="GOV-650 pilot promotion", to_verification_status="reviewed_source_linked",
            reason_category="promotion-card:test", commit=False,
        )
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
    m = aab.build_manifest(conn, corpus_root=corpus_root)
    for it in m["agenda_items"]:
        rng = RANGES.get(it["agenda_item_id"])
        if rng:
            it["range_start_s"], it["range_end_s"] = rng
    return m


# --- test 1a: strict-increment grammar excludes nested enumerations ----------

def test_grammar_strict_increment_excludes_nested() -> None:
    items = aab.extract_agenda_items(AGENDA_TEXT)
    assert [it["item_order"] for it in items] == [1, 2, 3, 4, 5]
    titles = [it["title"] for it in items]
    assert titles == [
        "CALL TO ORDER - Mayor Green",
        "ROLL CALL – Monica Chenault",
        "ACTION ITEMS",          # nested "1." / "2." budget clauses excluded
        "PUBLIC COMMENT",        # inline marker
        "ADJOURNMENT",           # inline marker
    ]


# --- test 1b: exact line-span citations ---------------------------------------

def test_grammar_citation_spans() -> None:
    items = {it["item_order"]: it for it in aab.extract_agenda_items(AGENDA_TEXT)}
    # marker-alone spans from the marker line through the first non-empty title line.
    assert items[1]["citation_target"] == "lines:4-6"
    # inline markers cite a single line.
    assert items[4]["line_start"] == items[4]["line_end"]
    assert items[4]["citation_target"].startswith("lines:")


# --- test 1c: a marker with no title fails closed -----------------------------

def test_grammar_untitled_marker_fails_closed() -> None:
    with pytest.raises(aab.AnchorScopeError):
        aab.extract_agenda_items("1.\n\n")


# --- test 2: propose is read-only, deterministic, null ranges -----------------

def test_propose_readonly_and_null_ranges(env) -> None:
    conn, corpus_root, _ = env
    before = conn.execute("SELECT COUNT(*) FROM agenda_items").fetchone()[0]
    m1 = aab.build_manifest(conn, corpus_root=corpus_root)
    m2 = aab.build_manifest(conn, corpus_root=corpus_root)
    after = conn.execute("SELECT COUNT(*) FROM agenda_items").fetchone()[0]
    assert before == after == 0                                  # no writes
    assert json.dumps(m1, sort_keys=True) == json.dumps(m2, sort_keys=True)
    assert m1["counts"] == {"agenda_items": 5, "statements": 4}
    for it in m1["agenda_items"]:
        assert it["range_start_s"] is None and it["range_end_s"] is None
        assert it["source_document_id"] == aab.AGENDA_DOC_ID
    # statements carry the timestamp used by the containment rule, in ts order.
    assert [s["timestamp_seconds"] for s in m1["statements"]] == [5, 20, 40, 999]


# --- test 3: a drifted agenda raw file fails closed (source_changed) ----------

def test_propose_source_changed_fails_closed(env) -> None:
    conn, corpus_root, _ = env
    row = conn.execute(
        "SELECT local_path FROM documents WHERE id = ?", (aab.AGENDA_DOC_ID,)
    ).fetchone()
    (corpus_root / row["local_path"]).write_text("TAMPERED", encoding="utf-8")
    with pytest.raises(aab.AnchorRefusedError):
        aab.build_manifest(conn, corpus_root=corpus_root)


# --- test 4: range-table validation ------------------------------------------

def test_range_validation(env) -> None:
    conn, corpus_root, _ = env
    m = aab.build_manifest(conn, corpus_root=corpus_root)
    items = {it["agenda_item_id"]: it for it in m["agenda_items"]}

    # overlapping ranges abort.
    items["agi:m129:doc-137:item-01"]["range_start_s"] = 0
    items["agi:m129:doc-137:item-01"]["range_end_s"] = 30
    items["agi:m129:doc-137:item-02"]["range_start_s"] = 20
    items["agi:m129:doc-137:item-02"]["range_end_s"] = 40
    with pytest.raises(aab.AnchorScopeError):
        aab.validated_ranges(m)

    # empty [start,end) aborts.
    m2 = aab.build_manifest(conn, corpus_root=corpus_root)
    it = m2["agenda_items"][0]
    it["range_start_s"], it["range_end_s"] = 10, 10
    with pytest.raises(aab.AnchorScopeError):
        aab.validated_ranges(m2)

    # half-filled aborts.
    m3 = aab.build_manifest(conn, corpus_root=corpus_root)
    m3["agenda_items"][0]["range_start_s"] = 5
    with pytest.raises(aab.AnchorScopeError):
        aab.validated_ranges(m3)


# --- test 5: apply --commit anchors by containment + full provenance ----------

def test_apply_commit_anchors_and_discloses_unanchored(env) -> None:
    conn, corpus_root, _ = env
    m = _filled_manifest(conn, corpus_root)
    report = aab.apply_manifest(conn, m, card_id="int-anchor-1", commit=True)

    c = report["counts"]
    assert c["agenda_items_inserted"] == 5          # all extracted items materialised
    assert c["anchored"] == 3                        # ts 5,20,40 -> items 1,2,3
    assert c["unanchored_remaining"] == 1            # ts 999 disclosed, never guessed
    assert report["unanchored_statement_ids"] == ["stmt:localdoc-142:seg-0003"]

    # anchors landed exactly where containment says.
    got = dict(conn.execute(
        "SELECT statement_id, agenda_item_id FROM statements "
        "WHERE agenda_item_id IS NOT NULL"
    ).fetchall())
    assert got == {
        "stmt:localdoc-142:seg-0000": "agi:m129:doc-137:item-01",
        "stmt:localdoc-142:seg-0001": "agi:m129:doc-137:item-02",
        "stmt:localdoc-142:seg-0002": "agi:m129:doc-137:item-03",
    }
    # agenda_items carry additive provenance (0020 columns).
    prov = conn.execute(
        "SELECT source_document_id, citation_target FROM agenda_items "
        "WHERE agenda_item_id = 'agi:m129:doc-137:item-01'"
    ).fetchone()
    assert prov["source_document_id"] == aab.AGENDA_DOC_ID
    assert prov["citation_target"] == "lines:4-6"


# --- test 6: the statement write is a WRITE-ONCE narrow UPDATE ----------------

def test_write_once_narrow_update_touches_only_anchor(env) -> None:
    conn, corpus_root, _ = env
    cols = ", ".join(aab._IMMUTABLE_STATEMENT_COLUMNS)
    before = {
        r["statement_id"]: dict(r) for r in conn.execute(
            f"SELECT statement_id, {cols} FROM statements"
        ).fetchall()
    }
    aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                       card_id="int-1", commit=True)
    after = {
        r["statement_id"]: dict(r) for r in conn.execute(
            f"SELECT statement_id, {cols} FROM statements"
        ).fetchall()
    }
    assert before == after                              # nothing but agenda_item_id moved
    # every anchored row is still reviewed_source_linked + not_publishable.
    for sid in ("stmt:localdoc-142:seg-0000", "stmt:localdoc-142:seg-0001"):
        row = conn.execute(
            "SELECT verification_status, publication_state FROM statements "
            "WHERE statement_id = ?", (sid,)
        ).fetchone()
        assert row["verification_status"] == "reviewed_source_linked"
        assert row["publication_state"] == "not_publishable"


# --- test 7: dry-run is the default and writes nothing ------------------------

def test_dry_run_default_writes_nothing(env) -> None:
    conn, corpus_root, _ = env
    report = aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                                card_id="int-1", commit=False)
    assert report["dry_run"] is True
    assert report["counts"]["anchored"] == 3            # reports the plan
    assert conn.execute("SELECT COUNT(*) FROM agenda_items").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM statements WHERE agenda_item_id IS NOT NULL"
    ).fetchone()[0] == 0


# --- test 8: re-run is idempotent (already_anchored) --------------------------

def test_reapply_idempotent(env) -> None:
    conn, corpus_root, _ = env
    m = _filled_manifest(conn, corpus_root)
    aab.apply_manifest(conn, m, card_id="int-1", commit=True)
    again = aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                               card_id="int-1", commit=True)
    assert again["counts"]["anchored"] == 0
    assert again["counts"]["already_anchored"] == 3
    assert again["counts"]["agenda_items_inserted"] == 0   # rows already present


# --- test 9: re-anchoring to a DIFFERENT item is a hard error ------------------

def test_reanchor_to_different_item_refused(env) -> None:
    conn, corpus_root, _ = env
    aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                       card_id="int-1", commit=True)
    # shift item-01's range so ts 5 would now fall in item-02 -> re-anchor attempt.
    m = aab.build_manifest(conn, corpus_root=corpus_root)
    for it in m["agenda_items"]:
        if it["agenda_item_id"] == "agi:m129:doc-137:item-02":
            it["range_start_s"], it["range_end_s"] = 0, 10   # now contains ts 5
    with pytest.raises(aab.AnchorRefusedError):
        aab.apply_manifest(conn, m, card_id="int-1", commit=True)
    # nothing changed (atomic rollback): ts 5 still on item-01.
    row = conn.execute(
        "SELECT agenda_item_id FROM statements WHERE statement_id='stmt:localdoc-142:seg-0000'"
    ).fetchone()
    assert row["agenda_item_id"] == "agi:m129:doc-137:item-01"


# --- test 10: reviewer identity gate ------------------------------------------

@pytest.mark.parametrize("bad", ["", "automation", "reviewer:ai", "reviewer:notisaac"])
def test_reviewer_identity_refused(env, bad) -> None:
    conn, corpus_root, _ = env
    with pytest.raises(aab.AnchorRefusedError):
        aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                           card_id="int-1", commit=True, reviewer_id=bad)
    assert conn.execute("SELECT COUNT(*) FROM agenda_items").fetchone()[0] == 0


# --- test 11: source drift (doc sha / statement set) refused ------------------

def test_apply_refuses_doc_sha_drift(env) -> None:
    conn, corpus_root, _ = env
    m = _filled_manifest(conn, corpus_root)
    conn.execute("UPDATE documents SET sha256 = ? WHERE id = ?",
                 ("z" * 64, aab.AGENDA_DOC_ID))
    conn.commit()
    with pytest.raises(aab.AnchorRefusedError):
        aab.apply_manifest(conn, m, card_id="int-1", commit=True)


def test_apply_refuses_statement_set_drift(env) -> None:
    conn, corpus_root, _ = env
    m = _filled_manifest(conn, corpus_root)
    # a new reviewed_source_linked row makes the live set differ from the manifest.
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('localdoc-142:seg-0099', ?, 99, 12, '00:00:12', 'x')", (TRANSCRIPT_ID,),
    )
    st.insert_statement(conn, {
        "statement_id": "stmt:localdoc-142:seg-0099", "segment_id": "localdoc-142:seg-0099",
        "statement_text": "extra", "verification_status": "reviewed_source_linked",
        "produced_by": "automation", "is_verbatim": 1,
    }, [_link()])
    conn.commit()
    with pytest.raises(aab.AnchorRefusedError):
        aab.apply_manifest(conn, m, card_id="int-1", commit=True)


# --- test 12: batch over the ceiling refused ----------------------------------

def test_batch_over_ceiling_refused(env) -> None:
    conn, corpus_root, _ = env
    m = _filled_manifest(conn, corpus_root)
    m["statements"] = m["statements"] * 20   # > MAX_BATCH
    with pytest.raises(aab.AnchorScopeError):
        aab.apply_manifest(conn, m, card_id="int-1", commit=True)


# --- test 13: manifest sha256 mismatch guard ----------------------------------

def test_manifest_sha256_guard(env) -> None:
    conn, corpus_root, _ = env
    m = _filled_manifest(conn, corpus_root)
    good = aab.canonical_manifest_sha256(m)
    # matching hash is accepted (dry-run).
    ok = aab.apply_manifest(conn, m, card_id="int-1", commit=False,
                            expect_manifest_sha256=good)
    assert ok["manifest_sha256"] == good
    # a wrong card-bound hash is refused.
    with pytest.raises(aab.AnchorRefusedError):
        aab.apply_manifest(conn, m, card_id="int-1", commit=False,
                           expect_manifest_sha256="0" * 64)


# --- test 14: board renders real cards; unanchored disclosed ------------------

def test_board_yields_cards_and_discloses_unanchored(env) -> None:
    conn, corpus_root, _ = env
    # baseline: rows serve reviewer-internally but produce no cards (all unanchored).
    assert len(read_api.reviewer_internal_records(conn)) == 4
    assert ab.agenda_board(conn)["cardCount"] == 0
    assert ab.agenda_board(conn)["unanchoredStatementCount"] == 4

    aab.apply_manifest(conn, _filled_manifest(conn, corpus_root),
                       card_id="int-1", commit=True)

    board = ab.agenda_board(conn)
    assert board["cardCount"] == 3                       # items 1,2,3 each get a card
    assert board["unanchoredStatementCount"] == 1        # ts 999 still disclosed
    card_ids = {c["agendaItemId"] for lane in board["lanes"] for c in lane["cards"]}
    assert card_ids == {
        "agi:m129:doc-137:item-01",
        "agi:m129:doc-137:item-02",
        "agi:m129:doc-137:item-03",
    }
    # all four rows still serve reviewer-internally (nothing dropped).
    assert len(read_api.reviewer_internal_records(conn)) == 4


# --- test 15: additive-only — no frozen surface re-implemented / bypassed -----

def test_cli_is_additive_only_no_bypass() -> None:
    src = (ROOT / "scripts" / "agenda_anchor_batch.py").read_text(encoding="utf-8")
    for frozen_def in (
        "def reviewer_internal_records", "def promote_statement",
        "def agenda_board", "def compute_ui_status", "def build_cards",
    ):
        assert frozen_def not in src
    lowered = src.lower()
    # the ONLY statements write is the narrow write-once agenda_item_id UPDATE.
    assert "update statements set verification_status" not in lowered
    assert "update statements set review_state" not in lowered
    assert "update statements set publication_state" not in lowered
    # never assigns the 'publishable' value (a bare doc mention of not_publishable is fine).
    assert "'publishable'" not in lowered
    assert "update statements set agenda_item_id" in lowered
