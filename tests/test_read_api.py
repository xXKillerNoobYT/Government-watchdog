"""Tests for the reviewer-internal read-API (GOV-98, Slice 4 Prereq-0).

Covers the binding acceptance criteria:

- only reviewed/eligible records served (both gates agree; default not returned);
- response body proven free of raw/absolute paths (transport-level);
- new node/edge types exposed (agenda_thread + members + topic_rollup chain);
- acyclicity rejection on the served tree;
- labels travel; no orphan claim served.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import concept_map as cm  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


def _seed(conn: sqlite3.Connection) -> None:
    # registry source (must resolve with source_type + source_class for pointers)
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "original_url) VALUES ('alpine_packet', 'Agenda Packet', 'alpine', "
        "'document', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    # meetings (for chronological member ordering)
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-04-10', 'Town Council', '2026-04-10T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (2, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-04-10:item-3', 1, 3, 'Fireworks ban — first reading')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-05-08:item-7', 2, 7, 'Fireworks ban — adoption')"
    )
    conn.commit()

    # An ELIGIBLE statement: reviewed + a valid evidence pointer + flipped to
    # publishable (the explicit reviewed transition the read lane requires).
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-eligible",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
        [
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
                # raw/private locators that MUST be stripped at the boundary:
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=3",
            }
        ],
    )
    # An UNREVIEWED statement (default machine_extracted_unreviewed) — gate 1 blocks.
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-unreviewed",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Draft claim pending review.",
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "references",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "archive_status": "not_checked",
                "scan_date": "2026-05-09",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 4,
                "verification_status": "machine_extracted_unreviewed",
                "confidence": "low",
            }
        ],
    )
    # A reviewed statement that was NEVER flipped to publishable — gate 2 blocks.
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-reviewed-not-published",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Reviewed but publication gate not opened.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "not_publishable",
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": "2026-05-09",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 5,
                "verification_status": "human_verified",
                "confidence": "high",
            }
        ],
    )

    # concept-map: agenda_thread + members + a typed lifecycle edge; topic tree.
    cm.insert_agenda_thread(conn, "alpine:thread:fireworks-ban", "Fireworks ban",
                            "alpine", "fireworks ban")
    cm.insert_edge(conn, "agenda_item_in_thread", "alpine:2026-04-10:item-3", "alpine:thread:fireworks-ban")
    cm.insert_edge(conn, "agenda_item_in_thread", "alpine:2026-05-08:item-7", "alpine:thread:fireworks-ban")
    cm.insert_edge(conn, "agenda_item_supersedes", "alpine:2026-05-08:item-7", "alpine:2026-04-10:item-3")

    for tid, name, label in (
        ("topic:fireworks", "Fireworks", "fireworks"),
        ("topic:fire", "Fire prevention", "fire prevention"),
        ("topic:safety", "General safety", "general safety"),
    ):
        cm.insert_topic(conn, tid, name, label, jurisdiction_id="alpine")
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")
    cm.insert_edge(conn, "topic_rollup", "topic:fire", "topic:safety")

    # plain-language label layer: a government alias on the 'general safety' topic,
    # carrying mandatory provenance INCLUDING a vault local_ref (must be stripped).
    cm.insert_label_alias(
        conn, "topic:safety", "topic", "public safety", "government_term",
        {
            "source_id": "alpine_packet",
            "original_url": "https://alpinewy.gov/packet.pdf",
            "local_ref": "/Users/IA/Obsidian Vault/Source-Data/alpine/safety.md",
            "page": 3,
        },
        first_seen_meeting_id=2,
        first_seen_date="2026-05-08",
    )


# --- eligibility (fail-closed, both gates) ---------------------------------


def test_only_eligible_served(conn: sqlite3.Connection) -> None:
    served_ids = {r["statement_id"] for r in read_api.published_records(conn)}
    assert served_ids == {"stmt-eligible"}
    assert "stmt-unreviewed" not in served_ids
    assert "stmt-reviewed-not-published" not in served_ids


def test_eligible_record_is_source_backed(conn: sqlite3.Connection) -> None:
    record = read_api.published_records(conn)[0]
    assert record["ui_status"] in pub.PUBLICATION_ELIGIBLE_UI_STATUSES
    assert record["ui_status"] == "source-backed"


# --- labels travel ----------------------------------------------------------


def test_labels_travel(conn: sqlite3.Connection) -> None:
    record = read_api.published_records(conn)[0]
    for label in ("verification_status", "produced_by", "correction_status", "ui_status"):
        assert label in record, f"missing label {label}"
    assert record["produced_by"] == "human"


# --- no orphan served -------------------------------------------------------


def test_orphan_not_served(conn: sqlite3.Connection) -> None:
    # Write an orphan straight to the table (insert_statement would reject it),
    # eligible+publishable, with NO segment and NO evidence link.
    conn.execute(
        "INSERT INTO statements (statement_id, statement_text, verification_status, "
        "produced_by, publication_state, ui_status) VALUES "
        "('stmt-orphan', 'No evidence at all.', 'human_verified', 'human', "
        "'publishable', 'source-backed')"
    )
    conn.commit()
    served_ids = {r["statement_id"] for r in read_api.published_records(conn)}
    assert "stmt-orphan" not in served_ids


# --- transport-level raw/absolute-path assertion ---------------------------


def test_transport_has_zero_raw_paths(conn: sqlite3.Connection) -> None:
    body = read_api.build_response(conn, thread_id="alpine:thread:fireworks-ban",
                                   topic_root="topic:safety")
    blob = json.dumps(body)
    # The seeded evidence link carried a vault path + raw deep-link; they must be
    # absent from the serialized body.
    assert "/Users/" not in blob
    assert "Obsidian Vault" not in blob
    assert "Raw-PDFs" not in blob
    assert "transcript_path" not in blob
    # Public URLs survive (they are not raw paths).
    assert "https://alpinewy.gov/packet.pdf" in blob


def test_assert_no_raw_paths_allows_urls() -> None:
    read_api.assert_no_raw_paths({"url": "https://web.archive.org/web/2026/x"})


def test_assert_no_raw_paths_rejects_posix_path() -> None:
    with pytest.raises(read_api.RawPathLeak):
        read_api.assert_no_raw_paths({"leak": "/Users/IA/raw.pdf"})


def test_assert_no_raw_paths_rejects_raw_marker_in_nonpath() -> None:
    with pytest.raises(read_api.RawPathLeak):
        read_api.assert_no_raw_paths({"leak": "see Obsidian Vault note"})


def test_build_response_raises_if_a_field_leaks(conn: sqlite3.Connection) -> None:
    # Defense-in-depth: even if a raw path were mis-allowlisted, the transport
    # sweep fails the whole response.
    bad = {"records": [{"statement_id": "x", "url": "/Volumes/secret/raw"}]}
    with pytest.raises(read_api.RawPathLeak):
        read_api.assert_no_raw_paths(bad)


# --- agenda thread exposed (members chronological + typed lifecycle) --------


def test_agenda_thread_exposed(conn: sqlite3.Connection) -> None:
    thread = read_api.agenda_thread(conn, "alpine:thread:fireworks-ban")
    assert thread is not None
    assert thread["thread"]["agenda_thread_id"] == "alpine:thread:fireworks-ban"
    member_ids = [m["agenda_item_id"] for m in thread["members"]]
    # chronological (known-then) by meeting date: April before May.
    assert member_ids == ["alpine:2026-04-10:item-3", "alpine:2026-05-08:item-7"]
    # typed lifecycle edge present and typed (never untyped "related").
    assert len(thread["lifecycle_edges"]) == 1
    assert thread["lifecycle_edges"][0]["edge_type"] == "agenda_item_supersedes"


# --- topic tree exposed + rollup filter + acyclic serve --------------------


def test_topic_tree_exposed_with_breadcrumb(conn: sqlite3.Connection) -> None:
    tree = read_api.topic_tree(conn, "topic:safety")
    assert tree["root"]["topic_id"] == "topic:safety"
    # safety -> fire -> fireworks chain present in the subtree.
    fire = tree["tree"]["children"][0]
    assert fire["topic"]["topic_id"] == "topic:fire"
    assert fire["children"][0]["topic"]["topic_id"] == "topic:fireworks"


def test_topic_breadcrumb_shows_where_a_leaf_sits(conn: sqlite3.Connection) -> None:
    tree = read_api.topic_tree(conn, "topic:fireworks")
    crumb = [t["topic_id"] for t in tree["breadcrumb"]]
    assert crumb == ["topic:safety", "topic:fire", "topic:fireworks"]


def test_rollup_filter_returns_descendants(conn: sqlite3.Connection) -> None:
    descendants = read_api.topic_descendants(conn, "topic:fire")
    assert descendants == {"topic:fire", "topic:fireworks"}
    # a leaf returns only itself
    assert read_api.topic_descendants(conn, "topic:fireworks") == {"topic:fireworks"}


# --- plain-language label layer (owner addendum / §A.7) --------------------


def test_topic_node_carries_canonical_human_label(conn: sqlite3.Connection) -> None:
    tree = read_api.topic_tree(conn, "topic:safety")
    assert tree["root"]["canonicalHumanLabel"] == "general safety"
    # every node in the subtree carries the primary label
    fire = tree["tree"]["children"][0]
    assert fire["topic"]["canonicalHumanLabel"] == "fire prevention"


def test_government_alias_exposed_with_provenance(conn: sqlite3.Connection) -> None:
    tree = read_api.topic_tree(conn, "topic:safety")
    aliases = tree["root"]["sourceAliases"]
    assert len(aliases) == 1
    alias = aliases[0]
    assert alias["term"] == "public safety"
    assert alias["aliasType"] == "government_term"
    # mandatory provenance present (public-citable form)
    assert alias["sourceRef"]["sourceId"] == "alpine_packet"
    assert alias["sourceRef"]["originalUrl"] == "https://alpinewy.gov/packet.pdf"
    assert alias["sourceRef"]["locator"]["page"] == 3
    assert alias["firstSeenMeetingId"] == 2


def test_alias_local_ref_never_web_projected(conn: sqlite3.Connection) -> None:
    tree = read_api.topic_tree(conn, "topic:safety")
    blob = json.dumps(tree)
    # the vault local_ref on the alias's sourceRef must be stripped at the boundary
    assert "/Users/" not in blob
    assert "Obsidian Vault" not in blob
    assert "local_ref" not in blob
    assert "localRef" not in blob


def test_canonical_label_is_primary_government_never_primary(conn: sqlite3.Connection) -> None:
    # The government string is only ever an alias — never the canonicalHumanLabel.
    tree = read_api.topic_tree(conn, "topic:safety")
    assert tree["root"]["canonicalHumanLabel"] != "public safety"
    terms = {a["term"] for a in tree["root"]["sourceAliases"]}
    assert "public safety" in terms


def test_full_response_label_layer_passes_transport(conn: sqlite3.Connection) -> None:
    # The label layer (incl. its provenance) survives the transport sweep clean.
    body = read_api.build_response(conn, thread_id="alpine:thread:fireworks-ban",
                                   topic_root="topic:safety")
    blob = json.dumps(body)
    assert "/Users/" not in blob and "Obsidian Vault" not in blob
    assert "canonicalHumanLabel" in blob and "sourceAliases" in blob
    # thread node also carries the label layer
    assert body["agenda_thread"]["thread"]["canonicalHumanLabel"] == "fireworks ban"


def test_topic_tree_rejects_cycle_before_serving(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO concept_edges (edge_id, edge_type, from_node_id, from_node_type, "
        "to_node_id, to_node_type) VALUES "
        "('c', 'topic_rollup', 'topic:safety', 'topic', 'topic:fireworks', 'topic')"
    )
    conn.commit()
    with pytest.raises(cm.TopicTreeCycleError):
        read_api.topic_tree(conn, "topic:safety")
