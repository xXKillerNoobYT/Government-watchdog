"""Slice 3 C end-to-end Lane-3 verification smoke (GOV-90, Stage 1 Slice 3 C).

Integration of the AI gateway **Lane 3** (verification layer) over a real Lane-2
output. Source: GOV-88 interface design (Docs/stage3-ai-gateway-gap-analysis.md
§4.2), contracts 1.09/1.11, AI_GATEWAY_PROCESSING_WORKFLOW.md lane 3. This is the
single end-to-end smoke proving Lane 3 holds its invariants against a real
(sanitized) Alpine fixture — not just in unit isolation.

It performs a real, OFFLINE + DETERMINISTIC:

    apply migrations  ->  reuse the Slice-1-seeded Alpine source registry  ->
    load the sanitized 2026-05-08 WWTP-financing fixture as meeting + transcript
    ->  deterministically segment it (Lane-1)  ->  run a Lane-2 AI extraction
    (injected proposer: one well-grounded paraphrase + one off-source claim)  ->
    run a Lane-3 verification over those AI rows  ->  assert the Lane-3 invariants.

There is NO live model: the Lane-2 proposer is a fixed source-grounded stand-in
and the Lane-3 compare is deterministic token-grounding.

Asserted invariants (GOV-90 acceptance + Slice-3 AI gates):
  1. LABELS ASSIGNED — every AI statement gets a verdict row; the grounded claim
     is `source_match`, the off-source claim is `source_mismatch` + contested.
  2. NEVER PROMOTED — after Lane 3, every AI row is still
     machine_extracted_unreviewed + not_publishable (a mismatch flags, never promotes).
  3. NO GATING WRITE — the statements digest is byte-identical pre/post Lane 3
     (Lane 3 writes its verdict beside the claim, never on it).
  4. GATEWAY RUN-LOG — the Lane-3 run is on ai_extraction_runs with
     lane='3_verification' + input set / tool version / errors / reviewer / timing.
  5. FAIL-CLOSED DOWNSTREAM — every verdict blocks publication except a
     source_match a human separately approved.

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): only the sanitized fixture under
tests/fixtures/alpine/ is read; the verdicts + run ledger are written to a
throwaway sandbox DB and never published.

Usage:
    python scripts/slice3c_smoke.py [--fixture PATH] [--keep] [--workdir DIR]
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
import ai_verification as av  # noqa: E402
import db  # noqa: E402
import segment_transcript as seg  # noqa: E402
import source_inventory as si  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
)

_MEETING_DATE = "2026-05-08"
_VIDEO_SOURCE_ID = "alpine_youtube_channel"   # Slice-1 seed (source_inventory.py)
_TRANSCRIPT_VAULT_PATH = "Transcripts/2026/alpine-2026-05-08-regular.json"  # synthetic
_LANE2_RUN = "alpine:ai-extract:2026-05-08:3c-smoke"
_LANE3_RUN = "alpine:ai-verify:2026-05-08:3c-smoke"

_MATCH_ID = "alpine:ai:3c:financing-match"
_MISMATCH_ID = "alpine:ai:3c:offsource-mismatch"

_FINANCING_SEGMENT_INDEX = 3   # "...financing gap ... treatment plant project..."
_CONTINUED_SEGMENT_INDEX = 6   # "...item is continued to the next regular meeting..."

_STATEMENT_COLS = (
    "statement_id", "segment_id", "statement_text", "produced_by", "verification_status",
    "correction_status", "review_state", "publication_state", "source_changed",
    "ui_status", "confidence", "ai_extraction_run_id",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SmokeFailure(AssertionError):
    """Raised by run_smoke(strict=True) when any Lane-3 invariant regresses."""


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
            "statement_id": _MATCH_ID,
            "segment_id": fin["segment_id"],
            "statement_text": "AI paraphrase: staff reports the financing gap for the treatment plant project.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(fin, video_url)],
        },
        {
            # Anchored (no orphan) but the text is NOT supported by the segment.
            "statement_id": _MISMATCH_ID,
            "segment_id": cont["segment_id"],
            "statement_text": "AI paraphrase: the mayor approved a new downtown park construction budget.",
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

def _check_labels(conn, result: dict) -> dict:
    detail: dict = {"name": "labels_assigned", "passed": False}
    verdicts = {v["statement_id"]: v["verdict"] for v in result["verdicts"]}
    detail["verdicts"] = verdicts
    rows = conn.execute(
        f"SELECT statement_id, verdict, contested FROM {av.RESULTS_TABLE}"
    ).fetchall()
    detail["result_rows"] = len(rows)
    if verdicts.get(_MATCH_ID) != "source_match":
        detail["error"] = f"grounded claim verdict={verdicts.get(_MATCH_ID)!r} (want source_match)"
    elif verdicts.get(_MISMATCH_ID) != "source_mismatch":
        detail["error"] = f"off-source claim verdict={verdicts.get(_MISMATCH_ID)!r} (want source_mismatch)"
    elif len(rows) != 2:
        detail["error"] = f"expected 2 verdict rows, found {len(rows)}"
    else:
        detail["passed"] = True
    return detail


def _check_not_promoted(conn) -> dict:
    detail: dict = {"name": "never_promoted", "passed": False}
    rows = conn.execute(
        "SELECT statement_id, verification_status, publication_state FROM statements"
    ).fetchall()
    offenders = [
        dict(r) for r in rows
        if not (r["verification_status"] == "machine_extracted_unreviewed"
                and r["publication_state"] == "not_publishable")
    ]
    detail["rows"] = len(rows)
    if offenders:
        detail["error"] = f"{len(offenders)} AI row(s) promoted by Lane 3"
        detail["offenders"] = offenders
    else:
        detail["passed"] = True
    return detail


def _check_no_gating_write(pre: tuple[int, str], post: tuple[int, str]) -> dict:
    detail: dict = {"name": "no_gating_write", "passed": False,
                    "pre": pre[1][:12], "post": post[1][:12]}
    if pre != post:
        detail["error"] = "Lane 3 mutated a statements gating field (digest changed)"
    else:
        detail["passed"] = True
    return detail


def _check_run_log(conn) -> dict:
    detail: dict = {"name": "gateway_run_log", "passed": False}
    run = ai.get_run(conn, _LANE3_RUN)
    ok = (
        run["lane"] == "3_verification"
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
        detail["error"] = "Lane-3 run-log missing a required field"
    else:
        detail["passed"] = True
    return detail


def _check_failclosed(conn) -> dict:
    detail: dict = {"name": "failclosed_downstream", "passed": False}
    match = av.latest_verdict(conn, _MATCH_ID)
    mismatch = av.latest_verdict(conn, _MISMATCH_ID)
    match_blocked_unapproved = av.verification_blocks_publication(match)
    match_unblocks_approved = not av.verification_blocks_publication(match, human_approved=True)
    mismatch_blocked = av.verification_blocks_publication(mismatch, human_approved=True)
    none_blocked = av.verification_blocks_publication(None)
    detail.update({
        "match_blocked_unapproved": match_blocked_unapproved,
        "match_unblocks_approved": match_unblocks_approved,
        "mismatch_blocked": mismatch_blocked, "none_blocked": none_blocked,
    })
    if not (match_blocked_unapproved and match_unblocks_approved
            and mismatch_blocked and none_blocked):
        detail["error"] = "fail-closed downstream gate did not hold"
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
        tmp_holder = tempfile.TemporaryDirectory(prefix="gov90-slice3c-smoke-")
        sandbox = Path(tmp_holder.name)
    sandbox = Path(sandbox)
    db_path = sandbox / "Database" / "slice3c_smoke.db"

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

            # Lane 2: write the AI rows Lane 3 will verify.
            claims = _build_claims(segments, video_url)
            anchored_seg_ids = [c["segment_id"] for c in claims]
            lane2 = ai.run_extraction(
                conn, run_id=_LANE2_RUN, input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids,
                proposer=lambda c, s, sg: claims,
                tool_version="gov-lane2-3c-smoke@local",
                model_name="offline-deterministic", model_version="smoke",
            )
            written = lane2["written_statements"]

            pre_digest = _statements_digest(conn)

            # Lane 3: verify the AI rows; assert it flags but never promotes.
            result = av.run_verification(
                conn, run_id=_LANE3_RUN, input_statement_ids=written,
                input_source_ids=[_VIDEO_SOURCE_ID], input_segment_ids=anchored_seg_ids,
                tool_version="gov-lane3-3c-smoke@local", dry_run=True,
            )
            post_digest = _statements_digest(conn)

            checks = [
                _check_labels(conn, result),
                _check_not_promoted(conn),
                _check_no_gating_write(pre_digest, post_digest),
                _check_run_log(conn),
                _check_failclosed(conn),
            ]
        outcome = {
            "ok": all(c["passed"] for c in checks),
            "transcript_id": transcript_id,
            "segment_count": len(segments),
            "lane2_run": _LANE2_RUN,
            "lane3_run": _LANE3_RUN,
            "written_count": len(written),
            "verified_count": result["verified_count"],
            "contested_count": result["contested_count"],
            "error_status": result["error_status"],
            "db_path": str(db_path),
            "checks": checks,
        }
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    if strict and not outcome["ok"]:
        failed = [c["name"] for c in outcome["checks"] if not c["passed"]]
        raise SmokeFailure(f"slice 3 C Lane-3 verification smoke FAILED: {failed}")
    return outcome


def _print_report(result: dict) -> None:
    print("=== GOV-90 Slice 3 C Lane-3 verification smoke (1.09/1.11 end-to-end) ===")
    print(
        f"sandbox db: {result['db_path']}  transcript={result['transcript_id']} "
        f"segments={result['segment_count']}"
    )
    print(
        f"lane2={result['lane2_run']} written={result['written_count']}  "
        f"lane3={result['lane3_run']} verified={result['verified_count']} "
        f"contested={result['contested_count']} error_status={result['error_status']}"
    )
    for check in result["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        print(f"[{status}] {check['name']}")
        if not check["passed"]:
            print(f"         -> {check.get('error', 'invariant failed')}")
            for item in check.get("offenders", []):
                print(f"         offender: {item}")
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
