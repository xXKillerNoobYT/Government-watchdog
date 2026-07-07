"""GOV-634: automation & credit-efficiency implementation (GOV-631 T1–T6).

Pins the credit-spend gates from the plan of record
(``Docs/gov-631-automation-credit-efficiency-plan.md`` @ ``4b0c47c``):

- T2 hash gate: an unchanged source gets ZERO processing on a re-run, logged
  ``skipped:hash`` (summary + `crawl_runs.skipped_hash` + notes); a dry-run
  plans the same outcome READ-ONLY against an existing DB.
- T1 runner: dry-run default, pilot scope only, full scope REFUSES naming the
  owner card, run logs embed metering, a gateway row appearing during a
  deterministic run is a flagged credit anomaly.
- T3 batch queue: pending set is hash-gated by prior successful lane-2 runs,
  batches default to the floor model, tier escalation without a logged
  low-confidence record refuses.
- T4 metering: ai_calls counts only live (`dry_run=0`) gateway rows; token/cost
  aggregation by model; honest ``None`` ratios on zero denominators.
- T5 filer: exactly the defined failure patterns file, with flood-proof dedupe
  keys, dry-run default, and CTO as assignee.

All offline/deterministic: tmp DBs, tmp corpora, injected proposers/transports.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import credit_metering as cm  # noqa: E402
import db  # noqa: E402
import failure_issue_filer as filer  # noqa: E402
import ingest_local_corpus as ing  # noqa: E402
import lane2_batch_queue as l2  # noqa: E402
import refresh_runner as rr  # noqa: E402
from raw_preservation import record_crawl_run  # noqa: E402

PILOT = rr.PILOT_ONLY_DATE  # 2026-06-23


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --- fixtures (GOV-621 pattern: tmp corpus + repo-root redirect) --------------

@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / PILOT).mkdir(parents=True)
    (root / PILOT / "MEET-Agenda_council.pdf").write_bytes(b"%PDF agenda v1")
    (root / PILOT / "Council_Packet.pdf").write_bytes(b"%PDF packet v1")
    return root


@pytest.fixture()
def patched_repo(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the managed raw store into tmp (never touch the real repo)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(ing, "REPO_ROOT", repo)
    return repo


def _migrated(tmp_path: Path, name: str = "t.db") -> Path:
    db_path = tmp_path / name
    db.apply_migrations(db_path)
    return db_path


def _seed_source(conn, source_id: str = "alpine_local_corpus") -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "seed", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _seed_segments(conn, source_id: str, n: int = 2, sha: str = "a" * 64) -> list[str]:
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, full_text, "
        "local_path, sha256, fetch_time_utc, source_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"vid-{sha[:8]}", "file:///tmp/x", PILOT, "full", "Transcripts/x.json",
         sha, _now(), source_id),
    )
    tid = int(cur.lastrowid)
    seg_ids = []
    for i in range(n):
        sid = f"seg-{sha[:6]}-{i:04d}"
        conn.execute(
            "INSERT INTO transcript_segments (segment_id, transcript_id, source_id, "
            "segment_index, timestamp_seconds, timestamp_human, segment_text, created_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, tid, source_id, i, i * 30, f"00:00:{i * 30:02d}", f"text {i}", _now()),
        )
        seg_ids.append(sid)
    conn.commit()
    return seg_ids


# --- T2: hash-skip end-to-end --------------------------------------------------

def test_migration_0019_is_additive_and_idempotent(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    db.apply_migrations(db_path)  # re-run: must be a no-op, not an error
    with db.open_db(db_path) as conn:
        crawl_cols = {r[1] for r in conn.execute("PRAGMA table_info(crawl_runs)")}
        gw_cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_extraction_runs)")}
    assert "skipped_hash" in crawl_cols
    assert {"tokens_input", "tokens_output", "estimated_cost_usd",
            "model_tier", "escalated_from_run_id", "low_confidence_items"} <= gw_cols


def test_second_run_skips_everything_by_hash(corpus: Path, patched_repo: Path,
                                             tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    first = ing.ingest(corpus, db_path, only_date=PILOT)
    assert first["new_documents"] == 2 and first["skipped_hash"] == 0

    second = ing.ingest(corpus, db_path, only_date=PILOT)
    assert second["skipped_hash"] == second["selected"] == 2
    assert second["new_documents"] == 0
    assert second["copied_to_raw_store"] == 0  # zero processing
    with db.open_db(db_path) as conn:
        run = conn.execute(
            "SELECT skipped_hash, notes FROM crawl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert run["skipped_hash"] == 2
    assert "skipped:hash=2" in run["notes"]


def test_changed_source_is_reprocessed_not_skipped(corpus: Path, patched_repo: Path,
                                                   tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    ing.ingest(corpus, db_path, only_date=PILOT)
    (corpus / PILOT / "Council_Packet.pdf").write_bytes(b"%PDF packet v2 CHANGED")
    third = ing.ingest(corpus, db_path, only_date=PILOT)
    assert third["skipped_hash"] == 1          # the unchanged agenda
    assert third["copied_to_raw_store"] == 1   # the changed packet re-snapshots
    assert third["new_documents"] == 0         # same source_url → upsert, not new


def test_dry_run_plans_skips_read_only(corpus: Path, patched_repo: Path,
                                       tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    ing.ingest(corpus, db_path, only_date=PILOT)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    plan = ing.ingest(corpus, db_path, dry_run=True, only_date=PILOT)
    assert plan["dry_run"] is True
    assert plan["planned"] == {"ingest_new": 0, "reprocess_changed": 0, "skipped_hash": 2}
    assert plan["skipped_hash"] == 2
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before  # untouched


def test_dry_run_without_db_reports_no_plan(corpus: Path, patched_repo: Path,
                                            tmp_path: Path) -> None:
    plan = ing.ingest(corpus, tmp_path / "missing.db", dry_run=True, only_date=PILOT)
    assert plan["planned"] is None            # never creates/migrates a DB
    assert not (tmp_path / "missing.db").exists()


# --- T1: refresh runner ---------------------------------------------------------

def test_runner_default_dry_run_logs_metering_and_zero_ai(corpus: Path,
                                                          patched_repo: Path,
                                                          tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    ing.ingest(corpus, db_path, only_date=PILOT)  # baseline apply

    record = rr.refresh(source_dir=corpus, db_path=db_path, apply=False,
                        log_dir=tmp_path / "logs")
    assert record["mode"] == "dry-run" and record["ok"] is True
    assert record["ingest"]["planned"]["skipped_hash"] == 2
    assert record["ai_run_delta"] == 0 and record["credit_anomaly"] is False
    assert record["metering"]["run_window"]["gateway"]["ai_calls"] == 0

    log = json.loads(Path(record["log_path"]).read_text(encoding="utf-8"))
    assert log["only_date"] == PILOT and log["deterministic"] is True
    assert log["metering"]["all_time"]["gateway"]["ai_calls"] == 0


def test_runner_full_scope_refuses_naming_owner_card(capsys) -> None:
    assert rr.main(["--scope", "full"]) == 2
    err = capsys.readouterr().err
    assert rr.FULL_INGEST_CARD in err and rr.FULL_INGEST_GATE_ISSUE in err


def test_runner_emit_cron_is_dry_run_schedule(capsys) -> None:
    assert rr.main(["--emit-cron"]) == 0
    out = capsys.readouterr().out
    assert "refresh_runner.py" in out and "--apply" not in out


def test_runner_flags_credit_anomaly_when_gateway_row_appears(tmp_path: Path,
                                                              monkeypatch) -> None:
    db_path = _migrated(tmp_path)

    def poisoned_ingest(source_dir, dbp, *, dry_run, only_date):
        with db.open_db(db_path) as conn:  # a lane-2 row sneaks in mid-"deterministic" run
            ai.create_run(conn, run_id="rogue-1", input_source_ids=[])
        return {"selected": 0, "new_documents": 0, "copied_to_raw_store": 0,
                "skipped_hash": 0, "failures": [], "planned": None,
                "by_doc_type": {}, "coverage": {}, "dry_run": dry_run, "run_id": None}

    monkeypatch.setattr(rr.ingest, "ingest", poisoned_ingest)
    record = rr.refresh(source_dir=tmp_path, db_path=db_path, apply=False,
                        log_dir=tmp_path / "logs")
    assert record["ai_run_delta"] == 1
    assert record["credit_anomaly"] is True and record["ok"] is False


# --- T3: lane-2 batch queue -----------------------------------------------------

def test_pending_queue_is_hash_gated_by_successful_runs(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        src = _seed_source(conn)
        seg_ids = _seed_segments(conn, src, n=2)
        assert [i["segment_id"] for i in l2.pending_items(conn)] == seg_ids

        # A successful run covers seg 0 → it leaves the queue permanently.
        ai.create_run(conn, run_id="ok-1", input_source_ids=[src],
                      input_segment_ids=[seg_ids[0]])
        ai.finalize_run(conn, "ok-1", output_statement_ids=[],
                        output_evidence_link_ids=[], orphan_rejected_count=0,
                        error_status="ok")
        assert [i["segment_id"] for i in l2.pending_items(conn)] == [seg_ids[1]]

        # A FAILED run does NOT cover: seg 1 stays pending (fail-closed retry).
        ai.create_run(conn, run_id="bad-1", input_source_ids=[src],
                      input_segment_ids=[seg_ids[1]])
        ai.finalize_run(conn, "bad-1", output_statement_ids=[],
                        output_evidence_link_ids=[], orphan_rejected_count=0,
                        error_status="failed")
        assert [i["segment_id"] for i in l2.pending_items(conn)] == [seg_ids[1]]


def test_batches_chunk_at_floor_model(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        src = _seed_source(conn)
        _seed_segments(conn, src, n=3)
        batches = l2.plan_batches(conn, batch_size=2)
    assert [len(b["segment_ids"]) for b in batches] == [2, 1]
    assert all(b["model_name"] == l2.FLOOR_MODEL for b in batches)
    assert all(b["model_tier"] == l2.TIER_FLOOR for b in batches)


def test_escalation_refuses_without_logged_low_confidence(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        src = _seed_source(conn)
        ai.create_run(conn, run_id="floor-1", input_source_ids=[src],
                      input_segment_ids=[])
        ai.finalize_run(conn, "floor-1", output_statement_ids=[],
                        output_evidence_link_ids=[], orphan_rejected_count=0,
                        error_status="ok")
        with pytest.raises(l2.EscalationWithoutReason):
            l2.escalate(conn, "floor-1", run_id="esc-1")


def test_escalation_reruns_only_logged_items_with_provenance(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        src = _seed_source(conn)
        seg_ids = _seed_segments(conn, src, n=2)
        # Floor run wrote one low- and one high-confidence statement.
        for sid, seg, conf in [("st-low", seg_ids[0], "low"), ("st-high", seg_ids[1], "high")]:
            conn.execute(
                "INSERT INTO statements (statement_id, segment_id, statement_text, "
                "confidence, produced_by) VALUES (?, ?, ?, ?, 'ai')",
                (sid, seg, "x", conf),
            )
        ai.create_run(conn, run_id="floor-1", input_source_ids=[src],
                      input_segment_ids=seg_ids, model_name=l2.FLOOR_MODEL,
                      dry_run=False)
        ai.finalize_run(conn, "floor-1", output_statement_ids=["st-low", "st-high"],
                        output_evidence_link_ids=[], orphan_rejected_count=0,
                        error_status="ok")

        items = l2.record_low_confidence(conn, "floor-1")
        assert [i["statement_id"] for i in items] == ["st-low"]

        result = l2.escalate(conn, "floor-1", run_id="esc-1",
                             proposer=lambda c, s, g: [], dry_run=False,
                             usage={"tokens_input": 1200, "tokens_output": 300,
                                    "estimated_cost_usd": 0.004})
        assert result["ok"] is True
        esc = ai.get_run(conn, "esc-1")
        assert esc["model_name"] == l2.ESCALATED_MODEL
        assert esc["model_tier"] == l2.TIER_ESCALATED
        assert esc["escalated_from_run_id"] == "floor-1"
        assert json.loads(esc["input_segment_ids"]) == [seg_ids[0]]  # ONLY the low item
        assert esc["tokens_input"] == 1200 and esc["estimated_cost_usd"] == 0.004


# --- T4: metering ----------------------------------------------------------------

def test_meter_counts_only_live_runs_as_ai_calls(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        src = _seed_source(conn)
        ai.create_run(conn, run_id="dry-1", input_source_ids=[src])  # dry_run=1
        ai.create_run(conn, run_id="live-1", input_source_ids=[src],
                      model_name=l2.FLOOR_MODEL, dry_run=False)
        cm.record_usage(conn, "live-1", tokens_input=1000, tokens_output=250,
                        estimated_cost_usd=0.002)
        record_crawl_run(conn, started_utc=_now(), finished_utc=_now(),
                         status="succeeded", source_set=[src], new_documents=4)
        m = cm.meter(conn)
    assert m["gateway"]["runs"] == 2
    assert m["gateway"]["ai_calls"] == 1  # the dry/offline row never counts
    assert m["gateway"]["by_model"][l2.FLOOR_MODEL]["tokens_input"] == 1000
    assert m["cost_per_document"] == pytest.approx(0.002 / 4)
    assert "AI calls" in cm.render_metering(m)


def test_meter_honest_none_on_zero_denominators(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        m = cm.meter(conn)
    assert m["cost_per_document"] is None
    assert m["lane1"]["skip_ratio"] is None
    assert m["gateway"]["ai_calls"] == 0


def test_record_usage_refuses_unknown_run(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        with pytest.raises(ValueError):
            cm.record_usage(conn, "no-such-run", tokens_input=1, tokens_output=1,
                            estimated_cost_usd=0.0)


# --- T5: failure→issue filer ------------------------------------------------------

def _runner_log(log_dir: Path, name: str, **fields) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {"runner": "refresh_runner", "mode": "dry-run", "ok": True,
              "started_utc": _now(), "failures": [], "credit_anomaly": False,
              "ai_run_delta": 0}
    record.update(fields)
    path = log_dir / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_scan_matches_exactly_the_defined_patterns(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        _seed_source(conn)
        record_crawl_run(conn, started_utc=_now(), finished_utc=_now(),
                         status="failed", source_set=[], new_documents=0)
        record_crawl_run(conn, started_utc=_now(), finished_utc=_now(),
                         status="succeeded", source_set=[], new_documents=1)
        # escalated run whose floor logged NO reason → credit anomaly
        ai.create_run(conn, run_id="floor-x", input_source_ids=[])
        ai.create_run(conn, run_id="esc-x", input_source_ids=[])
        conn.execute("UPDATE ai_extraction_runs SET escalated_from_run_id='floor-x' "
                     "WHERE run_id='esc-x'")
        conn.commit()

    logs = tmp_path / "logs"
    _runner_log(logs, "refresh-a-dry-run.json")                       # healthy → no finding
    _runner_log(logs, "refresh-b-dry-run.json", ok=False,
                failures=[{"path": "x", "error": "boom"}])
    _runner_log(logs, "refresh-c-dry-run.json", ok=False,
                credit_anomaly=True, ai_run_delta=1)

    findings = filer.scan(db_path, logs)
    patterns = sorted(f["pattern"] for f in findings)
    assert patterns == ["credit_anomaly", "credit_anomaly",
                        "lane1_run_failed", "runner_failures"]
    keys = {f["dedupe_key"] for f in findings}
    assert len(keys) == 4  # every finding independently dedupe-able


def test_filer_dry_run_default_and_dedupe_and_apply(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        record_crawl_run(conn, started_utc=_now(), finished_utc=_now(),
                         status="failed", source_set=[], new_documents=0)
    findings = filer.scan(db_path, tmp_path / "no-logs")
    assert len(findings) == 1
    key = findings[0]["dedupe_key"]

    calls: list[tuple[str, str, dict | None]] = []

    def transport(method, url, body):
        calls.append((method, url, body))
        if method == "GET":
            # one OPEN issue already carries this key → dedupe skips it
            return [{"title": f"old [auto:T5 {key}]", "status": "todo"}]
        return {"identifier": "GOV-999"}

    out = filer.file_issues(findings, transport=transport)  # dry-run default
    assert out["apply"] is False and out["would_file"] == []
    assert out["skipped_existing"] == [key]
    assert [m for m, _, _ in calls] == ["GET"]  # never POSTs on dry-run

    def transport2(method, url, body):
        calls.append((method, url, body))
        if method == "GET":
            return [{"title": f"old [auto:T5 {key}]", "status": "done"}]  # closed → refile
        return {"identifier": "GOV-999"}

    calls.clear()
    out2 = filer.file_issues(findings, apply=True, transport=transport2)
    assert out2["filed"] == [key] and len(out2["created"]) == 1
    method, url, body = calls[-1]
    assert method == "POST" and url.endswith("/api/issues")
    assert f"[auto:T5 {key}]" in body["title"]
    assert body["assigneeAgentId"] == filer.CTO_AGENT_ID


def test_filer_clean_state_files_nothing(tmp_path: Path, capsys) -> None:
    db_path = _migrated(tmp_path)
    assert filer.main(["--db", str(db_path), "--log-dir", str(tmp_path / "none")]) == 0
    assert "clean" in capsys.readouterr().out
