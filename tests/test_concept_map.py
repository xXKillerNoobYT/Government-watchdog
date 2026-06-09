"""Tests for the concept-map registry SSOT + GOV-98 additions (Slice 4 Prereq-0).

Covers: the registry vocabulary (1.07 set + GOV-98 node/edges), the edge
endpoint contract, generic-table parity with the migration CHECK, and the
topic_rollup acyclicity invariant (BEH-TOPICTREE-4) at both insert time and
serve time.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import concept_map as cm  # noqa: E402
import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    yield connection
    connection.close()


# --- registry vocabulary ----------------------------------------------------


def test_gov98_node_added() -> None:
    assert "agenda_thread" in cm.ALLOWED_NODE_TYPES


def test_gov98_edges_added() -> None:
    for edge in (
        "agenda_item_in_thread",
        "agenda_item_supersedes",
        "agenda_item_amends",
        "agenda_item_revisits",
        "topic_rollup",
    ):
        assert edge in cm.ALLOWED_EDGE_TYPES


def test_existing_1_07_vocabulary_preserved() -> None:
    # Additive: the GOV-98 change never drops a 1.07 type.
    for node in ("meeting", "agenda_item", "statement", "topic", "evidence_link"):
        assert node in cm.ALLOWED_NODE_TYPES
    for edge in ("contains_agenda_item", "statement_from_segment", "topic_groups"):
        assert edge in cm.ALLOWED_EDGE_TYPES


def test_every_edge_endpoint_is_a_known_node_type() -> None:
    for etype, (froms, tos) in cm.EDGE_ENDPOINTS.items():
        assert etype in cm.ALLOWED_EDGE_TYPES
        assert (froms | tos) <= cm.ALLOWED_NODE_TYPES


def test_generic_edge_types_match_migration_check(conn: sqlite3.Connection) -> None:
    # The Python generic-edge set must equal the migration-0012 CHECK literal, or
    # an edge the registry accepts would be rejected by the DB (or vice versa).
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'concept_edges'"
    ).fetchone()[0]
    for etype in cm.GENERIC_EDGE_TYPES:
        assert f"'{etype}'" in sql, f"{etype} missing from concept_edges CHECK"


# --- edge endpoint contract -------------------------------------------------


def test_insert_edge_rejects_wrong_endpoint_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.EdgeError):
        # topic_rollup must be topic->topic, not agenda_item->topic.
        cm.insert_edge(conn, "topic_rollup", "alpine:item-1", "topic:fire",
                       from_node_type="agenda_item")


def test_insert_edge_rejects_non_generic_edge(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.EdgeError):
        cm.insert_edge(conn, "statement_from_segment", "s1", "seg1")


def test_insert_edge_is_idempotent(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:fireworks", "Fireworks")
    cm.insert_topic(conn, "topic:fire", "Fire prevention")
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")  # no dup
    count = conn.execute(
        "SELECT COUNT(*) FROM concept_edges WHERE edge_type = 'topic_rollup'"
    ).fetchone()[0]
    assert count == 1


# --- acyclicity (BEH-TOPICTREE-4) ------------------------------------------


def _seed_topic_chain(conn: sqlite3.Connection) -> None:
    for tid, name in (
        ("topic:fireworks", "Fireworks"),
        ("topic:fire", "Fire prevention"),
        ("topic:safety", "General safety"),
    ):
        cm.insert_topic(conn, tid, name)
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")
    cm.insert_edge(conn, "topic_rollup", "topic:fire", "topic:safety")


def test_topic_rollup_chain_serves_acyclic(conn: sqlite3.Connection) -> None:
    _seed_topic_chain(conn)
    cm.assert_acyclic(conn)  # must not raise


def test_topic_rollup_self_loop_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:x", "X")
    with pytest.raises(cm.TopicTreeCycleError):
        cm.insert_edge(conn, "topic_rollup", "topic:x", "topic:x")


def test_topic_rollup_cycle_rejected_at_insert(conn: sqlite3.Connection) -> None:
    _seed_topic_chain(conn)
    # safety -> fireworks would close fireworks -> fire -> safety -> fireworks.
    with pytest.raises(cm.TopicTreeCycleError):
        cm.insert_edge(conn, "topic_rollup", "topic:safety", "topic:fireworks")


def test_assert_acyclic_catches_a_cycle_written_around_the_guard(conn: sqlite3.Connection) -> None:
    # Serve-time guard is independent: write a cycle straight to the table
    # (bypassing insert_edge) and prove assert_acyclic still rejects it.
    _seed_topic_chain(conn)
    conn.execute(
        "INSERT INTO concept_edges (edge_id, edge_type, from_node_id, from_node_type, "
        "to_node_id, to_node_type) VALUES "
        "('raw-cycle', 'topic_rollup', 'topic:safety', 'topic', 'topic:fireworks', 'topic')"
    )
    conn.commit()
    with pytest.raises(cm.TopicTreeCycleError):
        cm.assert_acyclic(conn)
