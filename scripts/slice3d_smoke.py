"""Slice 3 D end-to-end Lane-4 risk + Lane-5 reviewer-gate smoke (GOV-91).

Integration of the AI gateway **Lane 4** (risk layer) and **Lane 5** (runtime
reviewer-gate) over a real Lane-2 output. Source: GOV-88 interface design
(Docs/stage3-ai-gateway-gap-analysis.md §4.3 L4-5, §4.4 L5-1/L5-5), contracts
1.09/1.11, AI_GATEWAY_PROCESSING_WORKFLOW.md lanes 4 + 5. This is the single
end-to-end smoke proving Lane 4/5 hold their invariants against a real (sanitized)
Alpine fixture — not just in unit isolation.

It performs a real, OFFLINE + DETERMINISTIC:

    apply migrations  ->  reuse the Slice-1-seeded Alpine source registry  ->
    load the sanitized 2026-05-08 WWTP-financing fixture as meeting + transcript
    ->  deterministically segment it (Lane-1)  ->  run a Lane-2 AI extraction
    (injected proposer: one grounded paraphrase + one accusation claim)  ->
    run a Lane-4 risk screen over those AI rows  ->  exercise the Lane-5
    reviewer-gate  ->  assert the Lane-4/5 invariants.

There is NO live model: the Lane-2 proposer is a fixed stand-in and the Lane-4
screen is deterministic rule-matching.

Asserted invariants (GOV-91 acceptance + Slice-3 AI gates):
  1. RISK FLAGS RECORDED — the accusation claim carries a `legal` no-go flag; the
     Lane-4 run lands flags on ai_risk_flags (1.11 §4 / AI_GATEWAY lane 4).
  2. NO GATING WRITE — the statements digest is byte-identical pre/post Lane 4
     (the risk layer flags beside the claim, never on it).
  3. REVIEWER-GATE REJECTS — promoting an AI row WITHOUT a reviewer decision is
     rejected; nothing is written, the claim stays machine_extracted_unreviewed.
  4. FAILED RUN BLOCKS DOWNSTREAM — a claim from a failed Lane-2 run cannot be
     promoted (AI_GATEWAY "failed gateway processing must block downstream").
  5. PROMOTION NEVER PUBLISHES — a valid human promotion moves the clean claim to
     a reviewed status + records an audit row, but publication_state stays
     not_publishable (owner gate P8): nothing AI-written is publishable by default.
  6. GATEWAY RUN-LOG — the Lane-4 run is on ai_extraction_runs with lane='4_risk'
     + input set / tool version / errors / reviewer / timing.

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): only the sanitized fixture under
tests/fixtures/alpine/ is read; the flags + decisions + run ledger are written to
a throwaway sandbox DB and never published.

Usage:
    python scripts/slice3d_smoke.py [--fixture PATH] [--keep] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as rg  # noqa: E402
import db  # noqa: E402
import segment_transcript as seg  # noqa: E402
import source_inventory as si  # noqa: E402
import statements as stmt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
)

_MEETING_DATE = "2026-05-08"
_VIDEO_SOURCE_ID = "alpine_youtube_channel"   # Slice-1 seed (source_inventory.py)
_TRANSCRIPT_VAULT_PATH = "Transcripts/2026/alpine-2026-05-08-regular.json"  # synthetic
_LANE2_RUN = "alpine:ai-extract:2026-05-08:3d-smoke"
_LANE2_FAIL_RUN = "alpine:ai-extract:2026-05-08:3d-smoke-fail"
_LANE4_RUN = "alpine:ai-risk:2026-05-08:3d-smoke"

_CLEAN_ID = "alpine:ai:3d:financing-clean"
_LEGAL_ID = "alpine:ai:3d:accusation"
_FAILED_ID = "alpine:ai:3d:from-failed-run"

_FINANCING_SEGMENT_INDEX = 3   # "...financing gap ... treatment plant project..."
_CONTINUED_SEGMENT_INDEX = 6   # "...item is continued to the next regular meeting..."

_REVIEWER = "reviewer:isaac"

_STATEMENT_COLS = (
    "statement_id", "segment_id", "statement_text", "produced_by", "verification_status",
    "correction_status", "review_state", "publication_state", "source_changed",
    "ui_status", "confidence", "ai_extraction_run_id",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SmokeFailure(AssertionError):
    """Raised by run_smoke(strict=True) when any Lane-4/5 invariant regresses."""


def _load_meeting_and_transcript(conn, fixture: dict) -> int:
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, channel_id, channel_title, "
        "upload_date, meeting_date, duration_seconds, language, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], meta.get("channel_id"),
            meta.get("channel_title"), meta.get("upload_date"), _MEETING_DATE,
            meta.get("duration_seconds"), tr.get("language"), tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            _TRANSCRIPT_VAULT_PATH, "0" * 64, _now(), None,
        ),
    )
    transcript_id = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO meetings (meeting_date, body, title, transcript_id, fetch_time_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        (_MEETING_DATE, "Alpine Town Council", "Regular Meeting", transcript_id, _now()),
    )
    conn.commit()
    return transcript_id


def _segment_for(records: list[dict], index: int) -> dict:
    for rec in records:
        if rec["segment_index"] == index:
            return rec
    raise SmokeFailure(f"fixture produced no segment at index {index}")


def _pointer(segment: dict, video_url: str) -> dict:
    return {
        "to_source_id": _VIDEO_SOURCE_ID,
        "relation": "references",
        "locator_kind": "timestamp",
        "timestamp_seconds": segment["timestamp_seconds"],
        "timestamp_human": segment["timestamp_human"],
        "original_url": video_url,
        "archive_status": "not_checked",
        "scan_date": _MEETING_DATE,
        "captured_at_utc": _now(),
        "verification_status": "machine_extracted_unreviewed",
        "confidence": "medium",
        "transcript_path": _TRANSCRIPT_VAULT_PATH,  # vault-only provenance
    }


def _build_claims(segments: list[dict], video_url: str) -> list[dict]:
    fin = _segment_for(segments, _FINANCING_SEGMENT_INDEX)
    cont = _segment_for(segments, _CONTINUED_SEGMENT_INDEX)
    return [
        {
            "statement_id": _CLEAN_ID,
            "segment_id": fin["segment_id"],
            "statement_text": "AI paraphrase: staff reports the financing gap for the treatment plant project.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(fin, video_url)],
        },
        {
            # Anchored (no orphan), but the text is an ACCUSATION about a named
            # individual — a legal no-go (1.11 §4.1) the risk layer must flag.
            "statement_id": _LEGAL_ID,
            "segment_id": cont["segment_id"],
            "statement_text": "AI paraphrase: the mayor committed fraud by forcing the financing vote.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(cont, video_url)],
        },
    ]


def _statements_digest(conn) -> tuple[int, str]:
    rows = conn.execute(
        f"SELECT {', '.join(_STATEMENT_COLS)} FROM statements ORDER BY statement_id"
    ).fetchall()
    payload = json.dumps([[r[c] for c in _STATEMENT_COLS] for r in rows],
                         sort_keys=True, default=str)
    return len(rows), hashlib.sha256(payload.encode()).hexdigest()


# --- invariant checks ------------------------------------------------------

def _check_risk_flags(conn, result: dict) -> dict:
    detail: dict = {"name": "risk_flags_recorded", "passed": False}
    legal = conn.execute(
        f"SELECT COUNT(*) FROM {rg.RISK_TABLE} WHERE statement_id = ? "
        "AND risk_category = 'legal' AND severity = 'no_go'",
        (_LEGAL_ID,),
    ).fetchone()[0]
    total = conn.execute(f"SELECT COUNT(*) FROM {rg.RISK_TABLE}").fetchone()[0]
    detail.update({"legal_no_go_flags": legal, "total_flags": total,
                   "flag_count": result["flag_count"]})
    if legal < 1:
        detail["error"] = "accusation claim did not get a legal no-go flag"
    elif total < 1:
        detail["error"] = "no risk flags recorded"
    else:
        detail["passed"] = True
    return detail


def _check_no_gating_write(pre: tuple[int, str], post: tuple[int, str]) -> dict:
    detail: dict = {"name": "no_gating_write", "passed": False,
                    "pre": pre[1][:12], "post": post[1][:12]}
    if pre != post:
        detail["error"] = "Lane 4 mutated a statements gating field (digest changed)"
    else:
        detail["passed"] = True
    return detail


def _check_reviewer_gate_rejects(conn) -> dict:
    detail: dict = {"name": "reviewer_gate_rejects_unreviewed", "passed": False}
    rejected = False
    try:
        rg.promote_statement(
            conn, _CLEAN_ID, reviewer_id="", decision="approved",
            to_verification_status="reviewed_source_linked", reason="no reviewer",
        )
    except rg.ReviewerGateError:
        rejected = True
    row = conn.execute(
        "SELECT verification_status FROM statements WHERE statement_id = ?", (_CLEAN_ID,)
    ).fetchone()
    still_unreviewed = row["verification_status"] == "machine_extracted_unreviewed"
    no_decision = rg.latest_decision(conn, _CLEAN_ID) is None
    detail.update({"rejected": rejected, "still_unreviewed": still_unreviewed,
                   "no_decision_row": no_decision})
    if not (rejected and still_unreviewed and no_decision):
        detail["error"] = "promotion without a reviewer was not fully rejected"
    else:
        detail["passed"] = True
    return detail


def _check_failed_run_blocks(conn) -> dict:
    detail: dict = {"name": "failed_run_blocks_downstream", "passed": False}
    blocked = False
    try:
        rg.promote_statement(
            conn, _FAILED_ID, reviewer_id=_REVIEWER, decision="approved",
            to_verification_status="reviewed_source_linked",
            reason="attempt to promote a failed-run row",
        )
    except rg.ReviewerGateError:
        blocked = True
    detail["blocked"] = blocked
    if not blocked:
        detail["error"] = "a claim from a failed Lane-2 run was promotable"
    else:
        detail["passed"] = True
    return detail


def _check_promotion_never_publishes(conn) -> dict:
    detail: dict = {"name": "promotion_never_publishes", "passed": False}
    out = rg.promote_statement(
        conn, _CLEAN_ID, reviewer_id=_REVIEWER, decision="approved",
        to_verification_status="reviewed_source_linked",
        reason="paraphrase grounded in the financing segment", reason_category="source_match",
    )
    row = conn.execute(
        "SELECT verification_status, review_state, publication_state FROM statements "
        "WHERE statement_id = ?", (_CLEAN_ID,)
    ).fetchone()
    dec = rg.latest_decision(conn, _CLEAN_ID)
    detail.update({
        "verification_status": row["verification_status"],
        "review_state": row["review_state"],
        "publication_state": row["publication_state"],
        "audit_decision": dec["decision"] if dec else None,
    })
    if row["verification_status"] != "reviewed_source_linked":
        detail["error"] = "valid promotion did not reach reviewed_source_linked"
    elif row["publication_state"] != "not_publishable":
        detail["error"] = "promotion flipped publication_state (must stay owner-gated)"
    elif not (dec and dec["promoted"] == 1):
        detail["error"] = "no promoting audit decision recorded"
    else:
        detail["passed"] = True
    return detail


def _check_run_log(conn) -> dict:
    detail: dict = {"name": "gateway_run_log", "passed": False}
    run = ai.get_run(conn, _LANE4_RUN)
    ok = (
        run["lane"] == "4_risk"
        and _VIDEO_SOURCE_ID in json.loads(run["input_source_ids"])
        and run["tool_version"]
        and run["model_name"] is None        # deterministic — no model
        and run["error_status"] in ai.ALLOWED_RUN_ERROR_STATUS
        and run["reviewer_state"] in ai.ALLOWED_RUN_REVIEWER_STATE
        and run["started_utc"] and run["finished_utc"]
    )
    detail.update({
        "lane": run["lane"], "error_status": run["error_status"],
        "reviewer_state": run["reviewer_state"], "output_count": run["output_count"],
    })
    if not ok:
        detail["error"] = "Lane-4 run-log missing a required field"
    else:
        detail["passed"] = True
    return detail


def run_smoke(fixture: Path = DEFAULT_FIXTURE, sandbox: Path | None = None,
              *, strict: bool = False) -> dict:
    fixture = Path(fixture)
    if not fixture.exists():
        raise FileNotFoundError(f"fixture not found: {fixture}")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))

    tmp_holder: tempfile.TemporaryDirectory | None = None
    if sandbox is None:
        tmp_holder = tempfile.TemporaryDirectory(prefix="gov91-slice3d-smoke-")
        sandbox = Path(tmp_holder.name)
    sandbox = Path(sandbox)
    db_path = sandbox / "Database" / "slice3d_smoke.db"

    try:
        si.load(db_path)  # migrate + seed Slice-1 Alpine registry
        with db.open_db(db_path) as conn:
            transcript_id = _load_meeting_and_transcript(conn, fixture_data)
        si.load(db_path)  # reconcile transcript.source_id from the registry
        with db.open_db(db_path) as conn:
            video_url = conn.execute(
                "SELECT video_url FROM transcripts WHERE id = ?", (transcript_id,)
            ).fetchone()["video_url"]
            segments = seg.segment_transcript(conn, transcript_id)

            # Lane 2: write the AI rows Lane 4/5 will act on.
            claims = _build_claims(segments, video_url)
            anchored_seg_ids = [c["segment_id"] for c in claims]
            lane2 = ai.run_extraction(
                conn, run_id=_LANE2_RUN, input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids,
                proposer=lambda c, s, sg: claims,
                tool_version="gov-lane2-3d-smoke@local",
                model_name="offline-deterministic", model_version="smoke",
            )
            written = lane2["written_statements"]

            # A failed Lane-2 run + a row that names it as its producer (for the
            # failed-run downstream-block check).
            def _boom(c, s, sg):
                raise RuntimeError("offline provider unavailable (smoke)")
            ai.run_extraction(
                conn, run_id=_LANE2_FAIL_RUN, input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids, proposer=_boom,
                tool_version="gov-lane2-3d-smoke@local",
            )
            fin = _segment_for(segments, _FINANCING_SEGMENT_INDEX)
            stmt.insert_statement(
                conn,
                {
                    "statement_id": _FAILED_ID, "segment_id": fin["segment_id"],
                    "statement_text": "AI paraphrase: a claim produced by a failed gateway run.",
                    "is_verbatim": 0, "produced_by": "ai", "confidence": "low",
                    "ai_extraction_run_id": _LANE2_FAIL_RUN,
                },
                [_pointer(fin, video_url)],
            )

            pre_digest = _statements_digest(conn)

            # Lane 4: screen the AI rows; assert it flags but never promotes.
            risk = rg.run_risk(
                conn, run_id=_LANE4_RUN, input_statement_ids=written,
                input_source_ids=[_VIDEO_SOURCE_ID], input_segment_ids=anchored_seg_ids,
                tool_version="gov-lane4-3d-smoke@local", dry_run=True,
            )
            post_digest = _statements_digest(conn)

            # GOV-93: the Lane-5 gate is now an allowlist — register the human
            # reviewer the positive-path checks promote with. Empty/sentinel ids
            # stay rejected (they are never registered), so the reject check holds.
            rg.register_reviewer(
                conn, _REVIEWER,
                display_name="Isaac (smoke reviewer)", registered_by="slice3d-smoke",
            )

            checks = [
                _check_risk_flags(conn, risk),
                _check_no_gating_write(pre_digest, post_digest),
                _check_run_log(conn),
                # Lane 5 reviewer-gate (order matters: reject, then failed-run,
                # then a valid promotion of the clean row).
                _check_reviewer_gate_rejects(conn),
                _check_failed_run_blocks(conn),
                _check_promotion_never_publishes(conn),
            ]
        outcome = {
            "ok": all(c["passed"] for c in checks),
            "transcript_id": transcript_id,
            "segment_count": len(segments),
            "lane2_run": _LANE2_RUN,
            "lane4_run": _LANE4_RUN,
            "written_count": len(written),
            "flag_count": risk["flag_count"],
            "error_status": risk["error_status"],
            "db_path": str(db_path),
            "checks": checks,
        }
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    if strict and not outcome["ok"]:
        failed = [c["name"] for c in outcome["checks"] if not c["passed"]]
        raise SmokeFailure(f"slice 3 D Lane-4/5 risk+reviewer-gate smoke FAILED: {failed}")
    return outcome


def _print_report(result: dict) -> None:
    print("=== GOV-91 Slice 3 D Lane-4 risk + Lane-5 reviewer-gate smoke (1.09/1.11) ===")
    print(
        f"sandbox db: {result['db_path']}  transcript={result['transcript_id']} "
        f"segments={result['segment_count']}"
    )
    print(
        f"lane2={result['lane2_run']} written={result['written_count']}  "
        f"lane4={result['lane4_run']} flags={result['flag_count']} "
        f"error_status={result['error_status']}"
    )
    for check in result["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        print(f"[{status}] {check['name']}")
        if not check["passed"]:
            print(f"         -> {check.get('error', 'invariant failed')}")
    print("=== RESULT:", "OK" if result["ok"] else "FAILED", "===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    sandbox = args.workdir
    if sandbox is not None:
        sandbox.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke(args.fixture, sandbox)
    except FileNotFoundError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    _print_report(result)
    if sandbox is not None and not args.keep:
        shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
