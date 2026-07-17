"""PILOT-2026 harness CLI (GOV-781 leg 2; contract in protocol §5.1).

Subcommands (all local-only, dry-run by default per GOV-631):

    python3.12 scripts/pilot_run.py workload --db <path> [--apply] [--provider fake|ollama]
    python3.12 scripts/pilot_run.py snapshot --db <path> [--area alpine] [--period YYYY-MM]
    python3.12 scripts/pilot_run.py pack     --db <path> [--area alpine] [--period YYYY-MM]

Artifacts (run manifest, metric snapshot, decision pack, LED-6 export) are
written under ``Logs/pilot/`` (gitignored, local-only) unless ``--out`` is given.
Only sanitized aggregate metrics (units-only, no PII, no prices) ever leave the
machine. ``--apply`` on ``workload`` is the only mode that writes to the DB, and
it requires ``MCP_HMAC_SECRET`` in the environment (INV-7).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import db as _db  # noqa: E402
from pilot import ALPINE_AREA_ID, DEFAULT_SEED, LOG_ROOT  # noqa: E402
from pilot import pack as _pack  # noqa: E402
from pilot import snapshot as _snapshot  # noqa: E402
from pilot import workload as _workload  # noqa: E402
from economics import export as _export  # noqa: E402

_REPO_ROOT = _SCRIPTS_DIR.parent


def _provider(name: str) -> tuple[str, str]:
    if name == "ollama":
        return "ollama", "ollama"
    return "fake", "fake"


def _out_dir(explicit: str | None) -> Path:
    d = Path(explicit) if explicit else (_REPO_ROOT / LOG_ROOT)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(out_dir: Path, name: str, obj) -> Path:
    path = out_dir / name
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _cmd_workload(args) -> int:
    provider_id, provider_kind = _provider(args.provider)
    out_dir = _out_dir(args.out)
    conn = _db.open_db(Path(args.db))
    try:
        report = _workload.run(conn, seed=args.seed, apply=args.apply,
                               provider_id=provider_id, provider_kind=provider_kind)
    finally:
        conn.close()
    man_path = _write(out_dir, "wave0-manifest.json", report["manifest"]
                      if not args.apply else report)
    print(f"[pilot] workload {'APPLIED' if args.apply else 'DRY-RUN'} -> {man_path}")
    if args.apply:
        # Also drop the metric snapshot for the run's period (artifact §1.3).
        conn = _db.open_db(Path(args.db))
        try:
            snap = _snapshot.extract(conn, args.area, report["period"], seed=args.seed)
        finally:
            conn.close()
        snap_path = _write(out_dir, f"wave0-snapshot-{report['period']}.json", snap)
        print(f"[pilot] zero-credit: {report['zero_credit']['ok']} · snapshot -> {snap_path}")
    return 0


def _cmd_snapshot(args) -> int:
    out_dir = _out_dir(args.out)
    conn = _db.open_db(Path(args.db))
    try:
        snap = _snapshot.extract(conn, args.area, args.period, seed=args.seed,
                                 support_log_path=args.support_log)
    finally:
        conn.close()
    violations = _snapshot.lint(snap)
    if violations:
        print("[pilot] SNAPSHOT LINT FAILED (AM-7):", file=sys.stderr)
        for v in violations:
            print("  -", v, file=sys.stderr)
        return 1
    path = _write(out_dir, f"snapshot-{args.area}-{args.period}.json", snap)
    print(f"[pilot] snapshot -> {path} (basis lint clean)")
    return 0


def _cmd_pack(args) -> int:
    out_dir = _out_dir(args.out)
    conn = _db.open_db(Path(args.db))
    try:
        built = _pack.build_and_record(conn, area_id=args.area, period=args.period,
                                       active_users=args.active_users, seed=args.seed,
                                       support_log_path=args.support_log)
        rows = _pack.export_rows(built["pack"])
    finally:
        conn.close()
    pack_path = _write(out_dir, f"pack-{args.area}-{args.period}.json", built)
    export_path = out_dir / f"pack-{args.area}-{args.period}.led6.csv"
    # LED-6 export via the frozen no-price surface.
    import io

    buf = io.StringIO()
    import csv

    writer = csv.DictWriter(buf, fieldnames=_export.FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    export_path.write_text(buf.getvalue(), encoding="utf-8")
    print(f"[pilot] pack -> {pack_path} · sha256={built['content_sha256'][:12]}… · "
          f"LED-6 -> {export_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pilot_run", description="PILOT-2026 Wave-0 harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("workload", help="run/plan the WL-1..6 synthetic workload")
    w.add_argument("--db", required=True)
    w.add_argument("--apply", action="store_true",
                   help="execute against the live stack (default: dry-run plan only)")
    w.add_argument("--provider", choices=("fake", "ollama"), default="fake")
    w.add_argument("--seed", default=DEFAULT_SEED)
    w.add_argument("--area", default=ALPINE_AREA_ID)
    w.add_argument("--out", default=None)
    w.set_defaults(func=_cmd_workload)

    s = sub.add_parser("snapshot", help="extract the §2 metric snapshot")
    s.add_argument("--db", required=True)
    s.add_argument("--area", default=ALPINE_AREA_ID)
    s.add_argument("--period", required=True)
    s.add_argument("--seed", default=DEFAULT_SEED)
    s.add_argument("--support-log", default=None, dest="support_log")
    s.add_argument("--out", default=None)
    s.set_defaults(func=_cmd_snapshot)

    k = sub.add_parser("pack", help="build the §4 decision pack + LED-6 export")
    k.add_argument("--db", required=True)
    k.add_argument("--area", default=ALPINE_AREA_ID)
    k.add_argument("--period", required=True)
    k.add_argument("--active-users", type=int, default=None, dest="active_users")
    k.add_argument("--seed", default=DEFAULT_SEED)
    k.add_argument("--support-log", default=None, dest="support_log")
    k.add_argument("--out", default=None)
    k.set_defaults(func=_cmd_pack)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
