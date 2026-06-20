"""GOV-318 Stage 2.10 — read-surface integration safety net.

The five Stage-2 reviewer-internal overlays in ``scripts/read_api.py`` —
``_eligible_ui_status`` (10-value base), ``_confidence_label_for`` (GOV-283),
``_speaker_label_for`` (GOV-290), ``completeness_gap_cards`` (GOV-298), and
``_provenance_status_for`` (GOV-311) — each ship with their OWN isolated unit
test + poison driver. None of those compose all five over a realistic
multi-record Alpine corpus and assert the cross-surface invariants *together*.
This is that end-to-end safety net, the integration-level analogue of the
Stage-1 ``slice{1,2,3}`` smokes, run before the reviewable beta.

It builds ONE deterministic Alpine fixture corpus of multiple promoted
reviewer-internal statements spanning the overlay dimensions, drives the WHOLE
composed body through ``read_api.build_response`` once, and asserts:

- INV1 — all five overlays co-present on the same body and SSOT-bounded (no
  overlay silently absent);
- INV2 — a combined multi-dimension poison fails every overlay CLOSED
  independently on the SAME composed pass (proven RED: flip one overlay
  fail-open and the assertion fails);
- INV3 — cross-lane no-leak: zero reviewer-internal ids / envelope keys reach
  the public lane, which stays byte-identical to its overlay-free shape
  (proven RED: a lane-blind serialize leaks ``provenance_status`` publicly);
- INV4 — the transport guard (``assert_no_raw_paths``) holds over the whole
  composed body even with poisoned upstream rows;
- INV5 — determinism: two passes over the same fixture are byte-identical.

Test-only / read-only: imports existing ``read_api`` functions, adds NO
production projection, envelope key, schema, migration, AI, or network. Pure
sqlite + tmp files, Alpine-only. If a composed assertion surfaces a real
fail-open / leak in ``read_api.py``, that is a SEPARATE defect (CTO-routed) —
this ticket ships the net, it does not patch what the net catches.
"""

from __future__ import annotations

import json
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
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import speakers as sp  # noqa: E402
import statements as st  # noqa: E402
import transcript_class as tc  # noqa: E402

REVIEWER = "reviewer:isaac"

# A candidate-identity name that must NEVER surface in any served body. It is
# poisoned into the display_label of a NON-attributed speaker row; the GOV-290
# read-time re-guard must derive the label from speaker_class alone.
POISON_NAME = "Confidential Witness Q"

# The exact key set a served reviewer-internal record may carry: the web-safe
# allowlist plus the four derived API-envelope keys. A served body that is NOT a
# subset of this leaked a raw column (mirrors the GOV-311 no-leak posture).
_ALLOWED_RECORD_KEYS = set(pub.WEB_SAFE_FIELD_ALLOWLIST) | {
    "ui_status", "evidence", "confidence_label", "speaker_label", "provenance_status",
}

# Every envelope overlay key that MUST be present on a served reviewer-internal
# record — the "no overlay silently absent" set.
_REQUIRED_OVERLAY_KEYS = ("ui_status", "confidence_label", "speaker_label", "provenance_status")

# The SSOT-bounded ranges each overlay's value must fall inside.
_VALID_CONFIDENCE_LABELS = frozenset(tc.CONFIDENCE_LABEL_BY_CLASS.values())
# Only the two SSOT safe labels, plus any genuinely safe "Name, Role" stored on
# an attributed + on-record-official row, may appear. We assert the safe-label
# floor structurally per-record below; this set bounds the non-named outputs.
_SAFE_SPEAKER_LABELS = frozenset({sp.SAFE_GENERIC_LABEL, sp.SAFE_COMMUNITY_LABEL})

_FLOOR_CONFIDENCE = tc.CONFIDENCE_LABEL_BY_CLASS[tc.DEFAULT_TRANSCRIPT_CLASS]


