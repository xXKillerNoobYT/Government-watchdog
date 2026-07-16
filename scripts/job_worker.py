"""Job worker: CLI poller that leases, dispatches by lane, records outcome.

GOV-733 (implements GOV-719 plan CTRL-2026, rev c4d03918 §3.2). Workers run
ONLY from this CLI / a scheduler — never from a web request (RED-2). Dry-run is
the default (F2 / GOV-479 convention); ``--apply`` executes.

Lane seam (plan §3.2): handlers register in :data:`LANES`. The initial lane is
``noop_synthetic`` (a drill/self-test lane). GOV-717/718 register their MCP /
provider lanes here without touching ingress or the queue. A handler receives
``(job_row, apply)`` and returns an outcome dict::

    {"ok": bool, "error": str|None, "metrics": {queue_wait_s, cpu_s, cache_hit,
                                                 quality_outcome, reviewer_outcome}}

Usage::

    python scripts/job_worker.py --db /tmp/ctrl.db                 # dry-run (default)
    python scripts/job_worker.py --db /tmp/ctrl.db --apply --max 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import job_queue  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "Logs" / "control-plane"
DEFAULT_LEASE_OWNER = "job_worker"

# handler(job_row, apply) -> outcome dict. Registration seam for GOV-717/718.
Handler = Callable[[object, bool], dict]
LANES: dict[str, Handler] = {}


def register_lane(name: str, handler: Handler) -> None:
    LANES[name] = handler


def _noop_synthetic(job_row, apply: bool) -> dict:
    """Drill lane: always succeeds, reports zero-cost synthetic metrics."""
    return {
        "ok": True,
        "error": None,
        "metrics": {
            "cpu_s": 0.0,
            "cache_hit": 0,
            "quality_outcome": "synthetic_ok",
            "reviewer_outcome": "not_applicable",
        },
    }


register_lane("noop_synthetic", _noop_synthetic)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _queue_wait_s(job_row, now: datetime) -> float:
    try:
        enq = datetime.fromisoformat(job_row["enqueued_at"])
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, (now - enq).total_seconds())


def run_once(
    conn,
    *,
    apply: bool = False,
    lease_owner: str = DEFAULT_LEASE_OWNER,
    now: str | None = None,
) -> dict | None:
    """Process (at most) one job.

    Dry-run: report the next leasable job and the lane that WOULD handle it,
    mutating nothing. ``--apply``: reap expired leases, lease the next job,
    dispatch to its lane handler, then record success/failure (which may
    dead-letter + write an outbox row, all in one transaction).
    """
    now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    if apply:
        job_queue.reap_expired_leases(conn, now=job_queue._iso(now_dt))
        conn.commit()

    job = job_queue.poll_next(conn, now=job_queue._iso(now_dt))
    if job is None:
        return None

    lane = job["lane"]
    handler = LANES.get(lane)
    if not apply:
        return {
            "action": "would_lease",
            "job_id": job["job_id"],
            "lane": lane,
            "handler_registered": handler is not None,
            "state": job["state"],
        }

    if handler is None:
        # Unknown lane is an operational failure, not a crash.
        job_queue.lease_job(conn, job["job_id"], lease_owner, now=job_queue._iso(now_dt))
        state = job_queue.record_failure(
            conn, job["job_id"], error=f"no_handler_for_lane:{lane}",
            now=job_queue._iso(now_dt),
        )
        conn.commit()
        return {"action": "failed", "job_id": job["job_id"], "lane": lane,
                "result_state": state, "error": "no_handler"}

    queue_wait = _queue_wait_s(job, now_dt)
    job_queue.lease_job(conn, job["job_id"], lease_owner, now=job_queue._iso(now_dt))
    conn.commit()

    t0 = time.perf_counter()
    try:
        outcome = handler(job, apply)
    except Exception as exc:  # a handler crash is a retryable failure, not fatal
        outcome = {"ok": False, "error": f"handler_exception:{exc}", "metrics": {}}
    cpu_s = time.perf_counter() - t0

    metrics = dict(outcome.get("metrics") or {})
    metrics.setdefault("queue_wait_s", round(queue_wait, 3))
    metrics.setdefault("cpu_s", round(cpu_s, 6))

    if outcome.get("ok"):
        job_queue.record_success(conn, job["job_id"], metrics=metrics,
                                 now=job_queue._iso(now_dt))
        conn.commit()
        return {"action": "succeeded", "job_id": job["job_id"], "lane": lane}

    state = job_queue.record_failure(
        conn, job["job_id"], error=outcome.get("error") or "unknown",
        metrics=metrics, now=job_queue._iso(now_dt),
    )
    conn.commit()
    return {"action": "failed", "job_id": job["job_id"], "lane": lane,
            "result_state": state, "error": outcome.get("error")}


def run(conn, *, apply: bool = False, max_jobs: int = 1,
        lease_owner: str = DEFAULT_LEASE_OWNER) -> dict:
    """Process up to ``max_jobs`` jobs; returns a run summary."""
    results = []
    for _ in range(max_jobs):
        r = run_once(conn, apply=apply, lease_owner=lease_owner)
        if r is None:
            break
        results.append(r)
    return {"applied": apply, "processed": len(results), "results": results}


def _write_summary(log_dir: Path, summary: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    path = log_dir / f"worker-{day}.json"
    records = []
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            records = []
    records.append({"at": _utcnow(), **summary})
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Control-plane job worker (dry-run default)")
    p.add_argument("--db", required=True)
    p.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    p.add_argument("--max", type=int, default=1, dest="max_jobs")
    p.add_argument("--lease-owner", default=DEFAULT_LEASE_OWNER)
    p.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    args = p.parse_args(argv)

    conn = db.open_db(Path(args.db))
    try:
        summary = run(conn, apply=args.apply, max_jobs=args.max_jobs,
                      lease_owner=args.lease_owner)
    finally:
        conn.close()
    if args.apply:
        _write_summary(Path(args.log_dir), summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
