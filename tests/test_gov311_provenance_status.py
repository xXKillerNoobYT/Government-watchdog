"""GOV-311 Stage 2.12 surface — read-time per-record provenance_status projection.

The serving-lane companion to the GOV-306 read-surface auditor: where
``scripts/stage2_traceability.py`` emits a single whole-DB ``clean=True/False``
verdict, ``read_api`` now attaches a fail-closed, per-record ``provenance_status``
trust indicator to every served **reviewer-internal** statement. Asserts the
Stage 2.12 contract bar:

- AC1: every served reviewer-internal record carries ``provenance_status`` drawn
  from the frozen SSOT vocabulary (:data:`read_api.PROVENANCE_STATUS_VALUES`);
- AC2: the verdict is recomputed from canonical columns via the REUSED GOV-306
  per-row predicates (``stage2_traceability.statement_grounded`` / ``raw_linked``)
  — no fork, and no ``read_api`` <-> ``stage2_traceability`` circular import (the
  auditor is imported lazily inside the helper, never at module top);
- AC3: fail-closed, proven RED both directions — a true-grounded row reads
  ``"grounded"``; a served-but-dangling chain, an unpreserved raw, and a
  poisoned/absent AI run each collapse to ``"unverified"``; optimism is never the
  default;
- AC4: no-leak — the served body is a subset of the allowed key set, carries 0
  internal provenance ids / FS paths / PII, ``assert_no_raw_paths`` stays green,
  and ``provenance_status`` is an envelope key never smuggled into the allowlist
  (``publication.py`` untouched);
- AC5: SSOT parity — on a healthy real-shape Alpine corpus the per-row verdict
  agrees with ``stage2_traceability``'s per-row predicates, and the auditor's
  whole-DB pass equals the aggregate of the projected per-row statuses;
- the public lane never carries the field (reviewer-internal lane only).

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency. Mirrors the
GOV-290 / GOV-306 served-corpus seed pattern.
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
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import stage2_traceability as trace  # noqa: E402
import statements as st  # noqa: E402

REVIEWER = "reviewer:isaac"


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_base(connection)
    yield connection
    connection.close()


def _seed_base(conn: sqlite3.Connection) -> None:
    """Source + meeting + agenda item + a timed, hash-preserved transcript+segment.

    ``seg-1`` resolves segment->transcript and the transcript carries a recorded
    ``sha256`` — so a statement anchored on it is BOTH grounded and raw-preserved.
    A registered reviewer lets us promote statements into the reviewer-internal lane.
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
    # transcript 1: hash-preserved (grounded + raw-linked).
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
        "('seg-1', 1, 0, 0, '00:00', 'Mayor calls the meeting to order.')"
    )
    # transcript 2: BLANK sha256 (column is NOT NULL) -> grounded but NOT
    # raw-preserved, since raw_linked treats a falsy hash as unpreserved (RED).
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
    gate.register_reviewer(
        conn, REVIEWER, display_name="Isaac", registered_by="owner:isaac",
        note="GOV-311 provenance_status reviewer-internal tests",
    )
    conn.commit()


def _serve_reviewer_internal(
    conn: sqlite3.Connection,
    *,
    statement_id: str,
    segment_id: str | None = "seg-1",
    produced_by: str = "human",
    ai_run_id: str | None = None,
    evidence_source_id: str | None = None,
) -> None:
    """Insert + promote a statement into the reviewer-internal serve.

    Defaults to a healthy, grounded, raw-preserved row. Pass ``segment_id=None`` +
    ``evidence_source_id`` to anchor via an evidence link instead (used to plant a
    dangling-chain RED by pointing at a non-existent source).
    """
    record: dict[str, object] = {
        "statement_id": statement_id,
        "segment_id": segment_id,
        "agenda_item_id": "alpine:2026-05-08:item-7",
        "statement_text": "The council adopted the fireworks ban.",
        "produced_by": produced_by,
    }
    if produced_by == "ai":
        record["layer"] = "ai_thought_then"
        record["ai_extraction_run_id"] = ai_run_id
    st.insert_statement(conn, record)
    if evidence_source_id is not None:
        conn.execute(
            "INSERT INTO evidence_links (evidence_link_id, from_node_id, "
            "from_node_type, to_source_id, relation) VALUES (?, ?, 'statement', ?, "
            "'cites')",
            (f"el-{statement_id}", statement_id, evidence_source_id),
        )
        conn.commit()
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _ri(conn: sqlite3.Connection, statement_id: str) -> dict:
    record = next(
        (r for r in read_api.reviewer_internal_records(conn) if r["statement_id"] == statement_id),
        None,
    )
    assert record is not None, f"{statement_id!r} expected in the reviewer-internal serve"
    return record


