"""Area-economics CLI (LEDGER-2026 v0.1, GOV-720 plan §3 / GOV-743 impl).

Dry-run-default, read-only except the two explicit writers. Subcommands:

    report            build + print a per-area pack; record content_sha256
    rollup            aggregate child areas (county/state); record content_sha256
    export            emit LED-6 rows (CSV/JSON, no prices)         [read-only]
    verify-hash       recompute a report, assert content_sha256 equal [read-only]
    capacity-forecast synthetic-load headroom (LED-F6)             [read-only]
    eligibility       F-ELIG + entitlement readiness RECOMMENDATION [read-only]
    transition        the ONLY state writer; refuses without --owner-decision-ref

``report``/``rollup`` write only ``ledger_report_runs``. ``transition`` is the
sole path that moves an area between free/funded/paid/locked, and it is inert
without an owner decision reference — this is how "define, not activate" is
enforced operationally.

Usage::

    python scripts/area_economics.py report --db DB --area alpine --period 2026-07
    python scripts/area_economics.py transition --db DB --area alpine \\
        --to funded --owner-decision-ref card:GOV-999 --rule F-ELIG
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
from economics import (  # noqa: E402
    areas as _areas,
    capacity as _capacity,
    eligibility as _eligibility,
    export as _export,
    report as _report,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Area-economics ledger (LEDGER-2026).")
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", required=True, help="path to the SQLite registry DB")

    r = sub.add_parser("report", parents=[common], help="per-area pack + hash")
    r.add_argument("--area", required=True)
    r.add_argument("--period", required=True, help="YYYY-MM")
    r.add_argument("--active-users", type=int, default=None)

    ro = sub.add_parser("rollup", parents=[common], help="county/state rollup + hash")
    ro.add_argument("--scope", required=True, choices=["county", "state"])
    ro.add_argument("--id", required=True, dest="area")
    ro.add_argument("--period", required=True, help="YYYY-MM")

    e = sub.add_parser("export", parents=[common], help="LED-6 rows (no prices)")
    e.add_argument("--report", required=True, dest="report_id")
    e.add_argument("--format", choices=["csv", "json"], default="json")

    v = sub.add_parser("verify-hash", parents=[common], help="reproducibility proof")
    v.add_argument("--report", required=True, dest="report_id")

    c = sub.add_parser("capacity-forecast", parents=[common], help="LED-F6 headroom")
    c.add_argument("--area", required=True)
    c.add_argument("--seed", default=_capacity.DEFAULT_SEED)

    el = sub.add_parser("eligibility", parents=[common],
                        help="F-ELIG recommendation (never transitions)")
    el.add_argument("--area", required=True)
    el.add_argument("--period", required=True, help="YYYY-MM")

    t = sub.add_parser("transition", parents=[common],
                       help="THE ONLY state writer; refuses without an owner ref")
    t.add_argument("--area", required=True)
    t.add_argument("--to", required=True, dest="to_state")
    t.add_argument("--owner-decision-ref", required=True, dest="owner_ref",
                   help="AREA-5: no ownerless transition")
    t.add_argument("--rule", default=None)
    return p


def _scope_of(cli_scope: str) -> str:
    return {"county": "county_rollup", "state": "state_rollup"}[cli_scope]


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    conn = db.open_db(Path(args.db))
    try:
        if args.cmd == "report":
            pack = _report.build_pack(conn, args.area, args.period,
                                      active_users=args.active_users)
            report_id, digest = _report.record_run(conn, pack)
            print(json.dumps(
                {"report_id": report_id, "content_sha256": digest, "pack": pack},
                indent=2, ensure_ascii=False,
            ))

        elif args.cmd == "rollup":
            pack = _report.build_rollup(conn, _scope_of(args.scope), args.area,
                                        args.period)
            report_id, digest = _report.record_run(conn, pack)
            print(json.dumps(
                {"report_id": report_id, "content_sha256": digest, "pack": pack},
                indent=2, ensure_ascii=False,
            ))

        elif args.cmd == "export":
            row = conn.execute(
                "SELECT area_id, period, scope FROM ledger_report_runs WHERE report_id = ?",
                (args.report_id,),
            ).fetchone()
            if row is None:
                print(f"no report run {args.report_id!r}", file=sys.stderr)
                return 2
            if row["scope"] == "area":
                pack = _report.build_pack(conn, row["area_id"], row["period"])
            else:
                pack = _report.build_rollup(conn, row["scope"], row["area_id"],
                                            row["period"])
            print(_export.to_csv(pack) if args.format == "csv" else _export.to_json(pack))

        elif args.cmd == "verify-hash":
            result = _report.verify_hash(conn, args.report_id)
            print(json.dumps(result, indent=2))
            return 0 if result["match"] else 1

        elif args.cmd == "capacity-forecast":
            print(json.dumps(_capacity.forecast(args.area, seed=args.seed, conn=conn),
                             indent=2, ensure_ascii=False))

        elif args.cmd == "eligibility":
            print(json.dumps(_eligibility.evaluate(conn, args.area, args.period),
                             indent=2, ensure_ascii=False))

        elif args.cmd == "transition":
            try:
                tid = _areas.transition(
                    conn, area_id=args.area, to_state=args.to_state,
                    owner_decision_ref=args.owner_ref, rule=args.rule,
                )
            except (_areas.OwnerlessTransition, _areas.IllegalTransition) as exc:
                print(f"transition refused: {exc}", file=sys.stderr)
                return 2
            print(json.dumps({
                "transition_id": tid, "area_id": args.area,
                "to_state": args.to_state, "owner_decision_ref": args.owner_ref,
            }, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
