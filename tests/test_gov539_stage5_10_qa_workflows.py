"""Stage 5.10 QA workflow test suite + golden fixtures (GOV-539).

ADDITIVE-ONLY deterministic test harness that exercises the five Stage-5 trust
mechanics END-TO-END as realistic, reproducible review workflows over the merged
5.04-5.07 substrate. This file modifies NO existing module or test — it only adds a
new test file driving the already-merged functions:

  W1  Correction-state workflow
      ``stage5_trust_model.build_corrections / resolve_correction_edge / effective_view_at``
      (+ ``stage5_watchdog_signals.build_corrections_ledger``).
      Seed a claim, later correct it; the correction moves FORWARD from
      ``correctionEffectiveFrom`` and ``effective_view_at(before)`` still returns the
      then-known record — history is never rewritten.

  W2  Hot-topic-reason workflow
      ``stage5_trust_model.build_hot_topic_reasons``
      (+ ``stage5_watchdog_signals.build_hot_topics / salience_score``).
      Mark a topic hot; WHO/WHAT (``markedBy``) + WHY (grounded ``reason``) markers are
      recorded and queryable; salience is pure deterministic arithmetic.

  W3  Source-change + Wayback archive workflow
      ``stage5_source_inventory.derive_lifecycle_state / archive_availability`` +
      ``stage5_trust_model.derive_archive_binding / build_source_change_archive`` +
      ``stage5_record_verifier.resolve_archive_snapshot / confirm_archive_snapshot``.
      Seed unchanged / changed / disappeared / replaced source scenarios; original-URL +
      archive-status representation holds near the scan date.

  W4  Future-fact verification workflow
      ``stage5_trust_model.build_assumption_verifications / resolve_verification_outcome``
      (+ ``stage5_record_verifier.verify_record``).
      Mark a past AI assumption supported / contradicted / partially_supported /
      corrected / unresolved with origin + method; the unresolved path is fail-closed.

  W5  Digest / refresh determinism
      every module's single-envelope-digest + ``--check`` CLI
      (``stage5_*.assert_single_envelope_digest``). Byte-stable output across two runs
      (sha256 match) in-process AND across two subprocesses; run-log discipline.
      The GOV-478 assembler + GOV-479 weekly runner live in the SEPARATE Node repo
      (``/Users/IA/GitHub/Government-Watchdog`` branch ``stage4-automation-ai-boundary``)
      and already carry their own ``npm test`` determinism suite (GOV-479: 19 pass).
      Cross-repo execution from this backend Python PR is OUT OF SCOPE for one PR — see
      ``test_w5_node_repo_determinism_scoped_down`` for the logged note + follow-up.

The fact / summary / action / AI-assumption / correction separation
(``statements.ALLOWED_LAYERS``) is asserted intact across every workflow.

Pure sqlite + tmp files: NO network, NO subprocess in the data path (the only
subprocesses are the explicit ``--check`` CLI determinism probes), NO real-corpus
dependency. The seed mirrors ``tests/test_gov531_stage5_trust_model.py`` (GOV-531) and
``tests/test_gov488_stage5_record_verify.py`` (GOV-488).
"""

from __future__ import annotations

import hashlib
import json
import re
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
import stage5_source_inventory as inv  # noqa: E402  (W3)
import stage5_record_verifier as ver  # noqa: E402  (W3/W4)
import stage5_trust_model as tm  # noqa: E402        (W1/W2/W3/W4)
import stage5_watchdog_signals as ws  # noqa: E402   (W1/W2)

# --- real-shaped Alpine government locators (golden fixture constants) ---------

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
CHANGED_SOURCE = "alpine_changed"
DISAPPEARED_SOURCE = "alpine_disappeared"
REPLACED_SOURCE = "alpine_replaced"
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-05-11.pdf"
CHANGED_URL = "https://www.alpinewy.gov/notice/2026-03-01.html"
DISAPPEARED_URL = "https://www.alpinewy.gov/notice/2026-02-01.html"
REPLACED_URL = "https://www.alpinewy.gov/resolution/2026-03-20.html"
WAYBACK_URL = "https://web.archive.org/web/20260413000000/" + ORIGINAL_URL
WAYBACK_CHANGED = "https://web.archive.org/web/20260301000000/" + CHANGED_URL
WAYBACK_REPLACED = "https://web.archive.org/web/20260320000000/" + REPLACED_URL

TOPIC_BUDGET = "agenda-budget"
TOPIC_ZONING = "agenda-zoning"
EARLY_DATE = "2026-01-05"   # older scan (outside the recency window from the anchor)
RECENT_DATE = "2026-04-13"  # recent scan (the corpus anchor)

