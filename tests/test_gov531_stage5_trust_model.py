"""Stage 5.07 transcript/evidence/statement trust model RED tests (GOV-531).

Prove, over a seeded reviewer-internal Alpine corpus that mirrors the real merged
registry shape (records across all five layers; a corrected ``known_then`` record + its
``corrected_later`` superseding record; a second corrected record with NO superseding
ref; a past AI assumption + its later verifying record AND a second un-reverified
assumption; topic-anchored items; and an unchanged / changed / disappeared source set),
that ``scripts/stage5_trust_model.py``:

* makes the five-way record separation queryable, mapped onto — never collapsing — the
  existing ``statements.ALLOWED_LAYERS`` enum (§0, test 1);
* the layer->class mapping is RED-proof load-bearing, non-tautologically — neuter it and
  the separation assertion goes RED while the read surface still serves the same records
  (I5, test 2);
* Model 1: builds a forward-only corrections model that resolves a superseding ref +
  ``correctionEffectiveFrom`` AND fail-closed gaps an unresolvable one; the effective
  view at time T moves forward from the correction date without rewriting history
  (§1, tests 3-4);
* Model 2: hot-topic markers record WHO/WHAT (markedBy) + WHY (grounded refs); an
  unanchored record carries ``topic_anchor_missing`` (§2, test 5);
* Model 3: ≥1 source carries originalUrl + archiveStatus; changed/disappeared/replaced
  sources are representable with a formalized lifecycle<->archive binding (§3, test 6);
* Model 4: a past AI assumption is markable supported/contradicted/partially_supported/
  corrected/unresolved with origin + method; an un-reverified assumption fail-closes to
  ``unresolved`` (§4, tests 7-8);
* the verification-outcome resolver is RED-proof load-bearing, non-tautologically —
  neuter it and a resolved outcome falls to ``unresolved`` while the read surface still
  serves both records (I5, test 9);
* is deterministic + idempotent — re-projection is byte-identical (I7, test 10);
* lets no raw vault path / 64-hex / ``file://`` cross the emitted body; ``localSourcePath``
  never appears; exactly one envelope digest; never leaks onto the public lane
  (I1/I2/I3/I6, test 11);
* exposes a ``--check`` CLI that is sound (exit 0) and a CI gate (exit 1 on a defect)
  (test 12).

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
import stage5_trust_model as tm  # noqa: E402  (under test)

# --- real-shaped Alpine government locators -----------------------------------

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
CHANGED_SOURCE = "alpine_changed"
DISAPPEARED_SOURCE = "alpine_disappeared"
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-05-11.pdf"
CHANGED_URL = "https://www.alpinewy.gov/notice/2026-03-01.html"
DISAPPEARED_URL = "https://www.alpinewy.gov/notice/2026-02-01.html"
WAYBACK_URL = "https://web.archive.org/web/20260413000000/" + ORIGINAL_URL
WAYBACK_CHANGED = "https://web.archive.org/web/20260301000000/" + CHANGED_URL

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
    """All five layers + a forward-only correction + an AI assumption + its verifier."""
    # --- sources: unchanged (archived), changed (archived), disappeared (no archive) --
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status) "
        "VALUES (?, 'Town Council Minutes', 'alpine', 'minutes', 'official', 'official', "
        "?, ?, ?, 'available')",
        (MINUTES_SOURCE, ORIGINAL_URL, RECENT_DATE, WAYBACK_URL),
    )
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Council Agenda', "
        "'alpine', 'agenda', 'official', 'official', ?)",
        (AGENDA_SOURCE, AGENDA_URL),
    )
    # A CHANGED source that is still representable because it has a near-scan snapshot.
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status, "
        "source_changed) VALUES (?, 'Public Notice (changed)', 'alpine', 'notice', "
        "'official', 'official', ?, ?, ?, 'available', 1)",
        (CHANGED_SOURCE, CHANGED_URL, RECENT_DATE, WAYBACK_CHANGED),
    )
    # A DISAPPEARED source with NO archive -> archive_gap (honestly flagged).
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_status) "
        "VALUES (?, 'Public Notice (gone)', 'alpine', 'notice', 'official', 'official', "
        "?, ?, 'unavailable')",
        (DISAPPEARED_SOURCE, DISAPPEARED_URL, EARLY_DATE),
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-531 trust-model seed",
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

    # --- Model 1: a corrected known_then record + its superseding corrected_later ---
    _promote(
        conn, "stmt-corrected", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=EARLY_DATE, agenda_item_id=TOPIC_BUDGET,
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
    # A second corrected record with NO superseding ref served -> must gap fail-closed.
    _promote(
        conn, "stmt-orphan-correction", to_source_id=AGENDA_SOURCE,
        original_url=AGENDA_URL, scan_date=EARLY_DATE, agenda_item_id=TOPIC_ZONING,
    )
    conn.execute(
        "UPDATE statements SET correction_status='superseded' "
        "WHERE statement_id='stmt-orphan-correction'"
    )

    # --- Model 4: a past AI assumption + its later verifying record -----------------
    _promote(
        conn, "stmt-assumption", to_source_id=MINUTES_SOURCE, produced_by="ai",
        run_id="run-ai-1", original_url=ORIGINAL_URL, scan_date=EARLY_DATE,
        agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET layer='ai_thought_then' WHERE statement_id='stmt-assumption'"
    )
    _promote(
        conn, "stmt-verifier", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET layer='corrected_later', updates_statement_id='stmt-assumption' "
        "WHERE statement_id='stmt-verifier'"
    )
    # A SECOND assumption that is never re-verified -> fail-closed unresolved.
    _promote(
        conn, "stmt-assumption-open", to_source_id=MINUTES_SOURCE, produced_by="ai",
        run_id="run-ai-2", original_url=ORIGINAL_URL, scan_date=RECENT_DATE,
        agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET layer='ai_thought_then' WHERE statement_id='stmt-assumption-open'"
    )

    # --- §0: a presented_then (summary) + an actual_later (outcome) record ----------
    _promote(
        conn, "stmt-summary", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET layer='presented_then' WHERE statement_id='stmt-summary'"
    )
    _promote(
        conn, "stmt-outcome", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, scan_date=RECENT_DATE, agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET layer='actual_later' WHERE statement_id='stmt-outcome'"
    )
    conn.commit()


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


# --- test 1: five-way record separation is queryable, mapped onto SSOT layers (§0) ---


def test_record_separation_five_way(conn: sqlite3.Connection) -> None:
    separation = tm.build_record_separation(conn)
    by_id = {e["statementId"]: e for e in separation}

    # every served record is bucketed; the five conceptual classes are all reachable.
    assert by_id["stmt-corrected"]["recordClass"] == tm.RECORD_CLASS_FACT
    assert by_id["stmt-summary"]["recordClass"] == tm.RECORD_CLASS_SUMMARY
    assert by_id["stmt-outcome"]["recordClass"] == tm.RECORD_CLASS_ACTION_OUTCOME
    assert by_id["stmt-assumption"]["recordClass"] == tm.RECORD_CLASS_AI_ASSUMPTION
    assert by_id["stmt-superseding"]["recordClass"] == tm.RECORD_CLASS_VERIFICATION_CORRECTION

    # an AI assumption is NEVER collapsed into a verified fact.
    assert by_id["stmt-assumption"]["recordClass"] != tm.RECORD_CLASS_FACT

    # source trail + review status ride alongside every record.
    fact = by_id["stmt-corrected"]
    assert fact["sourceTrail"][0]["toSourceId"] == MINUTES_SOURCE
    assert fact["reviewStatus"]["verificationStatus"] == "reviewed_source_linked"

    # the mapping is total over the SSOT layer enum (parity with statements.ALLOWED_LAYERS).
    assert set(tm.LAYER_TO_RECORD_CLASS) == set(st.ALLOWED_LAYERS)


# --- test 2: layer->class mapping is RED-proof, non-tautological (I5) -----------------


def test_record_class_is_red_proof_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    sep = tm.build_record_separation(conn)
    assert any(e["recordClass"] == tm.RECORD_CLASS_AI_ASSUMPTION for e in sep)

    # The read surface still serves the records — the layer's input is unchanged.
    served_layers = {r["statement_id"]: r.get("layer") for r in read_api.reviewer_internal_records(conn)}
    assert served_layers.get("stmt-assumption") == "ai_thought_then"

    # Neuter ONLY the mapper: every class vanishes -> each entry falls to the fail-closed
    # gap, even though the read surface is untouched. Non-tautological RED.
    monkeypatch.setattr(tm, "record_class", lambda layer: None)
    neutered = tm.build_record_separation(conn)
    assert all(e["recordClass"] is None for e in neutered)
    assert all(tm.GAP_LAYER_UNRESOLVED in e["gaps"] for e in neutered)
    # the read surface STILL serves the assumption (proves the RED came from the mapper).
    assert "stmt-assumption" in {
        r["statement_id"] for r in read_api.reviewer_internal_records(conn)
    }


# --- test 3: Model 1 corrections resolve + fail-closed gap + effective date (§1) ------


def test_corrections_resolve_and_gap(conn: sqlite3.Connection) -> None:
    corrections = tm.build_corrections(conn)
    by_id = {e["correctedStatementId"]: e for e in corrections}
    assert set(by_id) == {"stmt-corrected", "stmt-orphan-correction"}

    resolved = by_id["stmt-corrected"]
    assert resolved["resolved"] is True
    assert resolved["supersedingStatementId"] == "stmt-superseding"
    assert resolved["correctionStatus"] == "corrected"
    # the correction is effective from the SUPERSEDING record's date (the correction date).
    assert resolved["correctionEffectiveFrom"] == RECENT_DATE
    # known-then context preserved (the corrected record's own older date, not rewritten).
    assert resolved["knownThen"]["recordDate"] == EARLY_DATE
    assert resolved["gaps"] == []

    gapped = by_id["stmt-orphan-correction"]
    assert gapped["resolved"] is False
    assert gapped["supersedingRef"] is None
    assert gapped["correctionEffectiveFrom"] is None
    assert gapped["gaps"] == [tm.GAP_CORRECTION_UNRESOLVED]


# --- test 4: effective view moves forward without rewriting history (§1, AC3d) --------


def test_effective_view_moves_forward(conn: sqlite3.Connection) -> None:
    corrections = tm.build_corrections(conn)

    # BEFORE the correction date: the correction is NOT yet in force (history preserved).
    before = tm.effective_view_at(corrections, EARLY_DATE)
    assert all(e["correctedStatementId"] != "stmt-corrected" for e in before)

    # ON/AFTER the correction date: the correction is in force.
    after = tm.effective_view_at(corrections, RECENT_DATE)
    assert any(e["correctedStatementId"] == "stmt-corrected" for e in after)

    # the unresolved correction (no effective date) is NEVER in force at any T.
    far_future = tm.effective_view_at(corrections, "2099-12-31")
    assert all(e["correctedStatementId"] != "stmt-orphan-correction" for e in far_future)


# --- test 5: Model 2 hot-topic reasons record WHO/WHAT + WHY (§2) ---------------------


def test_hot_topic_reasons_who_what_why(conn: sqlite3.Connection) -> None:
    reasons = tm.build_hot_topic_reasons(conn)
    by_topic = {t["topicId"]: t for t in reasons["topics"]}
    assert TOPIC_BUDGET in by_topic

    budget = by_topic[TOPIC_BUDGET]
    marked = {m["markedBy"] for m in budget["markers"]}
    # a corrected record on the topic -> changed_record; many records -> repeated_discussion.
    assert tm.MARKED_BY_CHANGED_RECORD in marked
    assert tm.MARKED_BY_REPEATED_DISCUSSION in marked
    # every marker is in vocab AND carries a grounding ref (WHY is never ungrounded).
    for marker in budget["markers"]:
        assert marker["markedBy"] in tm.MARKED_BY_VALUES
        assert marker["why"]["groundingRefs"]


# --- test 6: Model 3 source-change/archive + lifecycle<->archive binding (§3) ---------


def test_source_change_archive_binding(conn: sqlite3.Connection) -> None:
    entries = {e["sourceId"]: e for e in tm.build_source_change_archive(conn)}

    # AC3a: ≥1 source carries originalUrl + archiveStatus (+ a web snapshot ref).
    minutes = entries[MINUTES_SOURCE]
    assert minutes["originalUrl"] == ORIGINAL_URL
    assert minutes["archiveStatus"] == "available"
    assert minutes["nearestSnapshotRef"] == WAYBACK_URL
    assert minutes["archiveBinding"] == tm.ARCHIVE_BINDING_LIVE  # unchanged source

    # AC3b: a CHANGED source with a near-scan snapshot is representable (archive_backed).
    changed = entries[CHANGED_SOURCE]
    assert changed["lifecycleState"] == "changed"
    assert changed["archiveBinding"] == tm.ARCHIVE_BINDING_BACKED
    assert changed["gaps"] == []

    # AC3b: a DISAPPEARED source with NO archive is representable + honestly gapped.
    gone = entries[DISAPPEARED_SOURCE]
    assert gone["lifecycleState"] == "disappeared"
    assert gone["archiveBinding"] == tm.ARCHIVE_BINDING_GAP
    assert gone["gaps"] == [tm.GAP_ARCHIVE_UNAVAILABLE_FOR_CHANGED]


# --- test 7: Model 4 assumption verification resolves with origin + method (§4) -------


def test_assumption_verification_resolves(conn: sqlite3.Connection) -> None:
    verifications = {e["assumptionStatementId"]: e for e in tm.build_assumption_verifications(conn)}
    assert set(verifications) == {"stmt-assumption", "stmt-assumption-open"}

    verified = verifications["stmt-assumption"]
    assert verified["resolved"] is True
    assert verified["verifyingStatementId"] == "stmt-verifier"
    assert verified["verificationOutcome"] in tm.VERIFICATION_OUTCOMES
    # who/what (origin) + how (method) + verifying source + date all present.
    assert verified["verificationOrigin"] == "human"
    assert verified["verificationMethod"] == "substantiates"
    assert verified["verifyingSourceRef"] == MINUTES_SOURCE
    assert verified["verificationDate"] == RECENT_DATE
    # the original assumption is preserved (its own older date, never mutated).
    assert verified["assumptionThen"]["recordDate"] == EARLY_DATE


# --- test 8: every outcome reachable + un-reverified assumption fail-closes (§4) ------


def test_verification_outcomes_and_fail_closed(conn: sqlite3.Connection) -> None:
    # all five outcomes are reachable from the resolver (pure-function enumeration).
    assert tm.resolve_verification_outcome(None) == tm.VERIFICATION_UNRESOLVED
    assert (
        tm.resolve_verification_outcome({"correction_status": "corrected", "evidence": []})
        == tm.VERIFICATION_CORRECTED
    )
    assert (
        tm.resolve_verification_outcome({"correction_status": "superseded", "evidence": []})
        == tm.VERIFICATION_CONTRADICTED
    )
    assert (
        tm.resolve_verification_outcome(
            {"correction_status": "none", "produced_by": "ai", "evidence": []}
        )
        == tm.VERIFICATION_PARTIALLY_SUPPORTED
    )
    supported = {
        "correction_status": "none",
        "produced_by": "human",
        "ui_status": "source-backed",
        "provenance_status": read_api.PROVENANCE_GROUNDED,
        "evidence": [],
    }
    assert tm.resolve_verification_outcome(supported) == tm.VERIFICATION_SUPPORTED

    # the un-reverified assumption fail-closes to unresolved with NO fabricated verifier.
    verifications = {e["assumptionStatementId"]: e for e in tm.build_assumption_verifications(conn)}
    open_assumption = verifications["stmt-assumption-open"]
    assert open_assumption["resolved"] is False
    assert open_assumption["verificationOutcome"] == tm.VERIFICATION_UNRESOLVED
    assert open_assumption["verifyingRef"] is None
    assert open_assumption["verificationOrigin"] is None


# --- test 9: verification-outcome resolver is RED-proof, non-tautological (I5) --------


def test_verification_resolver_is_red_proof(conn: sqlite3.Connection, monkeypatch) -> None:
    before = {e["assumptionStatementId"]: e for e in tm.build_assumption_verifications(conn)}
    assert before["stmt-assumption"]["verificationOutcome"] != tm.VERIFICATION_UNRESOLVED

    # The read surface still serves BOTH the assumption and its verifier.
    served = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    assert {"stmt-assumption", "stmt-verifier"} <= served

    # Neuter ONLY the resolver: the resolved outcome falls to unresolved, even though the
    # read surface (and the verifier link) is untouched. Non-tautological RED.
    monkeypatch.setattr(tm, "resolve_verification_outcome", lambda verifier: tm.VERIFICATION_UNRESOLVED)
    after = {e["assumptionStatementId"]: e for e in tm.build_assumption_verifications(conn)}
    assert after["stmt-assumption"]["verificationOutcome"] == tm.VERIFICATION_UNRESOLVED
    # the verifier ref still resolves (proves the RED came from the resolver, not the spine).
    assert after["stmt-assumption"]["verifyingStatementId"] == "stmt-verifier"
    assert {"stmt-assumption", "stmt-verifier"} <= {
        r["statement_id"] for r in read_api.reviewer_internal_records(conn)
    }


# --- test 10: deterministic + idempotent re-projection (I7) ---------------------------


def test_trust_model_is_deterministic(conn: sqlite3.Connection) -> None:
    first = tm.build_trust_model(conn)
    second = tm.build_trust_model(conn)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["trustDigest"] == second["trustDigest"]
    assert first["scope"] == "alpine"
    assert first["access"] == "reviewer_internal"


# --- test 11: boundary invariants — no raw paths, single digest, not public (I1/2/3/6) -


def test_boundary_invariants(conn: sqlite3.Connection) -> None:
    body = tm.build_trust_model(conn)

    # I1/I2: transport sweep passes; no localSourcePath, no file://, no FS path leaks.
    read_api.assert_no_raw_paths(body)
    blob = json.dumps(body)
    assert "localSourcePath" not in blob
    assert "file://" not in blob
    assert "/Users/" not in blob and "/vault/" not in blob

    # I3: exactly one 64-hex string in the whole body — the envelope trustDigest.
    hexes = [s for s in read_api._iter_strings(body) if _is_hex64(s)]
    assert hexes == [body["trustDigest"]]
    assert tm.assert_single_envelope_digest(body) is True

    # I6: the body is reviewer-internal and never appears on the public published lane.
    assert body["access"] == "reviewer_internal"
    assert read_api.published_records(conn) == []


# --- test 12: --check CLI is sound (exit 0) and a CI gate (exit 1 on defect) ----------


def test_check_cli_sound_and_gate(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # the in-process guard suite passes on the clean seed.
    assert tm.check_trust_model(conn) is not None

    db_path = tmp_path / "cli.db"
    db.apply_migrations(db_path)
    cli_conn = db.open_db(db_path)
    _seed(cli_conn)
    cli_conn.close()

    env_root = str(ROOT / "scripts")
    ok = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage5_trust_model.py"),
         "--db", str(db_path), "--check"],
        capture_output=True, text=True, env={"PYTHONPATH": env_root, "PATH": ""},
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["scope"] == "alpine"


# --- test 13: each contract guard is load-bearing (RED on a poisoned body) ------------


def test_guards_are_load_bearing(conn: sqlite3.Connection) -> None:
    body = tm.build_trust_model(conn)

    # poison a correction edge: marked resolved but no ref -> guard RED.
    poisoned = json.loads(json.dumps(body))
    if poisoned["corrections"]:
        poisoned["corrections"][0]["resolved"] = True
        poisoned["corrections"][0]["supersedingRef"] = None
        poisoned["corrections"][0]["correctionEffectiveFrom"] = None
        with pytest.raises(tm.TrustModelError):
            tm.assert_corrections_resolved_or_gapped(poisoned)

    # poison an archive binding: changed source claimed live -> guard RED.
    poisoned2 = json.loads(json.dumps(body))
    for entry in poisoned2["sourceChangeArchive"]:
        if entry["lifecycleState"] == "disappeared":
            entry["archiveBinding"] = tm.ARCHIVE_BINDING_LIVE
            with pytest.raises(tm.TrustModelError):
                tm.assert_archive_binding_consistent(poisoned2)
            break

    # poison a verification: unresolved but carrying a fabricated verifier -> guard RED.
    poisoned3 = json.loads(json.dumps(body))
    for entry in poisoned3["assumptionVerifications"]:
        if not entry["resolved"]:
            entry["verifyingRef"] = "card:fabricated"
            with pytest.raises(tm.TrustModelError):
                tm.assert_verifications_fail_closed(poisoned3)
            break
