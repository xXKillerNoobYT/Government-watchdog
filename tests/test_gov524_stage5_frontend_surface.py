"""Stage 5.06 frontend/product surface contract RED tests (GOV-524).

Prove, over the same seeded reviewer-internal Alpine corpus the 5.05 backbone uses (a
corrected ``known_then`` record + its superseding record, a second corrected record with
NO superseding ref, two agenda-thread topic anchors, and an ``ai_presented`` observation),
that ``scripts/stage5_frontend_surface.py``:

* projects a corrections surface whose resolved card links the superseding ref and whose
  unresolved card renders the fail-closed ``correction_unresolved`` gap as a VISIBLE badge
  (§1, test 1);
* the gap presenter is RED-proof load-bearing, non-tautologically — neuter it and the
  gaps-visible guard goes RED while the 5.05 envelope still carries the gaps (I5, test 2);
* projects ranked hot-topic cards that disclose the agenda-thread anchor honestly — no
  card implies a topic edge that isn't in the data (§2, test 3);
* the anchor classifier is RED-proof load-bearing, non-tautologically — neuter it to claim
  a topic edge and the honest-anchor guard goes RED while the read surface + hotTopics
  envelope are byte-unchanged (I5, test 4);
* groups the watchdog board into all six frozen lanes in order, corrected records in the
  correction column and the ai_presented record in pending-decision (§3, test 5);
* never presents an unverified / AI record as ``Verified`` (safety, test 6);
* is deterministic + idempotent — re-projection is byte-identical (I7, test 7);
* lets no raw vault path / file:// cross the body; ``localSourcePath`` never appears
  (I1/I2, test 8);
* exposes exactly one envelope digest — no per-source raw hash (I3, test 9);
* never leaks onto the public lane (I6, test 10);
* exposes a ``--check`` CLI that is sound (exit 0) and a CI gate (exit 1 on a defect)
  (test 11);
* every load-bearing guard is GREEN on the live body (test 12).

Pure sqlite + tmp files: no network, no real-corpus dependency. The seed mirrors
``tests/test_gov520_stage5_watchdog_signals.py`` (GOV-520).
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
import stage5_watchdog_signals as w  # noqa: E402
import stage5_frontend_surface as fs  # noqa: E402  (under test — RED until it exists)

# --- real-shaped Alpine government locators (mirrors GOV-520 seed) -------------

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-05-11.pdf"
TOPIC_BUDGET = "agenda-budget"
TOPIC_ZONING = "agenda-zoning"
EARLY_DATE = "2026-01-05"
RECENT_DATE = "2026-04-13"


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
        registered_by="owner:isaac", note="GOV-524 frontend-surface seed",
    )
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

    _promote(
        conn, "stmt-corrected", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
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
    _promote(
        conn, "stmt-orphan-correction", to_source_id=AGENDA_SOURCE,
        original_url=AGENDA_URL, scan_date=EARLY_DATE, agenda_item_id=TOPIC_ZONING,
    )
    conn.execute(
        "UPDATE statements SET correction_status='superseded' "
        "WHERE statement_id='stmt-orphan-correction'"
    )
    _promote(
        conn, "stmt-ai", to_source_id=MINUTES_SOURCE, produced_by="ai", run_id="run-ai-1",
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    conn.commit()


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


# --- test 1: corrections surface resolves + renders the visible gap (§1) -------


def test_corrections_surface_resolves_and_shows_gap(conn: sqlite3.Connection) -> None:
    cards = fs.build_corrections_surface(w.build_corrections_ledger(conn))
    by_id = {c["correctedStatementId"]: c for c in cards}
    assert set(by_id) == {"stmt-corrected", "stmt-orphan-correction"}

    resolved = by_id["stmt-corrected"]
    assert resolved["resolved"] is True
    assert resolved["supersedingRef"] is not None
    assert resolved["resolutionBadge"] == fs.RESOLUTION_LINKED
    assert resolved["gapBadges"] == []
    # corrected status surfaced with the exact badge; known-then preserved.
    assert resolved["correctionStatusBadge"] == "Corrected"
    assert resolved["knownThen"]["recordDate"] == RECENT_DATE

    gapped = by_id["stmt-orphan-correction"]
    assert gapped["resolved"] is False
    assert gapped["supersedingRef"] is None
    assert gapped["resolutionBadge"] == fs.RESOLUTION_UNRESOLVED
    # the fail-closed gap is rendered as a VISIBLE human badge (never hidden).
    assert fs.GAP_BADGES[w.GAP_CORRECTION_UNRESOLVED] in gapped["gapBadges"]


# --- test 2: gap presenter is RED-proof, non-tautological (I5) -----------------


def test_gap_presenter_is_red_proof_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    envelope = w.build_signals(conn)
    body = fs.build_surface(conn)
    # GREEN: the unresolved correction's gap is visible and the guard passes.
    assert fs.assert_gaps_visible(body, envelope) is True
    gapped = next(
        c for c in body["correctionsSurface"]
        if c["correctedStatementId"] == "stmt-orphan-correction"
    )
    assert gapped["gapBadges"]  # non-empty

    # the 5.05 envelope still carries the gap — the layer's input is unchanged.
    edge = next(
        e for e in envelope["correctionsLedger"]
        if e["correctedStatementId"] == "stmt-orphan-correction"
    )
    assert w.GAP_CORRECTION_UNRESOLVED in edge["gaps"]

    # Neuter ONLY the gap presenter: every gap badge vanishes from the surface, so the
    # guard can no longer see the gap the envelope still carries. Non-tautological RED.
    monkeypatch.setattr(fs, "present_gap_badges", lambda gaps: [])
    neutered = fs.build_surface(conn)
    with pytest.raises(fs.FrontendSurfaceError):
        fs.assert_gaps_visible(neutered, envelope)
    # the envelope STILL carries the gap (proves the RED came from the presenter).
    assert w.GAP_CORRECTION_UNRESOLVED in next(
        e for e in w.build_signals(conn)["correctionsLedger"]
        if e["correctedStatementId"] == "stmt-orphan-correction"
    )["gaps"]


# --- test 3: hot-topic cards rank + disclose the anchor honestly (§2) ----------


def test_hot_topics_surface_honest_anchor(conn: sqlite3.Connection) -> None:
    envelope = w.build_signals(conn)
    records = read_api.reviewer_internal_records(conn)
    cards = fs.build_hot_topics_surface(envelope["hotTopics"], records)
    by_id = {c["topicId"]: c for c in cards}
    assert set(by_id) == {TOPIC_BUDGET, TOPIC_ZONING}

    # rank preserves the envelope's salience order: BUDGET (higher) is rank 1.
    assert [c["topicId"] for c in cards] == [TOPIC_BUDGET, TOPIC_ZONING]
    assert by_id[TOPIC_BUDGET]["rank"] == 1
    assert by_id[TOPIC_BUDGET]["salienceBadge"] == fs.SALIENCE_BADGES[w.SALIENCE_RANKED]
    # the thin topic carries the insufficient-data floor badge.
    assert by_id[TOPIC_ZONING]["salienceBadge"] == fs.SALIENCE_BADGES[w.SALIENCE_INSUFFICIENT]

    # topic_id is structurally absent today -> every anchor is the honest agenda-thread
    # fallback; NO card implies a topic edge that isn't in the data (VSR GOV-521).
    for card in cards:
        assert card["topicAnchor"]["kind"] == fs.ANCHOR_AGENDA_THREAD
        assert "agenda" in card["topicAnchor"]["disclosure"].lower()


# --- test 4: anchor classifier is RED-proof, non-tautological (I5) -------------


def test_anchor_classifier_is_red_proof_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    envelope = w.build_signals(conn)
    records = read_api.reviewer_internal_records(conn)
    body = fs.build_surface(conn)
    # GREEN: the honest-anchor guard passes (all anchors are agenda-thread).
    assert fs.assert_topic_anchors_honest(body, records) is True

    # the read surface carries NO explicit topic_id edge — the ground truth.
    real_topic_edges = {
        link.get("topic_id")
        for r in records for link in r.get("evidence", [])
        if link.get("topic_id")
    }
    assert real_topic_edges == set()

    # Neuter ONLY the classifier to overclaim a topic edge: cards now declare topic_edge,
    # but the read surface + hotTopics envelope are byte-unchanged. Non-tautological RED.
    monkeypatch.setattr(fs, "classify_topic_anchor", lambda topic_id, records: fs.ANCHOR_TOPIC_EDGE)
    neutered = fs.build_surface(conn)
    assert all(c["topicAnchor"]["kind"] == fs.ANCHOR_TOPIC_EDGE for c in neutered["hotTopicsSurface"])
    with pytest.raises(fs.FrontendSurfaceError):
        fs.assert_topic_anchors_honest(neutered, records)
    # the hotTopics envelope is byte-identical (proves the RED came from the classifier).
    assert w.build_signals(conn)["hotTopics"] == envelope["hotTopics"]


# --- test 5: watchdog board groups all six lanes in order (§3) -----------------


def test_watchdog_board_lanes(conn: sqlite3.Connection) -> None:
    board = fs.build_watchdog_board(w.build_watchdog_view(conn))
    # all six frozen lanes, in canonical order, each as a column (empties included).
    assert [col["lane"] for col in board] == list(fs.LANE_ORDER)
    by_lane = {col["lane"]: col for col in board}
    for col in board:
        assert col["cardCount"] == len(col["cards"])

    correction_ids = {c["statementId"] for c in by_lane[w.LANE_CORRECTION]["cards"]}
    assert {"stmt-corrected", "stmt-orphan-correction"} <= correction_ids
    pending_ids = {c["statementId"] for c in by_lane[w.LANE_PENDING_DECISION]["cards"]}
    assert "stmt-ai" in pending_ids
    # the ai card is labelled AI-presented, never Verified.
    ai_card = next(c for c in by_lane[w.LANE_PENDING_DECISION]["cards"] if c["statementId"] == "stmt-ai")
    assert ai_card["statusBadge"] == fs.STATUS_BADGES[fs.card_feed.STATUS_AI_PRESENTED]


# --- test 6: never presents unverified / AI as Verified (safety) ---------------


def test_no_false_verified(conn: sqlite3.Connection) -> None:
    body = fs.build_surface(conn)
    assert fs.assert_no_false_verified(body) is True
    # tamper: badge a non-verified card "Verified" -> guard trips.
    for col in body["watchdogBoard"]:
        if col["cards"]:
            tampered = next((c for c in col["cards"] if c["status"] != fs.card_feed.STATUS_VERIFIED), None)
            if tampered is not None:
                tampered["statusBadge"] = fs.BADGE_VERIFIED
                with pytest.raises(fs.FrontendSurfaceError):
                    fs.assert_no_false_verified(body)
                return
    pytest.skip("no non-verified card to tamper")


# --- test 7: deterministic + idempotent (I7) -----------------------------------


def test_idempotent_byte_identical(conn: sqlite3.Connection) -> None:
    first = json.dumps(fs.build_surface(conn), sort_keys=True)
    second = json.dumps(fs.build_surface(conn), sort_keys=True)
    assert first == second


# --- test 8: no raw path / vault marker / file:// leak; no localSourcePath (I1/I2) -


def test_no_raw_leak_no_local_source_path(conn: sqlite3.Connection) -> None:
    body = fs.build_surface(conn)  # build_surface sweeps via assert_no_raw_paths
    read_api.assert_no_raw_paths(body)
    for text in read_api._iter_strings(body):
        assert "localSourcePath" not in text
        assert "Source-Data" not in text
        assert "Obsidian" not in text
        assert not text.startswith("file://")


# --- test 9: exactly one envelope digest, no per-source raw hash (I3) ----------


def test_single_envelope_digest(conn: sqlite3.Connection) -> None:
    body = fs.build_surface(conn)
    assert _is_hex64(body["surfaceDigest"])
    assert fs.assert_single_surface_digest(body) is True
    for key, value in body.items():
        if key == "surfaceDigest":
            continue
        for text in read_api._iter_strings(value):
            assert not _is_hex64(text), f"stray 64-hex under {key}: {text}"


# --- test 10: never leaks onto the public lane (I6) ----------------------------


def test_absent_from_public_lane(conn: sqlite3.Connection) -> None:
    body = fs.build_surface(conn)
    assert body["access"] == "reviewer_internal"
    assert body["scope"] == "alpine"
    assert read_api.published_records(conn) == []


# --- test 11: --check CLI is sound (exit 0) and a CI gate (exit 1 on defect) ----


def test_cli_check_gate(conn: sqlite3.Connection) -> None:
    db_path = Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
    script = str(ROOT / "scripts" / "stage5_frontend_surface.py")

    sound = subprocess.run(
        [sys.executable, script, "--db", str(db_path), "--check"],
        capture_output=True, text=True,
    )
    assert sound.returncode == 0, sound.stderr
    payload = json.loads(sound.stdout)
    assert payload["access"] == "reviewer_internal"
    assert _is_hex64(payload["surfaceDigest"])

    # a defect (board missing a lane column) trips a guard.
    envelope = w.build_signals(conn)
    body = fs.build_surface(conn)
    body["watchdogBoard"] = body["watchdogBoard"][:-1]
    with pytest.raises(fs.FrontendSurfaceError):
        fs.assert_board_complete(body, envelope)


# --- test 12: every load-bearing guard is GREEN on the live body ---------------


def test_guards_pass_on_live_body(conn: sqlite3.Connection) -> None:
    body = fs.check_surface(conn)
    envelope = w.build_signals(conn)
    records = read_api.reviewer_internal_records(conn)
    assert fs.assert_reviewer_internal(body) is True
    assert fs.assert_no_false_verified(body) is True
    assert fs.assert_gaps_visible(body, envelope) is True
    assert fs.assert_topic_anchors_honest(body, records) is True
    assert fs.assert_board_complete(body, envelope) is True
    assert fs.assert_single_surface_digest(body) is True
