"""Slice 3 E — full AI-gateway integration smoke, Lane 2->3->4->5 (GOV-92).

The Slice-3 capstone. Where the per-lane smokes (GOV-89 Lane 2, GOV-90 Lane 3,
GOV-91 Lanes 4+5) each prove one lane's invariant in isolation, THIS smoke runs
the *whole* gateway as one continuous pipeline over a single DB and asserts the
load-bearing end-to-end property:

    **Nothing AI-written is publishable by default — at any point in the
    2->3->4->5 path — and the ONLY thing that ever moves a claim is an explicit
    human reviewer decision, which still does not publish it.**

Source: GOV-88 interface design (Docs/stage3-ai-gateway-gap-analysis.md),
contracts 1.09 (automation-vs-AI boundary, step 11 / G2) + 1.11 (publication /
privacy / legal / moderation gates), AI_GATEWAY_PROCESSING_WORKFLOW.md lanes 1-5.

The pipeline (real, OFFLINE + DETERMINISTIC — no model, no network):

    apply migrations 0001-0011  ->  reuse the Slice-1-seeded Alpine source
    registry  ->  load the sanitized 2026-05-08 WWTP-financing fixture as the
    *preserved raw* meeting + transcript  ->  deterministically segment it
    (Lane 1)  ->  Lane 2 AI extraction (injected proposer: one grounded
    paraphrase carrying an uncertain speaker guess, one anchored accusation, one
    orphan)  ->  Lane 3 verification (label each AI row vs its source)  ->
    Lane 4 risk screen (privacy/legal/moderation/publication no-gos)  ->  Lane 5
    reviewer-gate (reject the no-reviewer promotion, block the failed-run row and
    the open-no-go row, then a valid human promotion of the clean row)  ->  a
    final sweep asserting EVERY AI row is still publication-blocked.

Asserted invariants (GOV-92 acceptance + per-issue done-bar 7-11):
  1. PIPELINE RAN END-TO-END — orphan rejected, two anchored AI rows written,
     all four lane runs (2/3/4) recorded on the shared ledger.
  2. AI PROVENANCE + FAIL-CLOSED DEFAULTS — every AI row carries produced_by='ai'
     + verification_status='machine_extracted_unreviewed' + review_state=
     'unreviewed' + publication_state='not_publishable' + layer='ai_thought_then'
     + is_verbatim=0 + its ai_extraction_run_id (done-bar 7).
  3. NO ORPHAN CLAIMS — the unpointered AI claim is rejected; the run records it
     (done-bar 8).
  4. ATTRIBUTION SAFE — the uncertain AI speaker is name-free: no person_id, no
     made_statement edge, the candidate name never renders (done-bar 9).
  5. LANE 3/4 NEVER MUTATE GATING — the statements gating digest is byte-identical
     across Lane 2 -> Lane 3 -> Lane 4 (verify + risk flag *beside* the claim).
  6. LANE 4 RISK FLAGS — the accusation row gets a `legal` no-go; each unreviewed
     AI row gets a `publication` review flag (1.11 §4 / AI_GATEWAY lane 4).
  7. GATEWAY RUN-LOG (each lane) — runs 2/3/4 are on ai_extraction_runs with the
     input set, tool/model version, errors, reviewer state, retry, timing
     (done-bar 10; AI_GATEWAY §17).
  8. FAIL-CLOSED REVIEWER-GATE — promoting WITHOUT a reviewer decision is rejected
     (done-bar 11; the headline "no AI row publishable without a reviewer").
  9. OPEN NO-GO BLOCKS PROMOTION — the accusation row cannot be promoted while its
     legal no-go flag is unresolved.
 10. FAILED RUN BLOCKS DOWNSTREAM — a row from a failed Lane-2 run is not
     promotable (AI_GATEWAY "failed gateway processing must block downstream").
 11. PROMOTION NEVER PUBLISHES — a valid human promotion of the clean row reaches
     a reviewed status + an audit row, but publication_state stays
     not_publishable (owner gate 1.11 P8).
 12. NOTHING PUBLISHABLE BY DEFAULT (HEADLINE) — after the entire pipeline, a
     sweep of EVERY AI statement row finds statement_publication_blocked() True
     for all of them — including the human-approved clean row. Zero AI rows reach
     a publishable state without an owner publication decision; none reach it at
     all in this Alpine-only, local/vault-only slice.

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): only the sanitized fixture under
tests/fixtures/alpine/ is read; every AI byte (statements, verdicts, risk flags,
reviewer decisions, run ledger) is written to a throwaway sandbox DB and never
published. Nothing real is touched.

Usage:
    python scripts/slice3e_smoke.py [--fixture PATH] [--keep] [--workdir DIR]
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
import ai_verification as av  # noqa: E402
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
_AGENDA_SOURCE_ID = "alpinewy_gov"
_AGENDA_ITEM_ID = "alpine:2026-05-08:item-7"
_TRANSCRIPT_VAULT_PATH = "Transcripts/2026/alpine-2026-05-08-regular.json"  # synthetic

_LANE2_RUN = "alpine:ai-extract:2026-05-08:3e-smoke"            # clean + legal (ok)
_LANE2_ORPHAN_RUN = "alpine:ai-extract:2026-05-08:3e-smoke-orphan"  # orphan-only (failed)
_LANE2_FAIL_RUN = "alpine:ai-extract:2026-05-08:3e-smoke-fail"  # proposer boom (failed)
_LANE3_RUN = "alpine:ai-verify:2026-05-08:3e-smoke"
_LANE4_RUN = "alpine:ai-risk:2026-05-08:3e-smoke"

_CLEAN_ID = "alpine:ai:3e:financing-clean"      # grounded + uncertain speaker guess
_LEGAL_ID = "alpine:ai:3e:accusation"           # anchored but a legal no-go
_ORPHAN_ID = "alpine:ai:3e:orphan"              # no pointer -> Lane-2 rejects
_FAILED_ID = "alpine:ai:3e:from-failed-run"     # produced by a failed Lane-2 run

_FINANCING_SEGMENT_INDEX = 3   # "...financing gap ... treatment plant project..."
_CONTINUED_SEGMENT_INDEX = 6   # "...item is continued to the next regular meeting..."

_CANDIDATE_PERSON_ID = "alpine:person:pat-maxwell"
_CANDIDATE_NAME = "Pat Maxwell"
_REVIEWER = "reviewer:isaac"

# The gating columns the digest watches — Lanes 3 and 4 must never touch any.
_STATEMENT_COLS = (
    "statement_id", "segment_id", "statement_text", "produced_by", "verification_status",
    "correction_status", "review_state", "publication_state", "source_changed",
    "ui_status", "layer", "is_verbatim", "ai_extraction_run_id",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SmokeFailure(AssertionError):
    """Raised by run_smoke(strict=True) when any end-to-end invariant regresses."""


# --- pipeline setup ---------------------------------------------------------

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
            meta.get("duration_seconds"), tr.get("language"), tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            _TRANSCRIPT_VAULT_PATH, "0" * 64, _now(), None,
        ),
    )
    transcript_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO meetings (meeting_date, body, title, transcript_id, fetch_time_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        (_MEETING_DATE, "Alpine Town Council", "Regular Meeting", transcript_id, _now()),
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


def _build_claims(segments: list[dict], agenda_item_id: str, video_url: str) -> list[dict]:
    """The two ANCHORED Lane-2 claims that flow through Lanes 3/4/5 (offline stand-in).

    * a grounded financing paraphrase carrying an UNCERTAIN speaker guess (must be
      name-free),
    * an anchored ACCUSATION about a named individual (a legal no-go Lane 4 flags).

    These go in their own ``ok`` Lane-2 run so the clean row is later promotable —
    the orphan (which makes a run ``partial``) is proposed in a separate run by
    :func:`_orphan_claim`, mirroring the real fail-closed gate (a run with a
    rejected claim freezes its siblings from promotion).
    """
    fin = _segment_for(segments, _FINANCING_SEGMENT_INDEX)
    cont = _segment_for(segments, _CONTINUED_SEGMENT_INDEX)
    return [
        {
            "statement_id": _CLEAN_ID,
            "segment_id": fin["segment_id"],
            "agenda_item_id": agenda_item_id,
            "statement_text": "AI paraphrase: staff reports the financing gap for the treatment plant project.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(fin, video_url)],
            # An AI guess at the speaker — uncertain, so it must NOT be named.
            "speaker": {
                "candidate_person_id": _CANDIDATE_PERSON_ID,
                "speaker_class": "on-record-official",
                "role_title": "Mayor",
                "confidence": "high",
            },
        },
        {
            "statement_id": _LEGAL_ID,
            "segment_id": cont["segment_id"],
            "agenda_item_id": agenda_item_id,
            "statement_text": "AI paraphrase: the mayor committed fraud by forcing the financing vote.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(cont, video_url)],
        },
    ]


def _orphan_claim() -> dict:
    """An AI claim with no segment edge AND no evidence_link — Lane 2 must reject it."""
    return {
        "statement_id": _ORPHAN_ID,
        "statement_text": "AI claim with no source pointer (must be rejected).",
        "is_verbatim": 0,
        "evidence_links": [],
    }


def _statements_digest(conn) -> tuple[int, str]:
    """SHA-256 over the gating columns of every statement — stability fingerprint."""
    rows = conn.execute(
        f"SELECT {', '.join(_STATEMENT_COLS)} FROM statements ORDER BY statement_id"
    ).fetchall()
    payload = json.dumps([[r[c] for c in _STATEMENT_COLS] for r in rows],
                         sort_keys=True, default=str)
    return len(rows), hashlib.sha256(payload.encode()).hexdigest()


# --- invariant checks -------------------------------------------------------

def _check_pipeline_ran(conn, lane2: dict, orphan: dict, segments: list[dict]) -> dict:
    detail: dict = {"name": "pipeline_ran_end_to_end", "passed": False}
    written = set(lane2["written_statements"])
    runs = {r[0] for r in conn.execute(
        "SELECT run_id FROM ai_extraction_runs"
    ).fetchall()}
    detail.update({
        "segment_count": len(segments),
        "written": sorted(written),
        "orphan_rejected": len(orphan["rejected"]),
        "ledger_runs": sorted(runs),
    })
    expected_runs = {_LANE2_RUN, _LANE2_ORPHAN_RUN, _LANE2_FAIL_RUN, _LANE3_RUN, _LANE4_RUN}
    if written != {_CLEAN_ID, _LEGAL_ID}:
        detail["error"] = f"unexpected Lane-2 output set: {sorted(written)}"
    elif len(orphan["rejected"]) != 1:
        detail["error"] = "the orphan claim was not rejected by Lane 2"
    elif not expected_runs.issubset(runs):
        detail["error"] = f"missing lane runs on ledger: {sorted(expected_runs - runs)}"
    else:
        detail["passed"] = True
    return detail


def _check_ai_provenance(conn) -> dict:
    detail: dict = {"name": "ai_provenance_failclosed", "passed": False}
    rows = conn.execute(
        "SELECT statement_id, produced_by, verification_status, review_state, "
        "publication_state, is_verbatim, layer, ai_extraction_run_id FROM statements "
        "WHERE produced_by = 'ai' AND ai_extraction_run_id = ?",
        (_LANE2_RUN,),
    ).fetchall()
    detail["rows"] = len(rows)
    if not rows:
        detail["error"] = "no AI statements written — nothing to assert"
        return detail
    offenders = [
        dict(r) for r in rows
        if not (
            r["produced_by"] == "ai"
            and r["verification_status"] == "machine_extracted_unreviewed"
            and r["review_state"] == "unreviewed"
            and r["publication_state"] == "not_publishable"
            and r["is_verbatim"] == 0
            and r["layer"] == "ai_thought_then"
            and r["ai_extraction_run_id"] == _LANE2_RUN
        )
    ]
    if offenders:
        detail["error"] = f"{len(offenders)} AI row(s) not fail-closed/provenanced"
        detail["offenders"] = offenders
    else:
        detail["passed"] = True
    return detail


def _check_no_orphan(conn) -> dict:
    detail: dict = {"name": "no_orphan_claims", "passed": False}
    written = conn.execute(
        "SELECT COUNT(*) FROM statements WHERE statement_id = ?", (_ORPHAN_ID,)
    ).fetchone()[0]
    run = ai.get_run(conn, _LANE2_ORPHAN_RUN)
    detail.update({"orphan_written": written,
                   "orphan_rejected_count": run["orphan_rejected_count"]})
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
        "FROM speaker_attributions WHERE statement_id = ?", (_CLEAN_ID,)
    ).fetchone()
    if attr is None:
        detail["error"] = "no attribution row written for the speaker claim"
        return detail
    made = conn.execute(
        "SELECT COUNT(*) FROM made_statement WHERE statement_id = ?", (_CLEAN_ID,)
    ).fetchone()[0]
    safe = (
        attr["attribution_state"] != "attributed"
        and attr["person_id"] is None
        and _CANDIDATE_NAME not in (attr["display_label"] or "")
        and made == 0
    )
    detail.update({"attribution_state": attr["attribution_state"],
                   "person_id": attr["person_id"], "label": attr["display_label"],
                   "made_statement_rows": made})
    if not safe:
        detail["error"] = "AI speaker was named or bound (attribution-safety breach)"
    else:
        detail["passed"] = True
    return detail


def _check_lane3_labels_no_gating(conn, lane3: dict, pre: tuple, post: tuple) -> dict:
    detail: dict = {"name": "lane3_labels_never_promote", "passed": False,
                    "pre": pre[1][:12], "post": post[1][:12]}
    verdicts = {v["statement_id"]: v["verdict"] for v in lane3["verdicts"]}
    detail["verdicts"] = verdicts
    if set(verdicts) != {_CLEAN_ID, _LEGAL_ID}:
        detail["error"] = f"Lane 3 did not label every AI row: {sorted(verdicts)}"
    elif pre != post:
        detail["error"] = "Lane 3 mutated a statements gating field (digest changed)"
    else:
        detail["passed"] = True
    return detail


def _check_lane4_risk_flags(conn, risk: dict, pre: tuple, post: tuple) -> dict:
    detail: dict = {"name": "lane4_risk_flags", "passed": False,
                    "pre": pre[1][:12], "post": post[1][:12]}
    legal = conn.execute(
        f"SELECT COUNT(*) FROM {rg.RISK_TABLE} WHERE statement_id = ? "
        "AND risk_category = 'legal' AND severity = 'no_go'", (_LEGAL_ID,)
    ).fetchone()[0]
    pub_flags = conn.execute(
        f"SELECT COUNT(*) FROM {rg.RISK_TABLE} WHERE risk_category = 'publication'"
    ).fetchone()[0]
    detail.update({"legal_no_go": legal, "publication_flags": pub_flags,
                   "flag_count": risk["flag_count"]})
    if legal < 1:
        detail["error"] = "accusation claim did not get a legal no-go flag"
    elif pub_flags < 2:
        detail["error"] = "each unreviewed AI row should get a publication review flag"
    elif pre != post:
        detail["error"] = "Lane 4 mutated a statements gating field (digest changed)"
    else:
        detail["passed"] = True
    return detail


def _check_all_lanes_logged(conn) -> dict:
    detail: dict = {"name": "all_lanes_logged", "passed": False}
    expect = {_LANE2_RUN: "2_extraction", _LANE3_RUN: "3_verification",
              _LANE4_RUN: "4_risk"}
    lanes: dict = {}
    bad: list[str] = []
    for run_id, want_lane in expect.items():
        run = ai.get_run(conn, run_id)
        lanes[run_id] = run["lane"]
        ok = (
            run["lane"] == want_lane
            and _VIDEO_SOURCE_ID in json.loads(run["input_source_ids"])
            and run["tool_version"]
            and run["error_status"] in ai.ALLOWED_RUN_ERROR_STATUS
            and run["reviewer_state"] in ai.ALLOWED_RUN_REVIEWER_STATE
            and run["started_utc"] and run["finished_utc"]
        )
        if not ok:
            bad.append(f"{run_id}(lane={run['lane']})")
    detail["lanes"] = lanes
    if bad:
        detail["error"] = f"run-log incomplete for: {bad}"
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


def _check_open_nogo_blocks(conn) -> dict:
    detail: dict = {"name": "open_nogo_blocks_promotion", "passed": False}
    blocked = False
    try:
        rg.promote_statement(
            conn, _LEGAL_ID, reviewer_id=_REVIEWER, decision="approved",
            to_verification_status="reviewed_source_linked",
            reason="attempt to promote over an open legal no-go",
        )
    except rg.ReviewerGateError:
        blocked = True
    open_flags = rg.open_risk_flags(conn, _LEGAL_ID)
    detail.update({"blocked": blocked, "open_flag_count": len(open_flags)})
    if not (blocked and open_flags):
        detail["error"] = "accusation row was promotable while a no-go flag was open"
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
    detail.update({"verification_status": row["verification_status"],
                   "review_state": row["review_state"],
                   "publication_state": row["publication_state"],
                   "audit_decision": dec["decision"] if dec else None,
                   "promoted": out["promoted"]})
    if row["verification_status"] != "reviewed_source_linked":
        detail["error"] = "valid promotion did not reach reviewed_source_linked"
    elif row["publication_state"] != "not_publishable":
        detail["error"] = "promotion flipped publication_state (must stay owner-gated)"
    elif not (dec and dec["promoted"] == 1):
        detail["error"] = "no promoting audit decision recorded"
    else:
        detail["passed"] = True
    return detail


def _check_nothing_publishable(conn) -> dict:
    """HEADLINE: after the WHOLE pipeline, NO AI row is publishable — including
    the human-approved clean row. This is the cross-lane fail-closed guarantee."""
    detail: dict = {"name": "nothing_publishable_by_default", "passed": False}
    ai_ids = [r[0] for r in conn.execute(
        "SELECT statement_id FROM statements WHERE produced_by = 'ai' ORDER BY statement_id"
    ).fetchall()]
    publishable = [
        sid for sid in ai_ids if not rg.statement_publication_blocked(conn, sid)
    ]
    # The clean row is the strongest test: it has the MOST permissive state any AI
    # row reaches here — a source_match Lane-3 verdict AND a human-approved
    # promotion. The verdict-gate alone would let it through (belt), yet the DB
    # owner gate STILL blocks it (braces) because promotion never flips
    # publication_state. That gap is the whole "nothing publishable by default".
    verdict = av.latest_verdict(conn, _CLEAN_ID)
    verdict_gate_permits = not av.verification_blocks_publication(verdict, human_approved=True)
    owner_gate_blocks_clean = rg.statement_publication_blocked(conn, _CLEAN_ID)
    detail.update({
        "ai_row_count": len(ai_ids),
        "publishable_rows": publishable,
        "clean_verdict": (verdict or {}).get("verdict"),
        "clean_verdict_gate_permits": verdict_gate_permits,
        "clean_owner_gate_still_blocks": owner_gate_blocks_clean,
    })
    if publishable:
        detail["error"] = f"{len(publishable)} AI row(s) reached a publishable state: {publishable}"
    elif not (verdict_gate_permits and owner_gate_blocks_clean):
        detail["error"] = (
            "owner DB gate is not strictly stronger than the verdict gate "
            "(the human-approved source_match row was not still blocked)"
        )
    else:
        detail["passed"] = True
    return detail


# --- orchestration ----------------------------------------------------------

def run_smoke(fixture: Path = DEFAULT_FIXTURE, sandbox: Path | None = None,
              *, strict: bool = False) -> dict:
    fixture = Path(fixture)
    if not fixture.exists():
        raise FileNotFoundError(f"fixture not found: {fixture}")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))

    tmp_holder: tempfile.TemporaryDirectory | None = None
    if sandbox is None:
        tmp_holder = tempfile.TemporaryDirectory(prefix="gov92-slice3e-smoke-")
        sandbox = Path(tmp_holder.name)
    sandbox = Path(sandbox)
    db_path = sandbox / "Database" / "slice3e_smoke.db"

    try:
        si.load(db_path)  # migrate 0001-0011 + seed Slice-1 Alpine registry
        with db.open_db(db_path) as conn:
            meeting_id, transcript_id = _load_meeting_and_transcript(conn, fixture_data)
        si.load(db_path)  # reconcile transcript.source_id from the registry
        with db.open_db(db_path) as conn:
            video_url = conn.execute(
                "SELECT video_url FROM transcripts WHERE id = ?", (transcript_id,)
            ).fetchone()["video_url"]
            agenda_item_id = _create_agenda_item(conn, meeting_id)
            segments = seg.segment_transcript(conn, transcript_id)
            # A real official exists in the record; the AI must still not name them.
            conn.execute(
                "INSERT OR IGNORE INTO persons (person_id, display_name, person_type, created_utc) "
                "VALUES (?, ?, 'official', ?)",
                (_CANDIDATE_PERSON_ID, _CANDIDATE_NAME, _now()),
            )
            conn.commit()

            # === Lane 2 — AI extraction (offline deterministic proposer) =====
            claims = _build_claims(segments, agenda_item_id, video_url)
            anchored_seg_ids = [c["segment_id"] for c in claims if c.get("segment_id")]
            lane2 = ai.run_extraction(
                conn, run_id=_LANE2_RUN, input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids,
                proposer=lambda c, s, sg: claims,
                tool_version="gov-lane2-3e-smoke@local",
                model_name="offline-deterministic", model_version="smoke",
            )
            written = lane2["written_statements"]

            # The orphan in its OWN run (keeps _LANE2_RUN ok + promotable above).
            orphan = ai.run_extraction(
                conn, run_id=_LANE2_ORPHAN_RUN, input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids,
                proposer=lambda c, s, sg: [_orphan_claim()],
                tool_version="gov-lane2-3e-smoke@local",
                model_name="offline-deterministic", model_version="smoke",
            )

            # A row whose producing Lane-2 run finalizes FAILED (downstream-block).
            # GOV-278: the AI-provenance write-time gate requires an *ok* run at
            # write, so the row is written while the run is still open/ok and the
            # run is THEN finalized failed — the realistic ordering (a run emits a
            # row, then fails) and the only one compatible with the fail-closed
            # binding. Downstream still sees error_status='failed'.
            ai.create_run(
                conn, run_id=_LANE2_FAIL_RUN, input_source_ids=[_VIDEO_SOURCE_ID],
                input_segment_ids=anchored_seg_ids,
                tool_version="gov-lane2-3e-smoke@local",
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
            ai.finalize_run(
                conn, _LANE2_FAIL_RUN, output_statement_ids=[],
                output_evidence_link_ids=[], orphan_rejected_count=0,
                error_status="failed",
                error_detail="offline provider unavailable (smoke)",
            )

            digest_after_lane2 = _statements_digest(conn)

            # === Lane 3 — verification (label vs source; never promotes) ======
            lane3 = av.run_verification(
                conn, run_id=_LANE3_RUN, input_statement_ids=written,
                input_source_ids=[_VIDEO_SOURCE_ID], input_segment_ids=anchored_seg_ids,
                tool_version="gov-lane3-3e-smoke@local", dry_run=True,
            )
            digest_after_lane3 = _statements_digest(conn)

            # === Lane 4 — risk screen (flag beside the claim; never promotes) =
            risk = rg.run_risk(
                conn, run_id=_LANE4_RUN, input_statement_ids=written,
                input_source_ids=[_VIDEO_SOURCE_ID], input_segment_ids=anchored_seg_ids,
                tool_version="gov-lane4-3e-smoke@local", dry_run=True,
            )
            digest_after_lane4 = _statements_digest(conn)

            # GOV-93: the Lane-5 gate is now an allowlist — register the human
            # reviewer the positive-path checks promote/resolve with. Empty/sentinel
            # ids stay rejected (never registered), so the reject check still holds.
            rg.register_reviewer(
                conn, _REVIEWER,
                display_name="Isaac (smoke reviewer)", registered_by="slice3e-smoke",
            )

            # === checks (Lane-5-mutating checks run AFTER the digest checks) ===
            checks = [
                _check_pipeline_ran(conn, lane2, orphan, segments),
                _check_ai_provenance(conn),
                _check_no_orphan(conn),
                _check_attribution_safe(conn),
                _check_lane3_labels_no_gating(conn, lane3, digest_after_lane2, digest_after_lane3),
                _check_lane4_risk_flags(conn, risk, digest_after_lane3, digest_after_lane4),
                _check_all_lanes_logged(conn),
                # Lane 5 reviewer-gate, in order: reject no-reviewer, block open
                # no-go, block failed-run, then a valid promotion of the clean row.
                _check_reviewer_gate_rejects(conn),
                _check_open_nogo_blocks(conn),
                _check_failed_run_blocks(conn),
                _check_promotion_never_publishes(conn),
                # HEADLINE: after everything, nothing AI-written is publishable.
                _check_nothing_publishable(conn),
            ]
        outcome = {
            "ok": all(c["passed"] for c in checks),
            "meeting_id": meeting_id,
            "transcript_id": transcript_id,
            "segment_count": len(segments),
            "lane2_run": _LANE2_RUN, "lane3_run": _LANE3_RUN, "lane4_run": _LANE4_RUN,
            "written_count": len(written),
            "verified_count": lane3["verified_count"],
            "flag_count": risk["flag_count"],
            "db_path": str(db_path),
            "checks": checks,
        }
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    if strict and not outcome["ok"]:
        failed = [c["name"] for c in outcome["checks"] if not c["passed"]]
        raise SmokeFailure(f"slice 3 E end-to-end AI-gateway smoke FAILED: {failed}")
    return outcome


def _print_report(result: dict) -> None:
    print("=== GOV-92 Slice 3 E — full AI-gateway integration smoke (Lane 2->3->4->5) ===")
    print(
        f"sandbox db: {result['db_path']}  transcript={result['transcript_id']} "
        f"segments={result['segment_count']}"
    )
    print(
        f"lane2={result['lane2_run']} written={result['written_count']}  "
        f"lane3 verified={result['verified_count']}  "
        f"lane4={result['lane4_run']} flags={result['flag_count']}"
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
