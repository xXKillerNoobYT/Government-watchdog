"""GOV-322 Stage 2.13 — read-surface back-gap / coverage-regression auditor.

Proves :mod:`stage2_backgap` catches *silent shrinkage*: a canonical record that
SHOULD reach a reviewer/public lane but stops being served. The forward auditor
(GOV-306) is blind to this — every *remaining* served row is still grounded — so a
dedicated inverse net is required before a first external reviewer.

Builds ONE deterministic Alpine fixture corpus of promoted reviewer-internal
statements (mirroring the GOV-318 integration corpus), asserts the auditor is CLEAN
on the healthy DB, then for ≥2 checks injects a synthetic back-gap and proves the
auditor flips to non-clean. A check that cannot go RED is not a check.

Test-only / read-only: imports existing ``stage2_backgap`` + ``read_api`` functions,
adds NO production projection, envelope key, schema, migration, AI, or network. Pure
sqlite + tmp files, Alpine-only. If a real back-gap surfaced here, it would be a
SEPARATE CTO-routed defect — this ticket ships the net, not the patch.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402
import speakers as sp  # noqa: E402
import stage2_backgap as bg  # noqa: E402
import statements as st  # noqa: E402

REVIEWER = "reviewer:isaac"
POISON_NAME = "Confidential Witness Q"


# ---------------------------------------------------------------------------
# Deterministic Alpine fixture corpus (mirrors the GOV-318 integration corpus).
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_corpus(connection)
    yield connection
    connection.close()


def _seed_anchors(conn: sqlite3.Connection) -> None:
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
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (1, 'vid-1', "
        "'https://youtu.be/vid-1', 'Alpine council transcript text.', 'n/a', ?, "
        "'2026-05-08T00:00:00Z', 'official_transcript')",
        ("0" * 64,),
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-grounded', 1, 0, 0, '00:00', 'Mayor calls the meeting to order.')"
    )
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (3, 'vid-3', "
        "'https://youtu.be/vid-3', 'Class-poisoned transcript text.', 'n/a', '', "
        "'2026-05-08T00:00:00Z', 'no_transcript')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-poison', 3, 0, 0, '00:00', 'Class-poisoned segment.')"
    )
    gate.register_reviewer(
        conn, REVIEWER, display_name="Isaac", registered_by="owner:isaac",
        note="GOV-322 back-gap auditor",
    )
    conn.commit()


def _add_attribution(conn, *, attribution_id, statement_id, attribution_state,
                     speaker_class, display_label) -> None:
    conn.execute(
        "INSERT INTO speaker_attributions (speaker_attribution_id, statement_id, "
        "attribution_state, speaker_class, display_label) VALUES (?, ?, ?, ?, ?)",
        (attribution_id, statement_id, attribution_state, speaker_class, display_label),
    )
    conn.commit()


def _serve_statement(conn, *, statement_id, segment_id, produced_by="human",
                     ai_run_id=None, speaker_attribution_id=None) -> None:
    record: dict[str, object] = {
        "statement_id": statement_id,
        "segment_id": segment_id,
        "agenda_item_id": "alpine:2026-05-08:item-7",
        "statement_text": "The council adopted the fireworks ban.",
        "produced_by": produced_by,
    }
    if speaker_attribution_id is not None:
        record["speaker_attribution_id"] = speaker_attribution_id
    if produced_by == "ai":
        record["layer"] = "ai_thought_then"
        record["ai_extraction_run_id"] = ai_run_id
    st.insert_statement(conn, record)
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _seed_corpus(conn: sqlite3.Connection) -> None:
    _seed_anchors(conn)
    ok_run = "gov322:ok-run"
    ai.create_run(conn, run_id=ok_run, input_source_ids=[])

    _serve_statement(conn, statement_id="s-official", segment_id="seg-grounded",
                     speaker_attribution_id="attr-official")
    _add_attribution(conn, attribution_id="attr-official", statement_id="s-official",
                     attribution_state="attributed", speaker_class="on-record-official",
                     display_label="Jane Doe, Mayor")

    _serve_statement(conn, statement_id="s-ai", segment_id="seg-grounded",
                     produced_by="ai", ai_run_id=ok_run, speaker_attribution_id="attr-ai")
    _add_attribution(conn, attribution_id="attr-ai", statement_id="s-ai",
                     attribution_state="attributed", speaker_class="on-record-public",
                     display_label=POISON_NAME)

    _serve_statement(conn, statement_id="s-poison", segment_id="seg-poison",
                     speaker_attribution_id="attr-poison")
    _add_attribution(conn, attribution_id="attr-poison", statement_id="s-poison",
                     attribution_state="uncertain", speaker_class="on-record-official",
                     display_label=f"{POISON_NAME}, Mayor")

    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="meeting folder 2026-04-10 has only derived (.md) material", commit=True,
    )
    comp.record_gap(
        conn, subject_node_id="2026-04-11", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="see /Users/IA/Documents/TownOfAlpine/secret.md", commit=True,
    )
    conn.commit()


def _add_publishable(conn: sqlite3.Connection) -> None:
    """A genuinely publish-eligible (owner-published) row — exercises the public lane."""
    st.insert_statement(
        conn,
        {
            "statement_id": "s-published",
            "segment_id": "seg-grounded",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Published civic fact.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
    )
    conn.commit()


# ===========================================================================
# GREEN — the auditor is clean on a healthy Alpine DB.
# ===========================================================================


def test_audit_is_clean_on_healthy_corpus(conn: sqlite3.Connection) -> None:
    report = bg.audit_backgap(conn)
    assert report["clean"], report


def test_reviewer_lane_eligible_equals_served(conn: sqlite3.Connection) -> None:
    eligible = bg.reviewer_eligible_ids(conn)
    served = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    # Independent recompute and the actual serve agree exactly — no back-gap, no phantom.
    assert eligible == served
    assert eligible == {"s-official", "s-ai", "s-poison"}


def test_public_lane_clean_and_covers_published_row(conn: sqlite3.Connection) -> None:
    # Pre-publish: both empty.
    assert bg.public_lane_no_backgap(conn)["clean"]
    assert bg.publish_eligible_ids(conn) == set()
    # After an owner publish, the eligible set picks it up and the serve covers it.
    _add_publishable(conn)
    assert bg.publish_eligible_ids(conn) == {"s-published"}
    assert bg.public_lane_no_backgap(conn)["clean"]


def test_gap_coverage_parity_clean(conn: sqlite3.Connection) -> None:
    report = bg.completeness_gap_coverage_parity(conn)
    assert report["clean"]
    assert report["canonical_count"] == report["projected_count"] == 2
    assert report["no_primary_source_canonical"] == 2


def test_overlay_and_floor_clean(conn: sqlite3.Connection) -> None:
    assert bg.overlay_presence_no_regression(conn)["clean"]
    assert bg.stage1_field_floor(conn)["clean"]


def test_determinism_two_passes_identical(conn: sqlite3.Connection) -> None:
    report = bg.determinism_read_only(conn)
    assert report["byte_identical"] and report["row_counts_stable"]


def test_audit_does_not_mutate_db(conn: sqlite3.Connection) -> None:
    """Read-only proof: row counts before/after a full audit are unchanged."""
    before = conn.execute("SELECT count(*) FROM statements").fetchone()[0]
    bg.audit_backgap(conn)
    after = conn.execute("SELECT count(*) FROM statements").fetchone()[0]
    assert before == after


# ===========================================================================
# RED — each injected back-gap flips the corresponding check to non-clean.
# (≥2 required; five proven here.)
# ===========================================================================


def test_red_reviewer_lane_backgap_detected(conn: sqlite3.Connection, monkeypatch) -> None:
    """Check 1 RED: elide one served reviewer-internal record -> served < eligible."""
    real = read_api.reviewer_internal_records

    def _drop_one(conn_):
        return [r for r in real(conn_) if r["statement_id"] != "s-poison"]

    monkeypatch.setattr(read_api, "reviewer_internal_records", _drop_one)
    report = bg.reviewer_lane_no_backgap(conn)
    assert not report["clean"]
    assert report["backgap"] == ["s-poison"]
    assert bg.audit_backgap(conn)["clean"] is False


def test_red_public_lane_backgap_detected(conn: sqlite3.Connection, monkeypatch) -> None:
    """Check 2 RED: a publishable row exists but the serve drops it -> back-gap."""
    _add_publishable(conn)
    monkeypatch.setattr(read_api, "published_records", lambda conn_: [])
    report = bg.public_lane_no_backgap(conn)
    assert not report["clean"]
    assert report["backgap"] == ["s-published"]


def test_red_gap_coverage_drop_detected(conn: sqlite3.Connection, monkeypatch) -> None:
    """Check 3 RED: drop one projected gap card -> canonical gap silently dropped."""
    real = read_api.completeness_gap_cards

    def _drop_one(conn_):
        cards = real(conn_)
        return cards[1:]  # drop the first card

    monkeypatch.setattr(read_api, "completeness_gap_cards", _drop_one)
    report = bg.completeness_gap_coverage_parity(conn)
    assert not report["clean"]
    assert len(report["dropped"]) == 1


def test_red_overlay_missing_detected(conn: sqlite3.Connection, monkeypatch) -> None:
    """Check 4 RED: an overlay returns None -> present-but-off-SSOT, caught."""
    monkeypatch.setattr(read_api, "_provenance_status_for", lambda conn_, record: None)
    report = bg.overlay_presence_no_regression(conn)
    assert not report["clean"]
    assert report["missing"]


def test_red_stage1_floor_breach_detected(conn: sqlite3.Connection, monkeypatch) -> None:
    """Check 5 RED: a serializer that strips ui_status -> Stage-1 floor breach."""
    real = read_api._serialize_statement

    def _strip_ui_status(conn_, record, ui_status, *, include_provenance_status=False):
        safe = real(conn_, record, ui_status, include_provenance_status=include_provenance_status)
        safe.pop("ui_status", None)
        return safe

    monkeypatch.setattr(read_api, "_serialize_statement", _strip_ui_status)
    report = bg.stage1_field_floor(conn)
    assert not report["clean"]
    assert report["breaches"]


# ===========================================================================
# CLI exit ladder — 0 clean / 1 back-gap / 2 usage (DB missing).
# ===========================================================================


def test_cli_exit_0_on_clean_db(conn: sqlite3.Connection, tmp_path: Path) -> None:
    conn.commit()
    assert bg.main(["--db", str(tmp_path / "test.db")]) == 0


def test_cli_exit_1_on_injected_backgap(conn: sqlite3.Connection, tmp_path: Path, monkeypatch) -> None:
    """CLI exit ladder: an injected back-gap makes the gate exit 1 (non-zero)."""
    conn.commit()
    real = read_api.reviewer_internal_records
    monkeypatch.setattr(
        read_api, "reviewer_internal_records",
        lambda conn_: [r for r in real(conn_) if r["statement_id"] != "s-poison"],
    )
    assert bg.main(["--db", str(tmp_path / "test.db")]) == 1


def test_cli_exit_2_on_missing_db(tmp_path: Path) -> None:
    assert bg.main(["--db", str(tmp_path / "nope.db")]) == 2
