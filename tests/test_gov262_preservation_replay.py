"""Tests for the Stage 2.04 preservation-replay pass (GOV-262).

The Stage 2.04 contract (goal `7e4434b1`, GOV-230) defers the `transcript_class`
migration to 2.05, so the genuine 2.04 deliverable is a deterministic
preservation-VALIDITY pass that proves every Stage 2.03 unit re-hashes BEFORE any
extraction reads a byte. These tests cover the acceptance bar verbatim:

- the three legs (document reproducibility, transcript-text reconcile, sources
  validity) compose into one `crawl_runs` row tagged `preservation_replay`;
- fail-closed: ANY missing/mismatch/invalid unit ⇒ `status='failed'`, the offending
  units listed in `notes`, and (strict) `RawPreservationError` raised — the
  headline drift-injection test;
- `status='success'` can NEVER coexist with a non-empty miss list;
- drift is a DEFECT: the recorded `sha256` is never overwritten, no gap, no re-fetch;
- `seed_only` is INVALID for Stage 2 → upgraded (own bytes / valid children) or
  documented as a deliberate `no_primary_source` exception, else it FAILS;
- the document-replay leg is mandatory (never disabled);
- the aggregate-hash manifest is column-stable (deterministic across runs);
- no preservation field is reachable through the web-safe allowlist.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import completeness  # noqa: E402
import db  # noqa: E402
import publication  # noqa: E402
import raw_preservation as rp  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _fresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    return db_path


def _write_raw(repo_root: Path, rel_path: str, content: bytes) -> str:
    path = repo_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return rp.sha256_file(path)


def _insert_source(conn, *, source_id: str, status: str,
                   raw_local_path: str | None = None, raw_sha256: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, raw_preservation_status, "
        "raw_local_path, raw_sha256, scan_date) VALUES (?, ?, ?, ?, ?, ?)",
        (source_id, source_id, status, raw_local_path, raw_sha256, _now()),
    )
    conn.commit()


def _insert_document(conn, *, source_url: str, local_path: str, sha256: str,
                     source_id: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_url, local_path, sha256, _now(), source_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_transcript(conn, *, video_id: str, full_text: str, local_path: str,
                       sha256: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, full_text, local_path, sha256, "
        "fetch_time_utc) VALUES (?, ?, ?, ?, ?, ?)",
        (video_id, f"https://youtu.be/{video_id}", full_text, local_path,
         sha256 if sha256 is not None else rp.sha256_text(full_text), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _crawl_run(conn, run_id: int) -> dict:
    row = conn.execute(
        "SELECT status, targets, notes FROM crawl_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return {"status": row["status"], "targets": json.loads(row["targets"]),
            "notes": json.loads(row["notes"])}


# --- transcript-text reconcile leg (GOV-262 §1) ----------------------------

def test_transcript_reconcile_passes_on_intact_text(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        _insert_transcript(conn, video_id="v1", full_text="alpine council minutes",
                           local_path="Transcripts/v1.json")
        result = rp.reconcile_transcript_text(conn)
    assert result == {"checked": 1, "ok": 1, "mismatch": [], "missing_text": []}


def test_transcript_reconcile_detects_text_drift(tmp_path: Path) -> None:
    """A transcript whose stored text no longer re-hashes is a preservation defect."""
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        tid = _insert_transcript(conn, video_id="v2", full_text="original text",
                                 local_path="Transcripts/v2.json")
        # mutate the stored text AFTER the hash was recorded (text-hash, not file-hash)
        conn.execute("UPDATE transcripts SET full_text = ? WHERE id = ?",
                     ("tampered text", tid))
        conn.commit()
        recorded_before = conn.execute(
            "SELECT sha256 FROM transcripts WHERE id = ?", (tid,)).fetchone()[0]
        result = rp.reconcile_transcript_text(conn)
        recorded_after = conn.execute(
            "SELECT sha256 FROM transcripts WHERE id = ?", (tid,)).fetchone()[0]
    assert [e["id"] for e in result["mismatch"]] == [tid]
    assert result["ok"] == 0
    # absolute drift rule: the recorded sha256 is NEVER overwritten by the verifier
    assert recorded_before == recorded_after


def test_transcript_reconcile_uses_text_hash_not_file_hash(tmp_path: Path) -> None:
    """Regression guard: transcript sha256 is sha256(full_text), not the file bytes.

    `verify_reproducibility` (file-bytes) would false-positive every transcript; the
    reconcile leg must use the text hash. Proven by an intact transcript passing the
    text reconcile even with no file on disk at `local_path`.
    """
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        _insert_transcript(conn, video_id="v3", full_text="no file on disk here",
                           local_path="Transcripts/does_not_exist.json")
        result = rp.reconcile_transcript_text(conn, repo_root=tmp_path)
    assert result["ok"] == 1 and result["mismatch"] == []


# --- aggregate-hash manifest (column-stable / deterministic) ----------------

def test_manifest_is_column_stable_and_deterministic(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/a.pdf", b"bytes")
    with db.open_db(db_path) as conn:
        _insert_document(conn, source_url="u://a", local_path="Raw-Corpus/a.pdf", sha256=sha)
        _insert_transcript(conn, video_id="v", full_text="t", local_path="Transcripts/v.json")
        first = rp.preservation_manifest(conn)
        second = rp.preservation_manifest(conn)
    assert first == second
    assert first["unit_count"] == 2
    assert len(first["aggregate_sha256"]) == 64


# --- full pass: green path (GOV-262 §1/§2) ----------------------------------

def test_replay_green_writes_success_run_with_manifest(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/ok.pdf", b"%PDF good")
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="alpine_local_corpus", status="raw_preserved")
        _insert_document(conn, source_url="u://ok", local_path="Raw-Corpus/ok.pdf",
                         sha256=sha, source_id="alpine_local_corpus")
        _insert_transcript(conn, video_id="vok", full_text="ok", local_path="Transcripts/vok.json")
        result = rp.preservation_replay(conn, repo_root=tmp_path)
        run = _crawl_run(conn, result["run_id"])
    assert result["status"] == rp.RUN_STATUS_SUCCESS
    assert result["miss_count"] == 0
    assert run["status"] == "success"
    assert rp.PRESERVATION_REPLAY_TARGET in run["targets"]
    # umbrella source is preservation-valid by its (valid) children
    assert result["sources"]["preserved"] == ["alpine_local_corpus"]
    assert run["notes"]["manifest"]["unit_count"] == 2


# --- HEADLINE: drift injection is fail-closed (GOV-262 §4) ------------------

def test_replay_drift_injection_is_fail_closed(tmp_path: Path) -> None:
    """A tampered document MUST: raise RawPreservationError, write a `failed`
    crawl_runs row listing the unit, and never report success."""
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/drift.pdf", b"original bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(conn, source_url="u://drift",
                               local_path="Raw-Corpus/drift.pdf", sha256=sha)
        recorded_before = conn.execute(
            "SELECT sha256 FROM documents WHERE id = ?", (did,)).fetchone()[0]
        # inject drift AFTER the hash is recorded
        (tmp_path / "Raw-Corpus/drift.pdf").write_bytes(b"CORRUPTED!!")

        with pytest.raises(rp.RawPreservationError, match="FAILED"):
            rp.preservation_replay(conn, repo_root=tmp_path, strict=True)

        # the failed run row was durably written BEFORE the raise
        failed = conn.execute(
            "SELECT id, status, notes FROM crawl_runs "
            "WHERE status = ? ORDER BY id DESC LIMIT 1", (rp.RUN_STATUS_FAILED,)
        ).fetchone()
        notes = json.loads(failed["notes"])
        recorded_after = conn.execute(
            "SELECT sha256 FROM documents WHERE id = ?", (did,)).fetchone()[0]
    assert failed is not None
    assert [e["id"] for e in notes["documents"]["mismatch"]] == [did]
    # drift is a DEFECT, not a gap, and the recorded hash is never overwritten
    assert recorded_before == recorded_after


def test_replay_missing_file_is_fail_closed(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        did = _insert_document(conn, source_url="u://gone",
                               local_path="Raw-Corpus/gone.pdf", sha256="0" * 64)
        with pytest.raises(rp.RawPreservationError):
            rp.preservation_replay(conn, repo_root=tmp_path, strict=True)
        failed = conn.execute(
            "SELECT notes FROM crawl_runs WHERE status = ? ORDER BY id DESC LIMIT 1",
            (rp.RUN_STATUS_FAILED,)).fetchone()
    assert [e["id"] for e in json.loads(failed["notes"])["documents"]["missing"]] == [did]


def test_success_never_coexists_with_misses(tmp_path: Path) -> None:
    """Structural invariant (§4): status='success' ⟺ zero misses. Non-strict mode."""
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/x.pdf", b"abc")
    with db.open_db(db_path) as conn:
        _insert_document(conn, source_url="u://x", local_path="Raw-Corpus/x.pdf", sha256=sha)
        _insert_document(conn, source_url="u://y", local_path="Raw-Corpus/y.pdf", sha256="0" * 64)
        result = rp.preservation_replay(conn, repo_root=tmp_path, strict=False)
    assert result["status"] == rp.RUN_STATUS_FAILED
    assert result["miss_count"] > 0


# --- sources: seed_only is INVALID for Stage 2 (GOV-262 §3) -----------------

def test_seed_only_upgraded_by_own_bytes_on_apply(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/src.bin", b"source raw bytes")
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="s_own", status="seed_only",
                       raw_local_path="Raw-Corpus/src.bin", raw_sha256=sha)
        result = rp.preservation_replay(conn, repo_root=tmp_path, apply=True)
        status = conn.execute(
            "SELECT raw_preservation_status FROM sources WHERE source_id = 's_own'"
        ).fetchone()[0]
    assert result["sources"]["upgraded"] == ["s_own"]
    assert status == rp.CANONICAL_PRESERVED
    assert result["status"] == rp.RUN_STATUS_SUCCESS


def test_seed_only_upgraded_by_valid_children(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/child.pdf", b"child bytes")
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="s_kids", status="seed_only")
        _insert_document(conn, source_url="u://c", local_path="Raw-Corpus/child.pdf",
                         sha256=sha, source_id="s_kids")
        result = rp.preservation_replay(conn, repo_root=tmp_path, apply=True)
    assert result["sources"]["upgraded"] == ["s_kids"]


def test_undocumented_seed_only_is_invalid_and_fails(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="s_bare", status="seed_only")
        result = rp.preservation_replay(conn, repo_root=tmp_path, apply=True, strict=False)
    assert [e["source_id"] for e in result["sources"]["invalid"]] == ["s_bare"]
    assert result["status"] == rp.RUN_STATUS_FAILED


def test_seed_only_documented_exception_passes_and_records_gap(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="s_exc", status="seed_only_unconfigured")
        result = rp.preservation_replay(
            conn, repo_root=tmp_path, apply=True, gap_exceptions=("s_exc",))
        gap = conn.execute(
            "SELECT gap_type, subject_node_type, source_id, detected_run_id "
            "FROM completeness_gaps WHERE source_id = 's_exc'").fetchone()
    assert result["sources"]["exception_documented"] == ["s_exc"]
    assert result["status"] == rp.RUN_STATUS_SUCCESS
    assert gap["gap_type"] == "no_primary_source"
    assert gap["subject_node_type"] == "source"
    assert gap["detected_run_id"] == result["run_id"]


def test_preserved_marked_source_with_missing_bytes_is_invalid(tmp_path: Path) -> None:
    """A source claiming 'preserved' whose raw is gone is a defect, not a pass."""
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="s_liar", status="preserved",
                       raw_local_path="Raw-Corpus/missing.bin", raw_sha256="0" * 64)
        result = rp.preservation_replay(conn, repo_root=tmp_path, strict=False)
    assert [e["source_id"] for e in result["sources"]["invalid"]] == ["s_liar"]
    assert result["status"] == rp.RUN_STATUS_FAILED


def test_dry_run_does_not_upgrade_or_write_gap(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/d.bin", b"raw")
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="s_dry", status="seed_only",
                       raw_local_path="Raw-Corpus/d.bin", raw_sha256=sha)
        result = rp.preservation_replay(conn, repo_root=tmp_path, apply=False)
        status = conn.execute(
            "SELECT raw_preservation_status FROM sources WHERE source_id = 's_dry'"
        ).fetchone()[0]
    # dry-run reports the upgrade but persists nothing to the source row
    assert result["sources"]["upgraded"] == ["s_dry"]
    assert status == "seed_only"


# --- mandatory document leg + privacy boundary ------------------------------

def test_document_leg_is_mandatory_even_with_transcript_scope(tmp_path: Path) -> None:
    """Passing object_types=('transcript',) must NOT disable document reproducibility."""
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        did = _insert_document(conn, source_url="u://z",
                               local_path="Raw-Corpus/z.pdf", sha256="0" * 64)
        result = rp.preservation_replay(
            conn, repo_root=tmp_path, object_types=("transcript",), strict=False)
    assert result["documents"]["checked"] == 1
    assert [e["id"] for e in result["documents"]["missing"]] == [did]


def test_preservation_fields_never_web_safe() -> None:
    """No raw/preservation locator may cross to_web_safe (§ failure definition)."""
    leaky = {
        "local_path": "/Users/IA/Documents/secret.pdf",
        "raw_local_path": "/vault/raw.bin",
        "raw_sha256": "deadbeef",
        "raw_preservation_status": "preserved",
        "notes": "reviewer-internal miss list",
    }
    assert publication.to_web_safe(leaky) == {}
    for field in leaky:
        assert field not in publication.WEB_SAFE_FIELD_ALLOWLIST
