"""GOV-449 Stage 4.03 — reviewed Alpine newsletter item feed over ``read_api``.

Proves the GOV-448 contract (Newsletter Source/Data Inventory) against
:mod:`stage4_newsletter_feed`:

- **§2 item shape** — every item carries the contract fields and every value rides
  from / is derived on the already-web-safe Stage-3 read surface; ``id`` is the
  deterministic ``alpine-newsletter-item-NNN`` sequence; the Stage-3 card handle is
  the ``cardIds`` anchor (every item traces to a real reviewed record — zero
  invented items).
- **§2.3 zero-new-label rule (EG-7)** — the claim-axis label diff vs the Stage-3
  vocabulary is empty.
- **§2.4 chronology (EG-3)** — non-decreasing oldest→newest within a ``newsletterId``
  batch; a planted out-of-order emission goes RED (load-bearing, not a tautology).
- **§3 readiness record (EG-3)** — source categories + range named; no completion
  overclaim; honest ``knownGaps``.
- **§4 traceability + orphan routing (EG-4 / EG-11)** — one row per item with
  ``links_present``; an orphan candidate is held out of the feed and routed to VSR.
- **local-safe (§6)** — the fixture plants raw vault paths on every evidence link;
  none cross into any artifact (stripped upstream + transport-swept here);
  ``localSourcePath`` is always null. A neutered coverage guard goes RED.

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
import stage3_card_feed as card_feed  # noqa: E402
import statements as st  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402


# ---------------------------------------------------------------------------
# Unit — date / batch / coverage-period derivation (no DB)
# ---------------------------------------------------------------------------


def test_newsletter_id_is_iso_week_batch() -> None:
    # 2026-05-03 is a Sunday in ISO week 18; 2026-05-04 a Monday in week 19.
    assert nl._newsletter_id("2026-05-03") == "alpine-historical-2026-18"
    assert nl._newsletter_id("2026-05-04") == "alpine-historical-2026-19"


def test_newsletter_id_undated_never_fabricates_a_week() -> None:
    assert nl._newsletter_id(None) == nl._UNDATED_BATCH
    assert nl._newsletter_id("not-a-date") == nl._UNDATED_BATCH


def test_coverage_period_contains_record_date() -> None:
    period = nl._coverage_period("2026-05-06")
    assert period == {"startDate": "2026-05-04", "endDate": "2026-05-10"}
    assert period["startDate"] <= "2026-05-06" <= period["endDate"]


def test_coverage_period_none_when_no_grounded_date() -> None:
    assert nl._coverage_period(None) is None


# ---------------------------------------------------------------------------
# Unit — itemType vocabulary + orphan classification (no DB)
# ---------------------------------------------------------------------------


def test_item_type_mapping_within_allowed_vocab() -> None:
    for card_type in (
        card_feed.TYPE_AI_PRESENTED,
        card_feed.TYPE_CORRECTION,
        card_feed.TYPE_STATEMENT,
        card_feed.TYPE_INFO,
    ):
        item_type = nl._ITEM_TYPE_BY_CARD_TYPE[card_type]
        assert item_type in nl.ALLOWED_ITEM_TYPES


def test_classify_orphan_empty_source_ids() -> None:
    item = {"sourceIds": [], "cardIds": ["c1_x"]}
    assert nl.classify_orphan(item) == "empty_source_ids"


def test_classify_orphan_no_stage3_anchor() -> None:
    item = {"sourceIds": ["alpine_packet"], "cardIds": [], "topicIds": [], "meetingIds": []}
    assert nl.classify_orphan(item) == "no_stage3_anchor"


def test_classify_orphan_anchored_is_clean() -> None:
    item = {"sourceIds": ["alpine_packet"], "cardIds": ["c1_x"], "topicIds": [], "meetingIds": []}
    assert nl.classify_orphan(item) is None


def test_sort_key_total_order_undated_last() -> None:
    early = {"recordDate": "2026-05-01", "coveragePeriod": None, "cardIds": ["c1_a"]}
    late = {"recordDate": "2026-05-09", "coveragePeriod": None, "cardIds": ["c1_b"]}
    undated = {"recordDate": None, "coveragePeriod": None, "cardIds": ["c1_c"]}
    keys = sorted([late, undated, early], key=nl._sort_key)
    assert keys == [early, late, undated]


# ---------------------------------------------------------------------------
# Integration — real read surface (seeded DB, mirrors GOV-347)
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
    """Insert + reviewer-promote a statement (GOV-146 reviewer-internal gate).

    Every evidence link carries a raw ``transcript_path`` / ``deep_link`` that MUST
    be stripped upstream — the newsletter leak scan proves they never cross.
    """
    record = {
        "statement_id": statement_id,
        "agenda_item_id": None,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
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
        "source_authority_level, original_url) VALUES ('alpine_packet', 'Agenda Packet', "
        "'alpine', 'agenda_packet', 'official', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-449 newsletter-feed seed",
    )
    conn.commit()
    for i in range(1, 6):
        _promote(conn, f"stmt-{i}", page=i)
    _promote(conn, "stmt-ai", produced_by="ai", run_id="run-ai", page=6)
    _promote(conn, "stmt-corrected", correction="corrected", page=7)
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source", commit=True,
    )


def test_feed_envelope_shape(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    assert feed["scope"] == "alpine"
    assert feed["access"] == "reviewer_internal"
    assert set(feed.keys()) == {"scope", "access", "items"}
    assert isinstance(feed["items"], list) and feed["items"]


def test_every_item_has_contract_shape_and_traces(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    required = {
        "id", "newsletterId", "itemType", "jurisdiction", "recordDate",
        "coveragePeriod", "topicIds", "cardIds", "meetingIds", "sourceIds",
        "status", "labels", "links", "sourceTrail",
    }
    for item in feed["items"]:
        assert required <= set(item)
        assert item["itemType"] in nl.ALLOWED_ITEM_TYPES
        assert item["jurisdiction"] == {"state": "WY", "county": "Lincoln County", "town": "Alpine"}
        # EG-2 / §4 traceability: every item anchors to a real reviewed record.
        assert item["sourceIds"], "served item must carry >=1 sourceId"
        assert item["cardIds"], "served item must carry the Stage-3 card anchor"
        assert item["links"]["timelineUrl"].startswith("/alpine/timeline?card=")


def test_item_ids_are_deterministic_namespaced_sequence(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    ids = [item["id"] for item in feed["items"]]
    assert ids == [f"alpine-newsletter-item-{i:03d}" for i in range(1, len(ids) + 1)]
    # Byte-identical re-projection (pure function of the DB).
    assert json.dumps(feed, sort_keys=True) == json.dumps(
        nl.build_newsletter_feed(conn), sort_keys=True
    )


def test_chronology_non_decreasing_within_batch(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    assert nl.assert_chronology(feed) is True
    # Two ISO-week batches exist in the fixture (week 18 + week 19).
    batches = {item["newsletterId"] for item in feed["items"]}
    assert len(batches) >= 2


def test_chronology_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    # Neuter: emit a batch newest→oldest. A tautological guard would still pass;
    # the real guard must go RED.
    feed = nl.build_newsletter_feed(conn)
    one_batch = sorted(
        {item["newsletterId"] for item in feed["items"]}
    )[0]
    batch_items = [i for i in feed["items"] if i["newsletterId"] == one_batch]
    batch_items.reverse()
    others = [i for i in feed["items"] if i["newsletterId"] != one_batch]
    feed["items"] = batch_items + others
    if len(batch_items) > 1:
        with pytest.raises(nl.NewsletterContractError):
            nl.assert_chronology(feed)


def test_coverage_guard_passes_then_red_on_drop(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    assert nl.assert_feed_covers_surface(conn, feed) is True
    # Neuter: drop one item. The back-gap guard must go RED (load-bearing).
    feed["items"] = feed["items"][1:]
    with pytest.raises(nl.FeedCoverageError):
        nl.assert_feed_covers_surface(conn, feed)


def test_zero_new_label_diff(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    assert nl.label_vocabulary_diff(feed) == set()


def test_ai_and_corrected_items_keep_card_layer_labels(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    by_type = {item["itemType"] for item in feed["items"]}
    assert "ai_presented_context" in by_type
    assert "correction" in by_type
    ai_item = next(i for i in feed["items"] if i["itemType"] == "ai_presented_context")
    assert ai_item["status"] == "ai_presented"
    assert ai_item["labels"]["aiPresented"] is True
    corr_item = next(i for i in feed["items"] if i["itemType"] == "correction")
    assert corr_item["status"] == "corrected"


def test_no_item_styled_verified_without_grounding(conn: sqlite3.Connection) -> None:
    # AC-4 analog: no raw-preservation set up -> nothing reads 'verified'.
    feed = nl.build_newsletter_feed(conn)
    assert not any(item["status"] == "verified" for item in feed["items"])


def test_leak_scan_zero_hits_across_all_artifacts(conn: sqlite3.Connection) -> None:
    # Every evidence link planted a raw transcript_path / vault path; none may cross
    # into the feed, validation log, or readiness record.
    for artifact in (
        nl.build_newsletter_feed(conn),
        nl.source_link_validation(conn),
        nl.build_readiness_record(conn),
    ):
        blob = json.dumps(artifact)
        for marker in ("file://", "/Users/", ".sha256", "transcript_path", "deep_link",
                       "Source-Data", "Raw-PDFs", "\\"):
            assert marker not in blob, f"leak: {marker!r} reached {artifact.get('access')}"


def test_source_trail_local_path_always_null(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    for item in feed["items"]:
        for entry in item["sourceTrail"]:
            assert entry["localSourcePath"] is None
            assert entry["sourceId"] == "alpine_packet"
            assert entry["sourceType"] == "agenda_packet"  # from the sources enum
            assert entry["verificationStatus"]  # carried from the web-safe drawer


def test_source_link_validation_one_row_per_item_zero_orphans(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    result = nl.source_link_validation(conn)
    assert len(result["rows"]) == len(feed["items"])
    assert all(row["links_present"] for row in result["rows"])
    assert result["routing"] == []
    assert result["passed"] is True


def test_orphan_candidate_is_held_and_routed(conn: sqlite3.Connection, monkeypatch) -> None:
    # Neuter the anchor projection so every candidate becomes an empty-sourceIds
    # orphan: it must be HELD OUT of the feed and routed to VSR (never promoted).
    monkeypatch.setattr(
        nl, "_ids_from_evidence",
        lambda _ev: {"sourceIds": [], "meetingIds": [], "topicIds": []},
    )
    feed = nl.build_newsletter_feed(conn)
    assert feed["items"] == []  # orphans never promoted to the digest (§4 rule 2)
    result = nl.source_link_validation(conn)
    assert result["rows"], "a row per candidate is still emitted"
    assert all(not row["links_present"] for row in result["rows"])
    assert result["routing"], "each orphan carries a VSR routing entry"
    for entry in result["routing"]:
        assert entry["routed_to"] == nl.VSR
        assert entry["status"] == "held"
        assert entry["orphan_reason"] == "empty_source_ids"
    assert result["passed"] is True  # 0 promoted + every orphan routed


def test_readiness_record_shape_no_overclaim(conn: sqlite3.Connection) -> None:
    record = nl.build_readiness_record(conn)
    assert record["scope"] == "alpine"
    assert record["orderingPreserved"] == "oldest_to_newest"
    assert "agenda_packet" in record["sourceCategoriesReviewed"]
    assert record["chronologicalRangeProcessed"]["oldest"] <= record[
        "chronologicalRangeProcessed"
    ]["newest"]
    # Honest gaps + no completion overclaim.
    assert record["knownGaps"], "knownGaps populated from the completeness surface"
    assert "complete" not in record["completionFraming"].lower().replace(
        "full-history-complete", ""
    )
    assert "NOT full-history-complete" in record["completionFraming"]
