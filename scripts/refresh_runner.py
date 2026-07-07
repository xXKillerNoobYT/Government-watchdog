"""Scheduled deterministic refresh runner (GOV-634, implements GOV-631 T1).

Plan of record: ``Docs/gov-631-automation-credit-efficiency-plan.md`` @ ``4b0c47c``
§3 T1 — a cron-wrappable runner around the existing lane-1 ingest (+ optional
deterministic structuring), following the established F2/GOV-479 pattern:
**dry-run is the default; mutation requires ``--apply``.**

Hard gates carried by this runner (plan §2):

* **Scope gate** — the runner is PILOT-SCOPE ONLY (``--only-date 2026-06-23``,
  the GOV-620/GOV-621 Option-C window). ``--scope full`` REFUSES (exit 2) and
  names the pending owner card; there is deliberately no bypass flag. The full
  Alpine run stays gated on Isaac's card ``confirmation:GOV-612:full-ingest:v1``
  (GOV-625) — unlocking it is an owner decision recorded there, then a reviewed
  one-line change here.
* **Zero-AI invariant** — this runner is lane-1 deterministic by construction.
  As defense-in-depth it counts ``ai_extraction_runs`` rows before/after and
  reports the delta; any non-zero delta marks the run log ``credit_anomaly``
  (a T5 failure pattern) and exits non-zero.
* **Metering** — every run log embeds the GOV-631 T4 metering block (AI calls,
  tokens, cost-per-document, skip ratio) read from the ledgers.

Logs: one JSON per run under ``Logs/refresh-runner/`` (gitignored — run logs
are local/vault-only per the data-publication boundary). CTO reviews these on
each pipeline merge and at least weekly while scheduled (plan §5).

Scheduling: ``--emit-cron`` prints the crontab line for the DRY-RUN schedule.
Installing it is a deliberate operator action (CTO), not something this script
does to the system.

Usage:
    python scripts/refresh_runner.py                    # pilot dry-run (default)
    python scripts/refresh_runner.py --apply            # pilot apply (after CTO log review)
    python scripts/refresh_runner.py --apply --structure
    python scripts/refresh_runner.py --emit-cron
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import credit_metering as cm  # noqa: E402
import db  # noqa: E402
import ingest_local_corpus as ingest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "Logs" / "refresh-runner"

# GOV-620/GOV-621 Option-C pilot window — the ONLY scope this runner may touch
# until the owner card below is accepted (GOV-625).
PILOT_ONLY_DATE = "2026-06-23"
FULL_INGEST_CARD = "confirmation:GOV-612:full-ingest:v1"
FULL_INGEST_GATE_ISSUE = "GOV-625"

# Suggested schedule: daily 06:15 local, DRY-RUN only (no --apply in cron; an
# apply run follows CTO review of the dry-run log, per plan §2 dry-run gate).
CRON_LINE_TEMPLATE = (
    "15 6 * * * cd {repo} && {python} scripts/refresh_runner.py "
    ">> Logs/refresh-runner/cron.log 2>&1"
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _gateway_row_count(db_path: Path) -> int | None:
    """Read-only count of gateway-run rows; None when the DB/table doesn't exist."""
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT COUNT(*) FROM ai_extraction_runs").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _meter(db_path: Path, since_utc: str) -> dict | None:
    if not Path(db_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
        try:
            return {
                "run_window": cm.meter(conn, since_utc=since_utc),
                "all_time": cm.meter(conn),
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def refresh(*, source_dir: Path, db_path: Path, apply: bool,
            structure: bool = False, log_dir: Path = DEFAULT_LOG_DIR) -> dict:
    """One pilot-scope refresh pass. Returns the run record (also written to disk)."""
    started = _now_utc()
    ai_rows_before = _gateway_row_count(db_path)

    ingest_summary = ingest.ingest(
        source_dir, db_path, dry_run=not apply, only_date=PILOT_ONLY_DATE
    )

    structure_summary = None
    if apply and structure:
        import structure_real_corpus as sr  # deferred: heavier import graph
        structure_summary = sr.structure(
            source_dir, db_path, skip_ingest=True, only_date=PILOT_ONLY_DATE
        )

    ai_rows_after = _gateway_row_count(db_path)
    ai_run_delta = (
        (ai_rows_after - ai_rows_before)
        if (ai_rows_before is not None and ai_rows_after is not None) else 0
    )

    record = {
        "runner": "refresh_runner",
        "scope": "pilot",
        "only_date": PILOT_ONLY_DATE,
        "deterministic": True,
        "mode": "apply" if apply else "dry-run",
        "started_utc": started,
        "finished_utc": _now_utc(),
        "ingest": ingest_summary,
        "structure": structure_summary,
        "ai_run_delta": ai_run_delta,
        "credit_anomaly": ai_run_delta != 0,
        "metering": _meter(db_path, started),
        "failures": ingest_summary.get("failures", []),
        "ok": not ingest_summary.get("failures") and ai_run_delta == 0,
    }

    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.replace(":", "").replace(".", "")
    log_path = log_dir / f"refresh-{stamp}-{record['mode']}.json"
    log_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    record["log_path"] = str(log_path)
    return record


def render(record: dict) -> str:
    ing = record["ingest"]
    lines = [
        f"# Refresh runner — {record['mode']} · scope=pilot({record['only_date']})",
        f"- selected: {ing['selected']} · new: {ing['new_documents']} · "
        f"skipped:hash: {ing.get('skipped_hash', 0)} · failures: {len(record['failures'])}",
        f"- ai_run_delta (must be 0 for a deterministic run): {record['ai_run_delta']}"
        + (" ⚠️ CREDIT ANOMALY" if record["credit_anomaly"] else ""),
        f"- run log: {record['log_path']}",
    ]
    if ing.get("planned") is not None:
        p = ing["planned"]
        lines.insert(2, f"- planned vs existing DB: {p['skipped_hash']} skipped:hash · "
                        f"{p['ingest_new']} new · {p['reprocess_changed']} changed")
    m = record.get("metering")
    if m:
        lines.append(cm.render_metering(m["run_window"]))
    else:
        lines.append("- metering: no DB yet (nothing to meter)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic pilot-scope refresh runner (GOV-631 T1; "
        "dry-run default, --apply to mutate)."
    )
    parser.add_argument("--scope", choices=["pilot", "full"], default="pilot",
                        help="'full' REFUSES: gated on owner card "
                        f"{FULL_INGEST_CARD} ({FULL_INGEST_GATE_ISSUE})")
    parser.add_argument("--source-dir", type=Path, default=ingest.DEFAULT_CORPUS)
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="write rows/copy bytes (default is dry-run)")
    parser.add_argument("--structure", action="store_true",
                        help="with --apply: also run deterministic structuring")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--emit-cron", action="store_true",
                        help="print the suggested crontab line (dry-run schedule) and exit")
    args = parser.parse_args(argv)

    if args.emit_cron:
        print(CRON_LINE_TEMPLATE.format(repo=REPO_ROOT, python=sys.executable))
        return 0

    if args.scope == "full":
        print(
            "REFUSED: full-scope ingest is owner-gated. Isaac's pending card "
            f"{FULL_INGEST_CARD} ({FULL_INGEST_GATE_ISSUE}) must be accepted first; "
            "this runner has no bypass flag by design (GOV-631 §2 scope gate).",
            file=sys.stderr,
        )
        return 2

    record = refresh(
        source_dir=args.source_dir, db_path=args.db, apply=args.apply,
        structure=args.structure, log_dir=args.log_dir,
    )
    print(render(record))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
