"""GOV-347 Stage 3.05 — web-safe, stable-id card feed over ``read_api``.

Proves the GOV-346 contract (``Docs/stage3-05-card-feed-contract.md``) + the
GOV-337 acceptance bar against :mod:`stage3_card_feed`:

- **§1 handle** — deterministic, opaque, derived; NC-1 (uniqueness, no collision
  across a whole feed) + NC-2 (no raw-id / FS-path / ``.sha256`` leak through the
  handle).
- **§2 status** — first-match/top-down composition, fail-closed ``unverified``;
  ``corrected`` / ``ai_presented`` dominate as specified; ``verified`` requires an
  explicit ``grounded`` + an eligible ``ui_status``; no fabricated edge.
- **§3 feed** — the ``{scope, access, cards[]}`` envelope; AC-1 (≥5 sourced cards),
  AC-3 (zero-hit leak scan over the whole feed, even with a planted raw locator
  upstream), AC-4 (fail-closed provenance), and the back-gap guard (RED on a planted
  drop — the feed never silently drops a record/gap the read surface emits).

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
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
import read_api  # noqa: E402
import statements as st  # noqa: E402
import stage3_card_feed as feed  # noqa: E402


# ---------------------------------------------------------------------------
# §1 handle — unit (no DB)
# ---------------------------------------------------------------------------


def test_handle_shape_and_determinism() -> None:
    h1 = feed.card_handle("statement", "alpine-2025-03-04-stmt-007")
    h2 = feed.card_handle("statement", "alpine-2025-03-04-stmt-007")
    assert h1 == h2  # deterministic: pure function, no time/random/rowid
    assert h1.startswith("c1_")
    assert len(h1) == 3 + 40  # scheme prefix + 160-bit (40 hex) truncation
    hexpart = h1[3:]
    assert all(c in "0123456789abcdef" for c in hexpart)


def test_handle_nc1_distinct_records_distinct_handles() -> None:
    a = feed.card_handle("statement", "alpine-2025-03-04-stmt-007")
    b = feed.card_handle("statement", "alpine-2025-03-04-stmt-008")
    assert a != b


def test_handle_type_namespacing_prevents_alias() -> None:
    # The \x1f delimiter makes (type=meeting, key="a-b") and (type=meetin, key="ga-b")
    # un-aliasable even though the naive concatenation would collide.
    assert feed.card_handle("meeting", "a-b") != feed.card_handle("meetin", "ga-b")


def test_handle_nc2_no_raw_id_or_path_leak() -> None:
    # Even an allowlisted natural key is hashed (defense in depth): the digest can
    # carry no path / id / marker shape.
    h = feed.card_handle("statement", "file:///Users/IA/Vault/raw.sha256")
    for marker in ("/", "file://", ".sha256", "\\", "Users", "Vault"):
        assert marker not in h


def test_handle_empty_input_raises() -> None:
    with pytest.raises(ValueError):
        feed.card_handle("statement", "")
    with pytest.raises(ValueError):
        feed.card_handle("", "key")


# ---------------------------------------------------------------------------
# §2 status + §3.2 type — unit (crafted records, no DB)
# ---------------------------------------------------------------------------


def test_status_verified_requires_grounded_and_eligible_ui_status() -> None:
    rec = {"provenance_status": "grounded", "ui_status": "source-backed", "produced_by": "human"}
    assert feed._compose_record_status(rec) == feed.STATUS_VERIFIED


def test_status_fail_closed_unverified_on_absent_provenance() -> None:
    # AC-4: unknown / absent provenance never reads verified.
    assert feed._compose_record_status({"ui_status": "source-backed"}) == feed.STATUS_UNVERIFIED
    assert (
        feed._compose_record_status(
            {"provenance_status": "unverified", "ui_status": "source-backed"}
        )
        == feed.STATUS_UNVERIFIED
    )


def test_status_grounded_but_wrong_ui_status_not_verified() -> None:
    # grounded alone is not enough — the ui_status must be in the verified set.
    rec = {"provenance_status": "grounded", "ui_status": "corrected"}
    assert feed._compose_record_status(rec) == feed.STATUS_CORRECTED  # corrected wins (row 2)


def test_status_corrected_dominates() -> None:
    rec = {"ui_status": "corrected", "produced_by": "ai", "provenance_status": "grounded"}
    assert feed._compose_record_status(rec) == feed.STATUS_CORRECTED


def test_status_ai_presented_over_verified() -> None:
    # The AI flag dominates the single status (trust still rides provenance_status).
    rec = {"produced_by": "ai", "provenance_status": "grounded", "ui_status": "source-backed"}
    assert feed._compose_record_status(rec) == feed.STATUS_AI_PRESENTED


def test_type_resolution_fail_closed() -> None:
    assert feed._resolve_record_type({"produced_by": "ai"}) == feed.TYPE_AI_PRESENTED
    assert feed._resolve_record_type({"ui_status": "corrected"}) == feed.TYPE_CORRECTION
    assert (
        feed._resolve_record_type({"statement_id": "s", "statement_text": "x"})
        == feed.TYPE_STATEMENT
    )
    assert feed._resolve_record_type({"statement_id": "s"}) == feed.TYPE_INFO  # neutral fallback


def test_type_ai_dominates_kind_status_corrected_dominates_trust() -> None:
    # The documented §3.2-vs-§2.2 split: same record -> type ai_presented, status corrected.
    rec = {"statement_id": "s", "statement_text": "x", "produced_by": "ai", "ui_status": "corrected"}
    assert feed._resolve_record_type(rec) == feed.TYPE_AI_PRESENTED
    assert feed._compose_record_status(rec) == feed.STATUS_CORRECTED


def test_card_date_never_invented() -> None:
    # No record date + no evidence date -> None (the feed shows no date, never fabricates).
    assert feed._card_date({"statement_id": "s", "evidence": []}) is None
    # Falls back to the earliest evidence scan_date.
    rec = {"evidence": [{"scan_date": "2025-03-09"}, {"scan_date": "2025-03-04"}]}
    assert feed._card_date(rec) == "2025-03-04"
    # A record-level allowlisted timing field wins.
    assert feed._card_date({"scan_date": "2025-01-01", "evidence": []}) == "2025-01-01"


# ---------------------------------------------------------------------------
# §3 feed — integration (real read surface)
# ---------------------------------------------------------------------------


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
    produced_by: str = "human",
    correction: str | None = None,
    run_id: str | None = None,
    page: int = 1,
) -> None:
    """Insert a statement + evidence link, then promote it under reviewer:isaac.

    Mirrors the live reviewer-internal gate (GOV-146): reviewed + a promoting
    Lane-5 decision + a resolvable evidence pointer + not-publishable. The evidence
    link carries a raw ``transcript_path`` that MUST be stripped upstream — the feed
    leak scan proves it never crosses.
    """
    record = {
        "statement_id": statement_id,
        "agenda_item_id": None,
        "statement_text": f"Reviewed civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": produced_by,
    }
    if correction is not None:
        record["correction_status"] = correction
    if produced_by == "ai":
        if conn.execute(
            "SELECT 1 FROM ai_extraction_runs WHERE run_id=?", (run_id,)
        ).fetchone() is None:
            ai.create_run(conn, run_id=run_id, input_source_ids=[])
        record["ai_extraction_run_id"] = run_id
    st.insert_statement(
        conn,
        record,
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "final_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": f"2026-05-{page:02d}",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": page,
                "verification_status": "human_verified",
                "confidence": "high",
                # raw/private locators that MUST be stripped at the boundary:
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=1",
            }
        ],
    )
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
        "original_url) VALUES ('alpine_packet', 'Agenda Packet', 'alpine', "
        "'document', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-347 card-feed seed",
    )
    conn.commit()
    # 5 plain sourced statements (AC-1 floor) + 1 AI + 1 corrected.
    for i in range(1, 6):
        _promote(conn, f"stmt-{i}", page=i)
    _promote(conn, "stmt-ai", produced_by="ai", run_id="run-ai", page=6)
    _promote(conn, "stmt-corrected", correction="corrected", page=7)
    # A completeness gap (the gap lane — the only source of source_missing).
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source", commit=True,
    )


def test_feed_envelope_shape(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    assert f["scope"] == "alpine"
    assert f["access"] == "reviewer_internal"
    assert isinstance(f["cards"], list)
    assert set(f.keys()) == {"scope", "access", "cards"}


def test_ac1_at_least_five_sourced_cards(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    sourced = feed.sourced_cards(f)
    assert len(sourced) >= 5
    # every sourced card carries a non-empty evidence drawer (AC-1 "sourced").
    assert all(card["evidence"] for card in sourced)


def test_ac3_leak_scan_zero_hits(conn: sqlite3.Connection) -> None:
    # The fixture planted a raw transcript_path / vault path on EVERY evidence link;
    # none may cross into the feed (stripped upstream + transport-swept here).
    blob = json.dumps(feed.build_card_feed(conn))
    for marker in ("file://", "/Users/", ".sha256", "transcript_path", "deep_link", "\\"):
        assert marker not in blob, f"leak: {marker!r} reached the feed"


def test_ac4_fail_closed_provenance(conn: sqlite3.Connection) -> None:
    # No raw-preservation set up -> every record card reads fail-closed unverified,
    # never a reassuring 'verified' (AC-4).
    f = feed.build_card_feed(conn)
    record_cards = [c for c in f["cards"] if c["type"] != "source_missing"]
    for card in record_cards:
        assert card.get("provenance_status") == "unverified"
    assert not any(c["status"] == "verified" for c in record_cards)


def test_card_types_and_statuses_present(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    by_type = {c["type"] for c in f["cards"]}
    assert {"statement", "ai_presented", "correction", "source_missing"} <= by_type
    ai_card = next(c for c in f["cards"] if c["type"] == "ai_presented")
    assert ai_card["status"] == "ai_presented"
    corr_card = next(c for c in f["cards"] if c["type"] == "correction")
    assert corr_card["status"] == "corrected"


def test_gap_card_is_source_missing(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    gap_cards = [c for c in f["cards"] if c["type"] == "source_missing"]
    assert gap_cards
    for gap in gap_cards:
        assert gap["status"] == "source_missing"
        assert gap["jurisdiction"] == "alpine"
        assert "evidence" not in gap  # reduced shape — no statement fields
        assert gap["gap_type"] == "no_primary_source"


def test_nc1_all_feed_handles_unique(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    handles = [c["handle"] for c in f["cards"]]
    assert len(handles) == len(set(handles))  # zero duplicates across the whole feed
    assert all(h.startswith("c1_") for h in handles)


def test_nc2_no_handle_leaks_raw_shape(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    for card in f["cards"]:
        h = card["handle"]
        assert len(h) == 43
        for marker in ("/", "file://", ".sha256", "\\"):
            assert marker not in h


def test_feed_is_idempotent(conn: sqlite3.Connection) -> None:
    # Pure re-projection: same DB -> byte-identical feed (same handles).
    a = json.dumps(feed.build_card_feed(conn), sort_keys=True)
    b = json.dumps(feed.build_card_feed(conn), sort_keys=True)
    assert a == b


# --- back-gap guard (3.13 / GOV-322 pattern) -------------------------------


def test_back_gap_guard_passes_on_full_feed(conn: sqlite3.Connection) -> None:
    f = feed.build_card_feed(conn)
    assert feed.assert_feed_covers_surface(conn, f) is True
    # bijection: one card per served record + one per gap, nothing dropped.
    n_records = len(read_api.reviewer_internal_records(conn))
    n_gaps = len(read_api.completeness_gap_cards(conn))
    assert len(f["cards"]) == n_records + n_gaps


def test_back_gap_guard_red_on_planted_record_drop(conn: sqlite3.Connection) -> None:
    # Plant a silent drop of a RECORD card -> the guard must go RED.
    f = feed.build_card_feed(conn)
    tampered = {**f, "cards": [c for c in f["cards"] if c["type"] != "ai_presented"]}
    with pytest.raises(feed.FeedCoverageError):
        feed.assert_feed_covers_surface(conn, tampered)


def test_back_gap_guard_red_on_planted_gap_drop(conn: sqlite3.Connection) -> None:
    # Plant a silent drop of the GAP card -> the guard must go RED (gaps never hidden).
    f = feed.build_card_feed(conn)
    tampered = {**f, "cards": [c for c in f["cards"] if c["type"] != "source_missing"]}
    with pytest.raises(feed.FeedCoverageError):
        feed.assert_feed_covers_surface(conn, tampered)


def test_back_gap_guard_green_when_nothing_dropped_proves_red_is_load_bearing(
    conn: sqlite3.Connection,
) -> None:
    # Control: the SAME assertion passes on the untampered feed, proving the RED in
    # the two tests above is caused by the drop, not by an always-raising guard.
    assert feed.assert_feed_covers_surface(conn, feed.build_card_feed(conn)) is True
