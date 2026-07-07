"""Lane-2 batch queue: hash-gated pending set + floor-model batches + logged
escalation (GOV-634, implements GOV-631 T3).

Plan of record: ``Docs/gov-631-automation-credit-efficiency-plan.md`` @ ``4b0c47c``
§2 (hash gate / batch gate / model floor) + §3 T3. This module makes the three
credit-spend gates EXECUTABLE without weakening any AI-gateway rule:

* **Hash gate** — the pending queue is derived from the ledgers: a transcript
  segment is pending only if NO successful (``error_status='ok'``) lane-2 run
  has ever covered it. Lane-1's sha-addressed store guarantees a stored segment
  reflects unchanged source bytes, so "already covered" == "this content hash
  already has a lane-2 artifact". Unchanged content can never re-queue.
* **Batch gate** — ``plan_batches`` groups the pending set into fixed-size
  batches for scheduled runs; there is no one-off-per-document path here.
* **Model floor** — every planned batch carries ``FLOOR_MODEL`` (cheapest
  capable, Haiku-class). ``escalate`` refuses (``EscalationWithoutReason``)
  unless the floor run carries a logged per-item low-confidence record — the
  gateway log itself justifies every tier bump.

What this module does NOT do: call a model. Execution goes through
``ai_extraction.run_extraction`` unchanged, which requires an injected proposer
or a locally configured provider and refuses fail-closed otherwise. Under the
GOV-612/GOV-625 pilot scope (lane-2 AI excluded), the CLI is read-only
plan/queue reporting — zero credits by construction.

Boundary: Alpine-only, reviewer/vault-only. Queue/plan output is ids + counts
only (no segment text); AI rows keep their fail-closed gating fields
(``not_publishable`` / ``unreviewed``) via ``ai_extraction``'s bindings.

Usage:
    python scripts/lane2_batch_queue.py --db Database/gov_watchdog.db   # read-only plan
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_extraction as ai  # noqa: E402
import credit_metering as cm  # noqa: E402
import db  # noqa: E402

# Model floor (plan §2 "batches default to the cheapest capable model
# (Haiku-class)") and the single escalation tier above it. Version strings are
# recorded per-run in the ledger; these are the tier DEFAULTS, not billing truth.
FLOOR_MODEL = "claude-haiku-4-5"
ESCALATED_MODEL = "claude-sonnet-4-6"
TIER_FLOOR = "floor"
TIER_ESCALATED = "escalated"

DEFAULT_BATCH_SIZE = 25


class EscalationWithoutReason(ValueError):
    """Tier escalation attempted without a logged low-confidence record (plan §2)."""


def covered_segment_ids(conn: sqlite3.Connection) -> set[str]:
    """Segments already carrying a successful lane-2 artifact (the hash gate)."""
    covered: set[str] = set()
    for (raw,) in conn.execute(
        "SELECT input_segment_ids FROM ai_extraction_runs "
        "WHERE lane = '2_extraction' AND error_status = 'ok' "
        "AND input_segment_ids IS NOT NULL"
    ):
        try:
            covered.update(json.loads(raw))
        except (TypeError, ValueError):
            continue  # malformed ledger row never UNcovers anything
    return covered


def pending_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Deterministic pending queue: stored, timed segments with no lane-2 artifact.

    Ordered oldest→newest (meeting_date, transcript, segment_index) — the same
    processing order the rest of the pipeline uses. Items carry ids + the source
    document hash only, never text.
    """
    covered = covered_segment_ids(conn)
    items = []
    for row in conn.execute(
        "SELECT ts.segment_id, ts.transcript_id, ts.source_id, "
        "       t.meeting_date, t.sha256 "
        "FROM transcript_segments ts JOIN transcripts t ON ts.transcript_id = t.id "
        "ORDER BY t.meeting_date, ts.transcript_id, ts.segment_index"
    ):
        if row[0] in covered:
            continue
        items.append({
            "segment_id": row[0],
            "transcript_id": row[1],
            "source_id": row[2],
            "meeting_date": row[3],
            "source_sha256": row[4],
        })
    return items