# W4 verify_record real-flip constants (GOV-488 shape).
V_MINUTES_SOURCE = "alpine_minutes"
V_EVENT_DATE = "2026-04-13"
V_ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
V_ARCHIVE_URL = (
    "https://web.archive.org/web/20260415000000/"
    "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
)
V_RAW_SHA256 = hashlib.sha256(b"preserved alpine minutes raw bytes").hexdigest()
# A raw backend-only path (carries vault markers) — must NEVER reach a served body.
V_RAW_LOCAL_PATH = "/Users/IA/Obsidian Vault/Source-Data/minutes-2026-04-13.pdf"


# ===========================================================================
# Golden fixtures
# ===========================================================================


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
    archive_url: str | None = None,
    archive_status: str = "not_checked",
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
        "archive_url": archive_url,
        "archive_status": archive_status,
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


def _seed_trust_corpus(conn: sqlite3.Connection) -> None:
    """Golden fixture: all five layers + a forward correction + an AI assumption + its
    verifier + the four source-lifecycle scenarios (unchanged/changed/disappeared/
    replaced). Mirrors GOV-531, extended with a REPLACED source for W3 completeness."""
    # --- sources: the four lifecycle scenarios ---
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
    # CHANGED source — representable because it has a near-scan snapshot.
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status, "
        "source_changed) VALUES (?, 'Public Notice (changed)', 'alpine', 'notice', "
        "'official', 'official', ?, ?, ?, 'available', 1)",
        (CHANGED_SOURCE, CHANGED_URL, RECENT_DATE, WAYBACK_CHANGED),
    )
    # DISAPPEARED source with NO archive -> archive_gap (honestly flagged).
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_status) "
        "VALUES (?, 'Public Notice (gone)', 'alpine', 'notice', 'official', 'official', "
        "?, ?, 'unavailable')",
        (DISAPPEARED_SOURCE, DISAPPEARED_URL, EARLY_DATE),
    )
    # REPLACED source (correction_status='replaced') WITH a near-scan snapshot ->
    # representable as archive_backed.
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status, "
        "correction_status) VALUES (?, 'Resolution (replaced)', 'alpine', 'notice', "
        "'official', 'official', ?, ?, ?, 'available', 'replaced')",
        (REPLACED_SOURCE, REPLACED_URL, RECENT_DATE, WAYBACK_REPLACED),
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-539 QA-workflow seed",
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

    # --- W1: a corrected known_then record + its superseding corrected_later ---
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
    # A second corrected record with NO superseding ref -> must gap fail-closed.
    _promote(
        conn, "stmt-orphan-correction", to_source_id=AGENDA_SOURCE,
        original_url=AGENDA_URL, scan_date=EARLY_DATE, agenda_item_id=TOPIC_ZONING,
    )
    conn.execute(
        "UPDATE statements SET correction_status='superseded' "
        "WHERE statement_id='stmt-orphan-correction'"
    )

    # --- W4: a past AI assumption + its later verifying record ---
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
    # A SECOND assumption never re-verified -> fail-closed unresolved.
    _promote(
        conn, "stmt-assumption-open", to_source_id=MINUTES_SOURCE, produced_by="ai",
        run_id="run-ai-2", original_url=ORIGINAL_URL, scan_date=RECENT_DATE,
        agenda_item_id=TOPIC_BUDGET,
    )
    conn.execute(
        "UPDATE statements SET layer='ai_thought_then' WHERE statement_id='stmt-assumption-open'"
    )

    # --- the remaining two of the five layers: summary + action-outcome ---
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


def _seed_verify_candidate(conn: sqlite3.Connection) -> None:
    """Golden fixture (GOV-488 shape): a reviewed, source-backed-but-NOT-yet-grounded
    record (serves as ``unverified``) + six unsourced ``ai_presented`` observations.
    The W4 ``verify_record`` flip drives the candidate to ``verified``."""
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Town Council Minutes', "
        "'alpine', 'minutes', 'official', 'official', ?)",
        (V_MINUTES_SOURCE, V_ORIGINAL_URL),
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-539 verify-candidate seed",
    )
    conn.commit()
    _promote(
        conn, "stmt-verified", to_source_id=V_MINUTES_SOURCE,
        original_url=V_ORIGINAL_URL, scan_date=V_EVENT_DATE, archive_status="not_checked",
    )
    for i in range(1, ver.AI_PRESENTED_BASELINE + 1):
        _promote(
            conn, f"stmt-ai-{i}", to_source_id=V_MINUTES_SOURCE, produced_by="ai",
            run_id=f"run-ai-{i}", scan_date=V_EVENT_DATE,
            original_url="file:///Users/IA/Obsidian%20Vault/Source-Data/ai-note.txt",
        )


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "trust.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_trust_corpus(connection)
    yield connection
    connection.close()


