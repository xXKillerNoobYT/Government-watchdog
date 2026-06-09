"""Tests for the Lane-4 risk layer + Lane-5 runtime reviewer-gate (GOV-91, Slice 3 D).

Covers the GOV-91 acceptance criteria + the Slice-3 AI done-bar as it applies to
the risk/reviewer lanes:

- migration 0011 is additive + idempotent; both side tables + indexes exist and
  their CHECK literals match the module's vocabularies (no drift);
- Lane 4 records 1.11 risk flags (privacy/legal/moderation no-go + publication
  review) per AI statement, and writes NO gating field — the statements rows are
  byte-identical pre/post the risk run (acceptance "1.11 risk flags recorded");
- the Lane-4 run is recorded on the shared ai_extraction_runs ledger with
  lane='4_risk' + the required run-log fields (done-bar 10);
- **reviewer-gate rejects promotion without a reviewer decision** (acceptance +
  done-bar 11): an empty / automation / AI reviewer_id raises, writing nothing;
- **a failed gateway run blocks downstream** promotion (acceptance + done-bar 11);
- an unresolved no-go risk flag blocks promotion until a reviewer resolves it;
- a valid human promotion moves the claim to a reviewed status + records an audit
  row, but NEVER flips publication_state — nothing AI-written is publishable by
  default (acceptance "nothing AI-written is publishable by default");
- AI rows still enter produced_by=ai + machine_extracted_unreviewed +
  not_publishable (done-bar 7); no-orphan inherited (done-bar 8); the gate adds
  no speaker attribution (attribution-safety untouched, done-bar 9);
- data-publication boundary: neither side table is web-projected (done-bar 12).

No AI, no network: pure sqlite + the committed sanitized Alpine fixture + the
real Lane-2 writer producing the AI rows Lane 4/5 act on.
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
import ai_risk_gate as rg  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import statements as stmt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
SOURCE_ID = "alpine:video:2026-05-08-regular"

SEG_FINANCING = "alpine-sample-0001:seg-0003"
SEG_OPTIONS = "alpine-sample-0001:seg-0004"
SEG_CONTINUED = "alpine-sample-0001:seg-0006"

# statement ids for the four screened AI claims.
_CLEAN_ID = "alpine:ai:clean"          # grounded paraphrase, no risk text
_PRIVACY_ID = "alpine:ai:privacy"      # leaks a street address -> privacy no_go
_LEGAL_ID = "alpine:ai:legal"          # an accusation -> legal no_go
_MODERATION_ID = "alpine:ai:moderation"  # rumor framing -> moderation no_go


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


def _proposed_claims() -> list[dict]:
    # All four are AI paraphrases anchored to a real segment (no-orphan satisfied).
    return [
        {
            "statement_id": _CLEAN_ID,
            "segment_id": SEG_FINANCING,
            "statement_text": "AI paraphrase: staff reports the financing gap for the treatment plant project.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(93, "00:01:33")],
        },
        {
            "statement_id": _PRIVACY_ID,
            "segment_id": SEG_OPTIONS,
            "statement_text": "AI paraphrase: a resident at 742 Evergreen Terrace asked about the financing options.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(144, "00:02:24")],
        },
        {
            "statement_id": _LEGAL_ID,
            "segment_id": SEG_CONTINUED,
            "statement_text": "AI paraphrase: the mayor committed fraud by forcing the financing vote.",
            "is_verbatim": 0,
            "confidence": "medium",
            "evidence_links": [_pointer(309, "00:05:09")],
        },
        {
            "statement_id": _MODERATION_ID,
            "segment_id": SEG_FINANCING,
            "statement_text": "AI paraphrase: rumor has it the council already decided the financing in private.",
            "is_verbatim": 0,
            "confidence": "low",
            "evidence_links": [_pointer(93, "00:01:33")],
        },
    ]


def _run_lane2(conn, run_id: str = "r-lane2") -> list[str]:
    result = ai.run_extraction(
        conn,
        run_id=run_id,
        input_source_ids=[SOURCE_ID],
        input_segment_ids=[SEG_FINANCING, SEG_OPTIONS, SEG_CONTINUED],
        proposer=_static_proposer(_proposed_claims()),
        tool_version="gov-lane2@test",
    )
    return result["written_statements"]


def _setup(conn) -> list[str]:
    _seed_source(conn)
    _seed_segments(conn)
    return _run_lane2(conn)


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


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_sql(conn, table: str) -> str:
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


# --- migration 0011: schema shape + idempotency -----------------------------

def test_migration_creates_both_tables(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        for table in (rg.RISK_TABLE, rg.DECISION_TABLE):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None
        assert "risk_category" in _columns(conn, rg.RISK_TABLE)
        assert "reviewer_id" in _columns(conn, rg.DECISION_TABLE)


def test_migration_0011_idempotent(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    db.apply_migrations(db_path)  # second run must be a no-op, not raise
    with db.open_db(db_path) as conn:
        assert "flag_id" in _columns(conn, rg.RISK_TABLE)
        assert "decision_id" in _columns(conn, rg.DECISION_TABLE)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert violations == []


def test_vocab_matches_check_literals(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        risk_sql = _table_sql(conn, rg.RISK_TABLE)
        dec_sql = _table_sql(conn, rg.DECISION_TABLE)
    for value in rg.RISK_CATEGORIES:
        assert f"'{value}'" in risk_sql, f"risk_category CHECK missing {value!r}"
    for value in rg.RISK_SEVERITIES:
        assert f"'{value}'" in risk_sql, f"severity CHECK missing {value!r}"
    for value in rg.REVIEWER_DECISIONS:
        assert f"'{value}'" in dec_sql, f"decision CHECK missing {value!r}"


# --- Lane 4: deterministic screen -------------------------------------------

def test_scan_text_flags_privacy() -> None:
    found = {f["category"] for f in rg.scan_text("call me at 307-555-0142 about the meeting")}
    assert "privacy" in found
    found = {f["category"] for f in rg.scan_text("the packet listed 742 Evergreen Terrace")}
    assert "privacy" in found


def test_scan_text_flags_legal_and_moderation() -> None:
    legal = {f["category"] for f in rg.scan_text("the mayor committed fraud at the meeting")}
    assert "legal" in legal
    mod = {f["category"] for f in rg.scan_text("rumor has it the vote was rigged")}
    assert "moderation" in mod


def test_scan_text_clean_has_no_findings() -> None:
    assert rg.scan_text("staff reported the financing gap for the treatment plant") == []


def test_run_risk_records_flags_and_blocks(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        result = rg.run_risk(
            conn,
            run_id="r-risk",
            input_statement_ids=[_CLEAN_ID, _PRIVACY_ID, _LEGAL_ID, _MODERATION_ID],
            input_source_ids=[SOURCE_ID],
            tool_version="gov-lane4@test",
        )
        assert result["ok"] is True
        # privacy/legal/moderation rows each carry a no-go content flag.
        assert _PRIVACY_ID in result["blocked_statement_ids"]
        assert _LEGAL_ID in result["blocked_statement_ids"]
        assert _MODERATION_ID in result["blocked_statement_ids"]
        # every AI row gets at least a publication 'review' flag.
        cats = {
            r["risk_category"]
            for r in conn.execute(
                f"SELECT risk_category FROM {rg.RISK_TABLE} WHERE statement_id = ?",
                (_PRIVACY_ID,),
            )
        }
        assert "privacy" in cats and "publication" in cats
        # the clean row has only the publication review flag (no content no-go).
        clean = rg.open_risk_flags(conn, _CLEAN_ID)
        assert all(f["risk_category"] == "publication" or f["severity"] != "no_go" for f in clean)


def test_lane4_writes_no_gating_field(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        pre = _statements_digest(conn)
        rg.run_risk(conn, run_id="r-risk",
                    input_statement_ids=[_CLEAN_ID, _PRIVACY_ID, _LEGAL_ID, _MODERATION_ID])
        post = _statements_digest(conn)
    assert pre == post, "Lane 4 must not mutate any statements gating field"


def test_lane4_run_logged_on_ledger(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        rg.run_risk(conn, run_id="r-risk", input_statement_ids=[_CLEAN_ID],
                    input_source_ids=[SOURCE_ID], tool_version="gov-lane4@test")
        run = ai.get_run(conn, "r-risk")
    assert run["lane"] == "4_risk"
    assert run["error_status"] == "ok"
    assert run["tool_version"] == "gov-lane4@test"
    assert run["model_name"] is None  # deterministic — no model
    assert run["finished_utc"] is not None


# --- entry invariants (done-bar 7/8/9) --------------------------------------

def test_ai_rows_enter_unreviewed_not_publishable(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        rows = conn.execute(
            "SELECT produced_by, verification_status, publication_state FROM statements"
        ).fetchall()
    assert rows
    for r in rows:
        assert r["produced_by"] == "ai"
        assert r["verification_status"] == "machine_extracted_unreviewed"
        assert r["publication_state"] == "not_publishable"


def test_gate_adds_no_speaker_attribution(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        before = conn.execute("SELECT COUNT(*) FROM made_statement").fetchone()[0]
        rg.run_risk(conn, run_id="r-risk", input_statement_ids=[_CLEAN_ID])
        rg.promote_statement(conn, _CLEAN_ID, reviewer_id="reviewer:isaac",
                             decision="approved", to_verification_status="reviewed_source_linked",
                             reason="grounded in source")
        after = conn.execute("SELECT COUNT(*) FROM made_statement").fetchone()[0]
    assert before == after == 0  # neither lane binds a speaker name


# --- Lane 5: the runtime reviewer-gate (acceptance) -------------------------

def test_promote_without_reviewer_is_rejected(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        for bad in ("", "  ", "ai", "automation", "gateway", "system"):
            with pytest.raises(rg.ReviewerGateError):
                rg.promote_statement(
                    conn, _CLEAN_ID, reviewer_id=bad, decision="approved",
                    to_verification_status="reviewed_source_linked", reason="x",
                )
        # nothing was written: claim still unreviewed, no decision row.
        row = conn.execute(
            "SELECT verification_status FROM statements WHERE statement_id = ?", (_CLEAN_ID,)
        ).fetchone()
        assert row["verification_status"] == "machine_extracted_unreviewed"
        assert rg.latest_decision(conn, _CLEAN_ID) is None


def test_promote_requires_reviewed_target(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        with pytest.raises(rg.ReviewerGateError):
            rg.promote_statement(
                conn, _CLEAN_ID, reviewer_id="reviewer:isaac", decision="approved",
                to_verification_status="machine_extracted_unreviewed", reason="x",
            )


def test_failed_gateway_run_blocks_promotion(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_segments(conn)
        # A Lane-2 run that fails (proposer raises) -> error_status='failed'.
        def _boom(conn, s, g):
            raise RuntimeError("provider exploded")
        ai.run_extraction(
            conn, run_id="r-fail", input_source_ids=[SOURCE_ID],
            input_segment_ids=[SEG_FINANCING], proposer=_boom,
        )
        # Manually land an AI row that names the failed run as its producer.
        stmt.insert_statement(
            conn,
            {
                "statement_id": "alpine:ai:from-failed",
                "segment_id": SEG_FINANCING,
                "statement_text": "AI paraphrase: a claim from a failed run.",
                "is_verbatim": 0,
                "produced_by": "ai",
                "confidence": "low",
                "ai_extraction_run_id": "r-fail",
            },
            [_pointer(93, "00:01:33")],
        )
        with pytest.raises(rg.ReviewerGateError):
            rg.promote_statement(
                conn, "alpine:ai:from-failed", reviewer_id="reviewer:isaac",
                decision="approved", to_verification_status="reviewed_source_linked",
                reason="trying to promote a failed-run row",
            )


def test_open_risk_flag_blocks_promotion_until_resolved(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        rg.run_risk(conn, run_id="r-risk",
                    input_statement_ids=[_PRIVACY_ID], input_source_ids=[SOURCE_ID])
        # The privacy no-go flag blocks promotion.
        with pytest.raises(rg.ReviewerGateError):
            rg.promote_statement(
                conn, _PRIVACY_ID, reviewer_id="reviewer:isaac", decision="approved",
                to_verification_status="reviewed_source_linked", reason="attempt",
            )
        # A reviewer resolves the no-go flags, then promotion succeeds.
        for flag in rg.open_risk_flags(conn, _PRIVACY_ID):
            rg.resolve_flag(conn, flag["flag_id"], reviewer_id="reviewer:isaac",
                            reason="confirmed false positive (synthetic fixture)")
        out = rg.promote_statement(
            conn, _PRIVACY_ID, reviewer_id="reviewer:isaac", decision="approved",
            to_verification_status="reviewed_source_linked", reason="cleared + grounded",
        )
        assert out["to_verification_status"] == "reviewed_source_linked"


def test_valid_promotion_reviews_but_never_publishes(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        rg.run_risk(conn, run_id="r-risk", input_statement_ids=[_CLEAN_ID])
        out = rg.promote_statement(
            conn, _CLEAN_ID, reviewer_id="reviewer:isaac", decision="approved",
            to_verification_status="reviewed_source_linked",
            reason="paraphrase grounded in seg-0003", reason_category="source_match",
        )
        row = conn.execute(
            "SELECT verification_status, review_state, publication_state, ui_status "
            "FROM statements WHERE statement_id = ?", (_CLEAN_ID,)
        ).fetchone()
        # promoted to reviewed; review_state recorded.
        assert row["verification_status"] == "reviewed_source_linked"
        assert row["review_state"] == "reviewed"
        # NEVER publishable from the reviewer gate (owner P8 only).
        assert row["publication_state"] == "not_publishable"
        assert out["publication_state"] == "not_publishable"
        # an audit row exists (who/what/why).
        dec = rg.latest_decision(conn, _CLEAN_ID)
        assert dec["reviewer_id"] == "reviewer:isaac"
        assert dec["decision"] == "approved"
        assert dec["promoted"] == 1
        assert dec["from_verification_status"] == "machine_extracted_unreviewed"
        assert dec["to_verification_status"] == "reviewed_source_linked"
        # belt-and-braces: still publication-blocked because not owner-approved.
        assert rg.statement_publication_blocked(conn, _CLEAN_ID) is True


def test_terminal_decisions_are_recorded(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        out = rg.promote_statement(
            conn, _LEGAL_ID, reviewer_id="reviewer:isaac", decision="rejected",
            reason="accusation about a named individual — do not publish",
            reason_category="legal",
        )
        assert out["to_verification_status"] == "do_not_publish"
        row = conn.execute(
            "SELECT verification_status, ui_status FROM statements WHERE statement_id = ?",
            (_LEGAL_ID,),
        ).fetchone()
        assert row["verification_status"] == "do_not_publish"
        assert row["ui_status"] == "do-not-publish"


def test_statement_publication_blocked_failclosed(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _setup(conn)
        # unreviewed -> blocked.
        assert rg.statement_publication_blocked(conn, _CLEAN_ID) is True
        # unknown statement -> blocked.
        assert rg.statement_publication_blocked(conn, "does-not-exist") is True


# --- data-publication boundary (done-bar 12) --------------------------------

def test_side_tables_not_web_projected(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        risk_cols = _columns(conn, rg.RISK_TABLE)
        dec_cols = _columns(conn, rg.DECISION_TABLE)
    assert not (risk_cols & pub.WEB_SAFE_FIELD_ALLOWLIST), "risk flag columns must not be web-safe"
    assert not (dec_cols & pub.WEB_SAFE_FIELD_ALLOWLIST), "decision columns must not be web-safe"


def test_to_web_safe_drops_risk_and_decision_fields() -> None:
    record = {
        "matched_signal": "street_address:742 Evergreen Terrace",
        "reason": "internal reviewer note",
        "reviewer_id": "reviewer:isaac",
        "risk_category": "privacy",
        "decision": "rejected",
        "source_id": "alpine:video:2026-05-08-regular",  # the one allow-listed field
    }
    safe = pub.to_web_safe(record)
    assert safe == {"source_id": "alpine:video:2026-05-08-regular"}
