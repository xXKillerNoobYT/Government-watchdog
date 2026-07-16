"""Bounded micro-job state machine.

GOV-733 (implements GOV-719 plan CTRL-2026, rev c4d03918 §3.2). Leaf module:
stdlib + ``db`` + ``paperclip_outbox`` (for the same-transaction dead-letter
row). Does NOT import crawler/AI/provider modules.

State machine (plan §3.2)::

    queued ──lease──▶ leased ──▶ succeeded            (terminal)
       ▲                 │  ├──▶ failed_retryable ──▶ (re-lease after backoff)
       │                 │  └──▶ dead_letter          (terminal, + outbox row)
       └───reap──────────┘
    * ──cancel──▶ cancelled                           (terminal)

Every state change goes through the single guarded :func:`transition`, which
consults :data:`ALLOWED_TRANSITIONS` and refuses (raising
:class:`IllegalTransition`, logged, never silent) anything not permitted — so
terminal states are structurally terminal (AC-3). Each applied transition
writes exactly one ``job_transitions`` row.

Attempt accounting: ``attempt_count`` is incremented when an *outcome* is
recorded (a failure or a lease-reap), not at lease time. A job that fails is
retried with exponential backoff until ``attempt_count == max_attempts``, at
which point it dead-letters and — in the *same transaction* — writes one
umbrella-grouped outbox row (AC-4). Because job state and the outbox row share
one SQLite transaction, a rollback leaves neither (AC-6).
"""

from __future__ import annotations

import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone

import paperclip_outbox

log = logging.getLogger("control_plane.job_queue")

# --- states ------------------------------------------------------------------
QUEUED = "queued"
LEASED = "leased"
SUCCEEDED = "succeeded"
FAILED_RETRYABLE = "failed_retryable"
DEAD_LETTER = "dead_letter"
CANCELLED = "cancelled"

TERMINAL = frozenset({SUCCEEDED, DEAD_LETTER, CANCELLED})

# A job is re-leasable from either of these once its backoff (not_before) elapses.
LEASABLE = frozenset({QUEUED, FAILED_RETRYABLE})

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({LEASED, CANCELLED}),
    LEASED: frozenset({SUCCEEDED, FAILED_RETRYABLE, DEAD_LETTER, QUEUED, CANCELLED}),
    FAILED_RETRYABLE: frozenset({LEASED, DEAD_LETTER, CANCELLED}),
    SUCCEEDED: frozenset(),
    DEAD_LETTER: frozenset(),
    CANCELLED: frozenset(),
}

# Backoff schedule (plan §3.2): base 30 s, ×2 per attempt, capped at 1 h.
BACKOFF_BASE_S = 30
BACKOFF_CAP_S = 3600


