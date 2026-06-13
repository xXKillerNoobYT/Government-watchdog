"""Owner-authorized Option-A promotion seed — exactly 6 reviewer-internal rows (GOV-146).

Executes the owner-authorized Option-A promotion seed decided in GOV-144 (Isaac
accepted ``request_confirmation 1ad7e292`` 2026-06-13T00:26:20Z) and finally
pre-write-confirmed in GOV-146 (Isaac accepted card ``89318528`` 00:48:56Z over
the concrete ≤6 manifest). It promotes EXACTLY the six confirmed real Alpine AI
statements — nothing added or substituted — through the only sanctioned path,
:func:`ai_risk_gate.promote_statement`, under ``reviewer:isaac``, so the
reviewer-internal read serve (:func:`read_api.reviewer_internal_records`) returns
a non-empty real reviewed set ahead of GOV-129's frontend render.

BINDING SCOPE (from Isaac's accepted Option A — do not widen here)
------------------------------------------------------------------
* ≤ 6 real Alpine statements, source-grounded (char_span evidence intact).
* Name-free; mayor-investigation excluded; the GOV-140 Flag-1 PII row excluded.
* Truthful label only: ``verified`` -> the conservative reviewed record status
  ``reviewed_source_linked`` (grounded-in-cited-source, NOT ``human_verified``).
  No fabricated ``disputed``/``corrected`` rows.
* Reviewer-internal / vault-only. ``promote_statement`` NEVER flips
  ``publication_state`` — the rows stay ``not_publishable`` and no public surface
  serves them (``read_api.published_records`` still returns 0).

OPERATIONAL / VAULT-ONLY (1.11 §2.1; AI_GATEWAY §7.1)
-----------------------------------------------------
A local promotion runner, NOT a web/API surface. It writes only the
``reviewer_decisions`` audit ledger + the promoted rows' reviewer-state columns
in the local vault DB (``db.DEFAULT_DB_PATH`` unless ``--db`` overrides). It adds
no schema, widens no web-safe allowlist. The DB is git-ignored/ephemeral; this
SCRIPT is the durable, re-runnable deliverable (the GOV-135 seed precedent).

Fail-closed guarantees
----------------------
* Refuses unless ``reviewer:isaac`` is a registered, active reviewer (the GOV-135
  seed); the Lane-5 gate would reject anyway, this just fails earlier + clearer.
* Re-verifies each target is a ``produced_by='ai'`` row currently at the
  machine-extracted default (never re-promotes an already-reviewed/foreign row),
  and re-runs :func:`concept_map.assert_no_pii` on the promoted ``statement_text``
  and its evidence ``quoted_text`` (belt-and-braces over the manifest's pass).
* Promotes inside ONE transaction; on any error nothing commits.
* After apply, asserts the post-state: reviewer-internal serve == the 6 promoted
  rows (each ``source-backed`` + ``not_publishable``), public serve == 0, and the
  whole reviewer-internal response body passes the transport sweep.

Usage::

    # dry-run (default): print the plan + current state, write nothing
    python3 scripts/gov146_promotion_seed.py --db Database/gov_watchdog.db
    # apply the promotion
    python3 scripts/gov146_promotion_seed.py --db Database/gov_watchdog.db --apply
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
import publication as pub  # noqa: E402
import read_api  # noqa: E402

# The reviewer the GOV-135 seed registered (the sole Stage-1 reviewer, vault-only).
REVIEWER_ID = "reviewer:isaac"

# The EXACT six confirmed statement ids (Isaac's accepted card 89318528 / CEO
# execution mandate 789a5674). Order is the manifest order. Promotion target for
# every row is the conservative reviewed status reviewed_source_linked.
CONFIRMED_STATEMENT_IDS: tuple[str, ...] = (
    "alpine_local_corpus:ai:00000064:0021",  # 1 Special Town Council mtg Oct 9 2024 ~7:01pm
    "alpine_local_corpus:ai:01617859:0008",  # 2 mill levy 5 mills
    "alpine_local_corpus:ai:01661553:0010",  # 3 water system shutdown May 21 2026 (main break)
    "alpine_local_corpus:ai:01664750:0013",  # 4 Budget Work Session Thu Jun 11 2026 2pm
    "alpine_local_corpus:ai:01819080:0017",  # 5 bacteriological testing confirmed safe water
    "alpine_local_corpus:ai:01821771:0027",  # 6 council took no action in executive session
)
TARGET_VERIFICATION_STATUS = "reviewed_source_linked"
DECISION = "approved"
REASON = (
    "GOV-146 owner-authorized Option-A reviewer-internal seed: source-grounded, "
    "name-free, PII-clean, not mayor-investigation; conservative reviewed_source_linked "
    "(grounded-in-cited-source). Vault-only; stays not_publishable."
)


class SeedError(RuntimeError):
    """A pre-flight invariant failed; nothing was written."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _preflight(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Validate every binding invariant BEFORE any write. Returns the per-row plan."""
    if len(CONFIRMED_STATEMENT_IDS) > 6:
        raise SeedError(f"manifest has {len(CONFIRMED_STATEMENT_IDS)} rows; the owner gate caps at 6")
    if len(set(CONFIRMED_STATEMENT_IDS)) != len(CONFIRMED_STATEMENT_IDS):
        raise SeedError("manifest contains a duplicate statement id")
    if not gate.is_registered_reviewer(conn, REVIEWER_ID):
        raise SeedError(
            f"{REVIEWER_ID!r} is not a registered, active reviewer; run the GOV-135 "
            "reviewer-registry seed against this DB first"
        )

    plan: list[dict[str, str]] = []
    for sid in CONFIRMED_STATEMENT_IDS:
        row = conn.execute(
            "SELECT statement_id, statement_text, produced_by, verification_status "
            "FROM statements WHERE statement_id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            raise SeedError(f"statement {sid!r} does not resolve in this DB")
        if row["produced_by"] != "ai":
            raise SeedError(f"statement {sid!r} is produced_by={row['produced_by']!r}, expected 'ai'")
        if row["verification_status"] not in (None, "machine_extracted_unreviewed"):
            raise SeedError(
                f"statement {sid!r} already at verification_status="
                f"{row['verification_status']!r}; refusing to re-promote"
            )
        # belt-and-braces PII re-check on the verbatim-projected fields (manifest already passed).
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
    """Assert the serve invariants after promotion. Raises on any violation."""
    reviewer_served = read_api.reviewer_internal_records(conn)
    served_ids = {r["statement_id"] for r in reviewer_served}
    expected = set(CONFIRMED_STATEMENT_IDS)
    missing = expected - served_ids
    if missing:
        raise SeedError(f"reviewer-internal serve missing promoted rows: {sorted(missing)}")
    for rec in reviewer_served:
        if rec["statement_id"] not in expected:
            continue
        if rec.get("ui_status") != "source-backed":
            raise SeedError(f"{rec['statement_id']}: ui_status={rec.get('ui_status')!r}, expected source-backed")
        if rec.get("publication_state") != "not_publishable":
            raise SeedError(
                f"{rec['statement_id']}: publication_state={rec.get('publication_state')!r}, "
                "expected not_publishable"
            )
        if rec.get("verification_status") != TARGET_VERIFICATION_STATUS:
            raise SeedError(f"{rec['statement_id']}: verification_status drifted")

    public_served = read_api.published_records(conn)
    if public_served:
        raise SeedError(
            f"public lane served {len(public_served)} rows; reviewer-internal seed must NOT publish"
        )

    # Whole reviewer-internal body must pass the (file://-aware) transport sweep.
    body = read_api.build_response(conn, include_records=True, include_reviewer_internal=True)
    read_api.assert_no_raw_paths(body)
    return {
        "reviewer_internal_count": len(reviewer_served),
        "public_count": len(public_served),
        "served_ids": sorted(served_ids & expected),
    }


def run(db_path: Path, *, apply: bool, log: list[str]) -> int:
    def emit(msg: str) -> None:
        log.append(msg)
        print(msg)

    emit(f"[{_now_utc_iso()}] GOV-146 promotion seed — db={db_path} apply={apply}")
    with db.open_db(db_path) as conn:
        plan = _preflight(conn)
        emit(f"pre-flight OK: {len(plan)} target rows, reviewer={REVIEWER_ID}")
        for item in plan:
            emit(f"  PLAN promote {item['statement_id']} : {item['from']} -> {TARGET_VERIFICATION_STATUS}")

        if not apply:
            emit("DRY RUN — no write. Re-run with --apply to promote.")
            return 0

        results = []
        for sid in CONFIRMED_STATEMENT_IDS:
            res = gate.promote_statement(
                conn,
                sid,
                reviewer_id=REVIEWER_ID,
                decision=DECISION,
                reason=REASON,
                to_verification_status=TARGET_VERIFICATION_STATUS,
                # run_id defaults to the statement's producing ai_extraction_run
                # (the decision ledger's run_id FKs ai_extraction_runs).
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
        emit(f"served reviewer-internal ids: {post['served_ids']}")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOV-146 owner-authorized 6-row promotion seed.")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true", help="apply the promotion (default: dry-run)")
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="optional run-log path (defaults to Logs/gov146-promotion-<UTCdate>.log)",
    )
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
            / f"gov146-promotion-{datetime.now(timezone.utc):%Y%m%d}.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log) + "\n")
        print(f"run-log: {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_main())
