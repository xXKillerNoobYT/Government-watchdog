"""Failure→issue thresholds: runner failures auto-file Paperclip issues
(GOV-634, implements GOV-631 T5).

Plan of record: ``Docs/gov-631-automation-credit-efficiency-plan.md`` @ ``4b0c47c``
§3 T5 + §6 — "runner failures matching defined patterns file a Paperclip issue
automatically instead of waiting for an agent heartbeat"; CTO owns triage.

Defined failure patterns (the ONLY ones that file — everything else stays a
log line for the CTO's scheduled log review):

* ``lane1_run_failed`` — a ``crawl_runs`` row with status ``failed``/``partial``.
* ``runner_failures`` — a ``Logs/refresh-runner/*.json`` run record with
  ``ok: false`` or a non-empty ``failures`` list.
* ``credit_anomaly`` — a runner record flagged ``credit_anomaly`` (an
  ``ai_extraction_runs`` row appeared during a deterministic run), or a gateway
  row escalated to a costlier tier with NO logged ``low_confidence_items``
  on its floor run (escalation without reason, plan §2).

Control-plane pattern (mirrors ``governance/sync_goal_ledger.py``): **dry-run is
the default** — the scan prints exactly what WOULD be filed; ``--apply`` files.
Idempotent: every finding carries a stable dedupe key embedded in the issue
title (``[auto:T5 <key>]``); a key with an existing non-closed issue is skipped,
so re-running a scan never floods the board.

Boundary: issue bodies carry run ids, counts, paths, and error TYPES only —
never record payloads, raw text, or PII (run-log ``failures[].error`` strings
are truncated summaries of tool errors, not source content). Local control
plane only (``http://127.0.0.1:3100``).

Usage:
    python scripts/failure_issue_filer.py                       # dry-run scan
    python scripts/failure_issue_filer.py --apply               # file the findings
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "Logs" / "refresh-runner"
DEFAULT_BASE_URL = "http://127.0.0.1:3100"

COMPANY_ID = "bcac096e-4aff-4ce3-ad33-c4e0b693b36f"
BACKEND_PROJECT_ID = "0a1832c4-1556-49a1-bcc5-857f2ca72962"
CTO_AGENT_ID = "24fddc65-edca-462b-8647-61b596c8a46f"  # plan §6: CTO owns triage

TITLE_MARKER = "[auto:T5 {key}]"

# Transport: (method, url, json_body|None) -> parsed JSON. Injectable for tests.
Transport = Callable[[str, str, dict | None], Any]


def _http(method: str, url: str, body: dict | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- pattern scans -----------------------------------------------------------

def scan_lane1(conn: sqlite3.Connection, *, since_utc: str | None = None) -> list[dict]:
    findings = []
    where, params = ("started_utc >= ?", [since_utc]) if since_utc else ("1=1", [])
    for run_id, status, started, notes in conn.execute(
        f"SELECT id, status, started_utc, notes FROM crawl_runs "
        f"WHERE status IN ('failed', 'partial') AND {where}", params
    ):
        findings.append({
            "pattern": "lane1_run_failed",
            "dedupe_key": f"lane1-run-{run_id}",
            "title": f"Lane-1 ingest run {run_id} finished '{status}'",
            "body": (
                f"crawl_runs id {run_id} started {started} finished status "
                f"'{status}'. Run notes: {notes or '(none)'}. Downstream "
                "presentation stays blocked until repaired "
                "(AI_GATEWAY_PROCESSING_WORKFLOW failure rule)."
            ),
        })
    return findings


def scan_escalations(conn: sqlite3.Connection) -> list[dict]:
    """Escalated gateway runs whose floor run logged no low-confidence reason."""
    findings = []
    try:
        rows = conn.execute(
            "SELECT e.run_id, e.escalated_from_run_id, f.low_confidence_items "
            "FROM ai_extraction_runs e "
            "LEFT JOIN ai_extraction_runs f ON e.escalated_from_run_id = f.run_id "
            "WHERE e.escalated_from_run_id IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # pre-0019 schema: no escalation provenance to audit
    for run_id, floor_id, reasons in rows:
        try:
            has_reason = bool(json.loads(reasons or "[]"))
        except ValueError:
            has_reason = False
        if not has_reason:
            findings.append({
                "pattern": "credit_anomaly",
                "dedupe_key": f"escalation-no-reason-{run_id}",
                "title": f"Gateway run {run_id} escalated tier without logged reason",
                "body": (
                    f"ai_extraction_runs {run_id} names escalated_from_run_id "
                    f"{floor_id}, but that floor run has no non-empty "
                    "low_confidence_items record. GOV-631 §2 model-floor rule: "
                    "tier escalation requires a per-item low-confidence record."
                ),
            })
    return findings


def scan_runner_logs(log_dir: Path, *, since_utc: str | None = None) -> list[dict]:
    findings = []
    if not log_dir.is_dir():
        return findings
    for path in sorted(log_dir.glob("refresh-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            findings.append({
                "pattern": "runner_failures",
                "dedupe_key": f"unreadable-log-{path.name}",
                "title": f"Unreadable refresh-runner log {path.name}",
                "body": f"Run log {path} exists but is not valid JSON.",
            })
            continue
        if since_utc and (record.get("started_utc") or "") < since_utc:
            continue
        key = (record.get("started_utc") or path.stem).replace(":", "")
        if record.get("credit_anomaly"):
            findings.append({
                "pattern": "credit_anomaly",
                "dedupe_key": f"runner-credit-anomaly-{key}",
                "title": "Credit anomaly: AI run appeared during deterministic refresh",
                "body": (
                    f"Run log {path.name}: ai_run_delta="
                    f"{record.get('ai_run_delta')} on a deterministic lane-1 "
                    "run. Hash-gate/lane isolation must be triaged before the "
                    "next scheduled run (GOV-631 §6)."
                ),
            })
        elif not record.get("ok", False):
            n = len(record.get("failures") or [])
            findings.append({
                "pattern": "runner_failures",
                "dedupe_key": f"runner-failed-{key}",
                "title": f"Refresh runner {record.get('mode', '?')} run reported {n} failure(s)",
                "body": (
                    f"Run log {path.name} (started {record.get('started_utc')}) "
                    f"finished ok=false with {n} failure(s). See the local log "
                    "for per-file errors (log stays vault-only)."
                ),
            })
    return findings


def scan(db_path: Path, log_dir: Path, *, since_utc: str | None = None) -> list[dict]:
    findings: list[dict] = []
    if Path(db_path).exists():
        conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        try:
            findings += scan_lane1(conn, since_utc=since_utc)
            findings += scan_escalations(conn)
        finally:
            conn.close()
    findings += scan_runner_logs(log_dir, since_utc=since_utc)
    return findings


# --- filing (dedupe + dry-run default) ---------------------------------------

def open_dedupe_keys(base_url: str, transport: Transport = _http) -> set[str]:
    """Dedupe keys already on the board in a non-closed issue."""
    issues = transport("GET", f"{base_url}/api/companies/{COMPANY_ID}/issues", None)
    keys: set[str] = set()
    for issue in issues or []:
        if issue.get("status") in ("done", "cancelled"):
            continue
        title = issue.get("title") or ""
        if "[auto:T5 " in title:
            keys.add(title.split("[auto:T5 ", 1)[1].split("]", 1)[0])
    return keys


def file_issues(findings: list[dict], *, apply: bool = False,
                base_url: str = DEFAULT_BASE_URL,
                transport: Transport = _http) -> dict:
    """Dedupe then file (or, by default, just report) the findings."""
    existing = open_dedupe_keys(base_url, transport) if findings else set()
    to_file = [f for f in findings if f["dedupe_key"] not in existing]
    skipped = [f for f in findings if f["dedupe_key"] in existing]
    created = []
    if apply:
        for f in to_file:
            payload = {
                "companyId": COMPANY_ID,
                "projectId": BACKEND_PROJECT_ID,
                "assigneeAgentId": CTO_AGENT_ID,
                "title": f"{f['title']} {TITLE_MARKER.format(key=f['dedupe_key'])}",
                "description": (
                    f"**Auto-filed by failure_issue_filer.py (GOV-631 T5); "
                    f"pattern `{f['pattern']}`; CTO owns triage (plan §6).**\n\n"
                    f"{f['body']}\n\n"
                    "Evidence: local run logs/ledgers named above (vault-only). "
                    "Close by repairing the run and re-running the scan clean."
                ),
                "priority": "high",
                "status": "todo",
            }
            created.append(transport("POST", f"{base_url}/api/issues", payload))
    return {
        "findings": len(findings),
        "would_file" if not apply else "filed": [f["dedupe_key"] for f in to_file],
        "skipped_existing": [f["dedupe_key"] for f in skipped],
        "created": created,
        "apply": apply,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan run ledgers/logs for defined failure patterns and "
        "file Paperclip issues (dry-run default; --apply to file)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--since", metavar="ISO_UTC", default=None,
                        help="only scan runs/logs started at/after this instant")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--apply", action="store_true",
                        help="actually file issues (default: report only)")
    args = parser.parse_args(argv)

    findings = scan(args.db, args.log_dir, since_utc=args.since)
    if not findings:
        print("clean: no defined failure pattern matched")
        return 0
    result = file_issues(findings, apply=args.apply, base_url=args.base_url)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
