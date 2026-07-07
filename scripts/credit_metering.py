"""Credit metering over the existing run ledgers (GOV-634, implements GOV-631 T4).

Plan of record: ``Docs/gov-631-automation-credit-efficiency-plan.md`` @ ``4b0c47c``
(Node substrate repo) §3 T4 + §5 — "per-run log of AI calls, model, tokens, and
cost-per-document; surfaced in run summary".

Design: the meter is a PURE READ over the two ledgers that already exist —
``crawl_runs`` (lane 1, deterministic) and ``ai_extraction_runs`` (lanes 2–4,
gateway) — plus the token/cost columns migration 0019 adds to the gateway
ledger. No new table, no clock: callers pass an explicit ISO-UTC window (or
none for all-time), so the same DB always meters to the same numbers.

Definitions (documented so the numbers can't silently change meaning):

* **ai_calls** — gateway-run rows with ``dry_run = 0`` in the window: rows where
  a live model was actually invoked. Offline/injected-proposer runs and
  provider-refused runs stay ``dry_run = 1`` / ``failed`` and cost nothing.
* **cost_per_document** — total ``estimated_cost_usd`` ÷ lane-1 documents
  processed (``new_documents``) in the window; ``None`` (never 0) when no
  document was processed — the meter does not fabricate a ratio.
* **skip_ratio** — ``skipped_hash`` ÷ (``skipped_hash`` + ``new_documents``)
  over lane-1 runs; ``None`` when the denominator is 0.

Boundary: metering output is counts/models/costs only — no record payloads, no
raw text, no PII. Summary counts are safe for a Paperclip comment; the ledgers
themselves stay local/vault-only.
"""

from __future__ import annotations

import sqlite3
from typing import Any

LANE1_TABLE = "crawl_runs"
GATEWAY_TABLE = "ai_extraction_runs"


def _window_clause(column: str, since_utc: str | None, until_utc: str | None
                   ) -> tuple[str, list[str]]:
    """ISO-8601 strings compare lexicographically, so string bounds are correct."""
    clauses, params = [], []
    if since_utc:
        clauses.append(f"{column} >= ?")
        params.append(since_utc)
    if until_utc:
        clauses.append(f"{column} <= ?")
        params.append(until_utc)
    return (" AND ".join(clauses) or "1=1", params)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def meter(conn: sqlite3.Connection, *, since_utc: str | None = None,
          until_utc: str | None = None) -> dict[str, Any]:
    """Aggregate lane-1 throughput + lane-2/3/4 credit spend for the window."""
    where1, p1 = _window_clause("started_utc", since_utc, until_utc)
    skip_col = (
        "COALESCE(SUM(skipped_hash), 0)"
        if _has_column(conn, LANE1_TABLE, "skipped_hash") else "0"
    )
    runs, docs, skipped = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(new_documents), 0), {skip_col} "
        f"FROM {LANE1_TABLE} WHERE {where1}", p1
    ).fetchone()

    where2, p2 = _window_clause("started_utc", since_utc, until_utc)
    metered = _has_column(conn, GATEWAY_TABLE, "tokens_input")
    tok_in = "COALESCE(SUM(tokens_input), 0)" if metered else "0"
    tok_out = "COALESCE(SUM(tokens_output), 0)" if metered else "0"
    cost = "COALESCE(SUM(estimated_cost_usd), 0.0)" if metered else "0.0"
    gateway_rows = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(1 - dry_run), 0), {tok_in}, {tok_out}, {cost} "
        f"FROM {GATEWAY_TABLE} WHERE {where2}", p2
    ).fetchone()
    total_runs, ai_calls, tokens_input, tokens_output, est_cost = gateway_rows

    by_model: dict[str, dict[str, Any]] = {}
    if metered:
        for name, calls, t_in, t_out, c in conn.execute(
            f"SELECT COALESCE(model_name, '(unrecorded)'), COUNT(*), "
            f"COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0), "
            f"COALESCE(SUM(estimated_cost_usd), 0.0) "
            f"FROM {GATEWAY_TABLE} WHERE dry_run = 0 AND {where2} "
            f"GROUP BY model_name ORDER BY model_name", p2
        ):
            by_model[name] = {
                "ai_calls": calls, "tokens_input": t_in,
                "tokens_output": t_out, "estimated_cost_usd": c,
            }

    processed = docs
    return {
        "window": {"since_utc": since_utc, "until_utc": until_utc},
        "lane1": {
            "runs": runs,
            "documents_processed": processed,
            "skipped_hash": skipped,
            "skip_ratio": (
                round(skipped / (skipped + processed), 4)
                if (skipped + processed) else None
            ),
        },
        "gateway": {
            "runs": total_runs,
            "ai_calls": ai_calls,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "estimated_cost_usd": round(est_cost, 6),
            "by_model": by_model,
        },
        "cost_per_document": (
            round(est_cost / processed, 6) if processed else None
        ),
    }


def record_usage(conn: sqlite3.Connection, run_id: str, *,
                 tokens_input: int, tokens_output: int,
                 estimated_cost_usd: float, commit: bool = True) -> None:
    """Attach provider-reported usage to a gateway run (write side of T4)."""
    cur = conn.execute(
        f"UPDATE {GATEWAY_TABLE} SET tokens_input = ?, tokens_output = ?, "
        "estimated_cost_usd = ? WHERE run_id = ?",
        (int(tokens_input), int(tokens_output), float(estimated_cost_usd), run_id),
    )
    if cur.rowcount != 1:
        raise ValueError(f"run_id {run_id!r} not found; usage not recorded")
    if commit:
        conn.commit()


def render_metering(m: dict[str, Any]) -> str:
    """Markdown block for run summaries / Paperclip comments (counts only)."""
    l1, gw = m["lane1"], m["gateway"]
    lines = [
        "## Credit metering (GOV-631 T4)",
        f"- lane-1 runs: {l1['runs']} · documents processed: {l1['documents_processed']} "
        f"· skipped:hash: {l1['skipped_hash']} · skip ratio: {l1['skip_ratio']}",
        f"- AI calls (live model invocations): **{gw['ai_calls']}** "
        f"(gateway rows incl. dry/offline: {gw['runs']})",
        f"- tokens in/out: {gw['tokens_input']}/{gw['tokens_output']} "
        f"· estimated cost: ${gw['estimated_cost_usd']}",
        f"- cost per document: {m['cost_per_document'] if m['cost_per_document'] is not None else 'n/a (0 documents processed)'}",
    ]
    for name, row in gw["by_model"].items():
        lines.append(
            f"  - {name}: {row['ai_calls']} calls · "
            f"{row['tokens_input']}/{row['tokens_output']} tokens · "
            f"${row['estimated_cost_usd']}"
        )
    return "\n".join(lines)