# ---------------------------------------------------------------------------
# Deterministic Alpine fixture corpus.
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
    """Source + meeting + agenda item + the three transcript/segment anchors.

    * ``seg-grounded`` — official_transcript, sha256 recorded -> grounded AND
      raw-preserved (the healthy anchor): confidence ``source_anchored_timed``.
    * ``seg-unpreserved`` — official_transcript, BLANK sha256 -> grounded but the
      raw leg fails: provenance ``unverified``, confidence still timed.
    * ``seg-poison`` — ``no_transcript`` class (off the confidence map ->
      confidence FLOOR) AND blank sha256 (raw leg fails -> provenance unverified).
    """
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
    # transcript 1: hash-preserved, official_transcript (grounded + raw-linked).
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
    # transcript 2: official_transcript, BLANK sha256 -> grounded but NOT preserved.
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (2, 'vid-2', "
        "'https://youtu.be/vid-2', 'Unpreserved transcript text.', 'n/a', '', "
        "'2026-05-08T00:00:00Z', 'official_transcript')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-unpreserved', 2, 0, 0, '00:00', 'Unpreserved segment.')"
    )
    # transcript 3: no_transcript class (off confidence map) AND blank sha256.
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
        note="GOV-318 read-surface integration safety net",
    )
    conn.commit()


def _add_attribution(
    conn: sqlite3.Connection,
    *,
    attribution_id: str,
    statement_id: str,
    attribution_state: str,
    speaker_class: str,
    display_label: str | None,
) -> None:
    """INSERT one speaker_attributions row DIRECTLY (controls display_label exactly).

    Writing the row directly — not via the safe ``speakers.attribute_speaker``
    path — lets the corpus plant an adversarial name on a NON-attributed row,
    which the write path would never produce. That is the point of the RED
    fail-closed proof: read_api must re-guard regardless of how the value landed.
    """
    conn.execute(
        "INSERT INTO speaker_attributions (speaker_attribution_id, statement_id, "
        "attribution_state, speaker_class, display_label) VALUES (?, ?, ?, ?, ?)",
        (attribution_id, statement_id, attribution_state, speaker_class, display_label),
    )
    conn.commit()


def _promote(conn: sqlite3.Connection, statement_id: str) -> None:
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _serve_statement(
    conn: sqlite3.Connection,
    *,
    statement_id: str,
    segment_id: str,
    produced_by: str = "human",
    ai_run_id: str | None = None,
    speaker_attribution_id: str | None = None,
) -> None:
    """Insert + promote one statement into the reviewer-internal serve (segment-anchored)."""
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
    _promote(conn, statement_id)


def _seed_corpus(conn: sqlite3.Connection) -> None:
    """One deterministic multi-record Alpine corpus spanning every overlay dimension."""
    _seed_anchors(conn)
    ok_run = "gov318:ok-run"
    ai.create_run(conn, run_id=ok_run, input_source_ids=[])  # defaults error_status='ok'

    # (1) HEALTHY official: grounded + raw-preserved + attributed official speaker.
    #     confidence=source_anchored_timed, speaker="Jane Doe, Mayor", provenance=grounded.
    _serve_statement(conn, statement_id="s-official", segment_id="seg-grounded",
                     speaker_attribution_id="attr-official")
    _add_attribution(conn, attribution_id="attr-official", statement_id="s-official",
                     attribution_state="attributed", speaker_class="on-record-official",
                     display_label="Jane Doe, Mayor")

    # (2) HEALTHY AI: grounded + raw-preserved + ok producing run + community speaker.
    #     provenance=grounded (AI leg passes), speaker=Community Member.
    _serve_statement(conn, statement_id="s-ai", segment_id="seg-grounded",
                     produced_by="ai", ai_run_id=ok_run, speaker_attribution_id="attr-ai")
    _add_attribution(conn, attribution_id="attr-ai", statement_id="s-ai",
                     attribution_state="attributed", speaker_class="on-record-public",
                     display_label=POISON_NAME)  # on-record-public is NOT auto-nameable

    # (3) UNPRESERVED: grounded chain but blank sha256 -> provenance unverified
    #     (the raw leg, in isolation, on an otherwise-healthy timed anchor).
    _serve_statement(conn, statement_id="s-unpreserved", segment_id="seg-unpreserved")

    # (4) COMBINED MULTI-DIMENSION POISON on one served row:
    #     - no_transcript class      -> confidence FLOOR
    #     - blank sha256             -> raw leg fails -> provenance UNVERIFIED
    #     - non-attributed + name    -> speaker collapses to SAFE_GENERIC_LABEL
    _serve_statement(conn, statement_id="s-poison", segment_id="seg-poison",
                     speaker_attribution_id="attr-poison")
    _add_attribution(conn, attribution_id="attr-poison", statement_id="s-poison",
                     attribution_state="uncertain", speaker_class="on-record-official",
                     display_label=f"{POISON_NAME}, Mayor")

    # (5) Completeness-gap dimension: clean + PII + FS-path detail rows. The
    #     poisoned details must be OMITTED while the gap ROWS stay countable.
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="meeting folder 2026-04-10 has only derived (.md) material", commit=True,
    )
    comp.record_gap(  # record_gap's guard is PII-only, not path-aware: read-time must catch.
        conn, subject_node_id="2026-04-11", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="see /Users/IA/Documents/TownOfAlpine/secret.md", commit=True,
    )
    _plant_gap_pii(conn, gap_id="no_primary_source:meeting:m-pii", subject_node_id="m-pii",
                   detail="reported by resident jane.doe@example.com")
    conn.commit()