# ---------------------------------------------------------------------------
# AC1 — every served reviewer-internal record carries a vocab-valid status.
# ---------------------------------------------------------------------------


def test_every_reviewer_internal_record_has_provenance_status_in_vocab(conn: sqlite3.Connection) -> None:
    _serve_reviewer_internal(conn, statement_id="stmt-ok")
    record = _ri(conn, "stmt-ok")
    assert "provenance_status" in record
    assert record["provenance_status"] in read_api.PROVENANCE_STATUS_VALUES


def test_vocab_is_a_frozenset_of_exactly_two_values() -> None:
    assert isinstance(read_api.PROVENANCE_STATUS_VALUES, frozenset)
    assert read_api.PROVENANCE_STATUS_VALUES == {"grounded", "unverified"}
    assert read_api.PROVENANCE_GROUNDED == "grounded"
    assert read_api.PROVENANCE_UNVERIFIED == "unverified"


# ---------------------------------------------------------------------------
# AC3 — grounded direction: all three legs pass -> "grounded".
# ---------------------------------------------------------------------------


def test_grounded_when_chain_raw_and_ai_all_pass(conn: sqlite3.Connection) -> None:
    run_id = "gov311:ok-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])  # defaults error_status='ok'
    _serve_reviewer_internal(
        conn, statement_id="stmt-grounded", produced_by="ai", ai_run_id=run_id,
    )
    assert _ri(conn, "stmt-grounded")["provenance_status"] == read_api.PROVENANCE_GROUNDED


def test_grounded_human_row_with_no_ai_run(conn: sqlite3.Connection) -> None:
    """A human-origin row has no AI leg to verify; chain+raw passing -> grounded."""
    _serve_reviewer_internal(conn, statement_id="stmt-human")
    assert _ri(conn, "stmt-human")["provenance_status"] == read_api.PROVENANCE_GROUNDED


# ---------------------------------------------------------------------------
# AC3 — fail-closed, proven RED in every chain-break direction.
# ---------------------------------------------------------------------------


