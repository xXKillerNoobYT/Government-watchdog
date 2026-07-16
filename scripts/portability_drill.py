"""Synthetic migration/restore/verify drill (DEPLOY-2026, GOV-722 plan §3 D5).

Proves PORT-1/PORT-2 (AM-6): a system exported from the local backend and
restored into a scale backend yields **verification hashes equal** and **access
decisions identical**, with **no secret/private data in any artifact** (PORT-4).

Usage::

    python3 scripts/portability_drill.py --dry-run        # plan of record, no mutation (default)
    python3 scripts/portability_drill.py --apply           # run vs a second SQLite (CI-safe)
    python3 scripts/portability_drill.py --apply --backend postgres --pg-dsn "$DSN"   # leg 4

Guarantees baked into code (not convention):

* The drill NEVER touches the real registry. It builds its own synthetic fixture
  in a scratch dir; :func:`deploy_adapters.assert_synthetic_path` refuses any
  path that looks like the real DB / raw vault (INV-7).
* Runtime is stdlib-only. The Postgres backend shells to ``psql``; no driver is
  imported here or in the civic-domain graph (PORT-3, enforced by the lock-in
  lint).
* Every numeric metric in the report is basis-labelled (§0 basis-label rule):
  drill duration / RTO are ``MEASURED``; synthetic RPO is ``DERIVED`` (a
  point-in-time snapshot loses nothing).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import db  # noqa: E402
from deploy_adapters import base  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic fixture — deliberately seeds raw/secret-shaped columns so the leak
# scan and access-parity have something real to prove they strip.
# ---------------------------------------------------------------------------

def build_synthetic_fixture(db_path: Path) -> None:
    """Create a migrated SQLite fixture with one reviewer-cleared statement.

    The fixture mirrors the shape ``read_api.reviewer_internal_records`` needs to
    serve a non-empty set (segment + evidence + a promoting reviewer decision),
    and carries vault-only raw columns (``raw_local_path``, ``transcript_path``,
    ``full_text``) that must never reach an export artifact.
    """
    base.assert_synthetic_path(db_path)
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    try:
        conn.executescript(
            """
            INSERT INTO sources(source_id,name,scope,source_class,jurisdiction,
                scan_date,archive_url,raw_sha256,raw_local_path,local_note_path,
                verification_status)
            VALUES('src1','Alpine minutes','alpine','minutes','alpine','2026-06-23',
                'https://web.archive.org/web/2026/min.pdf','deadbeef',
                '/Users/IA/Obsidian Vault/TownOfAlpine/min.pdf',
                '/Users/IA/note.md','reviewed_source_linked');
            INSERT INTO transcripts(id,video_id,video_url,full_text,local_path,
                sha256,fetch_time_utc)
            VALUES(1,'vid1','https://youtube.com/watch?v=vid1',
                'full raw transcript text — vault only','/Users/IA/vault/t.txt',
                's','2026-06-23');
            INSERT INTO transcript_segments(segment_id,transcript_id,segment_index,
                timestamp_seconds,timestamp_human,segment_text,transcript_path)
            VALUES('seg1',1,0,12,'0:12',
                'The council approved the budget for the quarter.',
                '/Users/IA/vault/t.txt');
            INSERT INTO statements(statement_id,segment_id,statement_text,
                verification_status,publication_state)
            VALUES('stmt1','seg1','The council approved the quarterly budget line.',
                'reviewed_source_linked','not_publishable');
            INSERT INTO evidence_links(evidence_link_id,from_node_id,from_node_type,
                to_source_id,relation,locator_kind,page,transcript_path)
            VALUES('el1','stmt1','statement','src1','references','page',3,
                '/Users/IA/vault/t.txt');
            INSERT INTO reviewer_decisions(decision_id,statement_id,reviewer_id,
                decision,from_verification_status,to_verification_status,reason,
                reason_category,promoted,decided_utc,created_utc)
            VALUES('dec1','stmt1','reviewer:isaac','approved',
                'machine_extracted_unreviewed','reviewed_source_linked',
                'synthetic drill promotion','promotion',1,
                '2026-07-15T00:00:00Z','2026-07-15T00:00:00Z');
            INSERT INTO ai_extraction_runs(run_id,lane,model_name,model_version,
                tool_version,output_count,error_status,dry_run,started_utc,
                finished_utc)
            VALUES('run1','2_extraction','local-ollama','2026.7','t1',1,'ok',0,
                '2026-07-15T00:00:00Z','2026-07-15T00:00:01Z');
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The drill.
# ---------------------------------------------------------------------------

