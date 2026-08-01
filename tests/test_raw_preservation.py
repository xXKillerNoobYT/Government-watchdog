"""Tests for raw preservation & reproducibility hardening (GOV-75, Issue C).

Covers the 1.04 acceptance criteria:
- reproducibility check: re-hash of stored raw matches recorded sha256 (green),
  and a tampered/corrupted file is detected (does not silently pass);
- raw-before-parse gate: extraction is blocked when raw is missing or its hash
  mismatches — no derived record without a hash-verifiable predecessor;
- `crawl_runs` formalized as the Lane 1 run log: records source set + status +
  retry; migration 0004 is additive + idempotent.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import embed as embed_mod  # noqa: E402
import raw_preservation as rp  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _write_raw(repo_root: Path, rel_path: str, content: bytes) -> str:
    """Write a raw artifact under a fake repo root; return its sha256."""
    path = repo_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return rp.sha256_file(path)


def _insert_document(conn, *, source_url: str, local_path: str, sha256: str) -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc) "
        "VALUES (?, ?, ?, ?)",
        (source_url, local_path, sha256, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- migration 0004: crawl_runs Lane 1 fields -----------------------------

def test_migration_adds_lane1_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        cols = _columns(conn, "crawl_runs")
    for required in ("lane", "source_set", "retry_count"):
        assert required in cols, f"crawl_runs.{required} missing"


def test_migration_idempotent_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # must not raise (duplicate-column hazard)
    with db.open_db(db_path) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(crawl_runs)")]
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert cols.count("retry_count") == 1
    assert "0004_crawl_runs_lane1" in ledger


# --- crawl_runs as Lane 1 deterministic-ingest run log (1.04-f) ------------

def test_record_crawl_run_records_source_set_status_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        run_id = rp.record_crawl_run(
            conn,
            started_utc=_now(),
            finished_utc=_now(),
            status="ok",
            source_set=["alpinewy_gov", "municode_alpine"],
            retry_count=2,
            new_documents=5,
            notes="smoke",
        )
        row = conn.execute(
            "SELECT lane, source_set, status, retry_count, new_documents "
            "FROM crawl_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert row["lane"] == "lane1_deterministic_ingest"
    assert json.loads(row["source_set"]) == ["alpinewy_gov", "municode_alpine"]
    assert row["status"] == "ok"
    assert row["retry_count"] == 2
    assert row["new_documents"] == 5


# --- reproducibility check (1.04-b/e) -------------------------------------

def test_verify_reproducibility_passes_on_intact_store(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/a.pdf", b"%PDF-1.4 alpine bytes")
    with db.open_db(db_path) as conn:
        _insert_document(
            conn, source_url="https://www.alpinewy.gov/a.pdf",
            local_path="Raw-PDFs/2026/alpinewy/a.pdf", sha256=sha,
        )
        result = rp.verify_reproducibility(conn, repo_root=repo_root)
    assert result["checked"] == 1
    assert result["ok"] == 1
    assert result["missing"] == []
    assert result["mismatch"] == []


def test_verify_reproducibility_detects_tamper(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)

    good_sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/good.pdf", b"intact bytes")
    tampered_sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/bad.pdf", b"original bytes")
    with db.open_db(db_path) as conn:
        _insert_document(
            conn, source_url="https://www.alpinewy.gov/good.pdf",
            local_path="Raw-PDFs/2026/alpinewy/good.pdf", sha256=good_sha,
        )
        bad_id = _insert_document(
            conn, source_url="https://www.alpinewy.gov/bad.pdf",
            local_path="Raw-PDFs/2026/alpinewy/bad.pdf", sha256=tampered_sha,
        )
        # tamper with the stored file AFTER its hash was recorded
        (repo_root / "Raw-PDFs/2026/alpinewy/bad.pdf").write_bytes(b"corrupted bytes!!")
        result = rp.verify_reproducibility(conn, repo_root=repo_root)

    assert result["checked"] == 2
    assert result["ok"] == 1
    assert [e["id"] for e in result["mismatch"]] == [bad_id]
    assert result["missing"] == []


def test_verify_reproducibility_flags_missing_file(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        mid = _insert_document(
            conn, source_url="https://www.alpinewy.gov/gone.pdf",
            local_path="Raw-PDFs/2026/alpinewy/gone.pdf", sha256="0" * 64,
        )
        result = rp.verify_reproducibility(conn, repo_root=repo_root)
    assert result["ok"] == 0
    assert [e["id"] for e in result["missing"]] == [mid]


# --- raw-before-parse gate (1.04-a/b) -------------------------------------

def test_assert_raw_preserved_returns_hash_on_match(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/ok.pdf", b"good")
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/ok.pdf",
            local_path="Raw-PDFs/2026/alpinewy/ok.pdf", sha256=sha,
        )
        assert rp.assert_raw_preserved(conn, "document", did, repo_root) == sha


def test_assert_raw_preserved_raises_on_missing(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/x.pdf",
            local_path="Raw-PDFs/2026/alpinewy/x.pdf", sha256="0" * 64,
        )
        with pytest.raises(rp.RawPreservationError, match="missing"):
            rp.assert_raw_preserved(conn, "document", did, repo_root)


def test_assert_raw_preserved_raises_on_hash_mismatch(tmp_path: Path) -> None:
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/m.pdf", b"actual bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/m.pdf",
            local_path="Raw-PDFs/2026/alpinewy/m.pdf", sha256="9" * 64,  # wrong
        )
        with pytest.raises(rp.RawPreservationError, match="hash mismatch"):
            rp.assert_raw_preserved(conn, "document", did, repo_root)


def test_raw_before_parse_gate_blocks_extraction_on_tamper(tmp_path: Path) -> None:
    """A tampered raw artifact must not produce a derived raw_text record."""
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/t.pdf", b"some bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(
            conn, source_url="https://www.alpinewy.gov/t.pdf",
            local_path="Raw-PDFs/2026/alpinewy/t.pdf", sha256="a" * 64,  # mismatch
        )
        # The gate blocks before _extract_pdf_text is ever called, so no pypdf
        # dependency / real PDF is needed to prove extraction is blocked.
        extracted = embed_mod.extract_missing_document_text(conn, repo_root)
        raw_text = conn.execute(
            "SELECT raw_text FROM documents WHERE id = ?", (did,)
        ).fetchone()[0]
    assert extracted == 0
    assert raw_text is None


# --- GOV-1690 (C4): the "absolute drift rule" was stated, never enforced -------
#
# `validate_sources` promises, in capitals: "NEVER overwrites raw_sha256 and NEVER
# re-fetches (absolute drift rule)". Nothing tested it.
#
# This is the same class as `extract_metadata`'s provenance promise (GOV-1688), and
# it is the sharper instance: `raw_sha256` is the recorded fingerprint of the bytes
# this project captured from a government source. If validation quietly re-hashed a
# file that had changed on disk, the divergence would DISAPPEAR — the record would
# agree with the tampered bytes and the evidence that anything changed would be gone.


def _insert_source(conn, sid: str, *, status: str, local_path: str, sha: str) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, raw_preservation_status,"
        " raw_local_path, raw_sha256) VALUES (?, ?, 'alpine', ?, ?, ?)",
        (sid, f"Source {sid}", status, local_path, sha))
    conn.commit()


def test_drifted_bytes_are_a_DEFECT_and_the_recorded_hash_is_never_rewritten(
        tmp_path: Path) -> None:
    """Tamper the stored file; the hash on record must survive untouched.

    The module comments the branch itself — `own_state = "defect"  # drift —
    defect, not a gap, never overwritten`. Reporting the defect is the whole
    value: a system that re-hashed to match would be *self-healing into a lie*.
    """
    db_path = tmp_path / "drift.db"
    repo_root = tmp_path / "repo"
    db.apply_migrations(db_path)
    original_sha = _write_raw(repo_root, "Raw/alpine/packet.pdf", b"%PDF original bytes")

    with db.open_db(db_path) as conn:
        _insert_source(conn, "alpine-drift", status="preserved",
                       local_path="Raw/alpine/packet.pdf", sha=original_sha)
        # Someone (or something) changes the stored artifact after capture.
        (repo_root / "Raw/alpine/packet.pdf").write_bytes(b"%PDF TAMPERED bytes")

        out = rp.validate_sources(
            conn, repo_root, bad_document_ids=set(), apply=True,
            gap_exceptions=(), run_id=None)

        assert any(i["source_id"] == "alpine-drift" for i in out["invalid"]), (
            "drifted bytes must be reported INVALID, not silently accepted")
        still = conn.execute(
            "SELECT raw_sha256 FROM sources WHERE source_id = 'alpine-drift'"
        ).fetchone()[0]

    assert still == original_sha, (
        "validate_sources rewrote raw_sha256. Its docstring promises 'NEVER "
        "overwrites raw_sha256 ... (absolute drift rule)'. Re-hashing to match a "
        "changed file destroys the only evidence that the artifact drifted.")


def test_an_upgraded_seed_only_source_keeps_its_hash_and_only_status_moves(
        tmp_path: Path) -> None:
    """The `apply` write path touches status + last_validated_utc, nothing else."""
    db_path = tmp_path / "upgrade.db"
    repo_root = tmp_path / "repo"
    db.apply_migrations(db_path)
    sha = _write_raw(repo_root, "Raw/alpine/ord.pdf", b"%PDF ordinance")

    with db.open_db(db_path) as conn:
        _insert_source(conn, "alpine-seed", status="seed_only",
                       local_path="Raw/alpine/ord.pdf", sha=sha)
        out = rp.validate_sources(
            conn, repo_root, bad_document_ids=set(), apply=True,
            gap_exceptions=(), run_id=None)
        assert "alpine-seed" in out["upgraded"]
        row = conn.execute(
            "SELECT raw_preservation_status, raw_sha256, raw_local_path FROM sources"
            " WHERE source_id = 'alpine-seed'").fetchone()

    assert row[0] == rp.CANONICAL_PRESERVED
    assert row[1] == sha, "the upgrade path must not touch raw_sha256"
    assert row[2] == "Raw/alpine/ord.pdf", "nor the recorded locator"


def test_dry_run_writes_nothing_at_all(tmp_path: Path) -> None:
    """`apply=False` must classify without mutating — the safe-to-run guarantee."""
    db_path = tmp_path / "dry.db"
    repo_root = tmp_path / "repo"
    db.apply_migrations(db_path)
    sha = _write_raw(repo_root, "Raw/alpine/x.pdf", b"%PDF x")

    with db.open_db(db_path) as conn:
        _insert_source(conn, "alpine-dry", status="seed_only",
                       local_path="Raw/alpine/x.pdf", sha=sha)
        out = rp.validate_sources(
            conn, repo_root, bad_document_ids=set(), apply=False,
            gap_exceptions=(), run_id=None)
        assert "alpine-dry" in out["upgraded"], "it must still CLASSIFY"
        status = conn.execute(
            "SELECT raw_preservation_status FROM sources WHERE source_id = 'alpine-dry'"
        ).fetchone()[0]

    assert status == "seed_only", (
        "apply=False wrote to the database — a dry run that mutates is worse than "
        "no dry run, because operators rely on it being safe")


# --- GOV-1693 (C7b hunt): a STORED path must not escape the repository root ---
#
# `Path(root) / value` silently DISCARDS `root` when `value` is absolute:
# measured, `Path("/repo") / "/etc/passwd"` is `/etc/passwd` — no error. A `..`
# segment walks out just as quietly. Either way the caller reads, hashes, or
# reports a file OUTSIDE the preservation store while believing it is inside.
#
# NOT a live vulnerability: every writer of these columns constructs a contained
# relative path. The reason to guard the READ side is that the invariant is
# enforced at 6+ write sites and verified at none, so every future writer has to
# re-derive it. One check covers all of them.


def test_an_absolute_stored_path_is_refused_not_silently_followed(tmp_path: Path):
    """The foot-gun in one line: an absolute value makes `/` throw the root away."""
    db_path = tmp_path / "esc.db"
    repo_root = tmp_path / "repo"
    (repo_root / "Raw").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not ours")
    db.apply_migrations(db_path)

    with db.open_db(db_path) as conn:
        _insert_source(conn, "alpine-escape", status="preserved",
                       local_path=str(outside), sha=rp.sha256_file(outside))
        with pytest.raises(rp.RawPathEscape, match="outside the repository root"):
            rp.validate_sources(conn, repo_root, bad_document_ids=set(),
                                apply=False, gap_exceptions=(), run_id=None)


def test_a_dot_dot_stored_path_is_refused(tmp_path: Path):
    """`..` escapes without ever looking absolute."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (tmp_path / "sibling.txt").write_bytes(b"x")
    with pytest.raises(rp.RawPathEscape):
        rp._contained(repo_root, "../sibling.txt")


def test_an_ordinary_relative_path_still_resolves_normally(tmp_path: Path):
    """The guard must not break the 99.9% case it sits in front of."""
    repo_root = tmp_path / "repo"
    (repo_root / "Raw").mkdir(parents=True)
    (repo_root / "Raw" / "ok.pdf").write_bytes(b"%PDF")
    got = rp._contained(repo_root, "Raw/ok.pdf")
    assert got == repo_root / "Raw/ok.pdf"
    assert got.exists(), "a legitimate contained path must still be usable"