def plan_batches(conn: sqlite3.Connection, *,
                 batch_size: int = DEFAULT_BATCH_SIZE) -> list[dict[str, Any]]:
    """Chunk the pending queue into floor-model batches (batch gate + model floor)."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    pending = pending_items(conn)
    batches = []
    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        batches.append({
            "batch_index": i // batch_size,
            "model_name": FLOOR_MODEL,
            "model_tier": TIER_FLOOR,
            "segment_ids": [c["segment_id"] for c in chunk],
            "input_source_ids": sorted({c["source_id"] for c in chunk if c["source_id"]}),
        })
    return batches


def run_batch(conn: sqlite3.Connection, batch: dict[str, Any], *, run_id: str,
              proposer: "ai.Proposer | None" = None, dry_run: bool = True,
              model_name: str | None = None, model_tier: str = TIER_FLOOR,
              escalated_from_run_id: str | None = None,
              usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute ONE planned batch through the unchanged lane-2 adapter.

    All gateway rules (fail-closed gating fields, PII guard, no-orphan-claims,
    attribution safety, provider refusal without a proposer) are inherited from
    ``ai_extraction.run_extraction``. This wrapper only adds the T3/T4 ledger
    fields: model tier, escalation provenance, and provider-reported usage.
    """
    result = ai.run_extraction(
        conn,
        run_id=run_id,
        input_source_ids=batch["input_source_ids"],
        input_segment_ids=batch["segment_ids"],
        proposer=proposer,
        model_name=model_name or batch.get("model_name", FLOOR_MODEL),
        dry_run=dry_run,
        commit=False,
    )
    conn.execute(
        "UPDATE ai_extraction_runs SET model_tier = ?, escalated_from_run_id = ? "
        "WHERE run_id = ?",
        (model_tier, escalated_from_run_id, run_id),
    )
    if usage:
        cm.record_usage(
            conn, run_id,
            tokens_input=usage.get("tokens_input", 0),
            tokens_output=usage.get("tokens_output", 0),
            estimated_cost_usd=usage.get("estimated_cost_usd", 0.0),
            commit=False,
        )
    conn.commit()
    return result


def record_low_confidence(conn: sqlite3.Connection, run_id: str, *,
                          commit: bool = True) -> list[dict[str, Any]]:
    """Write the per-item low-confidence record onto a finished floor run.

    Reads the run's OWN written statements, keeps those with ``confidence='low'``,
    and stores them as JSON on ``low_confidence_items`` — the logged reason that
    (alone) authorizes a tier escalation. Returns the recorded items.
    """
    run = ai.get_run(conn, run_id)
    statement_ids = json.loads(run.get("output_statement_ids") or "[]")
    items: list[dict[str, Any]] = []
    for sid in statement_ids:
        row = conn.execute(
            "SELECT confidence, segment_id FROM statements WHERE statement_id = ?",
            (sid,),
        ).fetchone()
        if row and row[0] == "low":
            items.append({
                "statement_id": sid,
                "segment_id": row[1],
                "confidence": "low",
                "reason": "model reported low confidence at the floor tier",
            })
    conn.execute(
        "UPDATE ai_extraction_runs SET low_confidence_items = ? WHERE run_id = ?",
        (json.dumps(items), run_id),
    )
    if commit:
        conn.commit()
    return items


def escalate(conn: sqlite3.Connection, floor_run_id: str, *, run_id: str,
             proposer: "ai.Proposer | None" = None, dry_run: bool = True,
             usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Re-run ONLY the logged low-confidence items at the escalated tier.

    Refuses (``EscalationWithoutReason``) when the floor run has no non-empty
    ``low_confidence_items`` record — the hard rule from plan §2: "tier
    escalation requires a per-item low-confidence record in the gateway log".
    """
    floor = ai.get_run(conn, floor_run_id)
    items = json.loads(floor.get("low_confidence_items") or "[]")
    if not items:
        raise EscalationWithoutReason(
            f"floor run {floor_run_id!r} has no logged low_confidence_items; "
            "escalation to a costlier model tier is not allowed without one"
        )
    segment_ids = sorted({i["segment_id"] for i in items if i.get("segment_id")})
    batch = {
        "input_source_ids": json.loads(floor.get("input_source_ids") or "[]"),
        "segment_ids": segment_ids,
        "model_name": ESCALATED_MODEL,
    }
    return run_batch(
        conn, batch, run_id=run_id, proposer=proposer, dry_run=dry_run,
        model_name=ESCALATED_MODEL, model_tier=TIER_ESCALATED,
        escalated_from_run_id=floor_run_id, usage=usage,
    )


def render_plan(batches: list[dict[str, Any]], pending_count: int) -> str:
    lines = [
        "# Lane-2 batch queue plan (read-only; GOV-631 T3)",
        f"- pending segments (hash-gated): {pending_count}",
        f"- planned batches: {len(batches)} @ model floor `{FLOOR_MODEL}`",
    ]
    for b in batches:
        lines.append(
            f"  - batch {b['batch_index']}: {len(b['segment_ids'])} segments · "
            f"sources {', '.join(b['input_source_ids']) or '(none)'}"
        )
    if not batches:
        lines.append("- queue empty: every stored segment already has a lane-2 artifact "
                     "or no timed segments exist (zero credits to spend)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan (read-only) the hash-gated lane-2 batch queue. "
        "Execution requires a configured provider/proposer and is NOT exposed "
        "here — pilot scope (GOV-612/GOV-625) excludes live lane-2 AI."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"no DB at {args.db}; nothing pending", file=sys.stderr)
        return 0
    conn = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
    try:
        pending = pending_items(conn)
        batches = plan_batches(conn, batch_size=args.batch_size)
        print(render_plan(batches, len(pending)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
