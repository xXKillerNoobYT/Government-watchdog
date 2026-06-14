"""Tests for the GOV-149 owner-gated reviewer-internal topic-layer seed.

Proves the binding acceptance criteria of GOV-149 against a fixture DB shaped
like the real GOV-146 reviewer-internal data:

- a non-empty real ``topic_tree`` is served (1 jurisdiction root + 3 civic
  topics), each civic topic grounded in a promoted statement's cited source;
- caps hold (<=6 nodes / <=4 rollup edges) and the tree is acyclic;
- the seed never publishes (public lane stays 0) and never mutates the
  reviewer-internal set;
- agenda threads stay honest-EMPTY (no title-similarity threads);
- the served body is web-safe: the ``file://`` vault provenance URI used to
  ground a topic never crosses the boundary (transport sweep holds);
- fail-closed: a topic grounded in a NON-promoted statement is refused.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ai_risk_gate as gate  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import gov149_concept_graph_seed as seed  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402

# Every statement id the manifest grounds in (must all be promoted for the seed).
_GROUNDING_IDS = tuple(
    sid for topic in seed.CIVIC_TOPICS for sid in topic.grounding_statement_ids
)


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    try:
        yield connection
    finally:
        connection.close()


def _promote_ai_statement(conn: sqlite3.Connection, statement_id: str, page: int) -> None:
    """Insert one AI statement with a file:// vault provenance link and promote it."""
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "agenda_item_id": "alpine:item",
            "statement_text": "A grounded civic announcement from the Alpine record.",
            "produced_by": "ai",
            "layer": "ai_thought_then",
        },
        [
            {
                "to_source_id": "alpine_local",
                "relation": "references",
                # the vault provenance URI that must NOT cross the boundary:
                "original_url": f"file:///Users/IA/Documents/TOA/TownOfAlpine/{statement_id}.txt",
                "archive_status": "not_checked",
                "scan_date": "2026-06-13",
                "captured_at_utc": "2026-06-13T00:00:00Z",
                "locator_kind": "page",
                "page": page,
                "verification_status": "machine_extracted_unreviewed",
                "confidence": "high",
            }
        ],
    )
    gate.promote_statement(
        conn,
        statement_id,
        reviewer_id="reviewer:isaac",
        decision="approved",
        reason="GOV-146 reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _seed_promoted_corpus(conn: sqlite3.Connection) -> None:
    """Build the minimal real-shaped fixture: a source + the 6 promoted AI rows."""
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, original_url) "
        "VALUES ('alpine_local', 'Alpine Local Corpus', 'alpine', 'document', 'official', NULL)"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-06-11', 'Town Council', '2026-06-11T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:item', 1, 1, 'Item')"
    )
    conn.commit()
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-149 fixture",
    )
    for i, sid in enumerate(_GROUNDING_IDS, start=1):
        _promote_ai_statement(conn, sid, page=i)


def _apply_seed(conn: sqlite3.Connection):
    baseline = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    seed._preflight(conn)
    seed._write(conn, lambda _msg: None)
    conn.commit()
    return baseline


def test_seed_builds_real_topic_tree(conn: sqlite3.Connection) -> None:
    _seed_promoted_corpus(conn)
    _apply_seed(conn)

    tree = read_api.topic_tree(conn, seed.ROOT_TOPIC_ID)
    assert tree["root"]["topic_id"] == seed.ROOT_TOPIC_ID
    assert tree["root"]["canonicalHumanLabel"] == seed.ROOT_TOPIC_LABEL

    children = {c["topic"]["topic_id"]: c["topic"] for c in tree["tree"]["children"]}
    assert set(children) == {t.topic_id for t in seed.CIVIC_TOPICS}
    for topic in seed.CIVIC_TOPICS:
        node = children[topic.topic_id]
        assert node["canonicalHumanLabel"] == topic.label
        # each civic topic carries >=1 grounding alias with mandatory provenance.
        assert node["sourceAliases"], f"{topic.topic_id} has no grounding alias"
        alias = node["sourceAliases"][0]
        assert alias["term"] == topic.alias_term
        assert alias["sourceRef"]["sourceId"] == "alpine_local"
        assert "locator" in alias["sourceRef"]


def test_seed_caps_and_acyclic(conn: sqlite3.Connection) -> None:
    _seed_promoted_corpus(conn)
    _apply_seed(conn)

    node_count = conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]
    edge_count = conn.execute(
        "SELECT COUNT(*) AS n FROM concept_edges WHERE edge_type = 'topic_rollup'"
    ).fetchone()["n"]
    assert node_count == 1 + len(seed.CIVIC_TOPICS) <= seed.MAX_TOPIC_NODES
    assert edge_count == len(seed.CIVIC_TOPICS) <= seed.MAX_ROLLUP_EDGES
    # acyclic by construction (raises on a cycle).
    cm.assert_acyclic(conn)


def test_seed_public_stays_zero_and_threads_empty(conn: sqlite3.Connection) -> None:
    _seed_promoted_corpus(conn)
    baseline = _apply_seed(conn)

    assert read_api.published_records(conn) == []
    # reviewer-internal set unchanged by a pure topic-layer write.
    after = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    assert after == baseline == set(_GROUNDING_IDS)
    # honest-EMPTY agenda threads: the seed fabricates none.
    assert conn.execute("SELECT COUNT(*) AS n FROM agenda_threads").fetchone()["n"] == 0


def test_seed_body_is_web_safe(conn: sqlite3.Connection) -> None:
    _seed_promoted_corpus(conn)
    _apply_seed(conn)

    body = read_api.build_response(
        conn, topic_root=seed.ROOT_TOPIC_ID,
        include_records=True, include_reviewer_internal=True,
    )
    # transport sweep over the whole assembled body (raises on any raw/file:// path).
    read_api.assert_no_raw_paths(body)
    # the file:// vault URI never appears anywhere in the served tree.
    flat = repr(body)
    assert "file://" not in flat
    assert "TownOfAlpine" not in flat
    assert "/Users/" not in flat


def test_seed_refuses_ungrounded_topic(conn: sqlite3.Connection) -> None:
    # Build the corpus but promote only PART of it: drop the last grounding id.
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, original_url) "
        "VALUES ('alpine_local', 'Alpine Local Corpus', 'alpine', 'document', 'official', NULL)"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-06-11', 'Town Council', '2026-06-11T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:item', 1, 1, 'Item')"
    )
    conn.commit()
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-149 fixture",
    )
    for i, sid in enumerate(_GROUNDING_IDS[:-1], start=1):  # one short
        _promote_ai_statement(conn, sid, page=i)

    with pytest.raises(seed.SeedError, match="reviewer-internal serve"):
        seed._preflight(conn)
    # nothing was written on the refusal.
    assert conn.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"] == 0