class IllegalTransition(Exception):
    """Raised when a state change is not in ALLOWED_TRANSITIONS."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def backoff_seconds(job_id: int, attempt: int) -> float:
    """Exponential backoff with deterministic per-job jitter.

    ``attempt`` is 1-based (the attempt that just failed). Base 30 s doubles
    each attempt, capped at 1 h. Jitter in ``[0, base)`` is drawn from a PRNG
    seeded by ``(job_id, attempt)`` so the value is reproducible in tests yet
    differs across jobs (anti-thundering-herd).
    """
    raw = BACKOFF_BASE_S * (2 ** max(0, attempt - 1))
    rnd = random.Random(f"{job_id}:{attempt}")
    jitter = rnd.uniform(0, BACKOFF_BASE_S)
    return float(min(raw + jitter, BACKOFF_CAP_S))


# --- core --------------------------------------------------------------------

def enqueue_job(
    conn: sqlite3.Connection,
    *,
    envelope_id: int,
    lane: str,
    area_id: str | None = None,
    policy_version: str | None = None,
    lens_version: str | None = None,
    max_attempts: int = 5,
    enqueued_at: str | None = None,
) -> int:
    """Insert a fresh ``queued`` job. Caller owns the transaction."""
    now = enqueued_at or _iso(_utcnow())
    cur = conn.execute(
        "INSERT INTO event_jobs ("
        "envelope_id, lane, area_id, state, attempt_count, max_attempts, "
        "policy_version, lens_version, enqueued_at, retry_count"
        ") VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?, 0)",
        (envelope_id, lane, area_id, max_attempts, policy_version, lens_version, now),
    )
    return int(cur.lastrowid)


def _current_state(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(
        "SELECT state FROM event_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise IllegalTransition(f"job {job_id} does not exist")
    return row[0]


def transition(
    conn: sqlite3.Connection,
    job_id: int,
    to_state: str,
    *,
    reason: str | None = None,
    actor: str = "system",
    at: str | None = None,
    columns: dict | None = None,
) -> None:
    """The single guarded state change.

    Refuses (``IllegalTransition``, logged) any ``from -> to`` not in
    :data:`ALLOWED_TRANSITIONS`. On success updates ``event_jobs.state`` (plus
    any extra ``columns``) and appends one ``job_transitions`` audit row. Caller
    owns the transaction.
    """
    from_state = _current_state(conn, job_id)
    if to_state not in ALLOWED_TRANSITIONS.get(from_state, frozenset()):
        log.warning(
            "illegal transition refused: job=%s %s -> %s (reason=%s)",
            job_id, from_state, to_state, reason,
        )
        raise IllegalTransition(
            f"job {job_id}: {from_state} -> {to_state} not allowed"
        )
    now = at or _iso(_utcnow())
    sets = ["state = ?"]
    params: list = [to_state]
    for col, val in (columns or {}).items():
        sets.append(f"{col} = ?")
        params.append(val)
    params.append(job_id)
    conn.execute(
        f"UPDATE event_jobs SET {', '.join(sets)} WHERE job_id = ?", params
    )
    conn.execute(
        "INSERT INTO job_transitions (job_id, from_state, to_state, reason, actor, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, from_state, to_state, reason, actor, now),
    )


# --- lifecycle helpers -------------------------------------------------------

def poll_next(conn: sqlite3.Connection, *, now: str | None = None) -> sqlite3.Row | None:
    """Oldest leasable job whose backoff has elapsed, or None."""
    now = now or _iso(_utcnow())
    return conn.execute(
        "SELECT * FROM event_jobs "
        "WHERE state IN ('queued', 'failed_retryable') "
        "AND (not_before IS NULL OR not_before <= ?) "
        "ORDER BY enqueued_at, job_id LIMIT 1",
        (now,),
    ).fetchone()


def lease_job(
    conn: sqlite3.Connection,
    job_id: int,
    lease_owner: str,
    *,
    lease_seconds: int = 300,
    now: str | None = None,
) -> None:
    """Take a lease: (queued|failed_retryable) -> leased. Caller owns the tx."""
    now_dt = _parse(now) if now else _utcnow()
    expires = _iso(now_dt + timedelta(seconds=lease_seconds))
    transition(
        conn, job_id, LEASED,
        reason="lease", actor=lease_owner, at=_iso(now_dt),
        columns={
            "lease_owner": lease_owner,
            "lease_expires_at": expires,
            "started_at": _iso(now_dt),
        },
    )


def record_success(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    metrics: dict | None = None,
    now: str | None = None,
) -> None:
    """leased -> succeeded, stamping LED-1 outcome metrics. Caller owns the tx."""
    now = now or _iso(_utcnow())
    cols = {"finished_at": now, "lease_owner": None, "lease_expires_at": None}
    cols.update(_metric_columns(metrics))
    transition(conn, job_id, SUCCEEDED, reason="success", at=now, columns=cols)


def record_failure(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str,
    metrics: dict | None = None,
    now: str | None = None,
) -> str:
    """Record a failed attempt.

    Increments ``attempt_count``/``retry_count``. If the job has now used all
    ``max_attempts`` it dead-letters and writes one umbrella-grouped outbox row
    *in the same transaction* (AC-4/AC-6); otherwise it goes ``failed_retryable``
    with ``not_before`` set to the backoff deadline. Returns the resulting state.
    Caller owns the transaction.
    """
    now_dt = _parse(now) if now else _utcnow()
    now_iso = _iso(now_dt)
    row = conn.execute(
        "SELECT attempt_count, max_attempts, retry_count, lane, area_id "
        "FROM event_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    attempt = int(row["attempt_count"]) + 1
    retry_count = int(row["retry_count"]) + 1
    lane, area_id = row["lane"], row["area_id"]
    base_cols = {
        "attempt_count": attempt,
        "retry_count": retry_count,
        "last_error": error,
        "lease_owner": None,
        "lease_expires_at": None,
    }
    base_cols.update(_metric_columns(metrics))

    if attempt >= int(row["max_attempts"]):
        base_cols["finished_at"] = now_iso
        transition(
            conn, job_id, DEAD_LETTER,
            reason=f"max_attempts_exhausted:{error}", at=now_iso, columns=base_cols,
        )
        # SAME TRANSACTION: bounded, safe dead-letter notice for Paperclip.
        day = now_dt.date().isoformat()
        paperclip_outbox.write_outbox_row(
            conn,
            kind="dead_letter",
            dedupe_key=paperclip_outbox.dead_letter_dedupe_key(lane, area_id, day),
            umbrella_key=paperclip_outbox.dead_letter_umbrella_key(day),
            summary={
                "kind": "dead_letter",
                "lane": lane,
                "area_id": area_id,
                "job_id": job_id,
                "attempt_count": attempt,
                "max_attempts": int(row["max_attempts"]),
                "day": day,
            },
            created_at=now_iso,
        )
        return DEAD_LETTER

    not_before = _iso(now_dt + timedelta(seconds=backoff_seconds(job_id, attempt)))
    base_cols["not_before"] = not_before
    transition(
        conn, job_id, FAILED_RETRYABLE,
        reason=f"retry:{error}", at=now_iso, columns=base_cols,
    )
    return FAILED_RETRYABLE


def reap_expired_leases(conn: sqlite3.Connection, *, now: str | None = None) -> int:
    """Return abandoned (expired-lease) jobs to ``queued`` with a spent attempt.

    A worker that died mid-lease leaves a ``leased`` row whose
    ``lease_expires_at`` has passed. Reaping counts that as one used attempt
    (plan §3.2) and requeues (or dead-letters if it was the last attempt).
    Caller owns the transaction. Returns the number of jobs reaped.
    """
    now_dt = _parse(now) if now else _utcnow()
    now_iso = _iso(now_dt)
    rows = conn.execute(
        "SELECT job_id, attempt_count, max_attempts, lane, area_id "
        "FROM event_jobs WHERE state = 'leased' AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at <= ?",
        (now_iso,),
    ).fetchall()
    for row in rows:
        attempt = int(row["attempt_count"]) + 1
        cols = {
            "attempt_count": attempt,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_error": "lease_expired",
        }
        if attempt >= int(row["max_attempts"]):
            cols["finished_at"] = now_iso
            transition(
                conn, row["job_id"], DEAD_LETTER,
                reason="lease_expired_max_attempts", at=now_iso, columns=cols,
            )
            day = now_dt.date().isoformat()
            paperclip_outbox.write_outbox_row(
                conn,
                kind="dead_letter",
                dedupe_key=paperclip_outbox.dead_letter_dedupe_key(
                    row["lane"], row["area_id"], day
                ),
                umbrella_key=paperclip_outbox.dead_letter_umbrella_key(day),
                summary={
                    "kind": "dead_letter",
                    "lane": row["lane"],
                    "area_id": row["area_id"],
                    "job_id": row["job_id"],
                    "attempt_count": attempt,
                    "day": day,
                },
                created_at=now_iso,
            )
        else:
            cols["not_before"] = now_iso
            transition(
                conn, row["job_id"], QUEUED,
                reason="lease_expired_reap", at=now_iso, columns=cols,
            )
    return len(rows)


def cancel_job(
    conn: sqlite3.Connection, job_id: int, *, reason: str = "cancelled",
    now: str | None = None,
) -> None:
    """Move a non-terminal job to the terminal ``cancelled`` state."""
    now = now or _iso(_utcnow())
    transition(conn, job_id, CANCELLED, reason=reason, at=now,
               columns={"finished_at": now, "lease_owner": None,
                        "lease_expires_at": None})


# LED-1 outcome metric columns a handler may report.
_METRIC_COLS = frozenset(
    {"queue_wait_s", "cpu_s", "cache_hit", "quality_outcome", "reviewer_outcome"}
)


def _metric_columns(metrics: dict | None) -> dict:
    if not metrics:
        return {}
    return {k: v for k, v in metrics.items() if k in _METRIC_COLS}
