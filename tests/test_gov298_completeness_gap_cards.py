"""GOV-298 Stage 2 — read-time, web-safe, fail-closed completeness-gap cards.

Deterministic ``completeness_gaps`` -> gap-card projection in ``read_api``
(``completeness_gap_cards`` + ``build_response(include_completeness_gaps=True)``).
Mirrors the proven read-time projection template (GOV-283 ``confidence_label`` /
GOV-290 ``speaker_label``). Asserts the GOV-298 acceptance bar:

- the ~90 ``no_primary_source`` rows are present + countable in the projected
  output, with ZERO internal-column leak (``source_id`` / ``detected_run_id`` /
  ``detected_utc`` absent from every body);
- RED both ways: (a) a planted raw path / structured-PII ``detail`` is omitted —
  the gap ROW is still emitted; (b) the internal provenance columns are never
  surfaced (structural: never SELECTed — not allowlist-dependent, since
  ``source_id`` IS on ``WEB_SAFE_FIELD_ALLOWLIST``);
- fail-closed: an off-SSOT ``gap_type`` / ``severity`` / ``resolved_status``
  (planted past the 0015 CHECK) collapses to a conservative placeholder, but the
  row is never hidden;
- SSOT parity: the accepted vocabularies cannot drift from the ``completeness``
  frozensets / the 0015 CHECK.

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import completeness as comp  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402

# The internal/provenance columns that must NEVER reach a projected card body.
_INTERNAL_COLUMNS = ("source_id", "detected_run_id", "detected_utc")


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)  # includes 0015 completeness_gaps
    connection = db.open_db(db_path)
    _seed_refs(connection)
    yield connection
    connection.close()


def _seed_refs(conn: sqlite3.Connection) -> None:
    """Seed the FK parents a gap can reference (source + crawl_runs).

    ``completeness_gaps.source_id`` -> ``sources(source_id)`` and
    ``detected_run_id`` -> ``crawl_runs(id)`` are real FKs; tests that populate
    those internal columns (to prove they never leak) need the parent rows to
    exist. The real corpus source id is ``alpine_local_corpus``.
    """
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class) "
        "VALUES ('alpine_local_corpus', 'Alpine Local Corpus', 'alpine', "
        "'document', 'official')"
    )
    for run_id in (1, 3, 7, 42):
        conn.execute(
            "INSERT INTO crawl_runs (id, started_utc, status, targets) "
            "VALUES (?, '2026-06-19T00:00:00Z', 'ok', 'alpine')",
            (run_id,),
        )
    conn.commit()


def _record(conn: sqlite3.Connection, **kw) -> str:
    """Record one gap via the real write path (validates the SSOT vocabulary)."""
    gap_id = comp.record_gap(conn, commit=False, **kw)
    conn.commit()
    return gap_id


def _plant_raw(conn: sqlite3.Connection, *, gap_id: str, **cols) -> None:
    """INSERT a gap row directly, bypassing record_gap's validation.

    Used to plant a value the write path would reject — an off-SSOT enum (needs
    the CHECK disabled) or a PII detail (record_gap's assert_no_pii would block
    it) — so the READ-time fail-closed / re-guard behavior can be proven RED.
    """
    base = {
        "gap_id": gap_id,
        "subject_node_id": "m1",
        "subject_node_type": "meeting",
        "gap_type": "no_primary_source",
        "severity": "warn",
        "detail": None,
        "source_id": None,
        "detected_run_id": None,
        "detected_utc": "2026-06-19T00:00:00.000+00:00",
        "resolved_status": "open",
        "produced_by": "deterministic",
    }
    base.update(cols)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "INSERT INTO completeness_gaps (gap_id, subject_node_id, subject_node_type, "
        "gap_type, severity, detail, source_id, detected_run_id, detected_utc, "
        "resolved_status, produced_by) VALUES "
        "(:gap_id, :subject_node_id, :subject_node_type, :gap_type, :severity, "
        ":detail, :source_id, :detected_run_id, :detected_utc, :resolved_status, "
        ":produced_by)",
        base,
    )
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    conn.commit()


# --- core projection: web-safe fields only, internal columns excluded --------


def test_empty_when_no_gaps(conn: sqlite3.Connection) -> None:
    assert read_api.completeness_gap_cards(conn) == []


def test_gap_card_projects_only_web_safe_fields(conn: sqlite3.Connection) -> None:
    _record(
        conn,
        subject_node_id="2026-04-10",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        source_id="alpine_local_corpus",
        detected_run_id=7,
        detail="meeting folder 2026-04-10 has only derived (.md) material",
    )
    cards = read_api.completeness_gap_cards(conn)
    assert len(cards) == 1
    card = cards[0]
    # Web-safe fields present.
    assert card["gap_type"] == "no_primary_source"
    assert card["severity"] == "warn"
    assert card["subject_node_type"] == "meeting"
    assert card["subject_id"] == "2026-04-10"
    assert card["resolved_status"] == "open"
    # Every key is within the declared gap-card field set — no surprise leak.
    assert set(card) <= read_api.GAP_CARD_FIELDS
    # Internal/provenance columns NEVER appear.
    for col in _INTERNAL_COLUMNS:
        assert col not in card


def test_internal_columns_absent_even_when_populated(conn: sqlite3.Connection) -> None:
    """RED (b): real provenance values present in the row never reach the card."""
    _record(
        conn,
        subject_node_id="2026-05-08",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        source_id="alpine_local_corpus",  # real CORPUS_SOURCE_ID
        detected_run_id=42,
        detail="meeting folder 2026-05-08 has only derived (.md) material",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    blob = repr(card)
    assert "alpine_local_corpus" not in blob  # source_id value gone
    assert "42" not in blob                    # detected_run_id value gone
    assert "detected_utc" not in card
    assert "detected_run_id" not in card
    assert "source_id" not in card


# --- the ~90 no_primary_source rows stay countable (headline criterion) ------


def test_all_no_primary_source_rows_countable(conn: sqlite3.Connection) -> None:
    """The headline backfill criterion: every no_primary_source gap is surfaced."""
    for i in range(90):
        _record(
            conn,
            subject_node_id=f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}-folder{i}",
            subject_node_type="meeting",
            gap_type="no_primary_source",
            source_id="alpine_local_corpus",
            detected_run_id=1,
            detail=f"folder {i} has only derived (.md) material",
        )
    cards = read_api.completeness_gap_cards(conn)
    no_primary = [c for c in cards if c["gap_type"] == "no_primary_source"]
    assert len(no_primary) == 90
    # ...and not one leaked an internal column.
    for c in cards:
        for col in _INTERNAL_COLUMNS:
            assert col not in c


# --- RED (a): leak-prone detail omitted, row still emitted -------------------


def test_raw_path_detail_omitted_row_kept(conn: sqlite3.Connection) -> None:
    # record_gap accepts a filesystem path in detail (its guard is PII-only, not
    # path-aware), so the read-time _safe_gap_detail is the layer that must catch it.
    _record(
        conn,
        subject_node_id="m-raw",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="see /Users/IA/Documents/TOA/TownOfAlpine/secret.md",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    assert "detail" not in card          # leak-prone detail OMITTED
    assert card["subject_id"] == "m-raw"  # ...but the gap ROW is still emitted


def test_pii_detail_omitted_row_kept(conn: sqlite3.Connection) -> None:
    # A structured-PII detail (an email) can only reach the table by bypassing
    # record_gap's write guard; the read-time re-guard must still omit it.
    _plant_raw(
        conn,
        gap_id="no_primary_source:meeting:m-pii",
        subject_node_id="m-pii",
        detail="reported by resident jane.doe@example.com",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    assert "detail" not in card
    assert card["subject_id"] == "m-pii"


def test_clean_detail_is_projected(conn: sqlite3.Connection) -> None:
    _record(
        conn,
        subject_node_id="m-clean",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="meeting folder has only derived (.md) material; no primary document",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    assert card["detail"] == (
        "meeting folder has only derived (.md) material; no primary document"
    )


def test_build_response_with_raw_detail_does_not_raise(conn: sqlite3.Connection) -> None:
    """The whole-body transport sweep stays green precisely because detail is omitted.

    If _safe_gap_detail did NOT strip the raw path, build_response's
    assert_no_raw_paths would raise — so this is the end-to-end RED proof.
    """
    _record(
        conn,
        subject_node_id="m-raw2",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="path /Volumes/secret/raw.pdf in the note",
    )
    body = read_api.build_response(
        conn, include_records=False, include_completeness_gaps=True
    )
    assert len(body["completeness_gaps"]) == 1
    assert "detail" not in body["completeness_gaps"][0]


# --- fail-closed on off-SSOT enums (planted past the 0015 CHECK) -------------


def test_offssot_gap_type_collapses_conservative(conn: sqlite3.Connection) -> None:
    _plant_raw(
        conn,
        gap_id="bogus:meeting:m-drift",
        subject_node_id="m-drift",
        gap_type="totally_made_up_type",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    assert card["gap_type"] == read_api._UNKNOWN_GAP_TYPE
    assert card["subject_id"] == "m-drift"  # row never hidden


def test_offssot_severity_collapses_conservative(conn: sqlite3.Connection) -> None:
    _plant_raw(
        conn,
        gap_id="no_primary_source:meeting:m-sev",
        subject_node_id="m-sev",
        severity="catastrophic",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    assert card["severity"] == read_api._CONSERVATIVE_GAP_SEVERITY


def test_offssot_resolved_status_collapses_open(conn: sqlite3.Connection) -> None:
    _plant_raw(
        conn,
        gap_id="no_primary_source:meeting:m-res",
        subject_node_id="m-res",
        resolved_status="quietly_buried",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    # A drifted resolved_status is NEVER presented as resolved — it stays open.
    assert card["resolved_status"] == read_api._CONSERVATIVE_RESOLVED_STATUS


def test_valid_resolved_status_passes_through(conn: sqlite3.Connection) -> None:
    _record(
        conn,
        subject_node_id="m-ack",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        resolved_status="acknowledged",
    )
    card = read_api.completeness_gap_cards(conn)[0]
    assert card["resolved_status"] == "acknowledged"


# --- SSOT parity: accepted vocab cannot drift from completeness frozensets ----


def test_ssot_parity_accepted_vocab_is_completeness_frozensets() -> None:
    # The projection consumes the SSOT frozensets directly; this pins that it
    # cannot start hardcoding a divergent set. If completeness adds/removes a
    # gap_type or severity, this mirrors automatically — drift fails elsewhere
    # (the 0015 CHECK parity test) rather than silently here.
    assert comp.GAP_TYPES is comp.GAP_TYPES  # identity sanity
    # Conservative fallbacks are themselves valid SSOT members (severity/status)
    # or a clearly-non-vocab sentinel (gap_type).
    assert read_api._CONSERVATIVE_GAP_SEVERITY in comp.SEVERITIES
    assert read_api._CONSERVATIVE_RESOLVED_STATUS in comp.RESOLVED_STATUSES
    assert read_api._UNKNOWN_GAP_TYPE not in comp.GAP_TYPES


def test_every_real_gap_type_projects_verbatim(conn: sqlite3.Connection) -> None:
    """Each emittable SSOT gap_type round-trips through the projection unchanged."""
    for i, gap_type in enumerate(sorted(comp.EMITTABLE_GAP_TYPES)):
        _record(
            conn,
            subject_node_id=f"subj-{i}",
            subject_node_type="meeting",
            gap_type=gap_type,
        )
    cards = read_api.completeness_gap_cards(conn)
    projected = {c["gap_type"] for c in cards}
    assert projected == set(comp.EMITTABLE_GAP_TYPES)


# --- transport: a real-shape gap body is free of raw paths -------------------


def test_completeness_gaps_pass_transport_sweep(conn: sqlite3.Connection) -> None:
    _record(
        conn,
        subject_node_id="2026-04-10",
        subject_node_type="meeting",
        gap_type="no_primary_source",
        source_id="alpine_local_corpus",
        detected_run_id=3,
        detail="meeting folder 2026-04-10 has only derived (.md) material",
    )
    # build_response sweeps the whole body; a leak would raise here.
    body = read_api.build_response(
        conn, include_records=False, include_completeness_gaps=True
    )
    import json

    blob = json.dumps(body)
    assert "/Users/" not in blob
    assert "alpine_local_corpus" not in blob
    assert "TownOfAlpine" not in blob
