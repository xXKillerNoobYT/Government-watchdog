"""GOV-733 CTRL-2026 — job worker CLI: dry-run default, apply, lane dispatch.

Exercises the F2/GOV-479 dry-run convention, the noop_synthetic lane, unknown-
lane handling, and the end-to-end lease→dispatch→success path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import event_envelope as ee  # noqa: E402
import job_queue as jq  # noqa: E402
import job_worker as worker  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "ctrl.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    c.execute(
        "INSERT INTO webhook_sources (source_key, secret_ref, active, created_at) "
        "VALUES ('s', 'GW_SECRET_S', 1, '2026-07-15T00:00:00.000+00:00')"
    )
    c.commit()
    yield c
    c.close()


def _enqueue(conn, lane="noop_synthetic"):
    r = ee.insert_envelope(conn, source_key="s", event_kind="k", source_ref="ref",
                           content_sha256="a" * 64, policy_version="p",
                           payload={"x": 1}, area_id="AREA-1")
    jid = jq.enqueue_job(conn, envelope_id=r.envelope_id, lane=lane, area_id="AREA-1")
    conn.commit()
    return jid


def test_dry_run_reports_without_mutating(conn):
    jid = _enqueue(conn)
    result = worker.run_once(conn, apply=False)
    assert result["action"] == "would_lease"
    assert result["job_id"] == jid
    assert result["handler_registered"] is True
    # nothing changed: still queued, no transitions
    assert conn.execute("SELECT state FROM event_jobs WHERE job_id=?",
                        (jid,)).fetchone()["state"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM job_transitions").fetchone()[0] == 0


def test_apply_runs_noop_lane_to_success(conn):
    jid = _enqueue(conn)
    result = worker.run_once(conn, apply=True)
    assert result["action"] == "succeeded"
    row = conn.execute("SELECT state, quality_outcome FROM event_jobs WHERE job_id=?",
                       (jid,)).fetchone()
    assert row["state"] == "succeeded"
    assert row["quality_outcome"] == "synthetic_ok"


def test_apply_empty_queue_returns_none(conn):
    assert worker.run_once(conn, apply=True) is None


def test_unknown_lane_fails_gracefully(conn):
    _enqueue(conn, lane="lane_with_no_handler")
    result = worker.run_once(conn, apply=True)
    assert result["action"] == "failed"
    assert result["error"] == "no_handler"


def test_register_lane_seam(conn):
    seen = {}

    def handler(job_row, apply):
        seen["job_id"] = job_row["job_id"]
        return {"ok": True, "error": None, "metrics": {"quality_outcome": "custom"}}

    worker.register_lane("gov717_probe", handler)
    try:
        jid = _enqueue(conn, lane="gov717_probe")
        worker.run_once(conn, apply=True)
        assert seen["job_id"] == jid
        assert conn.execute("SELECT quality_outcome FROM event_jobs WHERE job_id=?",
                            (jid,)).fetchone()["quality_outcome"] == "custom"
    finally:
        worker.LANES.pop("gov717_probe", None)


def test_run_processes_multiple_jobs(conn):
    for _ in range(3):
        r = ee.insert_envelope(conn, source_key="s", event_kind="k",
                               source_ref=f"ref{_}", content_sha256=f"{_}" * 64,
                               policy_version="p", payload={"i": _}, area_id="AREA-1")
        jq.enqueue_job(conn, envelope_id=r.envelope_id, lane="noop_synthetic",
                       area_id="AREA-1")
    conn.commit()
    summary = worker.run(conn, apply=True, max_jobs=10)
    assert summary["processed"] == 3
    assert all(x["action"] == "succeeded" for x in summary["results"])