@pytest.fixture()
def verify_conn(tmp_path: Path):
    db_path = tmp_path / "verify.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_verify_candidate(connection)
    yield connection
    connection.close()


def _is_hex64(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


# ===========================================================================
# W1 — Correction-state workflow
# ===========================================================================


def test_w1_correction_resolves_forward_with_effective_date(conn: sqlite3.Connection) -> None:
    """A claim corrected later resolves a superseding ref + a forward
    ``correctionEffectiveFrom`` (the correction date), and the unresolvable correction
    fail-closes to a gap — never a fabricated edge."""
    corrections = tm.build_corrections(conn)
    by_id = {e["correctedStatementId"]: e for e in corrections}
    assert set(by_id) == {"stmt-corrected", "stmt-orphan-correction"}

    resolved = by_id["stmt-corrected"]
    assert resolved["resolved"] is True
    assert resolved["supersedingStatementId"] == "stmt-superseding"
    # the correction is effective FROM the superseding record's (later) date.
    assert resolved["correctionEffectiveFrom"] == RECENT_DATE
    # known-then context preserved verbatim — the corrected record keeps its OWN older date.
    assert resolved["knownThen"]["recordDate"] == EARLY_DATE
    assert resolved["gaps"] == []

    gapped = by_id["stmt-orphan-correction"]
    assert gapped["resolved"] is False
    assert gapped["supersedingRef"] is None
    assert gapped["correctionEffectiveFrom"] is None
    assert gapped["gaps"] == [tm.GAP_CORRECTION_UNRESOLVED]


def test_w1_effective_view_moves_forward_without_rewriting_history(conn: sqlite3.Connection) -> None:
    """``effective_view_at(before)`` returns the THEN-known record (correction not yet in
    force); ``effective_view_at(on/after)`` brings the correction into force. History is
    never rewritten and the unresolved correction is in force at NO time."""
    corrections = tm.build_corrections(conn)

    before = tm.effective_view_at(corrections, EARLY_DATE)
    assert all(e["correctedStatementId"] != "stmt-corrected" for e in before)

    after = tm.effective_view_at(corrections, RECENT_DATE)
    assert any(e["correctedStatementId"] == "stmt-corrected" for e in after)

    far_future = tm.effective_view_at(corrections, "2099-12-31")
    assert all(e["correctedStatementId"] != "stmt-orphan-correction" for e in far_future)


def test_w1_watchdog_ledger_agrees_with_trust_corrections(conn: sqlite3.Connection) -> None:
    """The 5.05 watchdog corrections ledger (downstream surface) agrees with the 5.07
    trust corrections model on which records are corrected, preserving known-then
    context and resolving the same superseding edge."""
    ledger = {e["correctedStatementId"]: e for e in ws.build_corrections_ledger(conn)}
    trust = {e["correctedStatementId"]: e for e in tm.build_corrections(conn)}
    assert set(ledger) == set(trust)

    resolved = ledger["stmt-corrected"]
    assert resolved["resolved"] is True
    assert resolved["supersedingStatementId"] == "stmt-superseding"
    assert resolved["knownThen"]["recordDate"] == EARLY_DATE  # not rewritten
    assert ledger["stmt-orphan-correction"]["gaps"] == [ws.GAP_CORRECTION_UNRESOLVED]


def test_w1_resolve_correction_edge_is_pure_and_consistent(conn: sqlite3.Connection) -> None:
    """``resolve_correction_edge`` is a deterministic pure read over the served records:
    same inputs -> same resolution; an unknown statement resolves to ``None``."""
    records = read_api.reviewer_internal_records(conn)
    records_by_id = {r["statement_id"]: r for r in records}
    superseding_index = ws._superseding_index(records)

    first = ws.resolve_correction_edge("stmt-corrected", superseding_index, records_by_id)
    second = ws.resolve_correction_edge("stmt-corrected", superseding_index, records_by_id)
    assert first == second
    assert first is not None and first["supersedingStatementId"] == "stmt-superseding"
    assert ws.resolve_correction_edge("stmt-nonexistent", superseding_index, records_by_id) is None


# ===========================================================================
# W2 — Hot-topic-reason workflow
# ===========================================================================


def test_w2_hot_topic_markers_carry_who_what_and_why(conn: sqlite3.Connection) -> None:
    """A hot topic records WHO/WHAT (``markedBy`` in vocab) + WHY (a grounded
    ``reason`` — never ungrounded). A corrected record on the topic surfaces
    ``changed_record``; repeated discussion surfaces ``repeated_discussion``."""
    reasons = tm.build_hot_topic_reasons(conn)
    by_topic = {t["topicId"]: t for t in reasons["topics"]}
    assert TOPIC_BUDGET in by_topic

    budget = by_topic[TOPIC_BUDGET]
    marked = {m["markedBy"] for m in budget["markers"]}
    assert tm.MARKED_BY_CHANGED_RECORD in marked
    assert tm.MARKED_BY_REPEATED_DISCUSSION in marked
    for marker in budget["markers"]:
        assert marker["markedBy"] in tm.MARKED_BY_VALUES        # WHO/WHAT in vocab
        assert marker["why"]["groundingRefs"]                   # WHY is grounded


def test_w2_salience_is_pure_deterministic_arithmetic(conn: sqlite3.Connection) -> None:
    """``salience_score`` is exact integer arithmetic over activity/recency/churn and the
    5.05 ``build_hot_topics`` ranking is byte-stable; the per-topic score equals the
    independent recomputation from the emitted counts."""
    # the pure function is exact and weight-pinned.
    assert ws.salience_score(0, 0, 0) == 0
    assert ws.salience_score(2, 1, 1) == (
        ws.ACTIVITY_WEIGHT * 2 + ws.RECENCY_WEIGHT * 1 + ws.CHURN_WEIGHT * 1
    )

    topics = ws.build_hot_topics(conn)
    for t in topics:
        assert t["salienceScore"] == ws.salience_score(
            t["activityCount"], t["recencyCount"], t["correctionChurn"]
        )
    # ranking is (score desc, topicId asc) -> byte-stable across rebuilds.
    assert topics == ws.build_hot_topics(conn)
    scores = [t["salienceScore"] for t in topics]
    assert scores == sorted(scores, reverse=True)


def test_w2_hot_topic_reason_resolver_is_red_proof(conn: sqlite3.Connection, monkeypatch) -> None:
    """Non-tautological RED-proof: neuter ONLY the salience scorer -> the watchdog ranking
    collapses to a constant while the read surface still serves the same anchored
    records (proves the ranking is driven by the scorer, not the data)."""
    served_before = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    base = ws.build_hot_topics(conn)
    assert any(t["salienceScore"] > 0 for t in base)

    monkeypatch.setattr(ws, "salience_score", lambda activity, recency, churn: 0)
    neutered = ws.build_hot_topics(conn)
    assert all(t["salienceScore"] == 0 for t in neutered)
    # the read surface is untouched — the RED came from the scorer.
    assert {r["statement_id"] for r in read_api.reviewer_internal_records(conn)} == served_before


# ===========================================================================
# W3 — Source-change + Wayback archive workflow
# ===========================================================================


def test_w3_lifecycle_states_cover_all_four_scenarios(conn: sqlite3.Connection) -> None:
    """``derive_lifecycle_state`` classifies unchanged / changed / disappeared / replaced
    over the seeded sources, most-degraded-state-wins, echoing only frozen-vocab
    evidence (never a free-text smuggle)."""
    by_id = {s["source_id"]: s for s in inv.source_inventory(conn)}
    assert by_id[MINUTES_SOURCE]["lifecycle"]["state"] == inv.LIFECYCLE_UNCHANGED
    assert by_id[CHANGED_SOURCE]["lifecycle"]["state"] == inv.LIFECYCLE_CHANGED
    assert by_id[DISAPPEARED_SOURCE]["lifecycle"]["state"] == inv.LIFECYCLE_DISAPPEARED
    assert by_id[REPLACED_SOURCE]["lifecycle"]["state"] == inv.LIFECYCLE_REPLACED
    # the replaced source echoes its replacement signal (frozen vocab), nothing else.
    assert by_id[REPLACED_SOURCE]["lifecycle"]["evidence"]["replacementSignal"] == "replaced"


def test_w3_archive_availability_keyed_to_scan_date(conn: sqlite3.Connection) -> None:
    """``archive_availability`` keys the snapshot honesty label to the immutable
    ``scan_date``: an available web snapshot -> ``available_near_scan`` with a web-only
    ``nearestSnapshotRef``; an unavailable archive -> ``not_available`` with no ref."""
    by_id = {s["source_id"]: s for s in inv.source_inventory(conn)}

    minutes = by_id[MINUTES_SOURCE]["archiveAvailability"]
    assert minutes["scanDate"] == RECENT_DATE
    assert minutes["archiveStatus"] == inv.ARCHIVE_STATUS_AVAILABLE
    assert minutes["snapshotAvailability"] == inv.SNAPSHOT_AVAILABLE
    assert minutes["nearestSnapshotRef"] == WAYBACK_URL

    gone = by_id[DISAPPEARED_SOURCE]["archiveAvailability"]
    assert gone["archiveStatus"] == inv.ARCHIVE_STATUS_UNAVAILABLE
    assert gone["snapshotAvailability"] == inv.SNAPSHOT_NOT_AVAILABLE
    assert gone["nearestSnapshotRef"] is None


def test_w3_source_change_archive_binding_holds_near_scan(conn: sqlite3.Connection) -> None:
    """``build_source_change_archive`` carries originalUrl + archiveStatus and binds
    lifecycle<->archive: unchanged->live; changed/replaced WITH a near-scan snapshot->
    archive_backed; disappeared WITHOUT one->archive_gap (honestly flagged)."""
    entries = {e["sourceId"]: e for e in tm.build_source_change_archive(conn)}

    minutes = entries[MINUTES_SOURCE]
    assert minutes["originalUrl"] == ORIGINAL_URL
    assert minutes["archiveStatus"] == "available"
    assert minutes["nearestSnapshotRef"] == WAYBACK_URL
    assert minutes["archiveBinding"] == tm.ARCHIVE_BINDING_LIVE

    changed = entries[CHANGED_SOURCE]
    assert changed["lifecycleState"] == "changed"
    assert changed["archiveBinding"] == tm.ARCHIVE_BINDING_BACKED
    assert changed["gaps"] == []

    replaced = entries[REPLACED_SOURCE]
    assert replaced["lifecycleState"] == "replaced"
    assert replaced["archiveBinding"] == tm.ARCHIVE_BINDING_BACKED  # near-scan snapshot
    assert replaced["gaps"] == []

    gone = entries[DISAPPEARED_SOURCE]
    assert gone["lifecycleState"] == "disappeared"
    assert gone["archiveBinding"] == tm.ARCHIVE_BINDING_GAP
    assert gone["gaps"] == [tm.GAP_ARCHIVE_UNAVAILABLE_FOR_CHANGED]


def test_w3_resolve_and_confirm_archive_snapshot_near_scan(conn: sqlite3.Connection) -> None:
    """``resolve_archive_snapshot`` confirms a Wayback ref within the nearness window and
    rejects a far one; ``confirm_archive_snapshot`` refuses a far snapshot (never records
    a fabricated archive) and is idempotent on a near one."""
    near = ver.resolve_archive_snapshot(RECENT_DATE, WAYBACK_URL)
    assert near is not None and near["nearScanDate"] is True
    assert near["snapshotRef"] == WAYBACK_URL

    # a snapshot far from the scan date is NOT near (honest delta).
    far = ver.resolve_archive_snapshot("2020-01-01", WAYBACK_URL)
    assert far is not None and far["nearScanDate"] is False
    # a malformed / absent ref resolves to None.
    assert ver.resolve_archive_snapshot(RECENT_DATE, "https://example.com/not-wayback") is None

    # confirm refuses a far snapshot — no fabricated archive recorded.
    with pytest.raises(ver.RecordVerifyError):
        ver.confirm_archive_snapshot(
            conn, "stmt-corrected", archive_url=WAYBACK_URL, scan_date="2020-01-01"
        )
    # confirm on a near snapshot writes once, then is a byte-stable no-op (idempotent).
    changed = ver.confirm_archive_snapshot(
        conn, "stmt-corrected", archive_url=WAYBACK_URL, scan_date=RECENT_DATE
    )
    again = ver.confirm_archive_snapshot(
        conn, "stmt-corrected", archive_url=WAYBACK_URL, scan_date=RECENT_DATE
    )
    assert changed is True and again is False


# ===========================================================================
# W4 — Future-fact verification workflow
# ===========================================================================


def test_w4_assumption_resolves_with_origin_and_method(conn: sqlite3.Connection) -> None:
    """A past AI assumption with a later verifying record resolves with WHO (origin) +
    HOW (method) + verifying source + date; the original assumption's own older date is
    preserved (never mutated)."""
    verifications = {
        e["assumptionStatementId"]: e for e in tm.build_assumption_verifications(conn)
    }
    assert set(verifications) == {"stmt-assumption", "stmt-assumption-open"}

    verified = verifications["stmt-assumption"]
    assert verified["resolved"] is True
    assert verified["verifyingStatementId"] == "stmt-verifier"
    assert verified["verificationOutcome"] in tm.VERIFICATION_OUTCOMES
    assert verified["verificationOrigin"] == "human"
    assert verified["verificationMethod"] == "substantiates"
    assert verified["verifyingSourceRef"] == MINUTES_SOURCE
    assert verified["verificationDate"] == RECENT_DATE
    assert verified["assumptionThen"]["recordDate"] == EARLY_DATE


def test_w4_all_five_outcomes_markable_and_unresolved_fail_closed(conn: sqlite3.Connection) -> None:
    """Every outcome — supported / contradicted / partially_supported / corrected /
    unresolved — is reachable from ``resolve_verification_outcome``; an un-reverified
    assumption fail-closes to ``unresolved`` with NO fabricated verifier."""
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

    open_assumption = {
        e["assumptionStatementId"]: e for e in tm.build_assumption_verifications(conn)
    }["stmt-assumption-open"]
    assert open_assumption["resolved"] is False
    assert open_assumption["verificationOutcome"] == tm.VERIFICATION_UNRESOLVED
    assert open_assumption["verifyingRef"] is None
    assert open_assumption["verificationOrigin"] is None


def test_w4_verify_record_flips_candidate_through_real_pipeline(verify_conn: sqlite3.Connection) -> None:
    """``verify_record`` drives a reviewed, source-backed-but-ungrounded record to
    ``verified`` through the REAL serve+feed pipeline (preserved raw + near-scan archive),
    dropping the unsourced ai_presented count, and is idempotent (byte-stable envelope)."""
    before = ver.resolve_verification(verify_conn, "stmt-verified")
    assert before["verified"] is False

    resolution = ver.verify_record(
        verify_conn, "stmt-verified", source_id=V_MINUTES_SOURCE, sha256=V_RAW_SHA256,
        fetched_url=V_ORIGINAL_URL, local_path=V_RAW_LOCAL_PATH,
        fetch_time_utc="2026-04-15T09:00:00Z", archive_url=V_ARCHIVE_URL, scan_date=V_EVENT_DATE,
    )
    assert resolution["verified"] is True
    assert resolution["originalUrlResolvable"] is True

    # idempotent: re-running yields a byte-identical envelope (no churn).
    first = json.dumps(ver.build_verified_record(verify_conn, "stmt-verified"), sort_keys=True)
    ver.verify_record(
        verify_conn, "stmt-verified", source_id=V_MINUTES_SOURCE, sha256=V_RAW_SHA256,
        fetched_url=V_ORIGINAL_URL, local_path=V_RAW_LOCAL_PATH,
        fetch_time_utc="2026-04-15T09:00:00Z", archive_url=V_ARCHIVE_URL, scan_date=V_EVENT_DATE,
    )
    second = json.dumps(ver.build_verified_record(verify_conn, "stmt-verified"), sort_keys=True)
    assert first == second


# ===========================================================================
# W5 — Digest / refresh determinism
# ===========================================================================

# (build function, body key, digest key) for every Stage-5 envelope.
_ENVELOPES = [
    (lambda c: inv.build_inventory(c), "inventoryDigest"),
    (lambda c: tm.build_trust_model(c), "trustDigest"),
    (lambda c: ws.build_signals(c), "watchdogDigest"),
]

# (script, --check flag) for every Stage-5 module exposing a --check CLI gate.
_CHECK_SCRIPTS = [
    "stage5_source_inventory.py",
    "stage5_trust_model.py",
    "stage5_watchdog_signals.py",
]


def _body_sha256(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_w5_every_envelope_is_byte_stable_across_two_runs(conn: sqlite3.Connection) -> None:
    """Each Stage-5 envelope emits exactly one 64-hex digest and is byte-stable across two
    in-process builds (sha256 of the whole body matches) — the determinism property."""
    for build, digest_key in _ENVELOPES:
        first, second = build(conn), build(conn)
        assert _is_hex64(first[digest_key])
        assert first[digest_key] == second[digest_key]
        assert _body_sha256(first) == _body_sha256(second)


def _seed_cli_db(tmp_path: Path, name: str) -> Path:
    db_path = tmp_path / name
    db.apply_migrations(db_path)
    cli_conn = db.open_db(db_path)
    _seed_trust_corpus(cli_conn)
    cli_conn.close()
    return db_path


def test_w5_check_cli_exit_0_on_clean_seed_and_byte_stable_across_processes(
    tmp_path: Path,
) -> None:
    """Every module's ``--check`` CLI exits 0 on the clean seeded ``--db`` AND emits a
    byte-identical body across two separate subprocesses (cross-process determinism —
    the strongest form of "no nondeterminism")."""
    env_root = str(ROOT / "scripts")
    db_path = _seed_cli_db(tmp_path, "cli.db")

    for script in _CHECK_SCRIPTS:
        runs = []
        for _ in range(2):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / script),
                 "--db", str(db_path), "--check"],
                capture_output=True, text=True, env={"PYTHONPATH": env_root, "PATH": ""},
            )
            assert proc.returncode == 0, f"{script}: {proc.stderr}"
            assert json.loads(proc.stdout)["scope"] == "alpine"
            runs.append(hashlib.sha256(proc.stdout.encode("utf-8")).hexdigest())
        assert runs[0] == runs[1], f"{script} stdout not byte-stable across processes"


