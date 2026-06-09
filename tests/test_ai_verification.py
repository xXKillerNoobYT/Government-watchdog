"""Tests for the Lane-3 verification layer (GOV-90, Slice 3 C).

Covers the GOV-90 acceptance criteria + the Slice-3 AI done-bar as it applies to
the verification lane:

- migration 0010 is additive + idempotent; the results table + indexes exist and
  its CHECK literals match the module's vocabularies (no drift);
- a well-grounded AI claim is labelled `source_match` but is NEVER promoted — the
  statement stays machine_extracted_unreviewed + not_publishable (acceptance);
- a low-confidence AI claim is never auto-matched (capped at `uncertain`) and
  stays machine_extracted_unreviewed + not-publishable (acceptance, core);
- a mismatched AI claim is labelled `source_mismatch` + contested, and likewise
  stays not-publishable (acceptance);
- Lane 3 writes NO gating field: the statements + evidence_links rows are
  byte-identical pre/post (gap analysis §4.2 L3-1);
- the Lane-3 run is recorded on the shared ai_extraction_runs ledger with
  lane='3_verification' + the required run-log fields (done-bar 10);
- fail-closed downstream: every verdict blocks publication except a source_match
  a human separately approved; no verdict and a failed run block too (done-bar 11);
- attribution safety is preserved (Lane 3 adds/modifies no attribution; done-bar 9);
- data-publication boundary: the verdict table is NOT web-projected (done-bar 12).

No AI, no network: pure sqlite + the committed sanitized Alpine fixture + the
real Lane-2 writer producing the AI rows Lane 3 verifies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import ai_verification as av  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import statements as stmt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
SOURCE_ID = "alpine:video:2026-05-08-regular"

# Real segment ids the deterministic segmenter produces from the fixture.
SEG_FINANCING = "alpine-sample-0001:seg-0003"   # "...financing gap ... treatment plant project..."
SEG_OPTIONS = "alpine-sample-0001:seg-0004"     # "The financing options under review include a state revolving fund loan..."
SEG_CONTINUED = "alpine-sample-0001:seg-0006"   # "...the item is continued to the next regular meeting for a financing decision."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _migrated(tmp_path: Path) -> Path:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    return db_path


def _seed_source(conn) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (SOURCE_ID, "Alpine Council 2026-05-08 video", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return SOURCE_ID


def _seed_segments(conn) -> list[str]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], "2026-05-08", tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            "Transcripts/2026/alpine-sample-0001.json", "0" * 64, _now(), SOURCE_ID,
        ),
    )
    tid = int(cur.lastrowid)
    rows = seg.segment_transcript(conn, tid, source_id=SOURCE_ID)
    return [r["segment_id"] for r in rows]


def _pointer(segment_ts: int, segment_human: str, **over) -> dict:
    pointer = {
        "to_source_id": SOURCE_ID,
        "relation": "references",
        "locator_kind": "timestamp",
        "timestamp_seconds": segment_ts,
        "timestamp_human": segment_human,
        "original_url": "https://example.gov/video",
        "archive_status": "available",
        "scan_date": "2026-05-10",
        "captured_at_utc": "2026-05-10T17:04:22Z",
        "verification_status": "machine_extracted_unreviewed",
        "confidence": "high",
    }
    pointer.update(over)
    return pointer


def _static_proposer(claims):
    def _p(conn, source_ids, segment_ids):
        return [dict(c, evidence_links=[dict(p) for p in c.get("evidence_links", [])])
                for c in claims]
    return _p


# Four AI claims that exercise every verdict band against the real source text.
_MATCH_ID = "alpine:ai:match"            # paraphrases seg-0003 closely -> source_match
_LOWCONF_ID = "alpine:ai:lowconf"        # paraphrases seg-0004 closely BUT confidence=low
_MISMATCH_ID = "alpine:ai:mismatch"      # anchored to seg-0006 but unrelated text
_UNVERIF_ID = "alpine:ai:unverifiable"   # pointer to a timestamp with no segment


def _proposed_claims() -> list[dict]:
    return [
        {
            "statement_id": _MATCH_ID,
            "segment_id": SEG_FINANCING,
            "statement_text": "AI paraphrase: staff reports the financing gap for the treatment plant project.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(93, "00:01:33")],
        },
        {
            "statement_id": _LOWCONF_ID,
            "segment_id": SEG_OPTIONS,
            "statement_text": "AI paraphrase: the financing options under review include a state revolving fund loan.",
            "is_verbatim": 0,
            "confidence": "low",   # high overlap but low confidence -> never auto-matched
            "evidence_links": [_pointer(144, "00:02:24")],
        },
        {
            "statement_id": _MISMATCH_ID,
            "segment_id": SEG_CONTINUED,
            "statement_text": "AI paraphrase: the mayor approved a new downtown park construction budget.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(309, "00:05:09")],
        },
        {
            "statement_id": _UNVERIF_ID,
            # No segment edge; a valid pointer to a timestamp that resolves to NO
            # transcript_segments row -> source cannot be resolved -> unverifiable.
            "statement_text": "AI paraphrase: a claim whose source span cannot be located.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(99999, "27:46:39")],
        },
    ]


def _run_lane2(conn) -> list[str]:
    """Write the AI rows via the real Lane-2 writer; return written statement ids."""
    claims = _proposed_claims()
    result = ai.run_extraction(
        conn,
        run_id="r-lane2",
        input_source_ids=[SOURCE_ID],
        input_segment_ids=[SEG_FINANCING, SEG_OPTIONS, SEG_CONTINUED],
        proposer=_static_proposer(claims),
        tool_version="gov-lane2@test",
    )
    return result["written_statements"]


def _setup(conn) -> list[str]:
    _seed_source(conn)
    _seed_segments(conn)
    return _run_lane2(conn)


def _open_run(conn, run_id: str = "r-v") -> str:
    """Open a lane-3 ledger row so a direct verify_statement() FK resolves."""
    ai.create_run(conn, run_id=run_id, lane=av.LANE, input_source_ids=[SOURCE_ID])
    return run_id


_STATEMENT_COLS = (
    "statement_id", "segment_id", "statement_text", "produced_by", "verification_status",
    "correction_status", "review_state", "publication_state", "source_changed",
    "ui_status", "confidence", "ai_extraction_run_id",
)


def _statements_digest(conn) -> tuple[int, str]:
    rows = conn.execute(
        f"SELECT {', '.join(_STATEMENT_COLS)} FROM statements ORDER BY statement_id"
    ).fetchall()
    payload = json.dumps([[r[c] for c in _STATEMENT_COLS] for r in rows],
                         sort_keys=True, default=str)
    return len(rows), hashlib.sha256(payload.encode()).hexdigest()


def _table_sql(conn, table: str) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# --- migration 0010: schema shape + idempotency -----------------------------

def test_migration_creates_results_table(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        cols = _columns(conn, av.RESULTS_TABLE)
    for required in (
        "result_id", "run_id", "statement_id", "evidence_link_id", "verdict",
        "match_method", "match_score", "uncertainty_flag", "contested",
        "source_excerpt", "detail", "compared_utc", "created_utc",
    ):
        assert required in cols, f"{av.RESULTS_TABLE}.{required} missing"


def test_migration_0010_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must be a no-op, never raise
    with db.open_db(db_path) as conn:
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({av.RESULTS_TABLE})")]
        rows = conn.execute(f"SELECT COUNT(*) FROM {av.RESULTS_TABLE}").fetchone()[0]
    assert "0010_ai_verification_results" in ledger
    assert cols.count("result_id") == 1
    assert rows == 0


def test_verdict_vocab_matches_check_literals(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        sql = _table_sql(conn, av.RESULTS_TABLE)
        run_sql = _table_sql(conn, "ai_extraction_runs")
    for value in av.ALLOWED_VERDICTS:
        assert f"'{value}'" in sql, f"verdict CHECK missing {value!r}"
    for value in av.ALLOWED_UNCERTAINTY_FLAGS:
        assert f"'{value}'" in sql, f"uncertainty_flag CHECK missing {value!r}"
    # Lane 3 runs on the shared ledger; its lane literal must be permitted there.
    assert f"'{av.LANE}'" in run_sql


# --- pure classifier ---------------------------------------------------------

def test_containment_ignores_ai_prefix_and_stopwords() -> None:
    # The "AI paraphrase:" lead-in and function words must not inflate grounding.
    score = av.containment_score(
        "AI paraphrase: the financing gap for the treatment plant",
        "staff reports the current financing gap for the treatment plant project",
    )
    assert score == 1.0  # every content token (financing, gap, treatment, plant) grounded


def test_classify_low_confidence_never_matches() -> None:
    # Even a perfect overlap is capped at 'uncertain' when the claim is low-confidence.
    verdict, score, flag = av.classify(
        source_text="the financing options under review include a state revolving fund loan",
        claim_text="AI paraphrase: the financing options under review include a state revolving fund loan",
        claim_confidence="low",
    )
    assert score == 1.0
    assert verdict == "uncertain"      # NOT source_match
    assert flag == "medium"


def test_classify_unverifiable_when_no_source() -> None:
    verdict, score, flag = av.classify(
        source_text=None, claim_text="anything", claim_confidence="high"
    )
    assert verdict == "unverifiable"
    assert score is None
    assert flag == "high"


# --- acceptance: match labels but never promotes ----------------------------

def test_match_claim_labelled_but_not_promoted(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        _open_run(conn)
        before = conn.execute(
            "SELECT verification_status, publication_state, review_state "
            "FROM statements WHERE statement_id=?", (_MATCH_ID,)
        ).fetchone()
        out = av.verify_statement(conn, _MATCH_ID, run_id="r-v")
        after = conn.execute(
            "SELECT verification_status, publication_state, review_state "
            "FROM statements WHERE statement_id=?", (_MATCH_ID,)
        ).fetchone()
    assert out["verdict"] == "source_match"
    assert out["contested"] == 0
    # The verdict is a flag — the claim is NOT promoted by Lane 3.
    assert after["verification_status"] == "machine_extracted_unreviewed"
    assert after["publication_state"] == "not_publishable"
    assert after["review_state"] == "unreviewed"
    assert tuple(after) == tuple(before)  # statement gating untouched


# --- acceptance (core): a low-confidence/mismatched claim stays unreviewed ---

def test_lowconfidence_claim_stays_unreviewed_and_not_publishable(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        _open_run(conn)
        out = av.verify_statement(conn, _LOWCONF_ID, run_id="r-v")
        row = conn.execute(
            "SELECT verification_status, publication_state, ui_status "
            "FROM statements WHERE statement_id=?", (_LOWCONF_ID,)
        ).fetchone()
    assert out["verdict"] == "uncertain"     # never auto-matched despite high overlap
    assert out["contested"] == 1
    assert row["verification_status"] == "machine_extracted_unreviewed"
    assert row["publication_state"] == "not_publishable"
    # And it is not publication-eligible by the SSOT uiStatus gate.
    assert pub.is_publication_eligible(
        {"verificationStatus": row["verification_status"], "sourcePresent": True}
    ) is False


def test_mismatched_claim_flagged_not_promoted(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        _open_run(conn)
        out = av.verify_statement(conn, _MISMATCH_ID, run_id="r-v")
        row = conn.execute(
            "SELECT verification_status, publication_state FROM statements WHERE statement_id=?",
            (_MISMATCH_ID,)
        ).fetchone()
        result = av.latest_verdict(conn, _MISMATCH_ID)
    assert out["verdict"] == "source_mismatch"
    assert out["contested"] == 1
    assert result["uncertainty_flag"] == "high"
    assert row["verification_status"] == "machine_extracted_unreviewed"
    assert row["publication_state"] == "not_publishable"


def test_unverifiable_when_source_unresolved(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        _open_run(conn)
        out = av.verify_statement(conn, _UNVERIF_ID, run_id="r-v")
    assert out["verdict"] == "unverifiable"
    assert out["contested"] == 1
    assert out["match_score"] is None


# --- Lane 3 writes NO gating field (gap analysis §4.2 L3-1) ------------------

def test_lane3_writes_no_gating_field(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        ids = _setup(conn)
        pre_count, pre_digest = _statements_digest(conn)
        ev_pre = conn.execute(
            "SELECT verification_status, correction_status, confidence "
            "FROM evidence_links ORDER BY evidence_link_id"
        ).fetchall()
        av.run_verification(conn, run_id="r-v", input_statement_ids=ids,
                            input_source_ids=[SOURCE_ID], tool_version="gov-lane3@test")
        post_count, post_digest = _statements_digest(conn)
        ev_post = conn.execute(
            "SELECT verification_status, correction_status, confidence "
            "FROM evidence_links ORDER BY evidence_link_id"
        ).fetchall()
    assert post_count == pre_count
    assert post_digest == pre_digest, "Lane 3 mutated a statements gating field"
    assert [tuple(r) for r in ev_post] == [tuple(r) for r in ev_pre], \
        "Lane 3 mutated an evidence_links field"


def test_lane3_adds_no_speaker_attribution(tmp_path: Path) -> None:
    # Attribution safety (done-bar 9) is preserved: Lane 3 never names or attributes.
    with db.open_db(_migrated(tmp_path)) as conn:
        ids = _setup(conn)
        before = conn.execute("SELECT COUNT(*) FROM speaker_attributions").fetchone()[0]
        av.run_verification(conn, run_id="r-v", input_statement_ids=ids)
        after = conn.execute("SELECT COUNT(*) FROM speaker_attributions").fetchone()[0]
    assert after == before


# --- done-bar 10: the Lane-3 run is recorded on the shared ledger -----------

def test_lane3_run_logged_on_ledger(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        ids = _setup(conn)
        result = av.run_verification(
            conn, run_id="r-v3", input_statement_ids=ids,
            input_source_ids=[SOURCE_ID], input_segment_ids=[SEG_FINANCING],
            tool_version="gov-lane3@abc", dry_run=True,
        )
        run = ai.get_run(conn, "r-v3")
    assert run["lane"] == "3_verification"
    assert json.loads(run["input_source_ids"]) == [SOURCE_ID]
    assert run["tool_version"] == "gov-lane3@abc"
    assert run["model_name"] is None              # Lane 3 is deterministic — no model
    assert run["error_status"] == "ok"
    assert run["reviewer_state"] == "unreviewed"
    assert run["retry_count"] == 0
    assert run["dry_run"] == 1
    assert run["started_utc"] and run["finished_utc"]
    # Output artifact ids recorded: verified statements + the verdict result ids.
    assert len(json.loads(run["output_statement_ids"])) == len(ids)
    assert len(json.loads(run["output_evidence_link_ids"])) == result["verified_count"]
    assert result["contested_count"] == 3         # lowconf + mismatch + unverifiable


def test_lane3_run_reviewer_gate_and_retry(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        ids = _setup(conn)
        av.run_verification(conn, run_id="r-v1", input_statement_ids=ids)
        av.run_verification(conn, run_id="r-v2", input_statement_ids=ids,
                            retry_of_run_id="r-v1", retry_count=1)
        r2 = ai.get_run(conn, "r-v2")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert r2["retry_of_run_id"] == "r-v1"
    assert r2["retry_count"] == 1
    assert violations == []


# --- done-bar 11: fail-closed downstream ------------------------------------

def test_verification_blocks_publication_failclosed() -> None:
    # No verdict -> blocked.
    assert av.verification_blocks_publication(None) is True
    for verdict in ("source_mismatch", "uncertain", "unverifiable"):
        assert av.verification_blocks_publication({"verdict": verdict}) is True
    # source_match alone does NOT unblock — a human must approve.
    assert av.verification_blocks_publication({"verdict": "source_match"}) is True
    assert av.verification_blocks_publication(
        {"verdict": "source_match"}, human_approved=True
    ) is False
    # mismatch stays blocked even with (mistaken) approval.
    assert av.verification_blocks_publication(
        {"verdict": "source_mismatch"}, human_approved=True
    ) is True


def test_failed_run_when_statement_missing(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        result = av.run_verification(
            conn, run_id="r-fail", input_statement_ids=["does-not-exist"],
        )
        run = ai.get_run(conn, "r-fail")
    assert result["ok"] is False
    assert result["error_status"] == "failed"
    assert run["error_status"] == "failed"


def test_verify_non_ai_statement_rejected(tmp_path: Path) -> None:
    # Lane 3 verifies Lane-2 output only; a human/automation row is out of scope.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_ids = _seed_segments(conn)
        stmt.insert_statement(
            conn,
            {
                "statement_id": "alpine:human:1",
                "segment_id": seg_ids[3],
                "statement_text": "A human-entered statement.",
                "produced_by": "human",
            },
            [],
        )
        with pytest.raises(av.VerificationError):
            av.verify_statement(conn, "alpine:human:1", run_id="r-v")


# --- done-bar 12: data-publication boundary ---------------------------------

def test_results_table_not_web_projected(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        cols = _columns(conn, av.RESULTS_TABLE)
    leaked = cols & pub.WEB_SAFE_FIELD_ALLOWLIST
    assert leaked == set(), f"verification-result fields leak to web: {leaked}"


def test_to_web_safe_drops_verdict_fields() -> None:
    record = {
        "source_id": "alpine:x",                       # allowlisted
        "verdict": "source_mismatch", "match_score": 0.1,  # not
        "source_excerpt": "raw source text", "detail": "score=0.1",  # not
    }
    safe = pub.to_web_safe(record)
    assert "verdict" not in safe
    assert "match_score" not in safe
    assert "source_excerpt" not in safe
    assert safe.get("source_id") == "alpine:x"
