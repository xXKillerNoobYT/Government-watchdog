"""First-slice end-to-end integration smoke (GOV-77, Stage 1 Slice 1 Issue E).

Integration of contracts 1.02 / 1.03 / 1.04 / 1.05. Source: GOV-71 §2.E.

This is the single end-to-end smoke that proves the first Alpine implementation
slice holds together when the four modules run in sequence — not just in
isolation. It performs a real:

    apply migrations  ->  seed Alpine source registry  ->  ingest one small
    sanitized Alpine fixture  ->  re-verify

and then asserts the three contract invariants the slice exists to guarantee:

  1. RAW PRESERVED + REPRODUCIBLE (1.04) — the ingested raw artifact is on disk
     and its bytes re-hash to the recorded sha256 (raw-before-parse gate +
     reproducibility verifier both pass).
  2. PROVENANCE PRESENT (1.02/1.03) — the record resolves to a registered
     `source_id` (via registry reconciliation) and carries `sha256`,
     `fetch_time`, and an archive URL.
  3. DEFAULT NOT-PUBLISHABLE (1.05) — every registered record defaults to
     `publication_state='not_publishable'` with an uncomputed `ui_status`, and
     its computed uiStatus is NOT on the fail-closed publication allowlist.

Design (why this is a deterministic tool, not just a test):
  * `run_smoke()` does all the work in a **caller-provided sandbox dir** — a
    throwaway DB + a throwaway raw store. It NEVER touches the real
    `Database/gov_watchdog.db` or `Raw-PDFs/`, so there is no `--apply` gate to
    trip: the smoke is read-only with respect to real data by construction.
  * It returns a structured result so `tests/test_slice1_integration_smoke.py`
    can assert each invariant granularly, while the CLI prints loud
    PASS/FAIL evidence lines and exits non-zero on any failure (so CI fails
    loudly if a contract invariant regresses — the Issue E success criterion).

Data boundary: only sanitized fixtures under `tests/fixtures/alpine/` are read;
nothing is published and no real raw/DB is committed (WORKFLOW_GOVERNANCE.md).

Usage:
    python scripts/slice1_smoke.py [--fixture PATH] [--keep] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import publication as pub  # noqa: E402
import raw_preservation as rp  # noqa: E402
import source_inventory as si  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-agenda.txt"

# The ingested fixture is published as if it were a document fetched from the
# Town of Alpine site, so registry reconciliation links it to `alpinewy_gov`.
_FIXTURE_SOURCE_URL = "https://www.alpinewy.gov/agendas/2026-sample-agenda.pdf"
_FIXTURE_ARCHIVE_URL = (
    "https://web.archive.org/web/2026id_/"
    "https://www.alpinewy.gov/agendas/2026-sample-agenda.pdf"
)
_FIXTURE_REL_PATH = "Raw-PDFs/2026/alpinewy/2026-sample-agenda.pdf"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SmokeFailure(AssertionError):
    """Raised by run_smoke(strict=True) when any contract invariant regresses."""


def _ingest_fixture(conn, sandbox: Path, fixture: Path) -> int:
    """Copy the sanitized fixture into the sandbox raw store and record it.

    Mirrors what the deterministic Lane 1 crawler records at fetch time
    (crawl_pdfs.py): raw bytes on disk + sha256 + fetch_time + archive URL.
    Inserts with source_id NULL so registry reconciliation must back-fill it
    (exercising the 1.02 provenance path, not hand-wiring the link).
    Returns the new document id.
    """
    raw_path = sandbox / _FIXTURE_REL_PATH
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, raw_path)
    sha = rp.sha256_file(raw_path)
    cur = conn.execute(
        "INSERT INTO documents (source_url, title, doc_type, local_path, sha256, "
        "size_bytes, fetch_time_utc, wayback_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _FIXTURE_SOURCE_URL,
            "Town of Alpine — sample council agenda (synthetic fixture)",
            "agenda",
            _FIXTURE_REL_PATH,
            sha,
            raw_path.stat().st_size,
            _now(),
            _FIXTURE_ARCHIVE_URL,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _check_raw_preserved(conn, sandbox: Path, doc_id: int) -> dict:
    """Invariant 1: raw on disk + re-hash matches recorded sha256 (1.04)."""
    detail: dict = {"name": "raw_preserved_and_reproducible", "passed": False}
    try:
        verified = rp.assert_raw_preserved(conn, "document", doc_id, repo_root=sandbox)
    except rp.RawPreservationError as exc:
        detail["error"] = f"raw-before-parse gate failed: {exc}"
        return detail
    repro = rp.verify_reproducibility(conn, repo_root=sandbox)
    detail["recorded_sha256"] = verified
    detail["reproducibility"] = repro
    if repro["checked"] < 1:
        detail["error"] = "reproducibility verifier checked 0 artifacts"
    elif repro["missing"] or repro["mismatch"]:
        detail["error"] = (
            f"reproducibility failed: missing={repro['missing']} "
            f"mismatch={repro['mismatch']}"
        )
    elif repro["ok"] != repro["checked"]:
        detail["error"] = "reproducibility ok != checked"
    else:
        detail["passed"] = True
    return detail


def _check_provenance(conn, doc_id: int) -> dict:
    """Invariant 2: source_id + sha256 + fetch_time + archive URL present (1.02/1.03)."""
    row = conn.execute(
        "SELECT source_id, sha256, fetch_time_utc, wayback_url FROM documents WHERE id = ?",
        (doc_id,),
    ).fetchone()
    fields = {
        "source_id": row["source_id"],
        "sha256": row["sha256"],
        "fetch_time": row["fetch_time_utc"],
        "archive_url": row["wayback_url"],
    }
    missing = [k for k, v in fields.items() if not v]
    detail = {"name": "provenance_present", "passed": not missing, "fields": fields}
    if missing:
        detail["error"] = f"missing provenance field(s): {missing}"
    return detail


def _check_default_not_publishable(conn) -> dict:
    """Invariant 3: every registered record defaults not-publishable (1.05).

    Three fail-closed checks across all `sources` rows:
      * DB column `publication_state` == 'not_publishable',
      * DB column `ui_status` uncomputed (NULL == fail-closed non-publishable),
      * computed uiStatus is NOT on the publication allowlist.

    The stored `verification_status` is the record-enum value for seeds
    (e.g. 'source_recorded'), so it is fed directly into compute_ui_status —
    NOT through the 11->6 registry map (which would reject a record-enum value).
    """
    rows = conn.execute(
        "SELECT source_id, verification_status, correction_status, source_changed, "
        "publication_state, ui_status, url, archive_url FROM sources"
    ).fetchall()
    detail: dict = {"name": "default_not_publishable", "passed": False, "rows": len(rows)}
    if not rows:
        detail["error"] = "no sources registered — nothing to assert"
        return detail
    offenders: list[dict] = []
    for row in rows:
        record = {
            "verificationStatus": row["verification_status"],
            "correctionStatus": row["correction_status"],
            "sourceChanged": bool(row["source_changed"]),
            "sourcePresent": bool(row["url"]),
            "archivePresent": bool(row["archive_url"]),
            "rawPreserved": False,
        }
        eligible = pub.is_publication_eligible(record)
        if (
            row["publication_state"] != pub.DEFAULT_PUBLICATION_STATE
            or row["ui_status"] is not None
            or eligible
        ):
            offenders.append({
                "source_id": row["source_id"],
                "publication_state": row["publication_state"],
                "ui_status": row["ui_status"],
                "computed_ui_status": pub.compute_ui_status(record),
                "publication_eligible": eligible,
            })
    if offenders:
        detail["error"] = f"{len(offenders)} record(s) not default-not-publishable"
        detail["offenders"] = offenders
    else:
        detail["passed"] = True
    return detail


def run_smoke(
    fixture: Path = DEFAULT_FIXTURE,
    sandbox: Path | None = None,
    *,
    strict: bool = False,
) -> dict:
    """Run the full first-slice integration smoke in a sandbox.

    `sandbox` is the throwaway root for both the DB (`<sandbox>/Database/...`)
    and the raw store (`<sandbox>/Raw-PDFs/...`). If None, a TemporaryDirectory
    is created and removed before returning. Returns a structured result:

        {ok: bool, doc_id, db_path, checks: [<invariant detail>, ...]}

    With `strict=True`, raises `SmokeFailure` if any invariant fails (used by
    the CLI for a loud non-zero exit).
    """
    fixture = Path(fixture)
    if not fixture.exists():
        raise FileNotFoundError(f"fixture not found: {fixture}")

    tmp_holder: tempfile.TemporaryDirectory | None = None
    if sandbox is None:
        tmp_holder = tempfile.TemporaryDirectory(prefix="gov77-slice1-smoke-")
        sandbox = Path(tmp_holder.name)
    sandbox = Path(sandbox)
    db_path = sandbox / "Database" / "slice1_smoke.db"

    try:
        # apply migrations -> seed Alpine registry (load() does both).
        si.load(db_path)
        with db.open_db(db_path) as conn:
            # ingest the sanitized fixture (source_id NULL on purpose)...
            doc_id = _ingest_fixture(conn, sandbox, fixture)
        # ...then reconcile so the registry back-fills source_id (provenance).
        si.load(db_path)
        with db.open_db(db_path) as conn:
            # log the deterministic Lane 1 run (1.04-f run log).
            rp.record_crawl_run(
                conn,
                started_utc=_now(),
                finished_utc=_now(),
                status="ok",
                source_set=["alpinewy_gov"],
                new_documents=1,
                lane="lane1_deterministic_ingest",
                notes="GOV-77 first-slice integration smoke",
            )
            checks = [
                _check_raw_preserved(conn, sandbox, doc_id),
                _check_provenance(conn, doc_id),
                _check_default_not_publishable(conn),
            ]
        result = {
            "ok": all(c["passed"] for c in checks),
            "doc_id": doc_id,
            "db_path": str(db_path),
            "checks": checks,
        }
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    if strict and not result["ok"]:
        failed = [c["name"] for c in result["checks"] if not c["passed"]]
        raise SmokeFailure(f"slice 1 integration smoke FAILED: {failed}")
    return result


def _print_report(result: dict) -> None:
    print("=== GOV-77 first-slice integration smoke (1.02/1.03/1.04/1.05) ===")
    print(f"sandbox db: {result['db_path']}  document id: {result['doc_id']}")
    for check in result["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        print(f"[{status}] {check['name']}")
        if check["name"] == "provenance_present":
            for k, v in check["fields"].items():
                print(f"         {k}: {v}")
        if not check["passed"]:
            print(f"         -> {check.get('error', 'invariant failed')}")
            if check.get("offenders"):
                for off in check["offenders"]:
                    print(f"         offender: {off}")
    print("=== RESULT:", "OK" if result["ok"] else "FAILED", "===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE,
                        help="sanitized Alpine fixture to ingest")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="sandbox dir (default: a removed TemporaryDirectory)")
    parser.add_argument("--keep", action="store_true",
                        help="with --workdir, keep the sandbox after the run")
    args = parser.parse_args(argv)

    sandbox = args.workdir
    if sandbox is not None:
        sandbox.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke(args.fixture, sandbox)
    except (FileNotFoundError, rp.RawPreservationError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    _print_report(result)
    if sandbox is not None and not args.keep:
        shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
