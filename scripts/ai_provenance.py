"""AI-provenance integrity audit (GOV-278, Stage 2.05 successor slice).

The write-time half of the Stage 2.05 AI-provenance rule lives in
:func:`statements.insert_statement` (an AI row is rejected unless it names an
``ai_extraction_runs`` row with ``error_status='ok'``; see
:class:`statements.AiProvenanceError`). This module is the **read-only**
companion: it scans an existing DB and proves the invariant *persisted* — that
there are no orphan ``produced_by='ai'`` rows.

GOV-233 §2.05 / GOV-230 define the rule; this auditor mirrors it, it does not
re-invent it. Two distinct facts are reported:

* **Orphans (hard / clean=False).** A ``produced_by='ai'`` statement whose
  ``ai_extraction_run_id`` is NULL/blank or does not resolve to a ledger row,
  and any ``evidence_link`` carrying a non-NULL run id that does not resolve.
  These are integrity breaks the write-time gate exists to prevent.
* **Non-ok runs (soft / informational).** An AI statement whose run *resolved*
  but is now ``partial``/``failed``. The write-time gate guarantees the run was
  ``ok`` at write; a run may finalize ``partial`` afterwards while its already
  written rows remain (the orphan-rejected siblings are what made it partial).
  Such a row is NOT an orphan, so it does not flip ``clean`` — it is surfaced for
  reviewer visibility only.

Read-only by construction: opens the DB, runs SELECTs, never writes (so it is
inherently dry-run; there is no ``--apply``). Alpine-only, local/vault-only, no
network. CLI exits non-zero when the DB is not clean, so it doubles as a gate.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def audit_ai_provenance(conn: sqlite3.Connection) -> dict[str, Any]:
    """Scan ``conn`` for AI-provenance integrity violations (GOV-278).

    Returns a JSON-able report::

        {
          "ai_statement_count": int,
          "null_run_statement_ids": [statement_id, ...],     # AI row, NULL/blank run id
          "unresolved_run": [[statement_id, run_id], ...],   # AI row, run id w/o ledger row
          "unresolved_evidence_run": [[evidence_link_id, run_id], ...],
          "non_ok_run": [[statement_id, run_id, error_status], ...],  # informational
          "orphan_count": int,    # null_run + unresolved_run + unresolved_evidence_run
          "clean": bool,          # orphan_count == 0
        }

    Fail-closed when the ledger table is absent: any ``produced_by='ai'`` row is
    then unresolvable and reported as an orphan.
    """
    if not _table_exists(conn, "statements"):
        return {
            "ai_statement_count": 0,
            "null_run_statement_ids": [],
            "unresolved_run": [],
            "unresolved_evidence_run": [],
            "non_ok_run": [],
            "orphan_count": 0,
            "clean": True,
        }

    ai_count = conn.execute(
        "SELECT count(*) FROM statements WHERE produced_by='ai'"
    ).fetchone()[0]

    null_run = [
        r[0]
        for r in conn.execute(
            "SELECT statement_id FROM statements "
            "WHERE produced_by='ai' "
            "AND (ai_extraction_run_id IS NULL OR trim(ai_extraction_run_id)='')"
        ).fetchall()
    ]

    has_ledger = _table_exists(conn, "ai_extraction_runs")

    if not has_ledger:
        # No ledger: every AI row with a (non-blank) run id is unresolvable.
        unresolved_run = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT statement_id, ai_extraction_run_id FROM statements "
                "WHERE produced_by='ai' "
                "AND ai_extraction_run_id IS NOT NULL AND trim(ai_extraction_run_id)<>''"
            ).fetchall()
        ]
        non_ok_run: list[tuple] = []
        unresolved_ev = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT evidence_link_id, ai_extraction_run_id FROM evidence_links "
                "WHERE ai_extraction_run_id IS NOT NULL AND trim(ai_extraction_run_id)<>''"
            ).fetchall()
        ] if _table_exists(conn, "evidence_links") else []
    else:
        unresolved_run = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT s.statement_id, s.ai_extraction_run_id FROM statements s "
                "LEFT JOIN ai_extraction_runs r ON r.run_id = s.ai_extraction_run_id "
                "WHERE s.produced_by='ai' "
                "AND s.ai_extraction_run_id IS NOT NULL AND trim(s.ai_extraction_run_id)<>'' "
                "AND r.run_id IS NULL"
            ).fetchall()
        ]
        non_ok_run = [
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT s.statement_id, s.ai_extraction_run_id, r.error_status FROM statements s "
                "JOIN ai_extraction_runs r ON r.run_id = s.ai_extraction_run_id "
                "WHERE s.produced_by='ai' AND r.error_status <> 'ok'"
            ).fetchall()
        ]
        unresolved_ev = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT e.evidence_link_id, e.ai_extraction_run_id FROM evidence_links e "
                "LEFT JOIN ai_extraction_runs r ON r.run_id = e.ai_extraction_run_id "
                "WHERE e.ai_extraction_run_id IS NOT NULL AND trim(e.ai_extraction_run_id)<>'' "
                "AND r.run_id IS NULL"
            ).fetchall()
        ] if _table_exists(conn, "evidence_links") else []

    orphan_count = len(null_run) + len(unresolved_run) + len(unresolved_ev)
    return {
        "ai_statement_count": ai_count,
        "null_run_statement_ids": null_run,
        "unresolved_run": [list(t) for t in unresolved_run],
        "unresolved_evidence_run": [list(t) for t in unresolved_ev],
        "non_ok_run": [list(t) for t in non_ok_run],
        "orphan_count": orphan_count,
        "clean": orphan_count == 0,
    }


def _format_report(report: dict[str, Any], db_path: Path) -> str:
    lines = [
        f"AI-provenance audit (GOV-278) — {db_path}",
        f"  produced_by='ai' statements : {report['ai_statement_count']}",
        f"  orphan rows (hard)          : {report['orphan_count']}",
        f"    null/blank run id         : {len(report['null_run_statement_ids'])}",
        f"    unresolved statement run  : {len(report['unresolved_run'])}",
        f"    unresolved evidence run   : {len(report['unresolved_evidence_run'])}",
        f"  non-ok resolved run (info)  : {len(report['non_ok_run'])}",
        f"  CLEAN                       : {report['clean']}",
    ]
    for sid in report["null_run_statement_ids"]:
        lines.append(f"    ORPHAN null-run: {sid}")
    for sid, rid in report["unresolved_run"]:
        lines.append(f"    ORPHAN unresolved-run: {sid} -> {rid!r}")
    for eid, rid in report["unresolved_evidence_run"]:
        lines.append(f"    ORPHAN unresolved-evidence-run: {eid} -> {rid!r}")
    for sid, rid, status in report["non_ok_run"]:
        lines.append(f"    INFO non-ok-run: {sid} -> {rid!r} ({status})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit AI-provenance integrity (read-only). GOV-278 Stage 2.05."
    )
    parser.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help=f"path to the sqlite DB (default: {db.DEFAULT_DB_PATH})",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"AI-provenance audit: DB not found at {args.db}", file=sys.stderr)
        return 2

    with db.open_db(args.db) as conn:
        report = audit_ai_provenance(conn)
    print(_format_report(report, args.db))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