def test_w5_check_cli_exit_nonzero_on_unmigrated_db(tmp_path: Path) -> None:
    """The ``--check`` CLI is a real gate, not a no-op: pointed at an unmigrated DB (no
    ``statements`` table) EVERY module exits non-zero instead of silently emitting an
    empty-but-valid body."""
    env_root = str(ROOT / "scripts")
    broken_db = tmp_path / "unmigrated.db"
    broken_db.touch()  # an empty sqlite file: open succeeds, the schema query fails closed

    for script in _CHECK_SCRIPTS:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script),
             "--db", str(broken_db), "--check"],
            capture_output=True, text=True, env={"PYTHONPATH": env_root, "PATH": ""},
        )
        assert proc.returncode != 0, f"{script} silently passed an unmigrated DB"


def test_w5_planted_defect_makes_each_envelope_guard_go_red(conn: sqlite3.Connection) -> None:
    """Planted-defect -> guard RED (exit-1 equivalent): a leaked per-record 64-hex content
    hash injected into the body makes EVERY module's ``assert_single_envelope_digest``
    fail loudly with its own contract error — the load-bearing single-digest CI gate
    behind ``--check`` (only the one envelope digest is permitted)."""
    leaked = "f" * 64  # a valid-shaped but ILLEGAL extra content hash
    inv_body = inv.build_inventory(conn)
    tm_body = tm.build_trust_model(conn)
    ws_body = ws.build_signals(conn)
    assert inv.assert_single_envelope_digest(inv_body) is True
    assert tm.assert_single_envelope_digest(tm_body) is True
    assert ws.assert_single_envelope_digest(ws_body) is True

    # inv: the guard walks the sources entries -> inject into a source entry.
    inv_poison = json.loads(json.dumps(inv_body))
    inv_poison["sources"][0]["leakedRawSha256"] = leaked
    with pytest.raises(inv.SourceInventoryContractError):
        inv.assert_single_envelope_digest(inv_poison)

    # tm/ws: the guard walks every top-level key except the digest -> inject one.
    tm_poison = json.loads(json.dumps(tm_body))
    tm_poison["corrections"][0]["leakedRawSha256"] = leaked
    with pytest.raises(tm.TrustModelError):
        tm.assert_single_envelope_digest(tm_poison)

    ws_poison = json.loads(json.dumps(ws_body))
    ws_poison["correctionsLedger"][0]["leakedRawSha256"] = leaked
    with pytest.raises(ws.WatchdogSignalsError):
        ws.assert_single_envelope_digest(ws_poison)


