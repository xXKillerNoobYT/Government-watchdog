"""Third-slice end-to-end Lane-2 AI-extraction smoke (GOV-89, Stage 1 Slice 3 B).

Integration of the AI gateway Lane 2 over the Slice-1 registry + Slice-2 1.07
model. Source: GOV-88 interface design (Docs/stage3-ai-gateway-gap-analysis.md),
contracts 1.09/1.11, AI_GATEWAY_PROCESSING_WORKFLOW.md. This is the single
end-to-end smoke proving the AI lane holds its invariants when it runs against a
real (sanitized) Alpine fixture — NOT just in unit isolation.

It performs a real, OFFLINE+DETERMINISTIC:

    apply migrations  ->  reuse the Slice-1-seeded Alpine source registry  ->
    load the sanitized 2026-05-08 WWTP-financing fixture as meeting + transcript
    ->  deterministically segment it (Lane-1 output)  ->  run a Lane-2 AI
    extraction over the *already-preserved* segments with an injected proposer
    (no model call, no network)  ->  assert the AI-gateway invariants.

There is NO live model: the proposer is a fixed, source-grounded stand-in so the
smoke is reproducible and offline. It proposes (a) two well-anchored AI
paraphrase claims, (b) one orphan claim with no pointer (must be rejected), and
(c) an uncertain speaker guess (must drop the name).

Asserted invariants (GOV-89 done-bar 7-11):
  1. AI PROVENANCE + FAIL-CLOSED DEFAULTS — every AI-written row carries
     produced_by='ai', verification_status='machine_extracted_unreviewed',
     review_state='unreviewed', publication_state='not_publishable',
     layer='ai_thought_then', is_verbatim=0, and its ai_extraction_run_id.
  2. NO ORPHAN CLAIMS — the unpointered AI claim is rejected (not written), and
     the run records orphan_rejected_count >= 1.
  3. ATTRIBUTION SAFETY — the uncertain AI speaker is name-free: no bound
     person_id, no made_statement edge, and the candidate name never renders.
  4. GATEWAY RUN-LOG — the ai_extraction_runs row records the input source/segment
     set, model+tool+prompt version, output artifact ids, error status, reviewer
     state, and retry fields.
  5. FAIL-CLOSED DOWNSTREAM — the run's outputs are publication-blocked while
     unreviewed (the human reviewer gate), and a separately-simulated failed run
     is blocked too.

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): only the sanitized fixture under
tests/fixtures/alpine/ is read; the ledger/error_detail and AI rows are written
to a throwaway sandbox DB and never published. Nothing real is touched.

Usage:
    python scripts/slice3_smoke.py [--fixture PATH] [--keep] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_extraction as ai  # noqa: E402
import db  # noqa: E402
import segment_transcript as seg  # noqa: E402
import source_inventory as si  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
)

_MEETING_DATE = "2026-05-08"
_MEETING_BODY = "Alpine Town Council"
_MEETING_TITLE = "Regular Meeting"
_VIDEO_SOURCE_ID = "alpine_youtube_channel"  # Slice-1 seed (source_inventory.py)
_AGENDA_SOURCE_ID = "alpinewy_gov"
_AGENDA_ITEM_ID = "alpine:2026-05-08:item-7"
_TRANSCRIPT_VAULT_PATH = "Transcripts/2026/alpine-2026-05-08-regular.json"  # synthetic
_RUN_ID = "alpine:ai-extract:2026-05-08:smoke"
_CANDIDATE_PERSON_ID = "alpine:person:pat-maxwell"
_CANDIDATE_NAME = "Pat Maxwell"

_FINANCING_SEGMENT_INDEX = 3
_CONTINUED_SEGMENT_INDEX = 6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SmokeFailure(AssertionError):
    """Raised by run_smoke(strict=True) when any AI-gateway invariant regresses."""


def _load_meeting_and_transcript(conn, fixture: dict) -> tuple[int, int]:
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, channel_id, channel_title, "
        "upload_date, meeting_date, duration_seconds, language, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], meta.get("channel_id"),
            meta.get("channel_title"), meta.get("upload_date"), _MEETING_DATE,
            meta.get("duration_seconds"), tr["language"], tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            _TRANSCRIPT_VAULT_PATH, "0" * 64, _now(), None,
        ),
    )
    transcript_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO meetings (meeting_date, body, title, transcript_id, fetch_time_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        (_MEETING_DATE, _MEETING_BODY, _MEETING_TITLE, transcript_id, _now()),
    )
    meeting_id = int(cur.lastrowid)
    conn.commit()
    return meeting_id, transcript_id


def _create_agenda_item(conn, meeting_id: int) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO agenda_items "
        "(agenda_item_id, meeting_id, item_order, title, agenda_doc_source_id, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_AGENDA_ITEM_ID, meeting_id, 7,
         "Wastewater treatment plant financing update", _AGENDA_SOURCE_ID, _now()),
    )
    conn.commit()
    return _AGENDA_ITEM_ID


def _segment_for(records: list[dict], index: int) -> dict:
    for rec in records:
        if rec["segment_index"] == index:
            return rec
    raise SmokeFailure(f"fixture produced no segment at index {index}")


def _timestamp_pointer(segment: dict, video_url: str) -> dict:
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


def _build_proposed_claims(segments: list[dict], agenda_item_id: str, video_url: str) -> list[dict]:
    """A deterministic, source-grounded Lane-2 proposer payload (offline stand-in).

    Two anchored AI paraphrases (one carrying an uncertain speaker guess) + one
    deliberate orphan (no pointer) to exercise the no-orphan rejection path.
    """
    fin = _segment_for(segments, _FINANCING_SEGMENT_INDEX)
    cont = _segment_for(segments, _CONTINUED_SEGMENT_INDEX)
    return [
        {
            "statement_id": "alpine:ai:2026-05-08:financing-gap",
            "segment_id": fin["segment_id"],
            "agenda_item_id": agenda_item_id,
            "statement_text": "AI paraphrase: the council reviewed a wastewater plant financing shortfall.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_timestamp_pointer(fin, video_url)],
            # An AI guess at the speaker — must NOT be named (uncertain -> no name).
            "speaker": {
                "candidate_person_id": _CANDIDATE_PERSON_ID,
                "speaker_class": "on-record-official",
                "role_title": "Mayor",
                "confidence": "high",
            },
        },
        {
            "statement_id": "alpine:ai:2026-05-08:continued",
            "segment_id": cont["segment_id"],
            "agenda_item_id": agenda_item_id,
            "statement_text": "AI paraphrase: the item was continued to the next meeting.",
            "is_verbatim": 0,
            "confidence": "low",
            "evidence_links": [_timestamp_pointer(cont, video_url)],
        },
        {
            # Orphan: no segment edge AND no evidence_link -> must be rejected.
            "statement_id": "alpine:ai:2026-05-08:orphan",
            "statement_text": "AI claim with no source pointer (must be rejected).",
            "is_verbatim": 0,
            "evidence_links": [],
        },
    ]


# --- invariant checks ------------------------------------------------------


def _check_ai_provenance(conn, run_id: str) -> dict:
    detail: dict = {"name": "ai_provenance_failclosed", "passed": False}
    rows = conn.execute(
        "SELECT statement_id, produced_by, verification_status, review_state, "
        "publication_state, is_verbatim, layer, ai_extraction_run_id FROM statements"
    ).fetchall()
    detail["rows"] = len(rows)
    if not rows:
        detail["error"] = "no AI statements written — nothing to assert"
        return detail
    offenders = []
    for row in rows:
        if not (
            row["produced_by"] == "ai"
            and row["verification_status"] == "machine_extracted_unreviewed"
            and row["review_state"] == "unreviewed"
            and row["publication_state"] == "not_publishable"
            and row["is_verbatim"] == 0
            and row["layer"] == "ai_thought_then"
            and row["ai_extraction_run_id"] == run_id
        ):
            offenders.append(dict(row))
    if offenders:
        detail["error"] = f"{len(offenders)} AI row(s) not fail-closed/provenanced"
        detail["offenders"] = offenders
    else:
        detail["passed"] = True
    return detail


def _check_no_orphan(conn, run: dict, result: dict) -> dict:
    detail: dict = {"name": "no_orphan_claims", "passed": False}
    written = conn.execute(
        "SELECT COUNT(*) FROM statements WHERE statement_id = 'alpine:ai:2026-05-08:orphan'"
    ).fetchone()[0]
    detail["orphan_written"] = written
    detail["orphan_rejected_count"] = run["orphan_rejected_count"]
    if written != 0:
        detail["error"] = "the orphan AI claim was written (should be rejected)"
    elif run["orphan_rejected_count"] < 1:
        detail["error"] = "orphan_rejected_count did not record the rejection"
    else:
        detail["passed"] = True
    return detail


def _check_attribution_safe(conn) -> dict:
    detail: dict = {"name": "attribution_safe", "passed": False}
    attr = conn.execute(
        "SELECT attribution_state, person_id, candidate_person_id, display_label "
        "FROM speaker_attributions WHERE statement_id = 'alpine:ai:2026-05-08:financing-gap'"
    ).fetchone()
    if attr is None:
        detail["error"] = "no attribution row written for the speaker claim"
        return detail
    made = conn.execute(
        "SELECT COUNT(*) FROM made_statement WHERE statement_id = 'alpine:ai:2026-05-08:financing-gap'"
    ).fetchone()[0]
    safe = (
        attr["attribution_state"] != "attributed"
        and attr["person_id"] is None
        and _CANDIDATE_NAME not in (attr["display_label"] or "")
        and made == 0
    )
    detail.update({
        "attribution_state": attr["attribution_state"], "person_id": attr["person_id"],
        "label": attr["display_label"], "made_statement_rows": made,
    })
    if not safe:
        detail["error"] = "AI speaker was named or bound (attribution-safety breach)"
    else:
        detail["passed"] = True
    return detail


def _check_run_log(conn, run: dict, seg_ids: list[str]) -> dict:
    detail: dict = {"name": "gateway_run_log", "passed": False}
    try:
        input_sources = json.loads(run["input_source_ids"])
        input_segments = json.loads(run["input_segment_ids"])
        outputs = json.loads(run["output_statement_ids"])
    except (TypeError, json.JSONDecodeError) as exc:
        detail["error"] = f"run-log JSON fields unreadable: {exc}"
        return detail
    ok = (
        _VIDEO_SOURCE_ID in input_sources
        and len(input_segments) >= 1
        and run["tool_version"]
        and run["prompt_id"] == ai.PROMPT_ID
        and len(outputs) == 2
        and run["error_status"] in ai.ALLOWED_RUN_ERROR_STATUS
        and run["reviewer_state"] in ai.ALLOWED_RUN_REVIEWER_STATE
        and run["started_utc"] and run["finished_utc"]
    )
    detail.update({
        "input_source_count": len(input_sources), "input_segment_count": len(input_segments),
        "output_count": run["output_count"], "error_status": run["error_status"],
        "reviewer_state": run["reviewer_state"], "retry_count": run["retry_count"],
    })
    if not ok:
        detail["error"] = "run-log missing a required field (input/model/tool/outputs/state)"
    else:
        detail["passed"] = True
    return detail


def _check_failclosed_downstream(conn, run: dict) -> dict:
    detail: dict = {"name": "failclosed_downstream", "passed": False}
    # The OK-but-unreviewed run is blocked downstream (human reviewer gate).
    unreviewed_blocked = ai.outputs_publication_blocked(run)
    # A simulated failed run is also blocked.
    failed_blocked = ai.outputs_publication_blocked(
        {"error_status": "failed", "reviewer_state": "unreviewed"}
    )
    # And approval flips ONLY when error_status is ok.
    approved_ok = not ai.outputs_publication_blocked(
        {"error_status": "ok", "reviewer_state": "approved"}
    )
    detail.update({
        "unreviewed_blocked": unreviewed_blocked, "failed_blocked": failed_blocked,
        "approved_unblocks": approved_ok,
    })
    if not (unreviewed_blocked and failed_blocked and approved_ok):
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
        tmp_holder = tempfile.TemporaryDirectory(prefix="gov89-slice3-smoke-")
        sandbox = Path(tmp_holder.name)
    sandbox = Path(sandbox)
    db_path = sandbox / "Database" / "slice3_smoke.db"

    try:
        si.load(db_path)  # migrate + seed Slice-1 Alpine registry
        with db.open_db(db_path) as conn:
            meeting_id, transcript_id = _load_meeting_and_transcript(conn, fixture_data)
        si.load(db_path)  # reconcile transcript.source_id from the registry
        with db.open_db(db_path) as conn:
            video_url = conn.execute(
                "SELECT video_url, source_id FROM transcripts WHERE id = ?",
                (transcript_id,),
            ).fetchone()
            agenda_item_id = _create_agenda_item(conn, meeting_id)
            segments = seg.segment_transcript(conn, transcript_id)
            seg_ids = [s["segment_id"] for s in segments]
            # A real official candidate exists in the record; the AI must still not name them.
            conn.execute(
                "INSERT OR IGNORE INTO persons (person_id, display_name, person_type, created_utc) "
                "VALUES (?, ?, 'official', ?)",
                (_CANDIDATE_PERSON_ID, _CANDIDATE_NAME, _now()),
            )
            conn.commit()

            claims = _build_proposed_claims(segments, agenda_item_id, video_url["video_url"])
            anchored_seg_ids = [c["segment_id"] for c in claims if c.get("segment_id")]
            result = ai.run_extraction(
                conn,
                run_id=_RUN_ID,
                input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids,
                proposer=lambda c, s, sg: claims,
                tool_version="gov-lane2-smoke@local",
                model_name="offline-deterministic",
                model_version="smoke",
                dry_run=True,
            )
            run = ai.get_run(conn, _RUN_ID)

            checks = [
                _check_ai_provenance(conn, _RUN_ID),
                _check_no_orphan(conn, run, result),
                _check_attribution_safe(conn),
                _check_run_log(conn, run, seg_ids),
                _check_failclosed_downstream(conn, run),
            ]
        outcome = {
            "ok": all(c["passed"] for c in checks),
            "meeting_id": meeting_id,
            "transcript_id": transcript_id,
            "transcript_source_id": video_url["source_id"],
            "segment_count": len(segments),
            "run_id": _RUN_ID,
            "output_count": result["output_count"],
            "rejected": len(result["rejected"]),
            "error_status": result["error_status"],
            "db_path": str(db_path),
            "checks": checks,
        }
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    if strict and not outcome["ok"]:
        failed = [c["name"] for c in outcome["checks"] if not c["passed"]]
        raise SmokeFailure(f"slice 3 AI-gateway smoke FAILED: {failed}")
    return outcome


def _print_report(result: dict) -> None:
    print("=== GOV-89 third-slice AI-gateway Lane-2 smoke (1.09/1.11 end-to-end) ===")
    print(
        f"sandbox db: {result['db_path']}  meeting={result['meeting_id']} "
        f"transcript={result['transcript_id']} -> source={result['transcript_source_id']}"
    )
    print(
        f"segments={result['segment_count']} run={result['run_id']} "
        f"written={result['output_count']} rejected={result['rejected']} "
        f"error_status={result['error_status']}"
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
