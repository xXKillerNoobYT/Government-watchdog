"""Stage 5.05 Watchdog signals layer RED tests (GOV-520).

Prove, over a seeded reviewer-internal Alpine corpus that mirrors the real merged
registry shape (a corrected ``known_then`` record + its ``corrected_later`` superseding
record, a second corrected record with NO superseding ref, topic-anchored items across
two scan dates, and an ``ai_presented`` observation), that
``scripts/stage5_watchdog_signals.py``:

* builds a typed corrections ledger that resolves a real superseding ref AND fail-closed
  gaps an unresolvable one with ``correction_unresolved`` (§1, test 1);
* the correction-edge resolver is RED-proof load-bearing, non-tautologically — neuter it
  and the resolved-ref assertion goes RED while the read surface still serves both
  records (I5, test 2);
* ranks topics by a deterministic arithmetic salience score, floor-labels a thin topic
  ``insufficientData`` (§2, test 3);
* the salience scorer is RED-proof load-bearing, non-tautologically — neuter it and the
  ranking assertion goes RED while the read surface still serves the same items
  (I5, test 4);
* composes a Kanban-precursor watchdog view whose lanes are all in the frozen vocab,
  with source confidence + gap labels over source-linked records only (§3, test 5);
* is deterministic + idempotent — re-projection is byte-identical (I7, test 6);
* lets no raw vault path / 64-hex / ``file://`` cross the emitted body; ``localSourcePath``
  never appears (I1/I2, test 7);
* exposes exactly one envelope digest — no per-source raw hash (I3, test 8);
* never leaks any signal onto the public lane (I6, test 9);
* exposes a ``--check`` CLI that is sound (exit 0) and a CI gate (exit 1 on a defect)
  (test 10).

Pure sqlite + tmp files: no network, no real-corpus dependency. The seed mirrors
``tests/test_gov488_stage5_record_verify.py`` (GOV-488).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
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
import stage5_watchdog_signals as w  # noqa: E402  (under test — RED until it exists)

# --- real-shaped Alpine government locators -----------------------------------

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-05-11.pdf"
# The topic/issue anchors are agenda-item ids (the agenda thread a claim sits in —
# Isaac's "agenda item references topic"). Two threads exercise the salience ranking.
TOPIC_BUDGET = "agenda-budget"
TOPIC_ZONING = "agenda-zoning"
EARLY_DATE = "2026-01-05"   # older scan (outside the recency window from the anchor)
RECENT_DATE = "2026-04-13"  # recent scan (the corpus anchor)


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


def _promote(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    to_source_id: str,
    original_url: str,
    scan_date: str,
    produced_by: str = "human",
    run_id: str | None = None,
    agenda_item_id: str | None = None,
) -> None:
    """Insert + reviewer-promote a source-linked statement (the GOV-146 serve gate)."""
    record = {
        "statement_id": statement_id,
        "agenda_item_id": agenda_item_id,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": produced_by,
    }
    if produced_by == "ai":
        if conn.execute(
            "SELECT 1 FROM ai_extraction_runs WHERE run_id=?", (run_id,)
        ).fetchone() is None:
            ai.create_run(conn, run_id=run_id, input_source_ids=[])
        record["ai_extraction_run_id"] = run_id
    link = {
        "to_source_id": to_source_id,
        "relation": "substantiates",
        "original_url": original_url,
        "final_url": original_url,
        "archive_status": "not_checked",
        "scan_date": scan_date,
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
    """A corrected record + its superseding record, an unresolved correction, topics."""
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
        registered_by="owner:isaac", note="GOV-520 watchdog-signals seed",
    )
    # Two agenda threads (the topic/issue anchors) under one meeting.
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, ?, 'Town Council', '2026-04-13T00:00:00Z')",
        (RECENT_DATE,),
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES (?, 1, 1, 'FY27 Budget')",
        (TOPIC_BUDGET,),
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES (?, 1, 2, 'Zoning Variance')",
        (TOPIC_ZONING,),
    )
    conn.commit()

    # A corrected known_then record on the BUDGET thread, recent scan.
    _promote(
        conn, "stmt-corrected", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    # Its superseding corrected_later record (points back via updates_statement_id).
    _promote(
        conn, "stmt-superseding", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET correction_status='corrected' WHERE statement_id='stmt-corrected'"
    )
    conn.execute(
        "UPDATE statements SET layer='corrected_later', updates_statement_id='stmt-corrected' "
        "WHERE statement_id='stmt-superseding'"
    )
    # A SECOND corrected record with NO superseding ref served -> must gap fail-closed.
    _promote(
        conn, "stmt-orphan-correction", to_source_id=AGENDA_SOURCE,
        original_url=AGENDA_URL, scan_date=EARLY_DATE, agenda_item_id=TOPIC_ZONING,
    )
    conn.execute(
        "UPDATE statements SET correction_status='superseded' "
        "WHERE statement_id='stmt-orphan-correction'"
    )
    # A plain ai_presented observation on the BUDGET thread (raises activity/churn-free).
    _promote(
        conn, "stmt-ai", to_source_id=MINUTES_SOURCE, produced_by="ai", run_id="run-ai-1",
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    conn.commit()


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


# --- test 1: corrections ledger resolves + fail-closed gaps (§1) --------------


def test_corrections_ledger_resolves_and_gaps(conn: sqlite3.Connection) -> None:
    ledger = w.build_corrections_ledger(conn)
    by_id = {e["correctedStatementId"]: e for e in ledger}

    # the two corrected records are present; the superseding (non-corrected) one is not.
    assert set(by_id) == {"stmt-corrected", "stmt-orphan-correction"}

    resolved = by_id["stmt-corrected"]
    assert resolved["resolved"] is True
    assert resolved["supersedingStatementId"] == "stmt-superseding"
    assert resolved["supersedingRef"] is not None
    assert resolved["gaps"] == []
    # known-then context preserved (correctionStatus carried, not rewritten).
    assert resolved["correctionStatus"] == "corrected"
    assert resolved["knownThen"]["recordDate"] == RECENT_DATE

    gapped = by_id["stmt-orphan-correction"]
    assert gapped["resolved"] is False
    assert gapped["supersedingRef"] is None
    assert gapped["gaps"] == [w.GAP_CORRECTION_UNRESOLVED]


# --- test 2: correction-edge resolver is RED-proof, non-tautological (I5) ------


def test_resolver_is_red_proof_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    # GREEN with the real resolver: the superseding ref resolves.
    ledger = w.build_corrections_ledger(conn)
    edge = next(e for e in ledger if e["correctedStatementId"] == "stmt-corrected")
    assert edge["resolved"] is True and edge["supersedingRef"] is not None

    # The read surface still serves BOTH records — the layer's input is unchanged.
    served_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    assert {"stmt-corrected", "stmt-superseding"} <= served_ids

    # Neuter ONLY the resolver: the superseding spine vanishes -> the edge falls to the
    # fail-closed gap, even though the read surface is untouched. Non-tautological RED.
    monkeypatch.setattr(w, "resolve_correction_edge", lambda *a, **k: None)
    neutered = w.build_corrections_ledger(conn)
    edge2 = next(e for e in neutered if e["correctedStatementId"] == "stmt-corrected")
    assert edge2["resolved"] is False
    assert edge2["supersedingRef"] is None
    assert edge2["gaps"] == [w.GAP_CORRECTION_UNRESOLVED]
    # the read surface STILL serves both records (proves the RED came from the resolver).
    assert {"stmt-corrected", "stmt-superseding"} <= {
        r["statement_id"] for r in read_api.reviewer_internal_records(conn)
    }


# --- test 3: hot topics ranked by arithmetic salience + floor label (§2) -------


def test_hot_topics_ranked_with_floor(conn: sqlite3.Connection) -> None:
    topics = w.build_hot_topics(conn)
    by_id = {t["topicId"]: t for t in topics}
    assert set(by_id) == {TOPIC_BUDGET, TOPIC_ZONING}

    budget = by_id[TOPIC_BUDGET]
    # BUDGET: 3 items (corrected + superseding + ai), all recent, 1 is a corrected record.
    assert budget["activityCount"] == 3
    assert budget["recencyCount"] == 3
    assert budget["correctionChurn"] == 1
    assert budget["salienceScore"] == w.salience_score(3, 3, 1)
    assert budget["salienceLabel"] == w.SALIENCE_RANKED

    # ZONING: 1 item, old scan (outside window), itself a corrected record.
    zoning = by_id[TOPIC_ZONING]
    assert zoning["activityCount"] == 1
    assert zoning["recencyCount"] == 0
    assert zoning["correctionChurn"] == 1
    assert zoning["salienceLabel"] == w.SALIENCE_INSUFFICIENT

    # ranking is by score desc -> BUDGET (higher) before ZONING.
    assert [t["topicId"] for t in topics] == [TOPIC_BUDGET, TOPIC_ZONING]


# --- test 4: salience scorer is RED-proof, non-tautological (I5) ---------------


def test_salience_scorer_is_red_proof_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    topics = w.build_hot_topics(conn)
    assert [t["topicId"] for t in topics] == [TOPIC_BUDGET, TOPIC_ZONING]
    assert topics[0]["salienceScore"] > topics[1]["salienceScore"]

    # The read surface still yields the same topic-anchored records.
    anchors = {
        a
        for r in read_api.reviewer_internal_records(conn)
        for a in w._record_topic_anchors(r)
    }
    assert {TOPIC_BUDGET, TOPIC_ZONING} <= anchors

    # Neuter ONLY the scorer to a constant: every score flattens, so the EMITTED ranking
    # assertion can no longer see a strict BUDGET>ZONING order. Non-tautological RED.
    monkeypatch.setattr(w, "salience_score", lambda activity, recency, churn: 0)
    flat = w.build_hot_topics(conn)
    assert all(t["salienceScore"] == 0 for t in flat)
    # the strict ordering the real scorer guaranteed is gone (all equal).
    assert not (flat[0]["salienceScore"] > flat[1]["salienceScore"])


# --- test 5: watchdog view lanes + confidence + gaps (§3) ----------------------


def test_watchdog_view_lanes_and_gaps(conn: sqlite3.Connection) -> None:
    view = w.build_watchdog_view(conn)
    by_stmt = {e["statementId"]: e for e in view}

    # every lane in the frozen vocab; every entry source-linked with a confidence label.
    for entry in view:
        assert entry["lane"] in w.WATCHDOG_LANES
        assert entry["sourceLinked"] is True
        assert entry["sourceConfidence"] is not None

    # the corrected records land in the correction lane.
    assert by_stmt["stmt-corrected"]["lane"] == w.LANE_CORRECTION
    assert by_stmt["stmt-orphan-correction"]["lane"] == w.LANE_CORRECTION
    # the unresolved correction carries the fail-closed gap in the view too.
    assert w.GAP_CORRECTION_UNRESOLVED in by_stmt["stmt-orphan-correction"]["gaps"]
    # the ai_presented observation awaits a verification decision.
    assert by_stmt["stmt-ai"]["lane"] == w.LANE_PENDING_DECISION


# --- test 6: deterministic + idempotent (I7) ----------------------------------


def test_idempotent_byte_identical(conn: sqlite3.Connection) -> None:
    first = json.dumps(w.build_signals(conn), sort_keys=True)
    second = json.dumps(w.build_signals(conn), sort_keys=True)
    assert first == second


# --- test 7: no raw path / vault marker / file:// leak; no localSourcePath (I1/I2) -


def test_no_raw_leak_no_local_source_path(conn: sqlite3.Connection) -> None:
    body = w.build_signals(conn)  # build_signals sweeps via assert_no_raw_paths
    # re-assert explicitly for clarity.
    read_api.assert_no_raw_paths(body)
    for text in read_api._iter_strings(body):
        assert "localSourcePath" not in text
        assert "Source-Data" not in text
        assert "Obsidian" not in text
        assert not text.startswith("file://")


# --- test 8: exactly one envelope digest, no per-source raw hash (I3) ----------


def test_single_envelope_digest(conn: sqlite3.Connection) -> None:
    body = w.build_signals(conn)
    assert _is_hex64(body["watchdogDigest"])
    assert w.assert_single_envelope_digest(body) is True
    # no 64-hex anywhere else in the body.
    for key, value in body.items():
        if key == "watchdogDigest":
            continue
        for text in read_api._iter_strings(value):
            assert not _is_hex64(text), f"stray 64-hex under {key}: {text}"


# --- test 9: never leaks onto the public lane (I6) ----------------------------


def test_absent_from_public_lane(conn: sqlite3.Connection) -> None:
    body = w.build_signals(conn)
    assert body["access"] == "reviewer_internal"
    assert body["scope"] == "alpine"
    # the public lane stays empty — reviewer-internal records are not published.
    assert read_api.published_records(conn) == []


# --- test 10: --check CLI is sound (exit 0) and a CI gate (exit 1 on defect) ----


def test_cli_check_gate(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # the seeded fixture DB path (the conn fixture already migrated + seeded it).
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    env_python = sys.executable
    script = str(ROOT / "scripts" / "stage5_watchdog_signals.py")

    sound = subprocess.run(
        [env_python, script, "--db", str(db_path), "--check"],
        capture_output=True, text=True,
    )
    assert sound.returncode == 0, sound.stderr
    payload = json.loads(sound.stdout)
    assert payload["access"] == "reviewer_internal"
    assert _is_hex64(payload["watchdogDigest"])

    # a defect (out-of-vocab lane) trips the gate.
    body = w.build_signals(conn)
    if body["watchdogView"]:
        body["watchdogView"][0]["lane"] = "not-a-lane"
        with pytest.raises(w.WatchdogSignalsError):
            w.assert_lanes_valid(body)


# --- test 11: in-vocab guards over the live body ------------------------------


def test_guards_pass_on_live_body(conn: sqlite3.Connection) -> None:
    body = w.check_signals(conn)
    assert w.assert_lanes_valid(body) is True
    assert w.assert_corrections_resolved_or_gapped(body) is True
    assert w.assert_hot_topics_ranked(body) is True
