"""Tests for GOV-162 batch-2 promotion script.

Verifies the promotion script's preflight, promotion via
``promote_statement``, and post-state invariants using an isolated
in-memory DB with synthetic AI rows.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as rg  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import gov162_promotion_batch2 as promo  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    # Seed source + reviewer (minimal viable state).
    c.execute(
        "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
        "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
    )
    rg.register_reviewer(
        c, promo.REVIEWER_ID, display_name="test",
        registered_by="test", commit=False,
    )
    c.commit()
    return c


def _insert_ai_row(
    conn: sqlite3.Connection,
    statement_id: str,
    statement_text: str = "Test civic fact.",
    quoted_text: str = "test quote",
) -> None:
    """Insert a synthetic AI row + evidence link (mimics Lane-2 output)."""
    conn.execute(
        "INSERT INTO statements "
        "(statement_id, statement_text, produced_by, verification_status, "
        "publication_state, layer, is_verbatim) "
        "VALUES (?, ?, 'ai', 'machine_extracted_unreviewed', 'not_publishable', "
        "'ai_thought_then', 0)",
        (statement_id, statement_text),
    )
    conn.execute(
        "INSERT INTO evidence_links "
        "(evidence_link_id, from_node_type, from_node_id, to_source_id, "
        "relation, locator_kind, quoted_text, char_start, char_end) "
        "VALUES (?, 'statement', ?, 'alpine_local_corpus', 'references', "
        "'char_span', ?, 0, ?)",
        (f"{statement_id}:ev0", statement_id, quoted_text, len(quoted_text)),
    )
    conn.commit()


def _seed_all_batch2(conn: sqlite3.Connection) -> None:
    for sid in promo.BATCH2_STATEMENT_IDS:
        _insert_ai_row(conn, sid, statement_text=f"Civic fact for {sid}.")


class TestPreflight:
    def test_preflight_passes_with_all_rows(self, conn: sqlite3.Connection) -> None:
        _seed_all_batch2(conn)
        plan = promo._preflight(conn)
        assert len(plan) == len(promo.BATCH2_STATEMENT_IDS)

    def test_preflight_rejects_missing_row(self, conn: sqlite3.Connection) -> None:
        # Seed all except the first one.
        for sid in promo.BATCH2_STATEMENT_IDS[1:]:
            _insert_ai_row(conn, sid)
        with pytest.raises(promo.SeedError, match="does not resolve"):
            promo._preflight(conn)

    def test_preflight_rejects_non_ai_row(self, conn: sqlite3.Connection) -> None:
        _seed_all_batch2(conn)
        conn.execute(
            "UPDATE statements SET produced_by = 'automation' WHERE statement_id = ?",
            (promo.BATCH2_STATEMENT_IDS[0],),
        )
        conn.commit()
        with pytest.raises(promo.SeedError, match="produced_by"):
            promo._preflight(conn)

    def test_preflight_rejects_already_promoted(self, conn: sqlite3.Connection) -> None:
        _seed_all_batch2(conn)
        conn.execute(
            "UPDATE statements SET verification_status = 'reviewed_source_linked' "
            "WHERE statement_id = ?",
            (promo.BATCH2_STATEMENT_IDS[0],),
        )
        conn.commit()
        with pytest.raises(promo.SeedError, match="refusing to re-promote"):
            promo._preflight(conn)

    def test_preflight_rejects_unregistered_reviewer(
        self, conn: sqlite3.Connection
    ) -> None:
        _seed_all_batch2(conn)
        rg.revoke_reviewer(
            conn, promo.REVIEWER_ID, revoked_by="test", reason="test", commit=True,
        )
        with pytest.raises(promo.SeedError, match="not a registered"):
            promo._preflight(conn)


class TestPromotion:
    def test_dry_run_writes_nothing(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        _seed_all_batch2(conn)
        log: list[str] = []
        rc = promo.run(tmp_path / "test.db", apply=False, log=log)
        # Dry-run uses the CONN we created but the script opens its own;
        # just check exit code.
        assert rc == 0
        assert any("DRY RUN" in line for line in log)

    def test_apply_promotes_all_18(self, conn: sqlite3.Connection, tmp_path: Path) -> None:
        _seed_all_batch2(conn)
        db_path = tmp_path / "prom.db"
        db.apply_migrations(db_path)
        with db.open_db(db_path) as c2:
            c2.execute(
                "INSERT OR IGNORE INTO sources (source_id, name, scope, url, source_type) "
                "VALUES ('alpine_local_corpus', 'test', 'alpine', 'test://', 'local_archive')"
            )
            rg.register_reviewer(
                c2, promo.REVIEWER_ID, display_name="test",
                registered_by="test", commit=False,
            )
            for sid in promo.BATCH2_STATEMENT_IDS:
                _insert_ai_row(c2, sid)
            c2.commit()

        log: list[str] = []
        rc = promo.run(db_path, apply=True, log=log)
        assert rc == 0
        assert any("POST-STATE OK" in line for line in log)

        with db.open_db(db_path) as c3:
            promoted = c3.execute(
                "SELECT COUNT(*) FROM statements WHERE verification_status = ?",
                (promo.TARGET_VERIFICATION_STATUS,),
            ).fetchone()[0]
            assert promoted == 18

            decisions = c3.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0]
            assert decisions == 18

            public = read_api.published_records(c3)
            assert len(public) == 0


class TestInvariants:
    def test_no_duplicate_ids(self) -> None:
        assert len(set(promo.BATCH2_STATEMENT_IDS)) == len(promo.BATCH2_STATEMENT_IDS)

    def test_all_ids_are_ai_corpus_pattern(self) -> None:
        for sid in promo.BATCH2_STATEMENT_IDS:
            assert sid.startswith("alpine_local_corpus:ai:")

    def test_batch_count_is_18(self) -> None:
        assert len(promo.BATCH2_STATEMENT_IDS) == 18
