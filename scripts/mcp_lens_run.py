"""Multi-lens run CLI (PLAN-2026-AI §3.8, INV-7).

The composition root for a lens run: this is the ONLY place a concrete provider
adapter is constructed — the domain core stays behind the ``ProviderAdapter``
protocol (PORT-3). Safe defaults, fail-closed:

* **Dry-run by default.** Without ``--apply`` the CLI assembles the evidence
  context read-only and prints the plan (evidence hash, lenses, provider); it
  writes nothing.
* **Fake adapter by default.** ``--provider fake`` is deterministic, offline, and
  zero-spend. ``--provider ollama`` builds the localhost-only real adapter for a
  MANUAL local smoke run — never used in CI/tests (which never invoke this CLI).
* **Local-only, zero-spend.** Both providers are local (D7); the runner refuses
  to route a local_only context anywhere else. Setup registers the local provider
  with a local unit budget so it is callable (BUD-5) without an owner card
  (plan §5: local-only operation needs none).
* **Logs are local (INV-7).** Run summaries (ids/hashes/verdicts only — never raw
  evidence) are written under ``Logs/mcp-lens/`` (gitignored).

Usage::

    python scripts/mcp_lens_run.py --db /tmp/mcp.db --job-id job1            # dry-run, fake
    python scripts/mcp_lens_run.py --db /tmp/mcp.db --job-id job1 --apply
    python scripts/mcp_lens_run.py --db /tmp/mcp.db --job-id job1 --provider ollama --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
from mcp_service import analysis, budget as budget_mod, lenses, routing  # noqa: E402
from mcp_service.providers import base as pbase  # noqa: E402
from mcp_service.providers.fake import FakeAdapter  # noqa: E402
from mcp_service.providers.ollama import LOCAL_ENDPOINT, OllamaAdapter  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent.parent / "Logs" / "mcp-lens"
# A generous local unit budget for zero-spend local providers (BUD-3 meters units
# even though the direct cost is 0). Owner-set for real paid providers only.
LOCAL_BUDGET_UNITS = 1_000_000
DEFAULT_JOB_KIND = analysis.DEFAULT_JOB_KIND


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_adapter(provider: str, *, model: str, endpoint: str) -> pbase.ProviderAdapter:
    if provider == "fake":
        return FakeAdapter(provider_id="fake", model=model or "fake-1")
    if provider == "ollama":
        return OllamaAdapter(model=model or "llama3.2", endpoint=endpoint)
    raise SystemExit(f"unknown provider {provider!r} (choose fake|ollama)")


def setup_local_routing(
    conn: sqlite3.Connection, *, provider_id: str, kind: str, model: str,
) -> None:
    """Idempotently make a local provider callable + install a local_only policy.

    Registers the provider, sets enabled + a local unit budget (BUD-5), creates a
    ``mcp_budgets`` row, and writes a ``lens_analysis``/``local_only`` routing
    policy that prefers this provider. All local, zero real spend.
    """
    lenses.seed_lens_packs(conn)
    pbase.register_provider(conn, provider_id=provider_id, kind=kind,
                            budget_cap_units=LOCAL_BUDGET_UNITS)
    conn.execute(
        "UPDATE mcp_provider_registry SET enabled = 1, budget_cap_units = ? "
        "WHERE provider_id = ?",
        (LOCAL_BUDGET_UNITS, provider_id),
    )
    budget_mod.create_budget(
        conn, budget_id=f"budget-{provider_id}-local", provider_id=provider_id,
        cap_units=LOCAL_BUDGET_UNITS, window_kind="total", basis="LOCAL-ZERO-SPEND",
    )
    conn.execute(
        "INSERT OR IGNORE INTO mcp_routing_policies "
        "(policy_id, version, job_kind, context_class, provider_preference, model, "
        " max_output_units, created_utc) VALUES (?, '1.0.0', ?, 'local_only', ?, ?, 512, ?)",
        (f"policy-lens-{provider_id}", DEFAULT_JOB_KIND,
         json.dumps([provider_id]), model, _utcnow()),
    )
    conn.commit()


def _write_log(summary: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _utcnow().replace(":", "").replace(".", "")
    path = LOG_DIR / f"lens-run-{summary.get('job_id', 'job')}-{stamp}.json"
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return path


def run(args: argparse.Namespace) -> int:
    conn = db.open_db(Path(args.db))
    try:
        lens_ids = ([s.strip() for s in args.lenses.split(",") if s.strip()]
                    if args.lenses else list(lenses.LENSES))
        provider_id = "fake" if args.provider == "fake" else "ollama"

        if not args.apply:
            # Read-only preview: assemble evidence and report the plan.
            evidence = analysis.assemble_evidence(conn, args.job_id)
            plan = {
                "mode": "dry-run", "job_id": args.job_id, "provider": provider_id,
                "lenses": lens_ids, "evidence_hash": evidence.evidence_hash,
                "evidence_refs": evidence.evidence_refs,
                "note": "no writes; pass --apply to run and stage outputs",
            }
            print(json.dumps(plan, indent=2))
            return 0

        model = args.model or ("fake-1" if provider_id == "fake" else "llama3.2")
        kind = "fake" if provider_id == "fake" else "ollama"
        setup_local_routing(conn, provider_id=provider_id, kind=kind, model=model)
        adapter = build_adapter(args.provider, model=model, endpoint=args.ollama_endpoint)
        token = analysis.mint_submit_token(conn, args.job_id)
        summary = analysis.run_multi_lens(
            conn, job_id=args.job_id, adapters={provider_id: adapter}, token=token,
            lens_ids=lens_ids, degrade_threshold=args.degrade_threshold,
        )
        log_path = _write_log(summary)
        summary["log"] = str(log_path)
        print(json.dumps(summary, indent=2))
        return 0
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run labelled multi-lens analysis (local-only).")
    p.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    p.add_argument("--job-id", required=True)
    p.add_argument("--provider", choices=["fake", "ollama"], default="fake")
    p.add_argument("--model", default="")
    p.add_argument("--lenses", default="", help="comma-separated lens ids (default: all three)")
    p.add_argument("--apply", action="store_true", help="run and stage outputs (default: dry-run)")
    p.add_argument("--ollama-endpoint", default=LOCAL_ENDPOINT)
    p.add_argument("--degrade-threshold", type=int, default=routing.health.DEFAULT_DEGRADE_THRESHOLD)
    return p


def main(argv: list[str] | None = None) -> int:
    return run(_build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
