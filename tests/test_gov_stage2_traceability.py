"""GOV-306 Stage 2.12 — read-surface traceability + audit-trail (read-only auditor).

Exercises ``scripts/stage2_traceability.py``: a read-only CLI auditor that proves
the end-to-end traceability invariant for everything the reviewer-internal
``read_api`` would surface — no orphan, no drift, no leak. Each check independently
recomputes the expected value from canonical columns (the ones ``to_web_safe``
strips from the served body) and compares it to the projection. Asserts the
GOV-306 acceptance bar:

- a fully assembled, healthy Alpine-shape corpus audits CLEAN (CLI exit 0);
- six dedicated RED proofs — no-orphan, no-name-leak, gap-parity, AI-backed,
  raw-linked, transport-clean — each flips ``clean=False``;
- the CLI exits non-zero on an injected break (poison-driver);
- the auditor is read-only (no row count changes) and re-uses the SSOT constants /
  the GOV-278 provenance auditor, never forking them; ``publication.py`` untouched.

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency. Mirrors the
GOV-290 served-corpus seed pattern.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import speakers as sp  # noqa: E402
import stage2_traceability as trace  # noqa: E402
import statements as st  # noqa: E402
import transcript_class as tc  # noqa: E402

POISON_EMAIL_LABEL = "Jane Doe jane.doe@alpine.gov, Mayor"


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_base(connection)
    yield connection
    connection.close()


def _seed_base(conn: sqlite3.Connection) -> None:
    """Source + meeting + agenda item + one timed, hash-preserved transcript+segment."""
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "original_url) VALUES ('alpine_packet', 'Agenda Packet', 'alpine', "
        "'document', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-05-08:item-7', 1, 7, 'Fireworks ban — adoption')"
    )
    _add_transcript_segment(conn, transcript_id=1, segment_id="seg-1",
                            transcript_class="official_transcript", sha256="0" * 64)
    conn.commit()


def _add_transcript_segment(
    conn: sqlite3.Connection, *, transcript_id: int, segment_id: str,
    transcript_class: str | None, sha256: str | None,
) -> None:
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (?, ?, ?, ?, 'n/a', ?, "
        "'2026-05-08T00:00:00Z', ?)",
        (transcript_id, f"vid-{transcript_id}", f"https://youtu.be/vid-{transcript_id}",
         "Alpine council transcript text.", sha256, transcript_class),
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES (?, ?, 0, 0, '00:00', "
        "'Mayor calls the meeting to order.')",
        (segment_id, transcript_id),
    )


def _publish_statement(
    conn: sqlite3.Connection, *, statement_id: str, segment_id: str = "seg-1",
    speaker_attribution_id: str | None = None,
) -> str:
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "segment_id": segment_id,
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "speaker_attribution_id": speaker_attribution_id,
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
    )
    return statement_id


def _add_attribution(
    conn: sqlite3.Connection, *, attribution_id: str, statement_id: str,
    attribution_state: str, speaker_class: str, display_label: str | None,
) -> None:
    conn.execute(
        "INSERT INTO speaker_attributions (speaker_attribution_id, statement_id, "
        "attribution_state, speaker_class, display_label) VALUES (?, ?, ?, ?, ?)",
        (attribution_id, statement_id, attribution_state, speaker_class, display_label),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Clean corpus — fully traceable.
# ---------------------------------------------------------------------------


def test_clean_corpus_is_fully_traceable(conn: sqlite3.Connection) -> None:
    _publish_statement(conn, statement_id="stmt-a")
    _publish_statement(conn, statement_id="stmt-b")
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="meeting folder 2026-04-10 has only derived (.md) material",
    )
    report = trace.audit_stage2_traceability(conn)
    assert report["clean"] is True, report
    assert report["served_count"] == 2
    for key in (
        "statement_grounding", "confidence_label", "speaker_label",
        "completeness_gap_parity", "ai_provenance", "raw_preservation", "transport",
    ):
        assert report[key]["clean"] is True, (key, report[key])
    # confidence_label provenance: official_transcript -> the SSOT timed label.
    assert report["confidence_label"]["drift"] == []
    assert report["completeness_gap_parity"]["no_primary_source_count"] == 1


def test_confidence_recompute_is_faithful_to_ssot(conn: sqlite3.Connection) -> None:
    """The independent recompute returns exactly the SSOT-mapped label per class."""
    sid = _publish_statement(conn, statement_id="stmt-c")
    assert trace.canonical_confidence_label(conn, sid) == "source_anchored_timed"
    # Re-class the underlying transcript -> the recompute follows the SSOT mapping.
    conn.execute("UPDATE transcripts SET transcript_class = 'minutes_only' WHERE id = 1")
    conn.commit()
    assert trace.canonical_confidence_label(conn, sid) == tc.CONFIDENCE_LABEL_BY_CLASS["minutes_only"]
    # A NULL class fails closed to the conservative default.
    conn.execute("UPDATE transcripts SET transcript_class = NULL WHERE id = 1")
    conn.commit()
    assert trace.canonical_confidence_label(conn, sid) == trace._CONSERVATIVE_CONFIDENCE_LABEL


# ---------------------------------------------------------------------------
# RED 1 — no orphan (grounding). Genuine: dangling transcript chain.
# ---------------------------------------------------------------------------


def test_grounding_orphan_red(conn: sqlite3.Connection, tmp_path: Path) -> None:
    # Orphan statement on its own transcript/segment; clean statement on seg-1.
    _publish_statement(conn, statement_id="stmt-clean")
    _add_transcript_segment(conn, transcript_id=2, segment_id="seg-2",
                            transcript_class="official_transcript", sha256="0" * 64)
    _publish_statement(conn, statement_id="stmt-orphan", segment_id="seg-2")
    assert trace.audit_stage2_traceability(conn)["statement_grounding"]["clean"] is True
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    # Raw connection (FK enforcement OFF) deletes the parent transcript, leaving the
    # segment — and therefore the served statement — grounded in nothing.
    raw = sqlite3.connect(db_path)
    raw.execute("DELETE FROM transcripts WHERE id = 2")
    raw.commit()
    raw.close()
    with db.open_db(db_path) as conn2:
        report = trace.audit_stage2_traceability(conn2)
    grounding = report["statement_grounding"]
    assert grounding["clean"] is False
    assert {o["statement_id"] for o in grounding["orphans"]} == {"stmt-orphan"}
    assert report["clean"] is False


def test_grounding_helper_both_directions(conn: sqlite3.Connection) -> None:
    sid = _publish_statement(conn, statement_id="stmt-g")
    assert trace.statement_grounded(conn, sid) is True
    assert trace.statement_grounded(conn, "no-such-statement") is False  # no canonical row


# ---------------------------------------------------------------------------
# RED 2 — no name leak (speaker_label).
# ---------------------------------------------------------------------------


def test_speaker_name_leak_red(conn: sqlite3.Connection) -> None:
    # A safely-named official is consistent; the SAME name on a non-attributed row
    # (only reachable by a forged/poisoned projection) is a leak.
    _publish_statement(conn, statement_id="stmt-off", speaker_attribution_id="off")
    _add_attribution(conn, attribution_id="off", statement_id="stmt-off",
                     attribution_state="attributed", speaker_class="on-record-official",
                     display_label="Jane Doe, Mayor")
    _publish_statement(conn, statement_id="stmt-pub", speaker_attribution_id="pub")
    _add_attribution(conn, attribution_id="pub", statement_id="stmt-pub",
                     attribution_state="unattributed", speaker_class="on-record-public",
                     display_label="Confidential Witness Q")

    # Safe set labels are always consistent.
    assert trace.speaker_label_consistent(conn, "stmt-pub", sp.SAFE_COMMUNITY_LABEL) is True
    # The proven naming gate permits the real name on the attributed official.
    assert trace.speaker_label_consistent(conn, "stmt-off", "Jane Doe, Mayor") is True
    # RED: the same name surfaced on the NON-attributed row is a leak.
    assert trace.speaker_label_consistent(conn, "stmt-pub", "Jane Doe, Mayor") is False
    # RED: a name on a statement with no backing attribution row at all is a leak.
    _publish_statement(conn, statement_id="stmt-bare")
    assert trace.speaker_label_consistent(conn, "stmt-bare", "Jane Doe, Mayor") is False
    # And the healthy audit (read_api never emits a name off-gate) is clean.
    assert trace.audit_stage2_traceability(conn)["speaker_label"]["clean"] is True


# ---------------------------------------------------------------------------
# RED 3 — gap parity (no phantom / no missing).
# ---------------------------------------------------------------------------


def test_gap_parity_red_and_clean(conn: sqlite3.Connection) -> None:
    comp.record_gap(conn, subject_node_id="m1", subject_node_type="meeting",
                    gap_type="no_primary_source")
    comp.record_gap(conn, subject_node_id="m2", subject_node_type="meeting",
                    gap_type="no_primary_source")
    cards = read_api.completeness_gap_cards(conn)
    # Healthy: read_api projects every canonical row 1:1.
    clean = trace.gap_parity(conn, cards)
    assert clean["clean"] is True
    assert clean["no_primary_source_count"] == 2 == clean["no_primary_source_projected"]
    # RED missing: drop a projected card -> a canonical gap is unaccounted for.
    missing = trace.gap_parity(conn, cards[:-1])
    assert missing["clean"] is False and missing["missing"]
    # RED phantom: add a card with no canonical row.
    phantom = trace.gap_parity(conn, cards + [{"gap_id": "ghost:meeting:x", "gap_type": "no_primary_source"}])
    assert phantom["clean"] is False and phantom["phantom"] == ["ghost:meeting:x"]


# ---------------------------------------------------------------------------
# RED 4 — AI-backed (reuse GOV-278 provenance auditor).
# ---------------------------------------------------------------------------


def test_ai_provenance_orphan_red(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _publish_statement(conn, statement_id="stmt-ok")
    assert trace.audit_stage2_traceability(conn)["ai_provenance"]["clean"] is True
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    # Raw INSERT plants a produced_by='ai' row with a NULL run id — the exact orphan
    # the GOV-278 write gate exists to prevent; the reused auditor must catch it.
    raw = sqlite3.connect(db_path)
    raw.execute(
        "INSERT INTO statements (statement_id, statement_text, produced_by, "
        "ai_extraction_run_id) VALUES ('orphan-ai', 'x', 'ai', NULL)"
    )
    raw.commit()
    raw.close()
    with db.open_db(db_path) as conn2:
        report = trace.audit_stage2_traceability(conn2)
    assert report["ai_provenance"]["clean"] is False
    assert report["ai_provenance"]["orphan_count"] == 1
    assert report["clean"] is False


# ---------------------------------------------------------------------------
# RED 5 — raw-linked (reproducible citation). Genuine: transcript with NULL sha256.
# ---------------------------------------------------------------------------


def test_raw_linkage_red(conn: sqlite3.Connection) -> None:
    # An empty sha256 (passes the NOT NULL column but is no real hash) means the raw
    # was never actually preserved -> the citation is not reproducible.
    _add_transcript_segment(conn, transcript_id=3, segment_id="seg-3",
                            transcript_class="official_transcript", sha256="")
    sid = _publish_statement(conn, statement_id="stmt-noraw", segment_id="seg-3")
    conn.commit()
    assert trace.raw_linked(conn, sid) is False
    report = trace.audit_stage2_traceability(conn)
    raw_check = report["raw_preservation"]
    assert raw_check["clean"] is False
    assert {u["statement_id"] for u in raw_check["unlinked"]} == {"stmt-noraw"}
    assert report["clean"] is False
    # A hash-preserved transcript IS linked.
    clean_sid = _publish_statement(conn, statement_id="stmt-raw-ok")
    assert trace.raw_linked(conn, clean_sid) is True


# ---------------------------------------------------------------------------
# RED 6 — transport (no raw path / PII). Genuine: PII rides in via display_label.
# ---------------------------------------------------------------------------


def test_transport_pii_red(conn: sqlite3.Connection) -> None:
    # An attributed official whose display_label carries an email: read_api serves
    # the label verbatim (gate passes), to_web_safe passes it, and assert_no_raw_paths
    # only scans PATHS — so the transport PII sweep is the backstop that catches it.
    _publish_statement(conn, statement_id="stmt-pii", speaker_attribution_id="pii")
    _add_attribution(conn, attribution_id="pii", statement_id="stmt-pii",
                     attribution_state="attributed", speaker_class="on-record-official",
                     display_label=POISON_EMAIL_LABEL)
    report = trace.audit_stage2_traceability(conn)
    assert report["transport"]["clean"] is False
    assert "email" in report["transport"]["error"].lower()
    assert report["clean"] is False


def test_transport_clean_on_healthy_body(conn: sqlite3.Connection) -> None:
    _publish_statement(conn, statement_id="stmt-t")
    assert trace.transport_clean(conn)["clean"] is True


# ---------------------------------------------------------------------------
# Reviewer-internal lane is audited too (GOV-146 view).
# ---------------------------------------------------------------------------


def test_reviewer_internal_lane_is_audited(conn: sqlite3.Connection) -> None:
    run_id = "gov306:ai-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-ri",
            "segment_id": "seg-1",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "speaker_attribution_id": "ri-attr",
            "statement_text": "A Town Council special meeting was convened.",
            "produced_by": "ai",
            "layer": "ai_thought_then",
            "ai_extraction_run_id": run_id,
        },
    )
    _add_attribution(conn, attribution_id="ri-attr", statement_id="stmt-ri",
                     attribution_state="unattributed", speaker_class="on-record-public",
                     display_label="Confidential Witness Q")
    ai.finalize_run(conn, run_id, output_statement_ids=["stmt-ri"],
                    output_evidence_link_ids=[], orphan_rejected_count=0, error_status="ok")
    gate.register_reviewer(conn, "reviewer:isaac", display_name="Isaac",
                           registered_by="owner:isaac", note="GOV-306 reviewer-internal audit")
    gate.promote_statement(conn, "stmt-ri", reviewer_id="reviewer:isaac", decision="approved",
                           reason="reviewer-internal source-grounded civic announcement",
                           to_verification_status="reviewed_source_linked")
    report = trace.audit_stage2_traceability(conn)
    # The reviewer-internal row is counted and traceable; it never enters the public lane.
    served_ids = {o["statement_id"] for o in report["statement_grounding"]["orphans"]}
    assert "stmt-ri" not in served_ids  # grounded, not an orphan
    assert report["served_count"] >= 1
    assert report["clean"] is True


# ---------------------------------------------------------------------------
# CLI poison-driver + exit codes.
# ---------------------------------------------------------------------------


def test_cli_exit_codes_and_poison_driver(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "cli.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as c:
        _seed_base(c)
        _publish_statement(c, statement_id="stmt-cli")
    # Clean corpus -> exit 0.
    assert trace.main(["--db", str(db_path)]) == 0
    # Missing DB -> usage/IO exit 2.
    assert trace.main(["--db", str(tmp_path / "nope.db")]) == 2
    # Poison: delete the grounding transcript (FK off) -> orphan -> exit 1.
    raw = sqlite3.connect(db_path)
    raw.execute("DELETE FROM transcripts WHERE id = 1")
    raw.commit()
    raw.close()
    assert trace.main(["--db", str(db_path)]) == 1
    # --json emits a machine report carrying the verdict.
    capsys.readouterr()
    assert trace.main(["--db", str(db_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is False
    assert payload["statement_grounding"]["clean"] is False


# ---------------------------------------------------------------------------
# Read-only by construction + SSOT parity + publication.py untouched.
# ---------------------------------------------------------------------------


def test_audit_is_read_only(conn: sqlite3.Connection) -> None:
    _publish_statement(conn, statement_id="stmt-ro")
    comp.record_gap(conn, subject_node_id="m-ro", subject_node_type="meeting",
                    gap_type="no_primary_source")
    counts = lambda: {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("statements", "completeness_gaps", "speaker_attributions",
                  "transcripts", "crawl_runs", "evidence_links")
    }
    before = counts()
    trace.audit_stage2_traceability(conn)
    assert counts() == before


def test_ssot_constants_imported_not_copied() -> None:
    assert trace._CONSERVATIVE_CONFIDENCE_LABEL == tc.CONFIDENCE_LABEL_BY_CLASS[
        tc.DEFAULT_TRANSCRIPT_CLASS
    ]
    assert trace._SAFE_SPEAKER_LABELS == {sp.SAFE_GENERIC_LABEL, sp.SAFE_COMMUNITY_LABEL}
    assert trace.sp.AUTO_NAMEABLE_CLASSES is sp.AUTO_NAMEABLE_CLASSES


def test_publication_allowlist_untouched() -> None:
    """This slice adds NO web-safe field and changes NO serving behavior."""
    for col in ("transcript_class", "speaker_attribution_id", "display_label",
                "ai_extraction_run_id", "segment_id"):
        assert col not in pub.WEB_SAFE_FIELD_ALLOWLIST
    # The derived envelope keys are not smuggled into the allowlist.
    for key in ("confidence_label", "speaker_label"):
        assert key not in pub.WEB_SAFE_FIELD_ALLOWLIST
