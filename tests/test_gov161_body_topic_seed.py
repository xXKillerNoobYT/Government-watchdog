"""Tests for GOV-161 P&Z Board body→topic seed script.

Verifies discovery, preflight, write, and post-state invariants using an
isolated in-memory DB seeded with the GOV-149 topic tree + synthetic promoted
rows (some naming the P&Z Board).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ai_risk_gate as rg  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import gov149_concept_graph_seed as gov149  # noqa: E402
import gov161_body_topic_seed as seed  # noqa: E402
import read_api  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Fresh DB with migrations, source, reviewer, GOV-149 topics, and synthetic rows."""
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)

    c.execute(
        "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
        "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
    )
    rg.register_reviewer(
        c, "reviewer:isaac", display_name="test",
        registered_by="test", commit=False,
    )
    c.commit()
    return c


def _insert_promoted_row(
    conn: sqlite3.Connection,
    statement_id: str,
    statement_text: str,
    quoted_text: str = "test quote from source",
) -> None:
    """Insert a synthetic promoted AI row + evidence link + reviewer decision.

    Leaves ``ai_extraction_run_id`` null — ``_producing_run_ok`` returns True
    for null run ids (no run to block on), which is correct for test fixtures.
    """
    conn.execute(
        "INSERT INTO statements "
        "(statement_id, statement_text, produced_by, verification_status, "
        "publication_state, layer, is_verbatim) "
        "VALUES (?, ?, 'ai', 'reviewed_source_linked', 'not_publishable', "
        "'ai_thought_then', 0)",
        (statement_id, statement_text),
    )
    conn.execute(
        "INSERT INTO evidence_links "
        "(evidence_link_id, from_node_type, from_node_id, to_source_id, "
        "relation, locator_kind, quoted_text, char_start, char_end, original_url) "
        "VALUES (?, 'statement', ?, 'alpine_local_corpus', 'references', "
        "'char_span', ?, 0, ?, 'file:///local/corpus.txt')",
        (f"{statement_id}:ev0", statement_id, quoted_text, len(quoted_text)),
    )
    rg.promote_statement(
        conn, statement_id,
        reviewer_id="reviewer:isaac",
        decision="approved",
        reason="test promotion",
        to_verification_status="reviewed_source_linked",
        commit=False,
    )
    conn.commit()


def _seed_gov149_topics(conn: sqlite3.Connection) -> None:
    """Write the GOV-149 topic tree (root + 3 civic topics) with synthetic grounding."""
    cm.insert_topic(conn, gov149.ROOT_TOPIC_ID, gov149.ROOT_TOPIC_NAME,
                    gov149.ROOT_TOPIC_LABEL, jurisdiction_id="alpine", commit=False)
    for topic in gov149.CIVIC_TOPICS:
        cm.insert_topic(conn, topic.topic_id, topic.name, topic.label,
                        jurisdiction_id="alpine", commit=False)
        cm.insert_edge(conn, "topic_rollup", topic.topic_id, gov149.ROOT_TOPIC_ID,
                       created_by="reviewer:isaac", commit=False)
        primary_sid = topic.grounding_statement_ids[0]
        _insert_promoted_row(conn, primary_sid, f"Civic fact about {topic.label}.")
        source_ref = {
            "source_id": "alpine_local_corpus",
            "local_ref": "file:///local/corpus.txt",
            "char_start": 0, "char_end": 10,
        }
        cm.insert_label_alias(
            conn, topic.topic_id, "topic", topic.alias_term, "government_term",
            source_ref, created_by="reviewer:isaac", commit=False,
        )
    conn.commit()


def _seed_pnz_statements(conn: sqlite3.Connection, count: int = 3) -> list[str]:
    """Insert synthetic promoted rows that name the P&Z Board."""
    ids = []
    for i in range(count):
        sid = f"alpine_local_corpus:ai:pnz{i:04d}:{i:04d}"
        _insert_promoted_row(
            conn, sid,
            f"The Alpine Planning and Zoning Board reviewed the subdivision plat {i}.",
            quoted_text=f"Planning and Zoning Board reviewed plat {i}",
        )
        ids.append(sid)
    return ids