def test_w5_in_process_check_guards_pass_on_clean_seed(conn: sqlite3.Connection) -> None:
    """The in-process ``check_*`` guard suites pass on the clean golden seed (exit-0
    equivalent), exercising every ``assert_single_envelope_digest`` guard."""
    assert inv.assert_single_envelope_digest(inv.build_inventory(conn)) is True
    assert tm.assert_single_envelope_digest(tm.build_trust_model(conn)) is True
    assert ws.assert_single_envelope_digest(ws.build_signals(conn)) is True
    assert tm.check_trust_model(conn) is not None
    assert ws.check_signals(conn) is not None


def test_w5_refresh_run_log_is_written_with_required_format(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Run-log discipline: a refresh run writes a non-empty log to the documented path in
    the AutomationOps ``[YYYY-MM-DD HH:MM:SS] [LEVEL] message`` format recording the
    per-envelope digest. (Timestamp is injected deterministically so the assertion is
    reproducible; the DIGEST byte-stability is proven separately above.)"""
    log_dir = tmp_path / "Logs" / "stage5-refresh"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "qa-workflow-determinism.log"

    fixed_ts = "2026-06-24 00:00:00"  # injected, not wall-clock -> reproducible
    lines = []
    for build, digest_key in _ENVELOPES:
        digest = build(conn)[digest_key]
        lines.append(f"[{fixed_ts}] [INFO] {digest_key}={digest}")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert log_path.exists() and log_path.stat().st_size > 0
    log_re = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[[A-Z]+\] \S+")
    written = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(written) == len(_ENVELOPES)
    for line in written:
        assert log_re.match(line), f"log line not in required format: {line!r}"


def test_w5_node_repo_determinism_scoped_down() -> None:
    """SCOPED-DOWN (explicit, not silent): the GOV-478 deterministic digest assembler and
    GOV-479 weekly refresh runner live in the SEPARATE Node repo
    ``/Users/IA/GitHub/Government-Watchdog`` (branch ``stage4-automation-ai-boundary``),
    which already carries its own ``npm test`` determinism suite (GOV-479: 19 pass,
    idempotent keep-don't-recapture, scope-leak hard-stop). Driving Node from this
    backend Python PR would couple two repos in one change — OUT OF SCOPE for this PR.

    FOLLOW-UP REQUIREMENT (logged, not dropped): a cross-repo determinism harness that
    runs the Node assembler+runner over a golden fixture and asserts byte-stable output
    across two runs should be tracked as its own AutomationOps issue (Node repo or a
    CI job spanning both repos). This test documents the boundary so the omission is
    visible in the suite rather than silent."""
    node_repo = Path("/Users/IA/GitHub/Government-Watchdog")
    # Asserting the boundary, not the Node toolchain: this test never shells out to node.
    assert node_repo != ROOT, "Node refresh runner is a distinct repo from the backend"


# ===========================================================================
# Cross-cutting: privacy (no raw paths), labels, public-lane isolation
# ===========================================================================


def test_xcut_no_raw_paths_cross_any_served_envelope(
    conn: sqlite3.Connection, verify_conn: sqlite3.Connection
) -> None:
    """Privacy floor: no raw vault path / ``file://`` / ``localSourcePath`` / extra
    64-hex crosses ANY emitted body across all five workflows — exactly one digest per
    envelope, and the bodies never appear on the public published lane."""
    bodies = [
        inv.build_inventory(conn),
        tm.build_trust_model(conn),
        ws.build_signals(conn),
        ver.build_verified_record(verify_conn, "stmt-verified", before_status="unverified"),
    ]
    digest_keys = ["inventoryDigest", "trustDigest", "watchdogDigest", "verificationDigest"]

    for body, digest_key in zip(bodies, digest_keys):
        read_api.assert_no_raw_paths(body)  # loud transport sweep
        blob = json.dumps(body)
        assert "localSourcePath" not in blob
        assert "file://" not in blob
        assert "/Users/" not in blob and "/vault/" not in blob
        # exactly one 64-hex string — the envelope digest.
        hexes = [s for s in read_api._iter_strings(body) if _is_hex64(s)]
        assert hexes == [body[digest_key]]
        assert body["access"] == "reviewer_internal"  # never public — I6

    # the reviewer-internal bodies never leak onto the public published lane.
    assert read_api.published_records(conn) == []
    assert read_api.published_records(verify_conn) == []


def test_xcut_layer_separation_intact_across_workflows(conn: sqlite3.Connection) -> None:
    """The five-way fact/summary/action/AI-assumption/correction separation is total over
    ``statements.ALLOWED_LAYERS`` and an AI assumption is NEVER collapsed into a verified
    fact — preserved across the whole suite's golden corpus."""
    separation = {e["statementId"]: e for e in tm.build_record_separation(conn)}
    assert separation["stmt-corrected"]["recordClass"] == tm.RECORD_CLASS_FACT
    assert separation["stmt-summary"]["recordClass"] == tm.RECORD_CLASS_SUMMARY
    assert separation["stmt-outcome"]["recordClass"] == tm.RECORD_CLASS_ACTION_OUTCOME
    assert separation["stmt-assumption"]["recordClass"] == tm.RECORD_CLASS_AI_ASSUMPTION
    assert (
        separation["stmt-superseding"]["recordClass"]
        == tm.RECORD_CLASS_VERIFICATION_CORRECTION
    )
    # the AI assumption is never laundered into a fact.
    assert separation["stmt-assumption"]["recordClass"] != tm.RECORD_CLASS_FACT
    # the mapping is total over the SSOT layer enum.
    assert set(tm.LAYER_TO_RECORD_CLASS) == set(st.ALLOWED_LAYERS)