def test_unverified_dangling_chain(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """A served row whose transcript chain is later broken -> unverified.

    Mirrors the GOV-306 orphan RED: insert+promote a healthy row, then delete its
    parent transcript with a raw (FK-off) connection so the segment — and the served
    statement — is grounded in nothing. read_api's serving gate still serves it (the
    segment row exists), but statement_grounded sees the dangling chain.
    """
    # dedicated transcript/segment so deleting it can't affect seg-1 rows.
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (3, 'vid-3', "
        "'https://youtu.be/vid-3', 'Dangling transcript text.', 'n/a', ?, "
        "'2026-05-08T00:00:00Z', 'official_transcript')",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-3', 3, 0, 0, '00:00', 'Dangling segment.')"
    )
    conn.commit()
    _serve_reviewer_internal(conn, statement_id="stmt-dangling", segment_id="seg-3")
    assert _ri(conn, "stmt-dangling")["provenance_status"] == read_api.PROVENANCE_GROUNDED

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()
    raw = sqlite3.connect(db_path)  # FK enforcement OFF
    raw.execute("DELETE FROM transcripts WHERE id = 3")
    raw.commit()
    raw.close()

    with db.open_db(db_path) as conn2:
        record = _ri(conn2, "stmt-dangling")
        assert record["provenance_status"] == read_api.PROVENANCE_UNVERIFIED
        assert trace.statement_grounded(conn2, "stmt-dangling") is False  # chain broken


def test_unverified_unpreserved_raw(conn: sqlite3.Connection) -> None:
    """Grounded (segment->transcript resolves) but the transcript has NO sha256.

    Isolates the raw_linked leg: the chain is intact, the citation is just not
    reproducible -> unverified.
    """
    _serve_reviewer_internal(conn, statement_id="stmt-unpreserved", segment_id="seg-unpreserved")
    record = _ri(conn, "stmt-unpreserved")
    assert record["provenance_status"] == read_api.PROVENANCE_UNVERIFIED
    assert trace.statement_grounded(conn, "stmt-unpreserved") is True  # chain OK
    assert trace.raw_linked(conn, "stmt-unpreserved") is False         # raw broken


def test_ai_provenance_ok_unit(conn: sqlite3.Connection) -> None:
    """The fail-closed AI-leg helper, isolated: only a resolvable ok run passes."""
    ok_run = "gov311:unit-ok"
    ai.create_run(conn, run_id=ok_run, input_source_ids=[])
    assert read_api._ai_provenance_ok(conn, {"produced_by": "human"}) is True  # no AI leg
    assert read_api._ai_provenance_ok(conn, {"produced_by": "ai", "ai_extraction_run_id": ok_run}) is True
    assert read_api._ai_provenance_ok(conn, {"produced_by": "ai", "ai_extraction_run_id": None}) is False
    assert read_api._ai_provenance_ok(conn, {"produced_by": "ai", "ai_extraction_run_id": "   "}) is False
    assert read_api._ai_provenance_ok(conn, {"produced_by": "ai", "ai_extraction_run_id": "ghost"}) is False


def test_ai_leg_flips_grounded_to_unverified(conn: sqlite3.Connection) -> None:
    """The AI leg, not the chain, can flip a grounded anchor to unverified.

    A real served AI row on the healthy seg-1 anchor reads ``grounded``; the SAME
    canonical anchor with its producing run broken (absent / non-ok) collapses to
    ``unverified`` — proving the AI leg is fail-closed and is what flipped it. (Such
    a break can't be inserted via the write gate, so it is exercised at the helper
    level on the real canonical record.)
    """
    ok_run = "gov311:ok-run-ai"
    ai.create_run(conn, run_id=ok_run, input_source_ids=[])
    _serve_reviewer_internal(conn, statement_id="stmt-ai-ok", produced_by="ai", ai_run_id=ok_run)
    assert _ri(conn, "stmt-ai-ok")["provenance_status"] == read_api.PROVENANCE_GROUNDED

    canonical = dict(
        conn.execute("SELECT * FROM statements WHERE statement_id = 'stmt-ai-ok'").fetchone()
    )
    assert trace.statement_grounded(conn, "stmt-ai-ok") is True  # chain intact
    assert trace.raw_linked(conn, "stmt-ai-ok") is True          # raw intact

    # absent run -> AI leg fails -> unverified (despite intact chain + raw).
    broken = {**canonical, "ai_extraction_run_id": None}
    assert read_api._ai_provenance_ok(conn, broken) is False
    assert read_api._provenance_status_for(conn, broken) == read_api.PROVENANCE_UNVERIFIED

    # a resolvable but FAILED run -> same fail-closed verdict.
    bad_run = "gov311:failed-run"
    ai.create_run(conn, run_id=bad_run, input_source_ids=[])
    ai.finalize_run(conn, bad_run, output_statement_ids=[], output_evidence_link_ids=[],
                    orphan_rejected_count=0, error_status="failed")
    failed = {**canonical, "ai_extraction_run_id": bad_run}
    assert read_api._provenance_status_for(conn, failed) == read_api.PROVENANCE_UNVERIFIED


def test_unverified_when_no_statement_id(conn: sqlite3.Connection) -> None:
    assert read_api._provenance_status_for(conn, {}) == read_api.PROVENANCE_UNVERIFIED


# ---------------------------------------------------------------------------
# Lane separation — public lane NEVER carries provenance_status.
# ---------------------------------------------------------------------------


def test_public_lane_never_carries_provenance_status(conn: sqlite3.Connection) -> None:
    """An owner-published row (the public lane) gets the byte-identical old shape."""
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-pub",
            "segment_id": "seg-1",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Published civic fact.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
    )
    published = read_api.published_records(conn)
    record = next(r for r in published if r["statement_id"] == "stmt-pub")
    assert "provenance_status" not in record
    # the published row is not duplicated into the reviewer-internal serve.
    assert "stmt-pub" not in {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}


# ---------------------------------------------------------------------------
# AC4 — no-leak: subset of allowed keys, 0 internal ids, transport-clean.
# ---------------------------------------------------------------------------

# The exact key set a served reviewer-internal record may carry: the web-safe
# allowlist plus the derived API-envelope keys. A served body that is NOT a subset
# of this leaked a column.
_ALLOWED_RECORD_KEYS = set(pub.WEB_SAFE_FIELD_ALLOWLIST) | {
    "ui_status", "evidence", "confidence_label", "speaker_label", "provenance_status",
}


def test_served_body_is_subset_of_allowed_keys(conn: sqlite3.Connection) -> None:
    run_id = "gov311:leak-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    _serve_reviewer_internal(conn, statement_id="stmt-leak", produced_by="ai", ai_run_id=run_id)
    record = _ri(conn, "stmt-leak")
    assert set(record).issubset(_ALLOWED_RECORD_KEYS), set(record) - _ALLOWED_RECORD_KEYS


def test_no_internal_provenance_ids_in_served_body(conn: sqlite3.Connection) -> None:
    run_id = "gov311:ids-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    _serve_reviewer_internal(conn, statement_id="stmt-ids", produced_by="ai", ai_run_id=run_id)
    record = _ri(conn, "stmt-ids")
    blob = json.dumps(record)
    for internal in ("segment_id", "speaker_attribution_id", "ai_extraction_run_id",
                     "to_source_id"):
        assert internal not in record
    # the run id value itself never rides across.
    assert run_id not in blob