def dry_run_plan() -> dict[str, object]:
    """The plan of record — printed by ``--dry-run``; performs no mutation."""
    return {
        "drill": "DEPLOY-2026 synthetic migration/restore/verify",
        "mutation": False,
        "retention_classes_exported": sorted(base.EXPORT_SPEC),
        "tables_per_class": {
            cls: sorted(tables) for cls, tables in base.EXPORT_SPEC.items()
        },
        "excluded_tables": sorted(base.EXCLUDED_TABLES),
        "steps": [
            "1. build synthetic fixture (scratch; never the real registry)",
            "2. export §5 b/c/d column-allowlisted rows + sha256 per class",
            "3. leak-scan the export (raw paths / secrets / excluded tables)",
            "4. restore into the scale backend (sqlite stand-in or postgres:16)",
            "5. re-export from the scale backend; assert hashes equal (AM-6)",
            "6. run frozen access gates on both backends; assert decisions equal",
            "7. record MEASURED duration/RTO, DERIVED synthetic RPO",
        ],
        "backends": ["sqlite (default, CI-safe)", "postgres (leg 4, psql stand-in)"],
    }


def _make_scale_backend(backend: str, workdir: Path, pg_dsn: str | None):
    if backend == "sqlite":
        return base.SqliteAdapter(workdir / "scale_standin.db")
    if backend == "postgres":
        if not pg_dsn:
            raise SystemExit("--backend postgres requires --pg-dsn")
        from deploy_adapters.postgres_adapter import PostgresAdapter

        return PostgresAdapter(pg_dsn)
    raise SystemExit(f"unknown backend: {backend}")


def run_drill(backend: str = "sqlite", *, workdir: Path | None = None,
              pg_dsn: str | None = None) -> dict[str, object]:
    """Execute the full drill and return a report dict (with verification hashes)."""
    started = time.monotonic()
    tmp = workdir or Path(tempfile.mkdtemp(prefix="deploy2026-drill-"))
    tmp.mkdir(parents=True, exist_ok=True)

    fixture = tmp / "synthetic_fixture.db"
    build_synthetic_fixture(fixture)
    source = base.SqliteAdapter(fixture)

    # (2) export + (3) leak scan.
    export = source.export()
    leaks = base.scan_export_for_leaks(export)
    if leaks:
        raise RuntimeError(f"PORT-4 leak scan failed: {leaks}")

    # (4) restore into the scale backend + (5) re-export + hash parity.
    target = _make_scale_backend(backend, tmp, pg_dsn)
    restore_start = time.monotonic()
    target.restore(export)
    rto_s = time.monotonic() - restore_start
    target_export = target.export()

    hashes_equal = export.manifest_hash == target_export.manifest_hash
    per_class_equal = export.class_hashes == target_export.class_hashes

    # (6) access-decision parity through the frozen gates.
    src_conn = source.access_view()
    tgt_conn = target.access_view()
    try:
        src_dec = base.access_decisions(src_conn)
        tgt_dec = base.access_decisions(tgt_conn)
    finally:
        src_conn.close()
        tgt_conn.close()
    decisions_equal = base.decisions_hash(src_dec) == base.decisions_hash(tgt_dec)

    # (7) leak scan the target artifact too, then assemble the report.
    target_leaks = base.scan_export_for_leaks(target_export)
    duration_s = time.monotonic() - started

    report = {
        "drill": "DEPLOY-2026 synthetic migration/restore/verify",
        "backend": backend,
        "source_manifest": export.to_manifest(),
        "target_manifest": target_export.to_manifest(),
        "verification": {
            "manifest_hash_equal": hashes_equal,
            "per_class_hashes_equal": per_class_equal,
            "access_decisions_equal": decisions_equal,
            "export_leaks": leaks,
            "target_leaks": target_leaks,
            "reviewer_internal_count": src_dec["reviewer_internal_count"],
            "published_count": src_dec["published_count"],
        },
        "metrics": {
            "drill_duration_s": {"value": round(duration_s, 4), "basis": "MEASURED"},
            "restore_rto_s": {"value": round(rto_s, 4), "basis": "MEASURED"},
            "restore_rpo_s": {
                "value": 0,
                "basis": "DERIVED",
                "note": "point-in-time synthetic snapshot loses no committed rows",
            },
            "hash_verification_pass": {
                "value": bool(hashes_equal and per_class_equal),
                "basis": "MEASURED",
            },
        },
        "passed": bool(
            hashes_equal
            and per_class_equal
            and decisions_equal
            and not leaks
            and not target_leaks
        ),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="DEPLOY-2026 portability drill (dry-run default)")
    p.add_argument("--apply", action="store_true", help="run the drill (default: dry-run plan)")
    p.add_argument("--dry-run", action="store_true", help="print the plan of record, no mutation")
    p.add_argument("--backend", default="sqlite", choices=["sqlite", "postgres"])
    p.add_argument("--pg-dsn", default=None, help="psql DSN for --backend postgres (leg 4)")
    p.add_argument("--workdir", default=None, help="scratch dir (default: temp)")
    p.add_argument("--report", default=None, help="write the report JSON to this path")
    args = p.parse_args(argv)

    if not args.apply:  # dry-run is the default posture.
        print(json.dumps(dry_run_plan(), indent=2))
        return 0

    report = run_drill(
        backend=args.backend,
        workdir=Path(args.workdir) if args.workdir else None,
        pg_dsn=args.pg_dsn,
    )
    text = json.dumps(report, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
