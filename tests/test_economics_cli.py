"""CLI smoke: report/rollup/export/verify-hash/eligibility/capacity + transition guard.

``transition`` is the ONLY state writer and refuses without an owner ref; the
read-only commands never mutate area_state.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import area_economics  # noqa: E402
from conftest import ECON_PERIOD, seed_economics  # noqa: E402
import db  # noqa: E402


@pytest.fixture()
def econ_db(tmp_path):
    p = tmp_path / "cli.db"
    db.apply_migrations(p)
    conn = db.open_db(p)
    seed_economics(conn)
    conn.close()
    return p


def test_report_then_verify_hash(econ_db, capsys):
    rc = area_economics.main(["report", "--db", str(econ_db), "--area", "alpine",
                              "--period", ECON_PERIOD])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    report_id = out["report_id"]
    rc = area_economics.main(["verify-hash", "--db", str(econ_db), "--report", report_id])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["match"] is True


def test_export_csv_has_no_price_tokens(econ_db, capsys):
    area_economics.main(["report", "--db", str(econ_db), "--area", "alpine",
                         "--period", ECON_PERIOD])
    report_id = json.loads(capsys.readouterr().out)["report_id"]
    area_economics.main(["export", "--db", str(econ_db), "--report", report_id,
                         "--format", "csv"])
    csv_out = capsys.readouterr().out.lower()
    for token in ("price", "usd", "dollar"):
        assert token not in csv_out


def test_rollup_command(econ_db, capsys):
    rc = area_economics.main(["rollup", "--db", str(econ_db), "--scope", "county",
                              "--id", "lincoln", "--period", ECON_PERIOD])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pack"]["variable_cost"]["value"] == 330


def test_transition_writes_state(econ_db, capsys):
    rc = area_economics.main(["transition", "--db", str(econ_db), "--area", "alpine",
                              "--to", "funded", "--owner-decision-ref", "card:GOV-1"])
    assert rc == 0
    with sqlite3.connect(econ_db) as conn:
        state = conn.execute("SELECT state FROM area_state WHERE area_id='alpine'").fetchone()[0]
    assert state == "funded"


def test_transition_refused_without_owner_ref(econ_db, capsys):
    rc = area_economics.main(["transition", "--db", str(econ_db), "--area", "alpine",
                              "--to", "funded", "--owner-decision-ref", "  "])
    assert rc == 2  # refused, non-zero, clean exit (no traceback)
    with sqlite3.connect(econ_db) as conn:
        rows = conn.execute("SELECT COUNT(*) FROM area_state").fetchone()[0]
    assert rows == 0  # zero writes


def test_read_commands_do_not_mutate_state(econ_db, capsys):
    area_economics.main(["eligibility", "--db", str(econ_db), "--area", "alpine",
                         "--period", ECON_PERIOD])
    area_economics.main(["capacity-forecast", "--db", str(econ_db), "--area", "alpine"])
    capsys.readouterr()
    with sqlite3.connect(econ_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM area_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM area_transitions").fetchone()[0] == 0
