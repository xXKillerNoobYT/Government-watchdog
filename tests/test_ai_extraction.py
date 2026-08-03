"""Tests for the Lane-2 AI extraction adapter + run ledger (GOV-89, Slice 3 B).

Covers the GOV-89 AI-specific done-bar (items 7-11) + the migration-0009 plan
(GOV-88 §5 D-1/D-2/D-4/D-5):

- migration 0009 is additive + idempotent; the `statements` CHECK rebuild
  preserves every landed row (count + content digest identical pre/post 0009),
  leaves `PRAGMA foreign_key_check` empty, and actually widens produced_by so an
  'ai' row now lands at the DB layer;
- every AI-written row carries produced_by='ai' + machine_extracted_unreviewed +
  not_publishable + its run provenance (done-bar 7);
- a no-orphan AI claim (no evidence_link, no segment) is rejected (done-bar 8);
- attribution safety: an uncertain AI speaker drops the name, never a wrong name
  (done-bar 9);
- the gateway run-log records input set / model+tool/prompt version / outputs /
  errors / reviewer state / retry (done-bar 10);
- fail-closed: a failed run blocks downstream, and the reviewer gate blocks
  promotion until a human approves (done-bar 11);
- data-publication boundary: the ledger + run-provenance columns are NOT
  web-projected (done-bar 12).

No AI, no network: pure sqlite + an injected deterministic proposer + the
committed sanitized Alpine fixture.
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
import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import speakers as spk  # noqa: E402
import statements as stmt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
SOURCE_ID = "alpine:video:2026-05-08-regular"

# The original (pre-0009) statements columns — the digest domain for the
# rebuild-preservation test.
_STATEMENTS_0007_COLUMNS = (
    "statement_id", "segment_id", "agenda_item_id", "speaker_attribution_id",
    "statement_text", "is_verbatim", "layer", "produced_by", "verification_status",
    "correction_status", "review_state", "publication_state", "source_changed",
    "ui_status", "confidence", "updates_statement_id", "created_utc",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _migrated(tmp_path: Path) -> Path:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    return db_path


def _seed_source(conn, source_id: str = SOURCE_ID) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "Alpine Council 2026-05-08 video", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _seed_segment(conn, *, source_id: str = SOURCE_ID,
                  segment_id: str = "alpine-sample-0001:seg-0000") -> str:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], "2026-05-08", tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            "Transcripts/2026/alpine-sample-0001.json", "0" * 64, _now(), source_id,
        ),
    )
    tid = int(cur.lastrowid)
    seg.segment_transcript(conn, tid, source_id=source_id)
    return segment_id


def _good_pointer(**overrides) -> dict:
    pointer = {
        "to_source_id": SOURCE_ID,
        "relation": "references",
        "locator_kind": "timestamp",
        "timestamp_seconds": 2533,
        "timestamp_human": "00:42:13",
        "original_url": "https://example.gov/video",
        "archive_status": "available",
        "scan_date": "2026-05-10",
        "captured_at_utc": "2026-05-10T17:04:22Z",
        "verification_status": "machine_extracted_unreviewed",
        "confidence": "high",
    }
    pointer.update(overrides)
    return pointer


def _static_proposer(claims):
    """A deterministic, offline proposer that returns a fixed claim list."""
    def _p(conn, source_ids, segment_ids):
        # deep-ish copy so the adapter's setdefault mutations don't leak across runs
        return [dict(c, evidence_links=[dict(p) for p in c.get("evidence_links", [])])
                for c in claims]
    return _p


def _table_sql(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0]


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# --- migration 0009: schema shape ------------------------------------------

def test_migration_creates_ledger_and_provenance_columns(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        run_cols = _columns(conn, "ai_extraction_runs")
        stmt_cols = _columns(conn, "statements")
        ev_cols = _columns(conn, "evidence_links")
    for required in (
        "run_id", "lane", "input_source_ids", "input_segment_ids", "model_name",
        "model_version", "tool_version", "prompt_id", "output_statement_ids",
        "output_evidence_link_ids", "output_count", "orphan_rejected_count",
        "error_status", "error_detail", "reviewer_state", "retry_of_run_id",
        "retry_count", "dry_run", "started_utc", "finished_utc",
    ):
        assert required in run_cols, f"ai_extraction_runs.{required} missing"
    assert "ai_extraction_run_id" in stmt_cols
    assert "ai_extraction_run_id" in ev_cols


def test_migration_0009_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must be a no-op, never raise
    with db.open_db(db_path) as conn:
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        stmt_cols = [r[1] for r in conn.execute("PRAGMA table_info(statements)")]
        run_count = conn.execute("SELECT COUNT(*) FROM ai_extraction_runs").fetchone()[0]
    assert "0009_ai_extraction_runs" in ledger
    assert stmt_cols.count("statement_id") == 1
    assert stmt_cols.count("ai_extraction_run_id") == 1
    assert run_count == 0


def test_produced_by_check_widened_to_include_ai(tmp_path: Path) -> None:
    # CTO D-1: the DB-level CHECK (not just the app set) must permit 'ai'.
    with db.open_db(_migrated(tmp_path)) as conn:
        sql = _table_sql(conn, "statements")
    for value in pub.ALLOWED_PRODUCED_BY:  # {automation, ai, human}
        assert f"'{value}'" in sql, f"statements.produced_by CHECK missing {value!r}"


def test_app_and_db_produced_by_parity(tmp_path: Path) -> None:
    # D-4 parity guard: the app-layer set == the SSOT set, and the DB CHECK agrees.
    assert stmt.ALLOWED_STATEMENT_PRODUCED_BY == pub.ALLOWED_PRODUCED_BY
    assert "ai" in stmt.ALLOWED_STATEMENT_PRODUCED_BY


# --- migration 0009: the statements rebuild preserves data (CTO D-1 ruling) --

def _statements_digest(conn) -> tuple[int, str]:
    rows = conn.execute(
        f"SELECT {', '.join(_STATEMENTS_0007_COLUMNS)} FROM statements ORDER BY statement_id"
    ).fetchall()
    payload = json.dumps([[r[c] for c in _STATEMENTS_0007_COLUMNS] for r in rows],
                         sort_keys=True, default=str)
    return len(rows), hashlib.sha256(payload.encode()).hexdigest()


def test_statements_rebuild_preserves_rows_and_widens_check(tmp_path, monkeypatch) -> None:
    """Apply 0001-0008, populate, then apply 0009; assert lossless + widened."""
    mig_dir = ROOT / "Database" / "migrations"
    pre = tmp_path / "migrations_pre"
    pre.mkdir()
    for f in sorted(mig_dir.glob("*.sql")):
        if f.stem <= "0008_speakers_persons_roles_temporal":
            (pre / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    db_path = tmp_path / "rebuild.db"
    monkeypatch.setattr(db, "MIGRATIONS_DIR", pre)
    db.apply_migrations(db_path)

    # Populate under the OLD (0007) schema via raw SQL (no ai_extraction_run_id col).
    with db.open_db(db_path) as conn:
        assert "ai_extraction_run_id" not in _columns(conn, "statements")
        for sid, pb in (("s-auto", "automation"), ("s-human", "human")):
            conn.execute(
                "INSERT INTO statements (statement_id, statement_text, produced_by, created_utc) "
                "VALUES (?, ?, ?, ?)",
                (sid, f"text for {sid}", pb, _now()),
            )
        conn.commit()
        pre_count, pre_digest = _statements_digest(conn)
        # The OLD CHECK must reject 'ai' (proving the rebuild is what unlocks it).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO statements (statement_id, statement_text, produced_by) "
                "VALUES ('s-ai-blocked', 't', 'ai')"
            )
        conn.rollback()

    # Now add 0009 and re-apply.
    full = tmp_path / "migrations_full"
    full.mkdir()
    for f in sorted(mig_dir.glob("*.sql")):
        (full / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", full)
    db.apply_migrations(db_path)

    with db.open_db(db_path) as conn:
        post_count, post_digest = _statements_digest(conn)
        assert post_count == pre_count == 2
        assert post_digest == pre_digest, "rebuild altered landed statement data"
        # FK integrity intact after the drop/rename.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        # The widened CHECK now lets an 'ai' row land at the DB layer.
        conn.execute(
            "INSERT INTO statements (statement_id, statement_text, produced_by, ai_extraction_run_id) "
            "VALUES ('s-ai-ok', 't', 'ai', NULL)"
        )
        conn.commit()
        assert conn.execute(
            "SELECT produced_by FROM statements WHERE statement_id='s-ai-ok'"
        ).fetchone()[0] == "ai"

    # Idempotent: a third apply is a no-op (ledger skip).
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        again_count, again_digest = _statements_digest(conn)
    assert again_count == 3  # the two originals + s-ai-ok, untouched


# --- done-bar 7: every AI row carries the AI provenance + fail-closed defaults --

def _seed_ai_claim(seg_id: str, statement_id: str = "alpine:ai:stmt-0001", **over) -> dict:
    claim = {
        "statement_id": statement_id,
        "segment_id": seg_id,
        "statement_text": "AI paraphrase: the council discussed the financing gap.",
        "is_verbatim": 0,
        "confidence": "medium",
        "evidence_links": [_good_pointer()],
    }
    claim.update(over)
    return claim


def test_ai_rows_carry_provenance_and_failclosed_defaults(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        result = ai.run_extraction(
            conn,
            run_id="alpine:ai-extract:2026-06-08:001",
            input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id],
            proposer=_static_proposer([_seed_ai_claim(seg_id)]),
            tool_version="gov-lane2@test",
            model_name="claude-test",
            model_version="v0",
        )
        assert result["ok"] is True
        assert result["output_count"] == 1
        rows = conn.execute(
            "SELECT produced_by, verification_status, review_state, publication_state, "
            "is_verbatim, layer, ai_extraction_run_id FROM statements"
        ).fetchall()
        ev_runs = conn.execute(
            "SELECT DISTINCT ai_extraction_run_id FROM evidence_links"
        ).fetchall()
    assert len(rows) == 1
    for row in rows:
        assert row["produced_by"] == "ai"
        assert row["verification_status"] == "machine_extracted_unreviewed"
        assert row["review_state"] == "unreviewed"
        assert row["publication_state"] == "not_publishable"
        assert row["is_verbatim"] == 0          # AI paraphrase, never verbatim
        assert row["layer"] == "ai_thought_then"
        assert row["ai_extraction_run_id"] == "alpine:ai-extract:2026-06-08:001"
    assert [r[0] for r in ev_runs] == ["alpine:ai-extract:2026-06-08:001"]


def test_ai_gating_fields_overridden_even_if_proposer_lies(tmp_path: Path) -> None:
    # Defense-in-depth: a proposer that tries to set gating fields is overridden.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        sneaky = _seed_ai_claim(seg_id, statement_id="alpine:ai:sneaky")
        sneaky.update({
            "produced_by": "human",
            "verification_status": "human_verified",
            "publication_state": "publishable",
            "review_state": "approved",
        })
        ai.run_extraction(
            conn, run_id="r-sneaky", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id], proposer=_static_proposer([sneaky]),
        )
        row = conn.execute(
            "SELECT produced_by, verification_status, publication_state, review_state "
            "FROM statements WHERE statement_id='alpine:ai:sneaky'"
        ).fetchone()
    assert row["produced_by"] == "ai"
    assert row["verification_status"] == "machine_extracted_unreviewed"
    assert row["publication_state"] == "not_publishable"
    assert row["review_state"] == "unreviewed"


# --- done-bar 8: no-orphan-claims (AI claim without evidence_link rejected) --


def test_layer_and_is_verbatim_are_overridden_too(tmp_path: Path) -> None:
    """GOV-1717 completes the override set the lane-2 contract already named.

    Invariant 1 says an AI row is "forced to ... layer='ai_thought_then',
    is_verbatim=0 ... regardless of proposer output". Until GOV-1717 those two
    were READ FROM THE CLAIM, so a proposer supplying nothing already produced
    known_then/verbatim rows. `test_ai_gating_fields_overridden_even_if_proposer_lies`
    sets only the five fields the code did override, which is why it passed
    throughout.
    """
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        liar = _seed_ai_claim(seg_id, layer="known_then", is_verbatim=1)
        liar["evidence_links"][0].update(
            {"layer": "actual_later", "is_verbatim": 1,
             "verification_status": "human_verified"})
        ai.run_extraction(
            conn, run_id="r-liar-2", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id], proposer=_static_proposer([liar]),
            tool_version="gov-lane2@test")
        stmt = conn.execute(
            "SELECT layer, is_verbatim FROM statements WHERE produced_by='ai'").fetchone()
        link = conn.execute(
            "SELECT layer, is_verbatim, verification_status FROM evidence_links").fetchone()

    assert stmt["layer"] == ai.AI_LAYER, "the proposer's layer won on the statement"
    assert stmt["is_verbatim"] == ai.AI_IS_VERBATIM
    assert link["layer"] == ai.AI_LAYER, "the proposer's layer won on the link"
    assert link["is_verbatim"] == ai.AI_IS_VERBATIM
    assert link["verification_status"] == ai.AI_ENTRY_VERIFICATION_STATUS, (
        "an AI evidence link kept a proposer-supplied verification_status — it "
        "would project to the web claiming human review")

def test_orphan_ai_claim_rejected(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        orphan = {
            "statement_id": "alpine:ai:orphan",
            "statement_text": "Unsupported AI claim with no source pointer.",
            "is_verbatim": 0,
            "evidence_links": [],   # no pointer
            # no segment_id -> orphan
        }
        result = ai.run_extraction(
            conn, run_id="r-orphan", input_source_ids=[SOURCE_ID],
            proposer=_static_proposer([orphan]),
        )
        n_stmt = conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
        run = ai.get_run(conn, "r-orphan")
    assert n_stmt == 0                        # nothing written
    assert result["output_count"] == 0
    assert len(result["rejected"]) == 1
    assert run["orphan_rejected_count"] == 1
    assert run["error_status"] == "failed"    # all claims rejected -> failed run


def test_partial_run_when_some_claims_orphan(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        good = _seed_ai_claim(seg_id, statement_id="alpine:ai:good")
        orphan = {"statement_id": "alpine:ai:bad", "statement_text": "no anchor",
                  "is_verbatim": 0, "evidence_links": []}
        result = ai.run_extraction(
            conn, run_id="r-partial", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id], proposer=_static_proposer([good, orphan]),
        )
        run = ai.get_run(conn, "r-partial")
    assert result["output_count"] == 1
    assert run["orphan_rejected_count"] == 1
    assert run["error_status"] == "partial"


# --- done-bar 9: attribution safety (uncertain speaker -> no name) ----------

def test_uncertain_ai_speaker_drops_name(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        candidate_name = "Pat Maxwell"
        person_id = "alpine:person:pat-maxwell"
        conn.execute(
            "INSERT INTO persons (person_id, display_name, person_type, created_utc) "
            "VALUES (?, ?, 'official', ?)",
            (person_id, candidate_name, _now()),
        )
        conn.commit()
        claim = _seed_ai_claim(seg_id, statement_id="alpine:ai:spoke")
        # The AI *guesses* an official by name with high confidence — it must still
        # NOT be named, because AI can never confirm an identity (person_confirmed=False).
        claim["speaker"] = {
            "candidate_person_id": person_id,
            "speaker_class": "on-record-official",
            "role_title": "Mayor",
            "confidence": "high",
        }
        ai.run_extraction(
            conn, run_id="r-attr", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id], proposer=_static_proposer([claim]),
        )
        attr = conn.execute(
            "SELECT attribution_state, person_id, candidate_person_id, display_label "
            "FROM speaker_attributions WHERE statement_id='alpine:ai:spoke'"
        ).fetchone()
        made = conn.execute(
            "SELECT COUNT(*) FROM made_statement WHERE statement_id='alpine:ai:spoke'"
        ).fetchone()[0]
    assert attr is not None
    assert attr["attribution_state"] != "attributed"     # never auto-attributed by AI
    assert attr["person_id"] is None                      # no bound identity
    assert candidate_name not in (attr["display_label"] or "")   # name never rendered
    assert attr["candidate_person_id"] == person_id       # guess survives as vault-only hint
    assert made == 0                                       # no made_statement edge


# --- done-bar 10: gateway run-log records the required fields ----------------

def test_run_log_records_required_fields(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        ai.run_extraction(
            conn, run_id="r-log", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id], proposer=_static_proposer([_seed_ai_claim(seg_id)]),
            tool_version="gov-lane2@abc123", model_name="claude-test", model_version="v9",
            retry_of_run_id=None, retry_count=0, dry_run=True,
        )
        run = ai.get_run(conn, "r-log")
    # input set
    assert json.loads(run["input_source_ids"]) == [SOURCE_ID]
    assert json.loads(run["input_segment_ids"]) == [seg_id]
    # model/tool/prompt version
    assert run["model_name"] == "claude-test"
    assert run["model_version"] == "v9"
    assert run["tool_version"] == "gov-lane2@abc123"
    assert run["prompt_id"] == ai.PROMPT_ID
    # outputs / errors / reviewer state / retry / timing
    assert json.loads(run["output_statement_ids"]) == ["alpine:ai:stmt-0001"]
    assert run["output_count"] == 1
    assert run["error_status"] == "ok"
    assert run["reviewer_state"] == "unreviewed"
    assert run["retry_count"] == 0
    assert run["dry_run"] == 1
    assert run["started_utc"] and run["finished_utc"]


def test_retry_chain_is_forward_only(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        ai.run_extraction(conn, run_id="r-1", input_source_ids=[SOURCE_ID],
                          input_segment_ids=[seg_id],
                          proposer=_static_proposer([_seed_ai_claim(seg_id)]))
        ai.run_extraction(conn, run_id="r-2", input_source_ids=[SOURCE_ID],
                          input_segment_ids=[seg_id],
                          proposer=_static_proposer([_seed_ai_claim(seg_id, statement_id="alpine:ai:retry")]),
                          retry_of_run_id="r-1", retry_count=1)
        r2 = ai.get_run(conn, "r-2")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert r2["retry_of_run_id"] == "r-1"
    assert r2["retry_count"] == 1
    assert violations == []


# --- done-bar 11: fail-closed (reviewer gate + failed run block downstream) --

def test_failed_run_blocks_downstream(tmp_path: Path) -> None:
    def _boom(conn, s, seg):
        raise RuntimeError("model unavailable")
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        result = ai.run_extraction(
            conn, run_id="r-fail", input_source_ids=[SOURCE_ID], proposer=_boom,
        )
        run = ai.get_run(conn, "r-fail")
        n_stmt = conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
    assert result["error_status"] == "failed"
    assert n_stmt == 0
    assert run["error_status"] == "failed"
    assert ai.outputs_publication_blocked(run) is True


def test_provider_not_configured_is_failclosed(tmp_path: Path) -> None:
    # No proposer + offline config -> a failed run, nothing written (never a live call).
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        result = ai.run_extraction(
            conn, run_id="r-noprov", input_source_ids=[SOURCE_ID],
            proposer=None, provider_config={"provider": "offline-disabled"},
        )
        run = ai.get_run(conn, "r-noprov")
    assert result["error_status"] == "failed"
    assert "ProviderNotConfigured" in (run["error_detail"] or "")


def test_reviewer_gate_blocks_promotion_until_approved(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        ai.run_extraction(
            conn, run_id="r-gate", input_source_ids=[SOURCE_ID], input_segment_ids=[seg_id],
            proposer=_static_proposer([_seed_ai_claim(seg_id)]),
        )
        unreviewed = ai.get_run(conn, "r-gate")
        assert unreviewed["error_status"] == "ok"
        # OK but unreviewed -> still blocked downstream (human gate not passed).
        assert ai.outputs_publication_blocked(unreviewed) is True

        ai.set_reviewer_state(conn, "r-gate", "approved")
        approved = ai.get_run(conn, "r-gate")
        assert ai.outputs_publication_blocked(approved) is False

        # Even after run-level approval, the row stays not_publishable at the DB
        # layer (a separate, explicit reviewed transition flips that; AI never does).
        pub_state = conn.execute(
            "SELECT publication_state FROM statements WHERE ai_extraction_run_id='r-gate'"
        ).fetchone()[0]
    assert pub_state == "not_publishable"


# --- done-bar 12: data-publication boundary (ledger never web-projected) -----

def test_ledger_and_provenance_not_web_projected() -> None:
    ledger_cols = {
        "run_id", "lane", "input_source_ids", "input_segment_ids", "model_name",
        "model_version", "tool_version", "prompt_id", "output_statement_ids",
        "output_evidence_link_ids", "output_count", "orphan_rejected_count",
        "error_status", "error_detail", "reviewer_state", "retry_of_run_id",
        "retry_count", "dry_run", "started_utc", "finished_utc",
        "ai_extraction_run_id",
    }
    leaked = ledger_cols & pub.WEB_SAFE_FIELD_ALLOWLIST
    assert leaked == set(), f"ledger/provenance fields leak to web: {leaked}"


def test_to_web_safe_drops_run_provenance() -> None:
    record = {
        "source_id": "alpine:x", "produced_by": "ai",          # allowlisted
        "ai_extraction_run_id": "r-secret", "error_detail": "stack trace",  # not
    }
    safe = pub.to_web_safe(record)
    assert "ai_extraction_run_id" not in safe
    assert "error_detail" not in safe
    assert safe.get("produced_by") == "ai"   # 'ai' producer IS publicly labelable


# --- prompt + vocabulary parity --------------------------------------------

def test_prompt_requires_source_grounding_and_uncertainty() -> None:
    p = ai.SOURCE_GROUNDED_PROMPT.lower()
    assert "source-grounded" in p or "source pointer" in p
    assert "uncertain" in p
    assert "unsupported allegation" in p or "no unsupported" in p
    assert "no name" in p  # no name is better than a wrong name


def test_run_vocab_matches_check_literals(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        sql = _table_sql(conn, "ai_extraction_runs")
    for value in ai.ALLOWED_LANES:
        assert f"'{value}'" in sql
    for value in ai.ALLOWED_RUN_ERROR_STATUS:
        assert f"'{value}'" in sql
    for value in ai.ALLOWED_RUN_REVIEWER_STATE:
        assert f"'{value}'" in sql


# --- GOV-1710 (C4): AI provenance was only ever checked on `statements` --------
#
# `slice3_smoke._check_ai_provenance` — the reference smoke's provenance gate —
# runs one query, `SELECT ... FROM statements`. Measured: the string
# `evidence_links` does not appear in it. So the check whose entire job is
# proving AI provenance is blind to half the surface AI writes, and that is why
# backend #256 (evidence links carry no AI bindings at all) went unnoticed
# through every smoke run.
#
# These tests give the links half the coverage the statements half already has.
# The correctness assertion is `xfail(strict=True)` because #256 is open and
# unfixed: today it xfails and the suite stays green, and the moment #256 lands
# it XPASSes — which `strict=True` turns into a failure telling the next person
# to delete the marker. A ratchet that fires on the FIX, not on the bug.


def _ai_links(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT evidence_link_id, layer, is_verbatim, verification_status, "
        "ai_extraction_run_id FROM evidence_links ORDER BY evidence_link_id"
    ).fetchall()
    return [dict(r) for r in rows]


def test_the_ai_statement_half_IS_bound_so_the_links_failure_is_specific(tmp_path):
    """Non-vacuity, and it localises the defect.

    If the adapter were broken for everything, the links assertion below would be
    unremarkable. It is not: the statement half binds `produced_by`,
    `verification_status` and `review_state` correctly. The gap is specific to
    evidence links, which is what makes it easy to miss.
    """
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        ai.run_extraction(
            conn, run_id="r-links-a", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id],
            proposer=_static_proposer([_seed_ai_claim(seg_id)]),
            tool_version="gov-lane2@test")
        row = conn.execute(
            "SELECT produced_by, verification_status, review_state, layer, is_verbatim "
            "FROM statements WHERE produced_by = 'ai'").fetchone()

    assert row is not None, "no AI statement written — the fixture stopped working"
    assert row["produced_by"] == ai.AI_PRODUCED_BY
    assert row["verification_status"] == ai.AI_ENTRY_VERIFICATION_STATUS
    assert row["review_state"] == ai.AI_REVIEW_STATE
    assert row["layer"] == ai.AI_LAYER, (
        "the STATEMENT half stopped defaulting to the AI layer — that is a "
        "different and larger regression than the links gap these tests cover")


def test_an_ai_run_writes_evidence_links_at_all(tmp_path):
    """The precondition for the xfail below. Without it that test proves nothing."""
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        ai.run_extraction(
            conn, run_id="r-links-b", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id],
            proposer=_static_proposer([_seed_ai_claim(seg_id)]),
            tool_version="gov-lane2@test")
        links = _ai_links(conn)

    assert links, "the AI run wrote no evidence_links; the guard below is vacuous"
    assert links[0]["ai_extraction_run_id"] == "r-links-b", (
        "the one field the adapter DOES bind on a link stopped being bound")


def test_ai_evidence_links_are_labelled_consistently_with_their_statement(tmp_path):
    """An AI link must not claim to be a verbatim, known-then record.

    Was `xfail(strict=True)` from GOV-1710 until GOV-1717 forced the bindings.
    The ratchet worked as designed: applying the fix turned this into
    `[XPASS(strict)]`, which failed the suite and named the marker to remove.
    Left as a plain passing test — the behaviour it describes is now real.

    The proposer here supplies **no** `layer` and **no** `is_verbatim` — it is not
    lying, it is simply silent. The defaults alone are enough: the link persists
    as `known_then` / verbatim while the statement it belongs to is
    `ai_thought_then` / paraphrase. Both fields are on
    `publication.WEB_SAFE_FIELD_ALLOWLIST`, and `evidence_links` has no
    `produced_by` column, so nothing downstream can tell the link came from AI.
    """
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        seg_id = _seed_segment(conn)
        ai.run_extraction(
            conn, run_id="r-links-c", input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id],
            proposer=_static_proposer([_seed_ai_claim(seg_id)]),
            tool_version="gov-lane2@test")
        links = _ai_links(conn)
        stmt = conn.execute(
            "SELECT layer, is_verbatim FROM statements WHERE produced_by='ai'").fetchone()

    assert links
    for link in links:
        assert link["layer"] == stmt["layer"], (
            f"AI evidence link {link['evidence_link_id']} is layer="
            f"{link['layer']!r} while its statement is {stmt['layer']!r}")
        assert link["is_verbatim"] == stmt["is_verbatim"], (
            f"AI evidence link {link['evidence_link_id']} claims "
            f"is_verbatim={link['is_verbatim']} while its statement says "
            f"{stmt['is_verbatim']}")


# --- GOV-1714 (C7b hunt): a crashed run recorded itself as HEALTHY --------------
#
# `run_extraction`'s docstring promises it "never raises on a proposer error (it is
# recorded as error_status='failed' instead — fail-closed and auditable)".
#
# Measured 2026-08-02, before the fix: a claim carrying an invalid `speaker_class`
# raised `ValueError` straight out of `run_extraction`, because `_apply_ai_speaker`
# sat AFTER the per-claim try/except. The exception went past `finalize_run`, so
# the audit ledger kept its column default:
#
#     error_status='ok'   finished_utc=None   output_count=0
#
# A run that crashed, reading as healthy, on the ledger that exists to say what
# happened. The same shape as a duplicate `statement_id`: `insert_statement` raises
# `sqlite3.IntegrityError`, which is NOT a `ValueError`, so it escaped the same way
# — and that is what a contract-sanctioned retry (`retry_of_run_id`) does.


class TestARunThatFailsNeverRecordsItselfAsOk:
    """The ledger is the audit record. It must not say `ok` for a run that died."""

    def _run(self, conn, seg_id, claim, run_id):
        return ai.run_extraction(
            conn, run_id=run_id, input_source_ids=[SOURCE_ID],
            input_segment_ids=[seg_id], proposer=_static_proposer([claim]),
            tool_version="gov-lane2@test")

    def test_an_invalid_speaker_class_is_rejected_not_raised(self, tmp_path):
        with db.open_db(_migrated(tmp_path)) as conn:
            _seed_source(conn)
            seg_id = _seed_segment(conn)
            claim = _seed_ai_claim(seg_id)
            claim["speaker"] = {"speaker_class": "not-a-real-class",
                                "role_only_label": "Council Member"}

            out = self._run(conn, seg_id, claim, "r-bad-speaker")

            row = conn.execute(
                "SELECT error_status, finished_utc FROM ai_extraction_runs "
                "WHERE run_id = 'r-bad-speaker'").fetchone()

        assert out["error_status"] != "ok", (
            "a run whose only claim was rejected reported ok")
        assert row["error_status"] != "ok", (
            f"the LEDGER says {row['error_status']!r} for a run that rejected "
            "everything. Before GOV-1714 this said 'ok' because the exception "
            "escaped past finalize_run entirely.")
        assert row["finished_utc"] is not None, (
            "finished_utc is NULL — finalize_run never ran, so the row is the "
            "column default rather than a record of what happened")

    def test_a_duplicate_statement_id_is_rejected_not_raised(self, tmp_path):
        """`sqlite3.IntegrityError` is not a `ValueError` — it used to escape.

        This is the path a sanctioned retry takes: the contract's `retry_of_run_id`
        expects re-proposing, and a re-proposed claim collides on `statement_id`.
        """
        with db.open_db(_migrated(tmp_path)) as conn:
            _seed_source(conn)
            seg_id = _seed_segment(conn)
            self._run(conn, seg_id, _seed_ai_claim(seg_id), "r-first")

            out = self._run(conn, seg_id, _seed_ai_claim(seg_id), "r-retry")

            row = conn.execute(
                "SELECT error_status, finished_utc FROM ai_extraction_runs "
                "WHERE run_id = 'r-retry'").fetchone()

        assert out["error_status"] != "ok"
        assert row["error_status"] != "ok", (
            "the ledger says ok for a run whose write collided")
        assert row["finished_utc"] is not None
        assert out["rejected"], "the collision was not recorded as a rejection"

    def test_a_clean_run_still_records_ok(self, tmp_path):
        """Non-vacuity: the fix must not mark every run failed."""
        with db.open_db(_migrated(tmp_path)) as conn:
            _seed_source(conn)
            seg_id = _seed_segment(conn)
            out = self._run(conn, seg_id, _seed_ai_claim(seg_id), "r-clean")
            row = conn.execute(
                "SELECT error_status, finished_utc FROM ai_extraction_runs "
                "WHERE run_id = 'r-clean'").fetchone()

        assert out["error_status"] == "ok", (
            f"a clean run reported {out['error_status']!r} — the rejection path is "
            "now swallowing good claims")
        assert row["error_status"] == "ok"
        assert row["finished_utc"] is not None