def _plant_gap_pii(conn: sqlite3.Connection, *, gap_id: str, subject_node_id: str, detail: str) -> None:
    """INSERT a PII-detail gap row directly (record_gap's write guard would block it)."""
    conn.execute(
        "INSERT INTO completeness_gaps (gap_id, subject_node_id, subject_node_type, "
        "gap_type, severity, detail, source_id, detected_run_id, detected_utc, "
        "resolved_status, produced_by) VALUES (?, ?, 'meeting', 'no_primary_source', "
        "'warn', ?, NULL, NULL, '2026-06-19T00:00:00.000+00:00', 'open', 'deterministic')",
        (gap_id, subject_node_id, detail),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Composed-pass helpers.
# ---------------------------------------------------------------------------


def _composed(conn: sqlite3.Connection) -> dict:
    """The whole reviewer-internal composed body in ONE pass (records + gaps + sweep)."""
    return read_api.build_response(
        conn, include_records=True, include_reviewer_internal=True,
        include_completeness_gaps=True,
    )


def _by_id(records: list[dict]) -> dict[str, dict]:
    return {r["statement_id"]: r for r in records}


# ===========================================================================
# INV1 — all five overlays co-present on the same body, SSOT-bounded.
# ===========================================================================


def test_inv1_all_five_overlays_copresent_and_ssot_bounded(conn: sqlite3.Connection) -> None:
    body = _composed(conn)
    records = body["reviewer_internal_records"]
    assert len(records) == 4, [r["statement_id"] for r in records]

    for record in records:
        # No overlay silently absent — every envelope key present.
        for key in _REQUIRED_OVERLAY_KEYS:
            assert key in record, f"{record['statement_id']}: overlay {key!r} absent"
        # ...and each within its SSOT-bounded range.
        assert record["ui_status"] in pub.PUBLICATION_ELIGIBLE_UI_STATUSES
        assert record["confidence_label"] in _VALID_CONFIDENCE_LABELS
        assert (
            record["speaker_label"] in _SAFE_SPEAKER_LABELS
            or record["speaker_label"] == "Jane Doe, Mayor"  # the one proven naming gate
        )
        assert record["provenance_status"] in read_api.PROVENANCE_STATUS_VALUES

    # The completeness-gap overlay is present and every card is value-bounded.
    cards = body["completeness_gaps"]
    assert len(cards) == 3
    for card in cards:
        assert set(card) <= read_api.GAP_CARD_FIELDS
        assert card["gap_type"] in comp.GAP_TYPES or card["gap_type"] == read_api._UNKNOWN_GAP_TYPE
        assert card["severity"] in comp.SEVERITIES
        assert card["resolved_status"] in comp.RESOLVED_STATUSES


def test_inv1_healthy_rows_carry_expected_overlay_values(conn: sqlite3.Connection) -> None:
    """Pin the healthy-row overlay values so a silent overlay regression is caught."""
    records = _by_id(read_api.reviewer_internal_records(conn))

    official = records["s-official"]
    assert official["confidence_label"] == "source_anchored_timed"
    assert official["speaker_label"] == "Jane Doe, Mayor"
    assert official["provenance_status"] == read_api.PROVENANCE_GROUNDED

    ai_row = records["s-ai"]
    assert ai_row["confidence_label"] == "source_anchored_timed"
    assert ai_row["speaker_label"] == sp.SAFE_COMMUNITY_LABEL  # on-record-public, name dropped
    assert ai_row["provenance_status"] == read_api.PROVENANCE_GROUNDED


# ===========================================================================
# INV2 — combined multi-dimension poison fails every overlay CLOSED, one pass.
# ===========================================================================


def _poison_violations(conn: sqlite3.Connection) -> list[str]:
    """The composed-pass poison checks, as a list of violations (empty == green).

    Returns rather than asserts so the RED proof can flip an overlay fail-open and
    observe the SAME checks start failing.
    """
    body = _composed(conn)
    records = _by_id(body["reviewer_internal_records"])
    cards = body["completeness_gaps"]
    blob = json.dumps(body)
    out: list[str] = []

    poison = records.get("s-poison")
    if poison is None:
        return ["s-poison not served"]
    # Speaker overlay fails CLOSED — candidate-identity name collapses to safe.
    if poison["speaker_label"] != sp.SAFE_GENERIC_LABEL:
        out.append(f"speaker not safe: {poison['speaker_label']!r}")
    # Confidence overlay fails CLOSED — off-map class -> conservative floor.
    if poison["confidence_label"] != _FLOOR_CONFIDENCE:
        out.append(f"confidence not floor: {poison['confidence_label']!r}")
    # Provenance overlay fails CLOSED — blank raw -> unverified.
    if poison["provenance_status"] != read_api.PROVENANCE_UNVERIFIED:
        out.append(f"provenance not unverified: {poison['provenance_status']!r}")
    # The independent raw-leg poison row is unverified too.
    if records["s-unpreserved"]["provenance_status"] != read_api.PROVENANCE_UNVERIFIED:
        out.append("unpreserved row not unverified")
    # Gap detail overlay fails CLOSED — PII / FS-path details OMITTED, rows kept.
    if any("detail" in c for c in cards if c["subject_id"] in ("2026-04-11", "m-pii")):
        out.append("poisoned gap detail not omitted")
    if {c["subject_id"] for c in cards} != {"2026-04-10", "2026-04-11", "m-pii"}:
        out.append("a poisoned gap ROW was hidden")
    # The poisoned candidate name appears NOWHERE in the whole composed body.
    if POISON_NAME in blob:
        out.append("poison name leaked into composed body")
    return out


def test_inv2_combined_poison_fails_every_overlay_closed(conn: sqlite3.Connection) -> None:
    assert _poison_violations(conn) == []


def test_inv2_red_speaker_fail_open_breaks_the_net(conn: sqlite3.Connection, monkeypatch) -> None:
    """RED: force the speaker overlay to trust the raw display_label -> net fails."""
    def _fail_open(conn_, record):
        row = conn_.execute(
            "SELECT display_label FROM speaker_attributions WHERE speaker_attribution_id = ?",
            (record.get("speaker_attribution_id"),),
        ).fetchone()
        return row["display_label"] if row and row["display_label"] else sp.SAFE_GENERIC_LABEL

    monkeypatch.setattr(read_api, "_speaker_label_for", _fail_open)
    violations = _poison_violations(conn)
    assert any("speaker" in v or "poison name leaked" in v for v in violations), violations


def test_inv2_red_provenance_fail_open_breaks_the_net(conn: sqlite3.Connection, monkeypatch) -> None:
    """RED: force the provenance overlay to optimistically grant grounded -> net fails."""
    monkeypatch.setattr(
        read_api, "_provenance_status_for", lambda conn_, record: read_api.PROVENANCE_GROUNDED
    )
    violations = _poison_violations(conn)
    assert any("provenance" in v for v in violations), violations


def test_inv2_red_confidence_fail_open_breaks_the_net(conn: sqlite3.Connection, monkeypatch) -> None:
    """RED: force the confidence overlay to never collapse to floor -> net fails."""
    monkeypatch.setattr(
        read_api, "_confidence_label_for", lambda conn_, record: "source_anchored_timed"
    )
    violations = _poison_violations(conn)
    assert any("confidence" in v for v in violations), violations


def test_inv2_red_gap_detail_fail_open_breaks_the_net(conn: sqlite3.Connection, monkeypatch) -> None:
    """RED: force the gap-detail guard to pass everything -> poisoned detail surfaces.

    The composed body would then carry an FS path; build_response's transport sweep
    raises FIRST (INV4), which is itself the fail-closed backstop — so we assert the
    net trips one way or the other (RawPathLeak OR a recorded violation)."""
    monkeypatch.setattr(read_api, "_safe_gap_detail", lambda detail: detail)
    try:
        violations = _poison_violations(conn)
    except read_api.RawPathLeak:
        return  # transport backstop tripped — fail-closed proven
    assert any("gap detail" in v for v in violations), violations


# ===========================================================================
# INV3 — cross-lane no-leak: nothing reviewer-internal reaches the public lane.
# ===========================================================================


def test_inv3_public_lane_carries_no_reviewer_internal_ids_or_keys(conn: sqlite3.Connection) -> None:
    published = read_api.published_records(conn)
    reviewer_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    public_ids = {r["statement_id"] for r in published}

    # Zero reviewer-internal statement ids appear publicly.
    assert reviewer_ids, "fixture must serve a non-empty reviewer-internal set"
    assert public_ids.isdisjoint(reviewer_ids)
    # The pre-publish corpus is entirely reviewer-internal: public lane is empty.
    assert published == []
    # Zero reviewer-internal envelope keys leak onto any public record.
    for record in published:
        assert "provenance_status" not in record


def test_inv3_public_lane_byte_identical_to_overlay_free_shape(conn: sqlite3.Connection) -> None:
    """An owner-published row gets the byte-identical pre-2.12 shape (no prov key)."""
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
    record = _by_id(read_api.published_records(conn))["s-published"]
    # The public lane carries the four Stage-1/2.07 overlays but NEVER the 2.12
    # reviewer-internal provenance key.
    assert "provenance_status" not in record
    assert set(record) <= (_ALLOWED_RECORD_KEYS - {"provenance_status"})
    # ...and is not duplicated into the reviewer-internal serve.
    assert "s-published" not in {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}


def test_inv3_red_lane_blind_serialize_leaks_provenance_publicly(
    conn: sqlite3.Connection, monkeypatch
) -> None:
    """RED: a lane-blind serialize (always attaching provenance_status) leaks the
    reviewer-internal key onto the public lane -> the no-leak assertion fails."""
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
    real_serialize = read_api._serialize_statement

    def _lane_blind(conn_, record, ui_status, *, include_provenance_status=False):
        return real_serialize(conn_, record, ui_status, include_provenance_status=True)

    monkeypatch.setattr(read_api, "_serialize_statement", _lane_blind)
    record = _by_id(read_api.published_records(conn))["s-published"]
    assert "provenance_status" in record  # the leak the real lane gate prevents


# ===========================================================================
# INV4 — transport guard holds over the whole composed body (poisoned upstream).
# ===========================================================================


def test_inv4_transport_sweep_holds_over_whole_composed_body(conn: sqlite3.Connection) -> None:
    # build_response runs assert_no_raw_paths internally; an explicit re-sweep of the
    # reviewer-internal records pins the contract criterion verbatim.
    body = _composed(conn)
    read_api.assert_no_raw_paths(body["reviewer_internal_records"])
    blob = json.dumps(body)
    for marker in ("/Users/", "/Volumes/", "TownOfAlpine", "secret.md", POISON_NAME):
        assert marker not in blob, f"transport leak: {marker!r}"


def test_inv4_raw_path_in_record_is_caught_loudly(conn: sqlite3.Connection) -> None:
    """Sanity: the transport guard is wired and not a no-op."""
    with pytest.raises(read_api.RawPathLeak):
        read_api.assert_no_raw_paths(
            {"record": {"note": "/Users/IA/Obsidian Vault/leak.md"}}
        )


# ===========================================================================
# INV5 — determinism: two composed passes are byte-identical.
# ===========================================================================


def test_inv5_two_composed_passes_are_byte_identical(conn: sqlite3.Connection) -> None:
    first = json.dumps(_composed(conn), sort_keys=True)
    second = json.dumps(_composed(conn), sort_keys=True)
    assert first == second


def test_inv5_reviewer_internal_records_stable_across_runs(conn: sqlite3.Connection) -> None:
    first = json.dumps(read_api.reviewer_internal_records(conn), sort_keys=True)
    second = json.dumps(read_api.reviewer_internal_records(conn), sort_keys=True)
    assert first == second
