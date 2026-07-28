"""Schema RED proofs for ACCESS-2026 v0.1."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "Database" / "migrations" / "0032_access_decision_core.sql"
TABLES = {
    "access_plan_assignments",
    "access_program_assignments",
    "access_feature_grants",
    "access_geography_grants",
}
TS = "2026-07-24T12:00:00.000+00:00"


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "access.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    connection.execute(
        "INSERT INTO users (user_id, email, created_utc)"
        " VALUES ('u1', 'u1@example.test', ?)",
        (TS,),
    )
    connection.execute(
        "INSERT INTO areas (area_id, kind, name, created_utc)"
        " VALUES ('wy', 'state', 'Wyoming', ?)",
        (TS,),
    )
    connection.execute(
        "INSERT INTO access_plan_assignments"
        " (assignment_id,user_id,plan_code,catalog_version,assignment_state,"
        " owner_decision_ref,operation_id,actor,recorded_utc,effective_utc)"
        " VALUES ('basis','u1','free','ACCESS-2026/v0.1','active',"
        " 'owner:basis','op:basis','test-suite',?,?)",
        (TS, TS),
    )
    connection.commit()
    yield connection
    connection.close()


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_four_tables_and_migration_ledger_row_exist(conn):
    names = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert TABLES <= names
    assert conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?",
        ("0032_access_decision_core",),
    ).fetchone()


def test_migration_is_inert_on_a_fresh_database(tmp_path):
    db_path = tmp_path / "inert.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    try:
        assert {
            table: _count(connection, table)
            for table in TABLES
        } == {table: 0 for table in TABLES}
    finally:
        connection.close()


def test_migration_is_create_only_and_raw_rerunnable(tmp_path):
    statements = db._statements(MIGRATION.read_text(encoding="utf-8"))
    assert statements
    for statement in statements:
        head = " ".join(statement.split()[:5]).upper()
        assert head.startswith(
            ("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS")
        )

    raw = sqlite3.connect(tmp_path / "raw.db")
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
    raw.execute("CREATE TABLE areas (area_id TEXT PRIMARY KEY)")
    for _ in range(2):
        for statement in statements:
            raw.execute(statement)
    raw.close()


@pytest.mark.parametrize(
    "table,sql",
    [
        (
            "access_plan_assignments",
            "INSERT INTO access_plan_assignments"
            " (assignment_id,user_id,plan_code,catalog_version,assignment_state,"
            " operation_id,actor,recorded_utc,effective_utc)"
            " VALUES ('p1','u1','free','v','active','op','actor',:ts,:ts)",
        ),
        (
            "access_program_assignments",
            "INSERT INTO access_program_assignments"
            " (assignment_id,user_id,program_code,catalog_version,"
            " assignment_state,operation_id,actor,recorded_utc,effective_utc)"
            " VALUES ('r1','u1','beta_tester','v','active','op','actor',:ts,:ts)",
        ),
        (
            "access_feature_grants",
            "INSERT INTO access_feature_grants"
            " (grant_id,user_id,feature_key,publication_lane,catalog_version,"
            " plan_assignment_id,grant_state,operation_id,actor,recorded_utc,"
            " effective_utc)"
            " VALUES ('f1','u1','timeline','public','v','basis','active',"
            " 'op','actor',:ts,:ts)",
        ),
        (
            "access_geography_grants",
            "INSERT INTO access_geography_grants"
            " (grant_id,user_id,area_id,catalog_version,plan_assignment_id,"
            " grant_state,operation_id,actor,recorded_utc,effective_utc)"
            " VALUES ('g1','u1','wy','v','basis','active','op','actor',:ts,:ts)",
        ),
    ],
)
def test_every_assignment_and_grant_rejects_missing_owner_ref(conn, table, sql):
    before = _count(conn, table)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, {"ts": TS})
    assert _count(conn, table) == before


def test_invalid_enums_and_non_exact_geography_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_plan_assignments"
            " (assignment_id,user_id,plan_code,catalog_version,"
            " assignment_state,owner_decision_ref,operation_id,actor,"
            " recorded_utc,effective_utc)"
            " VALUES ('p','u1','galaxy','v','active','owner:x','op:x',"
            " 'actor',?,?)",
            (TS, TS),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_geography_grants"
            " (grant_id,user_id,area_id,scope_kind,catalog_version,"
            " plan_assignment_id,grant_state,owner_decision_ref,operation_id,"
            " actor,recorded_utc,effective_utc)"
            " VALUES ('g','u1','wy','descendants','v','basis','active',"
            " 'owner:x','op:x','actor',?,?)",
            (TS, TS),
        )


def test_invalid_or_empty_time_window_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_feature_grants"
            " (grant_id,user_id,feature_key,publication_lane,catalog_version,"
            " plan_assignment_id,grant_state,owner_decision_ref,operation_id,"
            " actor,recorded_utc,effective_utc,expires_utc)"
            " VALUES ('f','u1','timeline','public','v','basis','active',"
            " 'owner:x','op:x','actor',?,?,?)",
            (TS, TS, TS),
        )


@pytest.mark.parametrize(
    "bad_timestamp",
    [
        "2026-07-24T08:00:00.000-04:00",
        "2026-07-24T12:00:00Z",
        "not-a-time",
        "2026-99-99T12:00:00.000+00:00",
    ],
)
def test_noncanonical_or_invalid_timestamp_rejected(conn, bad_timestamp):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_program_assignments"
            " (assignment_id,user_id,program_code,catalog_version,"
            " assignment_state,owner_decision_ref,operation_id,actor,"
            " recorded_utc,effective_utc)"
            " VALUES ('bad-time','u1','developer','v','active',"
            " 'owner:x','op:x','actor',?,?)",
            (TS, bad_timestamp),
        )


def test_feature_and_geography_require_exactly_one_assignment_basis(conn):
    for plan_id, program_id in ((None, None), ("basis", "missing-program")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO access_feature_grants"
                " (grant_id,user_id,feature_key,publication_lane,catalog_version,"
                " plan_assignment_id,program_assignment_id,grant_state,"
                " owner_decision_ref,operation_id,actor,recorded_utc,"
                " effective_utc)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"basis-{plan_id}-{program_id}",
                    "u1",
                    "timeline",
                    "public",
                    "v",
                    plan_id,
                    program_id,
                    "active",
                    "owner:x",
                    "op:x",
                    "actor",
                    TS,
                    TS,
                ),
            )


def test_audit_operation_and_recorded_time_are_required(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_plan_assignments"
            " (assignment_id,user_id,plan_code,catalog_version,assignment_state,"
            " owner_decision_ref,effective_utc)"
            " VALUES ('missing-audit','u1','free','v','active','owner:x',?)",
            (TS,),
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO access_plan_assignments"
            " (assignment_id,user_id,plan_code,catalog_version,assignment_state,"
            " owner_decision_ref,operation_id,actor,recorded_utc,effective_utc)"
            " VALUES ('blank-actor','u1','free','v','active','owner:x','op:x',"
            " ' ',?,?)",
            (TS, TS),
        )
