"""GOV-733 CTRL-2026 — job state machine (AC-3, AC-4, AC-5).

Transition-table guard, dead-letter + same-transaction outbox row, and the
envelope→job traceability join.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import event_envelope as ee  # noqa: E402
import job_queue as jq  # noqa: E402


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


def _job(conn, *, lane="noop_synthetic", max_attempts=5, area_id="AREA-1"):
    r = ee.insert_envelope(
        conn, source_key="s", event_kind="k", source_ref="ref",
        content_sha256="a" * 64, policy_version="p", payload={"x": 1},
        area_id=area_id,
    )
    jid = jq.enqueue_job(conn, envelope_id=r.envelope_id, lane=lane,
                         area_id=area_id, policy_version="p",
                         max_attempts=max_attempts)
    conn.commit()
    return jid


# --- AC-3: bounded state machine --------------------------------------------

def test_illegal_transition_refused(conn):
    jid = _job(conn)
    with pytest.raises(jq.IllegalTransition):
        jq.transition(conn, jid, jq.SUCCEEDED)  # queued -> succeeded not allowed


def test_terminal_states_are_terminal(conn):
    for term in (jq.SUCCEEDED, jq.DEAD_LETTER, jq.CANCELLED):
        assert jq.ALLOWED_TRANSITIONS[term] == frozenset()


def test_terminal_job_rejects_further_transition(conn):
    jid = _job(conn)
    jq.cancel_job(conn, jid)
    conn.commit()
    with pytest.raises(jq.IllegalTransition):
        jq.lease_job(conn, jid, "w")


def test_every_applied_transition_is_audited(conn):
    jid = _job(conn)
    jq.lease_job(conn, jid, "w", now="2026-07-15T00:00:00.000+00:00")
    jq.record_success(conn, jid, now="2026-07-15T00:00:01.000+00:00")
    conn.commit()
    rows = conn.execute(
        "SELECT from_state, to_state FROM job_transitions WHERE job_id=? ORDER BY transition_id",
        (jid,),
    ).fetchall()
    assert [(r["from_state"], r["to_state"]) for r in rows] == [
        ("queued", "leased"), ("leased", "succeeded"),
    ]


# --- AC-4: retry/backoff then dead-letter + same-tx outbox ------------------

def test_retries_exactly_max_attempts_then_dead_letter(conn):
    jid = _job(conn, max_attempts=5)
    now = "2026-07-15T00:00:00.000+00:00"
    states = []
    for i in range(5):
        jq.lease_job(conn, jid, "w", now=now)
        state = jq.record_failure(conn, jid, error="boom", now=now)
        states.append(state)
        conn.commit()
    assert states == [
        jq.FAILED_RETRYABLE, jq.FAILED_RETRYABLE, jq.FAILED_RETRYABLE,
        jq.FAILED_RETRYABLE, jq.DEAD_LETTER,
    ]
    row = conn.execute("SELECT state, attempt_count FROM event_jobs WHERE job_id=?",
                       (jid,)).fetchone()
    assert row["state"] == "dead_letter"
    assert row["attempt_count"] == 5


def test_dead_letter_writes_one_umbrella_outbox_row(conn):
    jid = _job(conn, max_attempts=2)
    now = "2026-07-15T00:00:00.000+00:00"
    for _ in range(2):
        jq.lease_job(conn, jid, "w", now=now)
        jq.record_failure(conn, jid, error="boom", now=now)
        conn.commit()
    rows = conn.execute("SELECT kind, umbrella_key, safe_summary FROM paperclip_outbox").fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "dead_letter"
    assert rows[0]["umbrella_key"].startswith("umbrella:dead-letter:")


def test_backoff_is_deterministic_and_capped():
    a = jq.backoff_seconds(42, 3)
    b = jq.backoff_seconds(42, 3)
    assert a == b  # deterministic per (job_id, attempt)
    assert jq.backoff_seconds(42, 3) != jq.backoff_seconds(99, 3)  # per-job jitter
    assert jq.backoff_seconds(1, 100) == jq.BACKOFF_CAP_S  # capped at 1 h


def test_failed_retryable_not_pollable_until_backoff_elapses(conn):
    jid = _job(conn, max_attempts=5)
    now = "2026-07-15T00:00:00.000+00:00"
    jq.lease_job(conn, jid, "w", now=now)
    jq.record_failure(conn, jid, error="boom", now=now)
    conn.commit()
    # Immediately after failure, backoff blocks re-lease...
    assert jq.poll_next(conn, now=now) is None
    # ...but well past the cap it is leasable again.
    assert jq.poll_next(conn, now="2026-07-15T02:00:00.000+00:00") is not None


# --- lease reaping -----------------------------------------------------------

def test_expired_lease_reaped_to_queued_with_attempt(conn):
    jid = _job(conn, max_attempts=5)
    jq.lease_job(conn, jid, "w", lease_seconds=300, now="2026-07-15T00:00:00.000+00:00")
    conn.commit()
    reaped = jq.reap_expired_leases(conn, now="2026-07-15T01:00:00.000+00:00")
    conn.commit()
    assert reaped == 1
    row = conn.execute("SELECT state, attempt_count FROM event_jobs WHERE job_id=?",
                       (jid,)).fetchone()
    assert row["state"] == "queued"
    assert row["attempt_count"] == 1


# --- AC-5: traceability ------------------------------------------------------

def test_job_joins_envelope_for_traceability(conn):
    jid = _job(conn, area_id="AREA-1")
    now = "2026-07-15T00:00:00.000+00:00"
    jq.lease_job(conn, jid, "w", now=now)
    jq.record_success(conn, jid, metrics={
        "cpu_s": 0.01, "cache_hit": 0, "queue_wait_s": 1.5,
        "quality_outcome": "ok", "reviewer_outcome": "n/a",
    }, now=now)
    conn.commit()
    row = conn.execute(
        "SELECT j.job_id, j.area_id, j.policy_version, j.cpu_s, j.queue_wait_s, "
        "j.quality_outcome, j.reviewer_outcome, e.source_hash, e.policy_version AS ep "
        "FROM event_jobs j JOIN event_envelopes e ON j.envelope_id = e.envelope_id "
        "WHERE j.job_id = ?", (jid,),
    ).fetchone()
    assert row["source_hash"] == "a" * 64
    assert row["policy_version"] == "p" == row["ep"]
    assert row["area_id"] == "AREA-1"
    assert row["cpu_s"] == 0.01
    assert row["queue_wait_s"] == 1.5
    assert row["quality_outcome"] == "ok"
    assert row["reviewer_outcome"] == "n/a"
