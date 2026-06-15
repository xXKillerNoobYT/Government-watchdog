"""GOV-192 batch-3 reviewer-internal promotion — 19 source-grounded Alpine AI rows.

Continues the GOV-146/GOV-162 promotion pattern under the GOV-166 standing
reviewer-internal policy (Isaac ACCEPTED: public bodies MAY be named; private
individuals stay gated). Promotes AI rows through the only sanctioned path
(:func:`ai_risk_gate.promote_statement`) under ``reviewer:isaac``, to the
conservative ``reviewed_source_linked`` status.

BINDING SCOPE
-------------
* 19 real Alpine AI statements, source-grounded (char_span evidence intact).
* Name-free; mayor-investigation excluded upstream; no PII flagged.
* Truthful label only: ``reviewed_source_linked`` (grounded-in-cited-source).
* Reviewer-internal / vault-only. ``promote_statement`` NEVER flips
  ``publication_state`` — rows stay ``not_publishable``.

COVERAGE
--------
* Claims span 7 transcripts from 2024-10-09 through 2026-06-02.
* Content: ordinance readings (2026-001 through 2026-00008), resolution
  adoptions (2026-005 through 2026-027), annexation agreements, budget items,
  moratorium scope, grant awards, and procedural records (quorum, agenda).
* Bodies named: Town Council, Planning and Zoning Board, Alpine Education
  Foundation, Alpine Fire District, Alpine Trails and Pathways (all public
  bodies/organizations — no private individuals).
* After promotion: corpus = 24 (batch-1+2) + 19 (batch-3) = 43 reviewer-
  internal rows / 0 public / vault-only.

Usage::

    python3 scripts/gov192_promotion_batch3.py --db Database/gov_watchdog.db
    python3 scripts/gov192_promotion_batch3.py --db Database/gov_watchdog.db --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_risk_gate as gate  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402

REVIEWER_ID = "reviewer:isaac"

BATCH3_STATEMENT_IDS: tuple[str, ...] = (
    "alpine_local_corpus:ai:00000517:0006",
    "alpine_local_corpus:ai:00000727:0007",
    "alpine_local_corpus:ai:00001865:0008",
    "alpine_local_corpus:ai:00002174:0009",
    "alpine_local_corpus:ai:00139862:0010",
    "alpine_local_corpus:ai:00251139:0000",
    "alpine_local_corpus:ai:00287190:0014",
    "alpine_local_corpus:ai:00374049:0002",
    "alpine_local_corpus:ai:00379840:0003",
    "alpine_local_corpus:ai:00384856:0004",
    "alpine_local_corpus:ai:00395665:0001",
    "alpine_local_corpus:ai:00405108:0005",
    "alpine_local_corpus:ai:00508420:0011",
    "alpine_local_corpus:ai:00982670:0012",
    "alpine_local_corpus:ai:00990262:0013",
    "alpine_local_corpus:ai:01822853:0018",
    "alpine_local_corpus:ai:01852411:0016",
    "alpine_local_corpus:ai:01867654:0015",
    "alpine_local_corpus:ai:01868033:0017",
)
TARGET_VERIFICATION_STATUS = "reviewed_source_linked"
DECISION = "approved"
REASON = (
    "GOV-192 batch-3 reviewer-internal promotion: source-grounded via char_span, "
    "name-free, PII-clean, no blocking risk flags, not mayor-investigation; "
    "conservative reviewed_source_linked (grounded-in-cited-source). Vault-only; "
    "stays not_publishable. Standing policy GOV-166."
)


class SeedError(RuntimeError):
    pass


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _preflight(conn: sqlite3.Connection) -> list[dict[str, str]]:
    if len(set(BATCH3_STATEMENT_IDS)) != len(BATCH3_STATEMENT_IDS):
        raise SeedError("manifest contains a duplicate statement id")
    if not gate.is_registered_reviewer(conn, REVIEWER_ID):
        raise SeedError(f"{REVIEWER_ID!r} is not a registered, active reviewer")

    plan: list[dict[str, str]] = []
    for sid in BATCH3_STATEMENT_IDS:
        row = conn.execute(
            "SELECT statement_id, statement_text, produced_by, verification_status "
            "FROM statements WHERE statement_id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            raise SeedError(f"statement {sid!r} does not resolve in this DB")
        if row["produced_by"] != "ai":
            raise SeedError(f"statement {sid!r} is produced_by={row['produced_by']!r}")
        if row["verification_status"] not in (None, "machine_extracted_unreviewed"):
            raise SeedError(
                f"statement {sid!r} already at verification_status="
                f"{row['verification_status']!r}; refusing to re-promote"
            )
        cm.assert_no_pii(row["statement_text"], f"{sid}.statement_text")
        for ev in conn.execute(
            "SELECT quoted_text FROM evidence_links WHERE from_node_id = ? "
            "AND from_node_type = 'statement'",
            (sid,),
        ):
            cm.assert_no_pii(ev["quoted_text"], f"{sid}.evidence.quoted_text")
        plan.append({"statement_id": sid, "from": row["verification_status"] or "None"})
    return plan


def _verify_post_state(conn: sqlite3.Connection) -> dict[str, object]:
    reviewer_served = read_api.reviewer_internal_records(conn)
    served_ids = {r["statement_id"] for r in reviewer_served}
    expected = set(BATCH3_STATEMENT_IDS)
    missing = expected - served_ids
    if missing:
        raise SeedError(f"reviewer-internal serve missing promoted rows: {sorted(missing)}")
    for rec in reviewer_served:
        if rec["statement_id"] not in expected:
            continue
        if rec.get("publication_state") != "not_publishable":
            raise SeedError(
                f"{rec['statement_id']}: publication_state={rec.get('publication_state')!r}"
            )

    public_served = read_api.published_records(conn)
    if public_served:
        raise SeedError(f"public lane served {len(public_served)} rows after promotion")

    body = read_api.build_response(conn, include_records=True, include_reviewer_internal=True)
    read_api.assert_no_raw_paths(body)
    return {
        "reviewer_internal_count": len(reviewer_served),
        "public_count": len(public_served),
        "batch3_served": sorted(served_ids & expected),
    }


def run(db_path: Path, *, apply: bool, log: list[str]) -> int:
    def emit(msg: str) -> None:
        log.append(msg)
        print(msg)

    emit(f"[{_now_utc_iso()}] GOV-192 batch-3 promotion — db={db_path} apply={apply}")
    with db.open_db(db_path) as conn:
        plan = _preflight(conn)
        emit(f"pre-flight OK: {len(plan)} target rows, reviewer={REVIEWER_ID}")
        for item in plan:
            emit(f"  PLAN promote {item['statement_id']} : {item['from']} -> {TARGET_VERIFICATION_STATUS}")

        if not apply:
            emit("DRY RUN — no write. Re-run with --apply to promote.")
            return 0

        results = []
        for sid in BATCH3_STATEMENT_IDS:
            res = gate.promote_statement(
                conn,
                sid,
                reviewer_id=REVIEWER_ID,
                decision=DECISION,
                reason=REASON,
                to_verification_status=TARGET_VERIFICATION_STATUS,
                commit=False,
            )
            results.append(res)
            emit(
                f"  PROMOTED {sid} : {res['from_verification_status']} -> "
                f"{res['to_verification_status']} (decision_id={res['decision_id']}, "
                f"publication_state={res['publication_state']})"
            )
        conn.commit()

        post = _verify_post_state(conn)
        emit(
            f"POST-STATE OK: reviewer_internal={post['reviewer_internal_count']} "
            f"public={post['public_count']} (transport sweep PASS)"
        )
        emit(f"batch3 served ids: {post['batch3_served']}")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOV-192 batch-3 promotion (19 rows).")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--log", type=Path, default=None)
    args = parser.parse_args(argv)

    log: list[str] = []
    try:
        rc = run(args.db, apply=args.apply, log=log)
    except (SeedError, gate.ReviewerGateError, cm.PiiGuardError) as exc:
        log.append(f"ABORTED: {type(exc).__name__}: {exc}")
        print(f"ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        rc = 2

    if args.apply or args.log is not None:
        log_path = args.log or (
            Path(__file__).resolve().parent.parent
            / "Logs"
            / f"gov192-promotion-{datetime.now(timezone.utc):%Y%m%d}.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log) + "\n")
        print(f"run-log: {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_main())
