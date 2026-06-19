"""GOV-283 Stage 2.07 — activate the dormant ``CONFIDENCE_LABEL_BY_CLASS`` SSOT.

Deterministic, read-time ``transcript_class -> confidence_label`` projection in
``read_api`` (Alpine, no-AI). Asserts the GOV-283 acceptance bar:

- ``read_api`` attaches a derived ``confidence_label`` to served statements via the
  documented join ``statement -> segment_id -> transcript_segments.transcript_id ->
  transcripts.transcript_class`` and the frozen SSOT — the dormant SSOT now has a
  real consumer, on both the public and reviewer-internal surfaces;
- SSOT-parity: the projection emits exactly the frozen ``CONFIDENCE_LABEL_BY_CLASS``
  mapping for every statement-producing class and cannot drift;
- fail-closed + no-upgrade: a missing ``segment_id``, a dangling segment, a NULL
  ``transcript_class``, an off-map class, and a PDF-only (non-transcript) anchor all
  collapse to the most conservative (``auto_caption_untimed``) label — never a
  higher confidence than the resolvable source class permits;
- no-leak: the raw ``transcript_class`` never crosses ``to_web_safe`` (absent from
  the allowlist, named in the unsafe set) — only the derived label projects.

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
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
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402
import transcript_class as tc  # noqa: E402

CONSERVATIVE = tc.CONFIDENCE_LABEL_BY_CLASS[tc.DEFAULT_TRANSCRIPT_CLASS]


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)  # includes 0018 transcript_class
    connection = db.open_db(db_path)
    _seed_base(connection)
    yield connection
    connection.close()


def _seed_base(conn: sqlite3.Connection) -> None:
    """A source + meeting + agenda item the statements can hang off."""
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
    conn.commit()


def _add_transcript_segment(
    conn: sqlite3.Connection,
    *,
    transcript_id: int,
    transcript_class: str | None,
    segment_id: str,
) -> str:
    """Create one transcript (of the given class) + a segment anchored to it.

    ``transcript_class=None`` leaves the column at its post-migration NULL
    (unclassified) state — the fail-closed branch under test.
    """
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            transcript_id,
            f"vid-{transcript_id}",
            f"https://youtu.be/vid-{transcript_id}",
            "Alpine council transcript text.",
            "n/a",
            "0" * 64,
            "2026-05-08T00:00:00Z",
            transcript_class,
        ),
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES (?, ?, ?, ?, ?, ?)",
        (segment_id, transcript_id, 0, 0, "00:00", "Mayor calls the meeting to order."),
    )
    conn.commit()
    return segment_id


def _insert_eligible_statement(
    conn: sqlite3.Connection,
    *,
    statement_id: str,
    segment_id: str | None,
    with_pdf_link: bool = False,
) -> None:
    """An eligible, published statement (served by :func:`published_records`).

    Anchored by a resolving ``segment_id`` (non-orphan) unless ``with_pdf_link`` is
    set, in which case it is anchored ONLY by a non-transcript PDF evidence_link.
    """
    links = []
    if with_pdf_link:
        links = [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "final_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": "2026-05-09",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 3,
                "verification_status": "human_verified",
                "confidence": "high",
            }
        ]
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "segment_id": segment_id,
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
        links,
    )


def _served(conn: sqlite3.Connection, statement_id: str) -> dict:
    record = next(
        (r for r in read_api.published_records(conn) if r["statement_id"] == statement_id),
        None,
    )
    assert record is not None, f"{statement_id!r} was expected to be served but was not"
    return record


# ---------------------------------------------------------------------------
# Per-class projection + SSOT parity (the dormant SSOT now has a real consumer).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transcript_class,expected_label", sorted(tc.CONFIDENCE_LABEL_BY_CLASS.items()))
def test_served_statement_gets_ssot_label_for_its_class(
    conn: sqlite3.Connection, transcript_class: str, expected_label: str
) -> None:
    """Every statement-producing class projects EXACTLY its frozen SSOT label."""
    seg = _add_transcript_segment(
        conn, transcript_id=10, transcript_class=transcript_class, segment_id="seg-class"
    )
    _insert_eligible_statement(conn, statement_id="stmt-x", segment_id=seg)
    assert _served(conn, "stmt-x")["confidence_label"] == expected_label


def test_ssot_parity_projection_domain_cannot_drift(conn: sqlite3.Connection) -> None:
    """The set of labels the projection can emit == frozen SSOT values ∪ conservative.

    Builds one served statement per *enum* class (including ``no_transcript``,
    which has no SSOT label) and proves the emitted label set is exactly the SSOT
    range plus the conservative fail-closed label — no extra, drift-introduced
    values. Ties read_api's projection to the transcript_class SSOT.
    """
    emitted: set[str] = set()
    for i, cls in enumerate(sorted(tc.TRANSCRIPT_CLASSES)):
        seg = _add_transcript_segment(
            conn, transcript_id=100 + i, transcript_class=cls, segment_id=f"seg-{i}"
        )
        _insert_eligible_statement(conn, statement_id=f"stmt-{i}", segment_id=seg)
        emitted.add(_served(conn, f"stmt-{i}")["confidence_label"])

    assert emitted == set(tc.CONFIDENCE_LABEL_BY_CLASS.values()) | {CONSERVATIVE}
    # The conservative label is itself derived from the SSOT default (no literal).
    assert read_api._CONSERVATIVE_CONFIDENCE_LABEL == tc.CONFIDENCE_LABEL_BY_CLASS[
        tc.DEFAULT_TRANSCRIPT_CLASS
    ]
    # The mapping covers exactly the producing classes; no_transcript is excluded.
    assert set(tc.CONFIDENCE_LABEL_BY_CLASS) == tc.TRANSCRIPT_CLASSES - {"no_transcript"}


# ---------------------------------------------------------------------------
# Fail-closed + no-upgrade.
# ---------------------------------------------------------------------------


def test_fail_closed_null_transcript_class(conn: sqlite3.Connection) -> None:
    """An unclassified (NULL) transcript_class -> conservative label, never upgraded."""
    seg = _add_transcript_segment(
        conn, transcript_id=20, transcript_class=None, segment_id="seg-null"
    )
    _insert_eligible_statement(conn, statement_id="stmt-null", segment_id=seg)
    assert _served(conn, "stmt-null")["confidence_label"] == CONSERVATIVE


def test_fail_closed_pdf_only_anchor_is_not_transcript_anchored(conn: sqlite3.Connection) -> None:
    """A statement anchored only via a non-transcript PDF evidence_link stays conservative."""
    _insert_eligible_statement(
        conn, statement_id="stmt-pdf", segment_id=None, with_pdf_link=True
    )
    assert _served(conn, "stmt-pdf")["confidence_label"] == CONSERVATIVE


def test_fail_closed_segment_with_no_row_defensive(conn: sqlite3.Connection) -> None:
    """A segment_id that resolves to no segment row -> conservative.

    The ``statements.segment_id`` FK makes a dangling segment_id un-insertable
    through the write path, so this defensive branch is exercised directly: a
    fabricated record whose segment_id has no ``transcript_segments`` row must
    still fail closed rather than raise.
    """
    label = read_api._confidence_label_for(conn, {"segment_id": "seg-does-not-exist"})
    assert label == CONSERVATIVE


def test_no_upgrade_low_class_never_yields_high_label(conn: sqlite3.Connection) -> None:
    """A low-confidence source class is never projected at the highest label."""
    seg = _add_transcript_segment(
        conn, transcript_id=30, transcript_class="auto_caption_untimed", segment_id="seg-low"
    )
    _insert_eligible_statement(conn, statement_id="stmt-low", segment_id=seg)
    label = _served(conn, "stmt-low")["confidence_label"]
    assert label == CONSERVATIVE
    assert label != tc.CONFIDENCE_LABEL_BY_CLASS["official_transcript"]


# ---------------------------------------------------------------------------
# No-leak: raw transcript_class never crosses to_web_safe.
# ---------------------------------------------------------------------------


def test_raw_transcript_class_never_web_projected(conn: sqlite3.Connection) -> None:
    seg = _add_transcript_segment(
        conn, transcript_id=40, transcript_class="official_transcript", segment_id="seg-off"
    )
    _insert_eligible_statement(conn, statement_id="stmt-off", segment_id=seg)
    record = _served(conn, "stmt-off")
    # only the derived label crosses; the raw class column key never appears.
    assert "transcript_class" not in record
    assert record["confidence_label"] == "source_anchored_timed"


def test_to_web_safe_strips_transcript_class_and_allowlist_unchanged() -> None:
    # belt-and-suspenders: the column is absent from the allowlist and named unsafe.
    assert "transcript_class" not in pub.WEB_SAFE_FIELD_ALLOWLIST
    assert "transcript_class" in pub.WEB_UNSAFE_FIELDS
    # the derived envelope key is NOT smuggled into the allowlist either.
    assert "confidence_label" not in pub.WEB_SAFE_FIELD_ALLOWLIST
    stripped = pub.to_web_safe({"statement_id": "s1", "transcript_class": "official_transcript"})
    assert "transcript_class" not in stripped


def test_full_response_with_labels_passes_transport_sweep(conn: sqlite3.Connection) -> None:
    seg = _add_transcript_segment(
        conn, transcript_id=50, transcript_class="auto_caption_timed", segment_id="seg-t"
    )
    _insert_eligible_statement(conn, statement_id="stmt-t", segment_id=seg)
    # build_response runs assert_no_raw_paths over the whole body; must not raise.
    body = read_api.build_response(conn, include_records=True)
    record = next(r for r in body["records"] if r["statement_id"] == "stmt-t")
    assert record["confidence_label"] == "auto_caption_timed"


# ---------------------------------------------------------------------------
# Reviewer-internal surface is labeled too.
# ---------------------------------------------------------------------------


def test_reviewer_internal_record_also_labeled(conn: sqlite3.Connection) -> None:
    """The reviewer-internal serve attaches confidence_label via the same path."""
    seg = _add_transcript_segment(
        conn, transcript_id=60, transcript_class="auto_caption_timed", segment_id="seg-ri"
    )
    run_id = "gov283:ai-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-ri",
            "segment_id": seg,
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "A Town Council special meeting was convened.",
            "produced_by": "ai",
            "layer": "ai_thought_then",
            "ai_extraction_run_id": run_id,
        },
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac", registered_by="owner:isaac",
        note="GOV-283 reviewer-internal label test",
    )
    gate.promote_statement(
        conn, "stmt-ri", reviewer_id="reviewer:isaac", decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )
    served = read_api.reviewer_internal_records(conn)
    record = next(r for r in served if r["statement_id"] == "stmt-ri")
    assert record["confidence_label"] == "auto_caption_timed"
    # the public lane never serves this pre-publish row.
    assert "stmt-ri" not in {r["statement_id"] for r in read_api.published_records(conn)}
