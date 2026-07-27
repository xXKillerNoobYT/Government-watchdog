"""Reviewer-gated backfill: non-URL origin_url prose -> provenance_note (GOV-1625).

Before GOV-1625, the intake API accepted whatever a supplier typed into
``origin_url`` — including free-text prose ("handed to me at the June meeting")
that was never a link. GOV-1625 splits the record so ``origin_url`` holds ONLY a
validated ``http(s)`` URL and prose lives in the new ``provenance_note`` column.
This script migrates the *data* already on disk to match that split.

It is deliberately NOT a silent rewrite (GOV-1566 hard gate: prior user-facing
state is preserved with lineage; corrections are deliberate, reviewer-approved):

  * **Dry-run is the default.** With no ``--apply`` the script only PRINTS the
    plan — every row it would touch, with before/after values — and writes
    nothing. This is what a reviewer reads to decide.
  * **``--apply`` requires ``--reviewer-ref``.** A write is refused unless a
    non-empty SPA/VSR decision reference is supplied, and every applied change is
    printed and appended to an audit log. There is no un-narrated path.
  * **In-place, lossless, non-decreasing history.** The backfill UPDATEs the SAME
    row (``file_id`` unchanged) — it never inserts, deletes, or supersedes — so
    ``SELECT COUNT(*) FROM supplied_files`` is invariant and every version group /
    supersede event is untouched. No supplier text is lost: the routing reuses
    :func:`beta.intake_api._route_provenance` (the exact live-write predicate, so
    backfilled rows and freshly-intaken rows are byte-identical), which joins an
    existing note rather than clobbering it.

Deterministic, no model, no network.

Usage:
    # report only (writes nothing):
    python scripts/backfill_provenance_note.py --db Database/gov_watchdog.db
    # apply, reviewer-gated (SPA/VSR co-sign ref required):
    python scripts/backfill_provenance_note.py --db … --apply \\
        --reviewer-ref "GOV-1625 SPA+VSR backfill co-sign 2026-07-27"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
# Reuse the LIVE intake routing predicate so a backfilled row is byte-identical
# to a freshly-intaken one — never a second, divergent copy of the URL rule.
from beta.intake_api import _route_provenance  # noqa: E402

#: Default audit log for applied backfills (append-only). Absolute at runtime.
DEFAULT_AUDIT_LOG = Path(__file__).resolve().parent.parent / "Logs" / "backfill_provenance_note.log"


class BackfillRefused(Exception):
    """An --apply run without the required reviewer reference (fail-closed)."""


@dataclass(frozen=True)
class PlannedChange:
    """One row the backfill would touch. before_* are current, after_* are the
    routed result. file_id is stable across the update (no lineage break)."""

    file_id: str
    review_state: str
    before_origin_url: str | None
    before_provenance_note: str | None
    after_origin_url: str | None
    after_provenance_note: str | None


def plan_backfill(conn: sqlite3.Connection) -> list[PlannedChange]:
    """Compute (do not write) every row whose stored fields would change.

    A row is planned only when routing its current ``(origin_url,
    provenance_note)`` through the live intake predicate yields a DIFFERENT
    result — i.e. ``origin_url`` currently holds non-URL prose. Rows already in
    the split shape are skipped, so the plan is exactly the delta and a re-run
    after apply is empty (idempotent).
    """
    conn.row_factory = sqlite3.Row
    planned: list[PlannedChange] = []
    rows = conn.execute(
        "SELECT file_id, review_state, origin_url, provenance_note "
        "FROM supplied_files ORDER BY created_at, file_id"
    ).fetchall()
    for row in rows:
        origin_url = row["origin_url"]
        note = row["provenance_note"]
        new_origin_url, new_note = _route_provenance(origin_url, note)
        if (new_origin_url, new_note) == (origin_url, note):
            continue  # already in the split shape; nothing to do
        planned.append(PlannedChange(
            file_id=row["file_id"],
            review_state=row["review_state"],
            before_origin_url=origin_url,
            before_provenance_note=note,
            after_origin_url=new_origin_url,
            after_provenance_note=new_note,
        ))
    return planned


def apply_backfill(
    conn: sqlite3.Connection,
    planned: list[PlannedChange],
    *,
    reviewer_ref: str,
    audit_log: Path | None = DEFAULT_AUDIT_LOG,
) -> int:
    """Apply the plan in-place, reviewer-gated. Returns the row count changed.

    Refuses (``BackfillRefused``) without a non-blank ``reviewer_ref`` — the
    SPA/VSR co-sign that authorizes the correction. Each change is appended to
    the audit log with the reviewer ref and a UTC timestamp. Row count is
    asserted invariant (in-place UPDATE, no insert/delete): history never
    decreases.
    """
    if not (isinstance(reviewer_ref, str) and reviewer_ref.strip()):
        raise BackfillRefused(
            "--apply requires a non-empty --reviewer-ref (SPA/VSR co-sign); "
            "a backfill is never a silent rewrite")

    before_count = conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()[0]
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    lines: list[str] = []
    for ch in planned:
        conn.execute(
            "UPDATE supplied_files SET origin_url = ?, provenance_note = ? "
            "WHERE file_id = ?",
            (ch.after_origin_url, ch.after_provenance_note, ch.file_id),
        )
        lines.append(json.dumps({
            "ts": stamp,
            "reviewer_ref": reviewer_ref.strip(),
            "file_id": ch.file_id,
            "before": {"origin_url": ch.before_origin_url,
                       "provenance_note": ch.before_provenance_note},
            "after": {"origin_url": ch.after_origin_url,
                      "provenance_note": ch.after_provenance_note},
        }, sort_keys=True))
    conn.commit()

    after_count = conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()[0]
    assert after_count == before_count, (
        f"history count changed ({before_count} -> {after_count}); backfill must "
        "be an in-place UPDATE only")

    if audit_log is not None and lines:
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with audit_log.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return len(planned)


def _render_plan(planned: list[PlannedChange]) -> str:
    if not planned:
        return "no rows need backfill (all origin_url values are already URLs or absent)"
    out = [f"{len(planned)} row(s) would be updated (non-URL origin_url -> provenance_note):"]
    for ch in planned:
        out.append(
            f"  {ch.file_id} [{ch.review_state}]\n"
            f"    origin_url:      {ch.before_origin_url!r} -> {ch.after_origin_url!r}\n"
            f"    provenance_note: {ch.before_provenance_note!r} -> {ch.after_provenance_note!r}"
        )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reviewer-gated backfill: non-URL origin_url prose -> "
                    "provenance_note (GOV-1625). Dry-run by default.")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (requires --reviewer-ref)")
    parser.add_argument("--reviewer-ref", default="",
                        help="SPA/VSR co-sign reference authorizing the backfill")
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG)
    args = parser.parse_args(argv)

    conn = db.open_db(args.db)
    try:
        planned = plan_backfill(conn)
        print(_render_plan(planned))
        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply and "
                  "--reviewer-ref to commit (reviewer-gated).")
            return 0
        n = apply_backfill(conn, planned, reviewer_ref=args.reviewer_ref,
                           audit_log=args.audit_log)
        print(f"\nAPPLIED {n} change(s); ref={args.reviewer_ref!r}; "
              f"audit -> {args.audit_log}")
        return 0
    except BackfillRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
