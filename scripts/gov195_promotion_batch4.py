"""GOV-195 batch-4 reviewer-internal promotion — 22 source-grounded Alpine AI rows.

Continues the GOV-146/GOV-162/GOV-192 promotion pattern under the GOV-166
standing reviewer-internal policy (Isaac ACCEPTED: public bodies MAY be named;
private individuals stay gated). Promotes AI rows through the only sanctioned
path (:func:`ai_risk_gate.promote_statement`) under ``reviewer:isaac``, to the
conservative ``reviewed_source_linked`` status.

BINDING SCOPE
-------------
* 22 real Alpine AI statements, source-grounded (char_span evidence intact).
* Name-free; mayor-investigation excluded upstream; no PII flagged.
* Truthful label only: ``reviewed_source_linked`` (grounded-in-cited-source).
* Reviewer-internal / vault-only. ``promote_statement`` NEVER flips
  ``publication_state`` — rows stay ``not_publishable``.

COVERAGE
--------
* Claims span 8 transcripts from 2026-03-17 through 2026-05-12.
* Content: ordinance readings (2026-001 third reading, 2026-008/2026-005/
  2026-010 second readings), resolution adoptions (2026-022, 2026-019),
  budget work session, tourism board funding allocations (Trout Unlimited,
  Winter Jubilee, Reggae and the Rockies, Alpine Mountain Days, Fourth of
  July fireworks, snow groomer lease, bridge trail, trash receptacles,
  Alpine Fire District), building permits, grant reconciliation, and
  administrative fee ordinance discussions.
* Bodies named: Town Council, Planning and Zoning Board, Tourism Board,
  Alpine Fire District, Alpine Trails and Pathways, Trout Unlimited,
  Town of Alpine (all public bodies/organizations — no private individuals).
* After promotion: corpus = 43 (batch-1+2+3) + 22 (batch-4) = 65 reviewer-
  internal rows / 0 public / vault-only.

TRANSCRIPT COVERAGE (batch-4 new ground)
-----------------------------------------
* T6  (2026-03-17)  Town Council meeting — 3 claims (first coverage)
* T8  (2026-04-14)  P&Z meeting — 2 claims (first coverage)
* T11 (2026-04-22)  Tourism Board — 1 claim (first coverage)
* T14 (2026-05-05)  Town Council meeting — 5 claims (first coverage)
* T16 (2026-05-07)  Budget work session — 1 claim (first coverage)
* T17 (2026-05-07)  Tourism Board meeting — 9 claims (first coverage)
* T18 (2026-05-12)  P&Z meeting — 1 claim (first coverage)
Total: 7 previously uncovered transcripts now have claims.
Transcripts still uncovered after batch-4: T7, T9, T12, T13, T15,
  T19, T24, T25, T27 (mostly short or procedural).

Usage::

    python3 scripts/gov195_promotion_batch4.py --db Database/gov_watchdog.db
    python3 scripts/gov195_promotion_batch4.py --db Database/gov_watchdog.db --apply
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

BATCH4_STATEMENT_IDS: tuple[str, ...] = (
    "alpine_local_corpus:ai:00556841:0001",
    "alpine_local_corpus:ai:00570313:0002",
    "alpine_local_corpus:ai:00591211:0000",
    "alpine_local_corpus:ai:00754985:0003",
    "alpine_local_corpus:ai:00760353:0004",
    "alpine_local_corpus:ai:01068778:0005",
    "alpine_local_corpus:ai:01260769:0006",
    "alpine_local_corpus:ai:01263450:0007",
    "alpine_local_corpus:ai:01265413:0008",
    "alpine_local_corpus:ai:01275362:0009",
    "alpine_local_corpus:ai:01280970:0010",
    "alpine_local_corpus:ai:01285492:0011",
    "alpine_local_corpus:ai:01462493:0012",
    "alpine_local_corpus:ai:01472253:0016",
    "alpine_local_corpus:ai:01478203:0013",
    "alpine_local_corpus:ai:01491523:0014",
    "alpine_local_corpus:ai:01499210:0020",
    "alpine_local_corpus:ai:01499627:0015",
    "alpine_local_corpus:ai:01513578:0017",
    "alpine_local_corpus:ai:01513983:0018",
    "alpine_local_corpus:ai:01514299:0019",
    "alpine_local_corpus:ai:01539768:0021",
)
TARGET_VERIFICATION_STATUS = "reviewed_source_linked"
DECISION = "approved"
REASON = (
    "GOV-195 batch-4 reviewer-internal promotion: source-grounded via char_span, "
    "name-free, PII-clean, no blocking risk flags, not mayor-investigation; "
    "conservative reviewed_source_linked (grounded-in-cited-source). Vault-only; "
    "stays not_publishable. Standing policy GOV-166."
)


class SeedError(RuntimeError):
    pass


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _preflight(conn: sqlite3.Connection) -> list[dict[str, str]]:
    if len(set(BATCH4_STATEMENT_IDS)) != len(BATCH4_STATEMENT_IDS):
        raise SeedError("manifest contains a duplicate statement id")
    if not gate.is_registered_reviewer(conn, REVIEWER_ID):
        raise SeedError(f"{REVIEWER_ID!r} is not a registered, active reviewer")

    plan: list[dict[str, str]] = []
    for sid in BATCH4_STATEMENT_IDS:
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
    expected = set(BATCH4_STATEMENT_IDS)
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
        "batch4_served": sorted(served_ids & expected),
    }


def run(db_path: Path, *, apply: bool, log: list[str]) -> int:
    def emit(msg: str) -> None:
        log.append(msg)
        print(msg)

    emit(f"[{_now_utc_iso()}] GOV-195 batch-4 promotion — db={db_path} apply={apply}")
    with db.open_db(db_path) as conn:
        plan = _preflight(conn)
        emit(f"pre-flight OK: {len(plan)} target rows, reviewer={REVIEWER_ID}")
        for item in plan:
            emit(f"  PLAN promote {item['statement_id']} : {item['from']} -> {TARGET_VERIFICATION_STATUS}")

        if not apply:
            emit("DRY RUN — no write. Re-run with --apply to promote.")
            return 0

        results = []
        for sid in BATCH4_STATEMENT_IDS:
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
        emit(f"batch4 served ids: {post['batch4_served']}")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOV-195 batch-4 promotion (22 rows).")
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
            / f"gov195-promotion-{datetime.now(timezone.utc):%Y%m%d}.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log) + "\n")
        print(f"run-log: {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_main())
