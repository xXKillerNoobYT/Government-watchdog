"""Owner CLI for the beta allowlist + gate flag (GOV-801).

The operational path for an owner to admit or lock out an email, and to flip
the fail-closed ``beta_gate_enabled`` flag. Every mutating action requires an
``--owner-decision-ref`` (a real Isaac board card), matching the in-schema and
service-layer owner gates. Reads never mutate.

    python3 scripts/beta/admin.py allow   --db DB --email a@b.com --owner-decision-ref GOV-784
    python3 scripts/beta/admin.py revoke  --db DB --email a@b.com --owner-decision-ref GOV-784
    python3 scripts/beta/admin.py list     --db DB
    python3 scripts/beta/admin.py enable   --db DB --owner-decision-ref GOV-799
    python3 scripts/beta/admin.py disable  --db DB --owner-decision-ref GOV-799
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beta import allowlist, http_api  # noqa: E402
from email_gateway import flags  # noqa: E402


def _open(db_path: Path):
    import db  # scripts/db.py
    return db.open_db(db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Beta allowlist/gate owner CLI")
    parser.add_argument("--db", required=True, type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_allow = sub.add_parser("allow", help="add/re-activate an allowlisted email")
    p_allow.add_argument("--email", required=True)
    p_allow.add_argument("--owner-decision-ref", required=True)
    p_allow.add_argument("--note")

    p_revoke = sub.add_parser("revoke", help="revoke an email + its sessions")
    p_revoke.add_argument("--email", required=True)
    p_revoke.add_argument("--owner-decision-ref", required=True)

    sub.add_parser("list", help="list allowlist rows (no emails printed raw)")

    p_enable = sub.add_parser("enable", help="turn the beta gate ON")
    p_enable.add_argument("--owner-decision-ref", required=True)

    p_disable = sub.add_parser("disable", help="turn the beta gate OFF")
    p_disable.add_argument("--owner-decision-ref", required=True)

    args = parser.parse_args(argv)
    conn = _open(args.db)
    try:
        if args.cmd == "allow":
            allowlist.add(conn, args.email,
                          owner_decision_ref=args.owner_decision_ref,
                          note=args.note)
            print(f"allowed: {args.email}")
        elif args.cmd == "revoke":
            ok = allowlist.revoke(conn, args.email,
                                  owner_decision_ref=args.owner_decision_ref)
            print("revoked" if ok else "no active row to revoke")
        elif args.cmd == "list":
            rows = conn.execute(
                "SELECT status, added_utc, revoked_utc FROM beta_allowlist"
                " ORDER BY added_utc").fetchall()
            print(f"{len(rows)} allowlist row(s):")
            for r in rows:
                print(f"  status={r['status']} added={r['added_utc']}"
                      f" revoked={r['revoked_utc']}")
        elif args.cmd == "enable":
            flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=True,
                           owner_decision_ref=args.owner_decision_ref)
            print("beta gate ENABLED")
        elif args.cmd == "disable":
            flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=False,
                           owner_decision_ref=args.owner_decision_ref)
            print("beta gate DISABLED")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