def test_full_reviewer_internal_body_passes_transport_sweep(conn: sqlite3.Connection) -> None:
    _serve_reviewer_internal(conn, statement_id="stmt-sweep")
    body = read_api.build_response(conn, include_reviewer_internal=True)  # runs assert_no_raw_paths
    record = next(
        r for r in body["reviewer_internal_records"] if r["statement_id"] == "stmt-sweep"
    )
    assert record["provenance_status"] == read_api.PROVENANCE_GROUNDED


def test_provenance_status_not_in_allowlist_publication_untouched() -> None:
    """provenance_status is a derived envelope key, never a passthrough column."""
    assert "provenance_status" not in pub.WEB_SAFE_FIELD_ALLOWLIST
    # the internal columns the verdict is recomputed from stay unsafe (no regression).
    assert "segment_id" in pub.WEB_UNSAFE_FIELDS
    assert "speaker_attribution_id" in pub.WEB_UNSAFE_FIELDS


# ---------------------------------------------------------------------------
# AC2 — no fork, no circular import; pure/deterministic.
# ---------------------------------------------------------------------------


def test_no_top_level_circular_import() -> None:
    """read_api must not import stage2_traceability at module top (would be circular).

    The auditor imports read_api at its module top; the projection breaks the cycle
    with a LOCAL import, so stage2_traceability is never bound as a read_api module
    attribute even after the helper has run.
    """
    assert not hasattr(read_api, "stage2_traceability")
    # sanity: the lazily-imported predicates are the GOV-306 originals (reused, not forked).
    assert "statement_grounded" in dir(trace)
    assert "raw_linked" in dir(trace)


def test_determinism_byte_identical_across_two_runs(conn: sqlite3.Connection) -> None:
    run_id = "gov311:det-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    _serve_reviewer_internal(conn, statement_id="stmt-d1", produced_by="ai", ai_run_id=run_id)
    _serve_reviewer_internal(conn, statement_id="stmt-d2", segment_id="seg-unpreserved")
    first = json.dumps(read_api.reviewer_internal_records(conn), sort_keys=True)
    second = json.dumps(read_api.reviewer_internal_records(conn), sort_keys=True)
    assert first == second


# ---------------------------------------------------------------------------
# AC5 — SSOT parity: per-row verdict agrees with the GOV-306 predicates, and the
# auditor's whole-DB pass equals the aggregate of the projected per-row statuses.
# ---------------------------------------------------------------------------


def _expected_status(conn: sqlite3.Connection, record: dict) -> str:
    grounded = (
        trace.statement_grounded(conn, record["statement_id"])
        and trace.raw_linked(conn, record["statement_id"])
        and read_api._ai_provenance_ok(conn, record)
    )
    return read_api.PROVENANCE_GROUNDED if grounded else read_api.PROVENANCE_UNVERIFIED


def test_per_row_parity_with_auditor_predicates(conn: sqlite3.Connection) -> None:
    """Every served reviewer-internal status equals the recomputed predicate verdict.

    Mix of healthy and broken rows so both verdicts are exercised.
    """
    run_id = "gov311:parity-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    _serve_reviewer_internal(conn, statement_id="p-grounded-ai", produced_by="ai", ai_run_id=run_id)
    _serve_reviewer_internal(conn, statement_id="p-grounded-human")
    _serve_reviewer_internal(conn, statement_id="p-unpreserved", segment_id="seg-unpreserved")
    served = read_api.reviewer_internal_records(conn)
    assert served, "expected a non-empty reviewer-internal serve"
    for record in served:
        # NOTE: re-fetch the canonical record (the served body has dropped the raw
        # columns) so _ai_provenance_ok sees produced_by / run id.
        canonical = dict(
            conn.execute(
                "SELECT * FROM statements WHERE statement_id = ?", (record["statement_id"],)
            ).fetchone()
        )
        assert record["provenance_status"] == _expected_status(conn, canonical)


def test_aggregate_matches_auditor_whole_db_pass(conn: sqlite3.Connection) -> None:
    """When the GOV-306 auditor passes (grounding+raw+ai clean), EVERY served
    reviewer-internal row reads "grounded" — the surface is the per-row aggregate of
    the whole-DB verdict."""
    run_id = "gov311:agg-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    _serve_reviewer_internal(conn, statement_id="a-grounded-ai", produced_by="ai", ai_run_id=run_id)
    _serve_reviewer_internal(conn, statement_id="a-grounded-human")
    report = trace.audit_stage2_traceability(conn)
    assert report["statement_grounding"]["clean"] is True
    assert report["raw_preservation"]["clean"] is True
    assert report["ai_provenance"]["clean"] is True
    served = read_api.reviewer_internal_records(conn)
    assert served
    assert all(r["provenance_status"] == read_api.PROVENANCE_GROUNDED for r in served)