def _seed_non_pnz_statements(conn: sqlite3.Connection, count: int = 2) -> list[str]:
    """Insert synthetic promoted rows that do NOT name the P&Z Board."""
    ids = []
    for i in range(count):
        sid = f"alpine_local_corpus:ai:other{i:04d}:{i:04d}"
        _insert_promoted_row(
            conn, sid,
            f"The Town Council discussed the water system issue {i}.",
            quoted_text=f"water system maintenance {i}",
        )
        ids.append(sid)
    return ids


class TestDiscovery:
    def test_discovers_pnz_statements(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        pnz_ids = _seed_pnz_statements(conn, count=3)
        _seed_non_pnz_statements(conn, count=2)

        matches = seed.discover_pnz_statements(conn)
        discovered_ids = [m["statement_id"] for m in matches]
        assert set(discovered_ids) == set(pnz_ids)

    def test_no_pnz_statements_returns_empty(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        _seed_non_pnz_statements(conn, count=5)

        matches = seed.discover_pnz_statements(conn)
        assert matches == []

    def test_discovery_is_sorted(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        _seed_pnz_statements(conn, count=3)

        matches = seed.discover_pnz_statements(conn)
        ids = [m["statement_id"] for m in matches]
        assert ids == sorted(ids)

    def test_case_insensitive_match(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        sid = "alpine_local_corpus:ai:pnzupper:0000"
        _insert_promoted_row(
            conn, sid,
            "PLANNING AND ZONING BOARD met in special session.",
        )

        matches = seed.discover_pnz_statements(conn)
        assert len(matches) == 1


class TestPreflight:
    def test_preflight_passes(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        _seed_pnz_statements(conn)

        plan = seed._preflight(conn)
        assert len(plan["pnz_matches"]) >= 1
        assert plan["primary_statement_id"]

    def test_rejects_missing_root(self, conn: sqlite3.Connection) -> None:
        _seed_pnz_statements(conn)
        with pytest.raises(seed.SeedError, match="jurisdiction root"):
            seed._preflight(conn)

    def test_rejects_missing_gov149_topics(self, conn: sqlite3.Connection) -> None:
        cm.insert_topic(conn, gov149.ROOT_TOPIC_ID, gov149.ROOT_TOPIC_NAME,
                        gov149.ROOT_TOPIC_LABEL, jurisdiction_id="alpine")
        _seed_pnz_statements(conn)
        with pytest.raises(seed.SeedError, match="GOV-149 civic topics missing"):
            seed._preflight(conn)

    def test_rejects_no_pnz_statements(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        _seed_non_pnz_statements(conn)
        with pytest.raises(seed.SeedError, match="no reviewer-internal statements name"):
            seed._preflight(conn)

    def test_rejects_duplicate_topic(self, conn: sqlite3.Connection) -> None:
        _seed_gov149_topics(conn)
        _seed_pnz_statements(conn)
        cm.insert_topic(conn, seed.PNZ_TOPIC_ID, "test", "test",
                        jurisdiction_id="alpine")
        with pytest.raises(seed.SeedError, match="already exists"):
            seed._preflight(conn)


class TestApply:
    def test_dry_run_writes_nothing(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        _seed_gov149_topics(conn)
        _seed_pnz_statements(conn)

        db_path = tmp_path / "dry.db"
        db.apply_migrations(db_path)
        with db.open_db(db_path) as c2:
            c2.execute(
                "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
                "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
            )
            rg.register_reviewer(c2, "reviewer:isaac", display_name="test",
                                 registered_by="test", commit=False)
            c2.commit()
            _seed_gov149_topics(c2)
            _seed_pnz_statements(c2)

        log: list[str] = []
        rc = seed.run(db_path, apply=False, log_lines=log)
        assert rc == 0
        assert any("DRY RUN" in line for line in log)

        with db.open_db(db_path) as c3:
            pnz = c3.execute(
                "SELECT topic_id FROM topics WHERE topic_id = ?",
                (seed.PNZ_TOPIC_ID,),
            ).fetchone()
            assert pnz is None

    def test_apply_creates_topic(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        db_path = tmp_path / "apply.db"
        db.apply_migrations(db_path)
        with db.open_db(db_path) as c2:
            c2.execute(
                "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
                "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
            )
            rg.register_reviewer(c2, "reviewer:isaac", display_name="test",
                                 registered_by="test", commit=False)
            c2.commit()
            _seed_gov149_topics(c2)
            _seed_pnz_statements(c2)

        log: list[str] = []
        rc = seed.run(db_path, apply=True, log_lines=log)
        assert rc == 0
        assert any("POST-STATE OK" in line for line in log)

        with db.open_db(db_path) as c3:
            pnz = c3.execute(
                "SELECT topic_id, canonical_human_label FROM topics WHERE topic_id = ?",
                (seed.PNZ_TOPIC_ID,),
            ).fetchone()
            assert pnz is not None
            assert pnz["canonical_human_label"] == seed.PNZ_TOPIC_LABEL

            edge = c3.execute(
                "SELECT * FROM concept_edges WHERE edge_type = 'topic_rollup' "
                "AND from_node_id = ? AND to_node_id = ?",
                (seed.PNZ_TOPIC_ID, gov149.ROOT_TOPIC_ID),
            ).fetchone()
            assert edge is not None

            aliases = cm.aliases_for_node(c3, seed.PNZ_TOPIC_ID, "topic")
            assert len(aliases) >= 1
            assert aliases[0]["term"] == seed.PNZ_ALIAS_TERM

    def test_apply_preserves_gov149_topics(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        db_path = tmp_path / "preserve.db"
        db.apply_migrations(db_path)
        with db.open_db(db_path) as c2:
            c2.execute(
                "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
                "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
            )
            rg.register_reviewer(c2, "reviewer:isaac", display_name="test",
                                 registered_by="test", commit=False)
            c2.commit()
            _seed_gov149_topics(c2)
            _seed_pnz_statements(c2)

        log: list[str] = []
        rc = seed.run(db_path, apply=True, log_lines=log)
        assert rc == 0

        with db.open_db(db_path) as c3:
            tree = read_api.topic_tree(c3, gov149.ROOT_TOPIC_ID)
            child_ids = {c["topic"]["topic_id"] for c in tree["tree"]["children"]}
            assert seed.GOV149_CIVIC_TOPIC_IDS <= child_ids
            assert seed.PNZ_TOPIC_ID in child_ids

    def test_apply_keeps_public_zero(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        db_path = tmp_path / "pub.db"
        db.apply_migrations(db_path)
        with db.open_db(db_path) as c2:
            c2.execute(
                "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
                "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
            )
            rg.register_reviewer(c2, "reviewer:isaac", display_name="test",
                                 registered_by="test", commit=False)
            c2.commit()
            _seed_gov149_topics(c2)
            _seed_pnz_statements(c2)

        log: list[str] = []
        seed.run(db_path, apply=True, log_lines=log)

        with db.open_db(db_path) as c3:
            public = read_api.published_records(c3)
            assert len(public) == 0


class TestInvariants:
    def test_pnz_topic_id_is_stable(self) -> None:
        assert seed.PNZ_TOPIC_ID == "topic:alpine:pnz-board"

    def test_pnz_topic_not_in_gov149_set(self) -> None:
        assert seed.PNZ_TOPIC_ID not in seed.GOV149_CIVIC_TOPIC_IDS

    def test_gov149_set_is_correct(self) -> None:
        expected = {"topic:alpine:water-system", "topic:alpine:budget-taxes",
                    "topic:alpine:council-governance"}
        assert seed.GOV149_CIVIC_TOPIC_IDS == expected
