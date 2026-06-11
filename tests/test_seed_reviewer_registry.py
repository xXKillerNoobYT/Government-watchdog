"""Tests for the GOV-135 Stage-1 reviewer-registry seed (scripts/seed_reviewer_registry.py).

Proves the acceptance criteria of the owner-decision seed:

- after the seed, ``is_registered_reviewer('reviewer:isaac')`` is True and the
  registry holds EXACTLY ONE active reviewer row;
- every other id stays fail-closed (False) — the GOV-131 default-deny is intact;
- the seed is idempotent (re-running converges to the same single row);
- a dry-run writes nothing (the registry stays empty until ``--apply``);
- the seed REFUSES (and rolls back) if any non-Isaac active identity is present —
  the "exactly one" owner-decision boundary is enforced in code, not trusted;
- the seeded row is vault-only: ``to_web_safe()`` drops every distinctive
  reviewer-identity column, so the seed exposes nothing on a public surface.

No AI, no network: pure sqlite over a tmp-path vault DB migrated through 0014.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_risk_gate as rg  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import seed_reviewer_registry as seed  # noqa: E402


def _migrated(tmp_path: Path) -> Path:
    db_path = tmp_path / "vault.db"
    db.apply_migrations(db_path)
    return db_path


def _active(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            f"SELECT reviewer_id FROM {rg.REVIEWER_REGISTRY_TABLE} WHERE status = 'active'"
        )
    ]


# --- the seed: exactly one active reviewer, fail-closed preserved -----------

def test_seed_creates_exactly_one_active_reviewer(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        summary = seed.seed_isaac(conn, apply=True)
        assert summary["active_reviewer_count"] == 1
        assert summary["isaac_registered"] is True
        assert _active(conn) == [seed.REVIEWER_ID]
        # Isaac passes the gate; nobody else does (GOV-131 default-deny intact).
        assert rg.is_registered_reviewer(conn, seed.REVIEWER_ID) is True
        for other in ("reviewer:unknown", "ai", "automation", "", None):
            assert rg.is_registered_reviewer(conn, other) is False


def test_seed_is_idempotent(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        seed.seed_isaac(conn, apply=True)
    # second run converges to the same single row (register_reviewer upserts).
    with db.open_db(db_path) as conn:
        summary = seed.seed_isaac(conn, apply=True)
        assert summary["active_reviewer_count"] == 1
        assert _active(conn) == [seed.REVIEWER_ID]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        summary = seed.seed_isaac(conn, apply=False)
        assert summary["applied"] is False
    # the registry is still empty — the safe default — until --apply.
    with db.open_db(db_path) as conn:
        assert _active(conn) == []
        assert rg.is_registered_reviewer(conn, seed.REVIEWER_ID) is False


def test_seed_refuses_extra_active_identity(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        # an unexpected active identity is present (e.g. a mis-seed): the seed must
        # refuse and roll back — the "exactly one" boundary is enforced, not hoped.
        rg.register_reviewer(
            conn, "reviewer:someone-else", display_name="Stranger", registered_by="x"
        )
        with pytest.raises(seed.SeedError):
            seed.seed_isaac(conn, apply=True)
        # the refused transaction was rolled back: Isaac was NOT added.
        assert "reviewer:someone-else" in _active(conn)
        assert seed.REVIEWER_ID not in _active(conn)


# --- data boundary: the seeded row is never web-projected (ADR §5) ----------

def test_seeded_row_not_web_projected(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        seed.seed_isaac(conn, apply=True)
        row = dict(
            conn.execute(
                f"SELECT * FROM {rg.REVIEWER_REGISTRY_TABLE} WHERE reviewer_id = ?",
                (seed.REVIEWER_ID,),
            ).fetchone()
        )
    safe = pub.to_web_safe(row)
    # every distinctive reviewer-identity field is dropped; nothing identifying
    # the reviewer survives the web-safe projection.
    for distinctive in (
        "reviewer_id", "display_name", "registered_by", "registered_utc", "note",
    ):
        assert distinctive not in safe
    assert seed.DISPLAY_NAME not in safe.values()
    assert seed.REVIEWER_ID not in safe.values()


# --- CLI entrypoint: dry-run default, --apply commits -----------------------

def test_main_dry_run_default_leaves_registry_empty(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    rc = seed.main(["--db", str(db_path)])  # no --apply
    assert rc == 0
    with db.open_db(db_path) as conn:
        assert _active(conn) == []


def test_main_apply_seeds_exactly_one(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    rc = seed.main(["--db", str(db_path), "--apply"])
    assert rc == 0
    with db.open_db(db_path) as conn:
        assert _active(conn) == [seed.REVIEWER_ID]
        assert rg.is_registered_reviewer(conn, seed.REVIEWER_ID) is True
