"""Compose entrypoint for the MCP service (DEPLOY-2026, GOV-722).

The MCP service (``scripts/mcp_service/``) is a byte-0 **frozen** surface, so its
stdio JSON-RPC binding cannot gain a ``__main__``. This tiny launcher lives in
``deploy/`` instead: it opens the mounted registry and hands the connection to
the frozen ``jsonrpc.serve_stdio`` loop (line-delimited JSON-RPC over stdin/
stdout — no socket is ever opened, INV-4). Run under ``stdin_open`` so the
service stays resident awaiting requests.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import db  # noqa: E402
from mcp_service import jsonrpc  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="MCP stdio JSON-RPC service (loopback, private)")
    p.add_argument("--db", required=True, help="path to the mounted registry DB")
    args = p.parse_args(argv)
    conn = db.open_db(Path(args.db))
    print(f"mcp stdio service ready (db={args.db}); awaiting JSON-RPC on stdin",
          file=sys.stderr, flush=True)
    try:
        jsonrpc.serve_stdio(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
