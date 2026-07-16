"""Paperclip outbox: same-transaction writer + bounded relay CLI + LED-6 report.

GOV-733 (implements GOV-719 plan CTRL-2026, rev c4d03918 §3.2). Leaf module:
stdlib + ``db`` only (does NOT import ``job_queue`` — ``job_queue`` imports the
writer here, so the dependency runs one way and there is no cycle).

Boundary (RED per §11 GOV-719 + INV-7): Paperclip ever receives only
whitelist-serialized safe summaries — counts, ids, hashes, states, lanes,
areas. Raw payloads / source text / PII / reviewer notes are *structurally
absent* because :func:`safe_summary` builds the dict from an allow-set of field
names rather than filtering a denylist (fails closed).

Idempotency: ``paperclip_outbox.dedupe_key`` is UNIQUE, so both the writer
(``INSERT OR IGNORE``) and the relay (skip already-``delivered``) are safe to
re-run (AC-6). Flood control mirrors ``failure_issue_filer.py``: rows sharing an
``umbrella_key`` collapse into one Paperclip issue (subsequent rows become
comments).

Usage::

    python scripts/paperclip_outbox.py report --db /tmp/ctrl.db      # read-only metrics
    python scripts/paperclip_outbox.py relay  --db /tmp/ctrl.db      # dry-run (default)
    python scripts/paperclip_outbox.py relay  --db /tmp/ctrl.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:3100"

# The ONLY field names allowed into a safe_summary. Every value must be a scalar
# (str/int/float/bool/None) — counts, ids, hashes, states, lanes, areas, days.
# Nothing here can carry payload text, source text, PII, or reviewer notes.
SAFE_SUMMARY_ALLOWLIST = frozenset({
    "kind", "lane", "area_id", "day",
    "job_id", "envelope_id", "outbox_id",
    "dedupe_key", "umbrella_key", "source_hash", "payload_sha256",
    "policy_version", "lens_version",
    "state", "from_state", "to_state",
    "attempt_count", "max_attempts", "retry_count",
    "count", "dead_letter_count", "dedupe_hit_count", "envelope_count",
    "queue_wait_s", "cpu_s", "cache_hit",
    "quality_outcome", "reviewer_outcome",
})

_SCALAR = (str, int, float, bool, type(None))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_summary(data: dict) -> dict:
    """Whitelist serializer. Returns only allow-listed keys with scalar values.

    Keys not in :data:`SAFE_SUMMARY_ALLOWLIST` are dropped outright; an
    allow-listed key whose value is non-scalar (dict/list) is also dropped
    (a nested object could smuggle text). The result is safe to hand to
    Paperclip.
    """
    out = {}
    for key, value in data.items():
        if key in SAFE_SUMMARY_ALLOWLIST and isinstance(value, _SCALAR):
            out[key] = value
    return out


def dead_letter_dedupe_key(lane: str, area_id: str | None, day: str) -> str:
    """Stable per-lane/area/day key (plan §3.1 example shape)."""
    return f"outbox:dead-letter:{lane}:{area_id or 'shared'}:{day}"


def dead_letter_umbrella_key(day: str) -> str:
    """One Paperclip issue per day of dead letters."""
    return f"umbrella:dead-letter:{day}"


def write_outbox_row(
    conn: sqlite3.Connection,
    *,
    kind: str,
    dedupe_key: str,
    umbrella_key: str,
    summary: dict,
    created_at: str | None = None,
) -> int | None:
    """Idempotent same-transaction outbox write.

    Serializes ``summary`` through :func:`safe_summary` (so nothing unsafe can
    be persisted) and ``INSERT OR IGNORE``s on the UNIQUE ``dedupe_key``. Caller
    owns the transaction — this is invoked from ``job_queue`` inside the same
    transaction as the dead-letter transition (AC-4/AC-6). Returns the new
    ``outbox_id``, or ``None`` if the row already existed.
    """
    created_at = created_at or _utcnow()
    payload = json.dumps(safe_summary(summary), sort_keys=True, ensure_ascii=False)
    cur = conn.execute(
        "INSERT OR IGNORE INTO paperclip_outbox ("
        "created_at, kind, dedupe_key, umbrella_key, safe_summary, state, attempts"
        ") VALUES (?, ?, ?, ?, ?, 'pending', 0)",
        (created_at, kind, dedupe_key, umbrella_key, payload),
    )
    return int(cur.lastrowid) if cur.rowcount else None


# --- read-only report (LED-6 surface consumed by GOV-720) --------------------

def report(conn: sqlite3.Connection) -> dict:
    """Per-area operational metrics from the control-plane tables (read-only)."""
    areas: dict[str, dict] = {}

    def bucket(area_id) -> dict:
        key = area_id or "shared"
        return areas.setdefault(key, {
            "area_id": key,
            "job_count": 0,
            "dead_letter_count": 0,
            "retry_count": 0,
            "queue_wait_s_avg": None,
            "_wait_total": 0.0,
            "_wait_n": 0,
        })

    for row in conn.execute(
        "SELECT area_id, state, retry_count, queue_wait_s FROM event_jobs"
    ):
        b = bucket(row["area_id"])
        b["job_count"] += 1
        b["retry_count"] += int(row["retry_count"] or 0)
        if row["state"] == "dead_letter":
            b["dead_letter_count"] += 1
        if row["queue_wait_s"] is not None:
            b["_wait_total"] += float(row["queue_wait_s"])
            b["_wait_n"] += 1

    for b in areas.values():
        if b["_wait_n"]:
            b["queue_wait_s_avg"] = round(b["_wait_total"] / b["_wait_n"], 3)
        del b["_wait_total"]
        del b["_wait_n"]

    envelope_count = conn.execute(
        "SELECT COUNT(*) FROM event_envelopes"
    ).fetchone()[0]
    dedupe_hit_count = conn.execute(
        "SELECT COUNT(*) FROM event_dedupe_hits"
    ).fetchone()[0]
    denom = envelope_count + dedupe_hit_count
    dedupe_hit_rate = round(dedupe_hit_count / denom, 4) if denom else 0.0

    return {
        "generated_at": _utcnow(),
        "envelope_count": envelope_count,
        "dedupe_hit_count": dedupe_hit_count,
        "dedupe_hit_rate": dedupe_hit_rate,
        "pending_outbox": conn.execute(
            "SELECT COUNT(*) FROM paperclip_outbox WHERE state = 'pending'"
        ).fetchone()[0],
        "areas": sorted(areas.values(), key=lambda a: a["area_id"]),
    }


# --- relay CLI ---------------------------------------------------------------

Transport = Callable[[str, str, dict | None], Any]


def _http(method: str, url: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pending_by_umbrella(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        "SELECT * FROM paperclip_outbox WHERE state = 'pending' "
        "ORDER BY umbrella_key, outbox_id"
    ):
        groups.setdefault(row["umbrella_key"], []).append(row)
    return groups


def relay(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
    transport: Transport | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> dict:
    """Relay pending outbox rows to Paperclip, grouped by umbrella_key.

    Dry-run (default) reports what WOULD be sent and mutates nothing. With
    ``apply=True`` each umbrella group posts once (first row → issue, rest →
    comments), rows are marked ``delivered``; re-running is idempotent because
    delivered rows are no longer pending. ``transport`` is injectable for tests.
    """
    groups = pending_by_umbrella(conn)
    plan = [
        {"umbrella_key": key, "row_count": len(rows),
         "dedupe_keys": [r["dedupe_key"] for r in rows]}
        for key, rows in groups.items()
    ]
    if not apply:
        return {"applied": False, "umbrella_groups": plan}

    send = transport or _http
    delivered = 0
    for key, rows in groups.items():
        summaries = [json.loads(r["safe_summary"]) for r in rows]
        # One call per umbrella — the relay's flood bound. Transport is expected
        # to file/comment; here we only record delivery + a reference.
        ref = send("POST", f"{base_url}/relay/{key}", {"summaries": summaries})
        paperclip_ref = None
        if isinstance(ref, dict):
            paperclip_ref = ref.get("ref") or ref.get("id")
        now = _utcnow()
        for r in rows:
            conn.execute(
                "UPDATE paperclip_outbox SET state='delivered', delivered_at=?, "
                "attempts=attempts+1, paperclip_ref=? WHERE outbox_id=?",
                (now, str(paperclip_ref) if paperclip_ref is not None else None,
                 r["outbox_id"]),
            )
            delivered += 1
    conn.commit()
    return {"applied": True, "delivered": delivered, "umbrella_groups": plan}


def _build_parser() -> argparse.ArgumentParser:
    # --db is shared by every subcommand (usage: `report --db X`, `relay --db X`),
    # so it lives on a parent parser and is accepted after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    p = argparse.ArgumentParser(description="Paperclip outbox report/relay")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report", parents=[common],
                   help="read-only per-area metrics (LED-6)")
    r = sub.add_parser("relay", parents=[common],
                       help="relay pending rows (dry-run default)")
    r.add_argument("--apply", action="store_true", help="actually deliver")
    r.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    conn = db.open_db(Path(args.db))
    try:
        if args.cmd == "report":
            print(json.dumps(report(conn), indent=2))
        elif args.cmd == "relay":
            print(json.dumps(
                relay(conn, apply=args.apply, base_url=args.base_url), indent=2
            ))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
