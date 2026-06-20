"""Stage 3.04 raw-preservation read-time auditor (GOV-367) — RED tests.

Implements the §5 RED test list of the 3.04 contract
(`Docs/stage3-04-raw-preservation-contract.md`). Each test fails before
`scripts/stage3_preservation_audit.py` exists (collection-time ImportError) and
passes once the additive auditor module lands. The auditor is read-only over the
existing Alpine corpus; it re-uses the `raw_preservation.py` engine, the
`publication.WEB_*` SSOT, and the `read_api.assert_no_raw_paths` backstop —
`publication.py` / `read_api.py` stay 0-diff.

Invariants proven (contract §1):
  * RP-1 raw retained / RP-2 content hash — re-hash verdict per unit (`hash_ok`);
  * RP-3 version/as-of — first-captured + last-validated timestamps per unit;
  * RP-4 archive reference — archive presence/state per unit (from the source leg);
  * RP-0 raw-before-parse ordering — no derived `raw_text` row without a
    hash-verifiable raw predecessor.

No-leak (contract §3/§4): the reviewer-internal overlay passes `assert_no_raw_paths`
(no path / `.sha256` / 64-hex per-unit / vault marker), and the web-safe projection
carries only the already-allowlisted `scan_date` / `last_validated_utc` /
`archive_status` / `ui_status`.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import publication as pub  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402
import stage3_preservation_audit as audit  # noqa: E402

# A 64-hex sha256 — the value that must NEVER appear in a per-unit overlay row
# (only the boolean verdict `hash_ok` and the one envelope-level digest may show).
_SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_raw(repo_root: Path, rel_path: str, content: bytes) -> str:
    path = repo_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return rp.sha256_file(path)


def _insert_source(
    conn,
    *,
    source_id: str,
    raw_local_path: str | None,
    raw_sha256: str | None,
    raw_preservation_status: str = "preserved",
    scan_date: str = "2026-01-01",
    last_validated_utc: str = "2026-06-01T00:00:00.000+00:00",
    archive_url: str | None = "https://web.archive.org/web/2026/alpine",
    archive_status: str = "available",
) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scan_date, last_validated_utc, "
        "archive_url, archive_status, raw_local_path, raw_sha256, "
        "raw_preservation_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id, f"Source {source_id}", scan_date, last_validated_utc,
            archive_url, archive_status, raw_local_path, raw_sha256,
            raw_preservation_status,
        ),
    )
    conn.commit()


def _insert_document(
    conn, *, source_id: str, source_url: str, local_path: str, sha256: str,
    raw_text: str | None = "extracted text",
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, "
        "raw_text, source_id) VALUES (?, ?, ?, ?, ?, ?)",
        (source_url, local_path, sha256, _now(), raw_text, source_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_transcript(
    conn, *, source_id: str, video_id: str, full_text: str, local_path: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, source_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            video_id, f"https://youtu.be/{video_id}", full_text, local_path,
            rp.sha256_text(full_text), _now(), source_id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_intact_corpus(tmp_path: Path):
    """One valid source + one valid document + one valid transcript on disk."""
    repo_root = tmp_path
    db_path = tmp_path / "Database" / "t.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    src_sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/src.pdf", b"%PDF source bytes")
    doc_sha = _write_raw(repo_root, "Raw-PDFs/2026/alpinewy/a.pdf", b"%PDF doc bytes")
    conn = db.open_db(db_path)
    _insert_source(
        conn, source_id="alpinewy_gov",
        raw_local_path="Raw-PDFs/2026/alpinewy/src.pdf", raw_sha256=src_sha,
    )
    doc_id = _insert_document(
        conn, source_id="alpinewy_gov",
        source_url="https://www.alpinewy.gov/a.pdf",
        local_path="Raw-PDFs/2026/alpinewy/a.pdf", sha256=doc_sha,
    )
    tr_id = _insert_transcript(
        conn, source_id="alpinewy_gov", video_id="vid1",
        full_text="meeting transcript text",
        local_path="Transcripts/2026/vid1.json",
    )
    return repo_root, conn, doc_id, tr_id


# --- 1. all invariants on an intact corpus --------------------------------

def test_preservation_audit_reports_all_invariants_on_intact_corpus(tmp_path: Path) -> None:
    repo_root, conn, _doc_id, _tr_id = _seed_intact_corpus(tmp_path)
    overlay = audit.audit_preservation(conn, repo_root=repo_root)
    rows = overlay["preservation_status"]
    # one row per preserved unit: document + transcript + source
    assert {r["unit_ref"]["object_type"] for r in rows} == {"document", "transcript", "source"}
    for row in rows:
        assert row["retained"] is True, row
        assert row["hash_ok"] is True, row
        assert row["as_of"]["first_captured"], row
        assert row["as_of"]["last_validated"], row
        assert "present" in row["archive"] and "status" in row["archive"], row
        assert row["preservation_state"] == "preserved", row
    assert overlay["defect_count"] == 0
    assert overlay["raw_before_parse_ok"] is True


# --- 2. tamper => defect, recorded sha unchanged, no re-fetch --------------

def test_preservation_audit_flags_tamper_as_defect(tmp_path: Path) -> None:
    repo_root, conn, doc_id, _tr_id = _seed_intact_corpus(tmp_path)
    recorded = conn.execute(
        "SELECT sha256 FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()["sha256"]
    # corrupt the stored artifact AFTER its hash was recorded
    (repo_root / "Raw-PDFs/2026/alpinewy/a.pdf").write_bytes(b"corrupted!!")
    overlay = audit.audit_preservation(conn, repo_root=repo_root)
    doc_row = next(
        r for r in overlay["preservation_status"]
        if r["unit_ref"] == {"object_type": "document", "id": doc_id}
    )
    assert doc_row["hash_ok"] is False
    assert doc_row["preservation_state"] == "defect"
    assert doc_row["retained"] is True  # file present, only the bytes drifted
    # absolute drift rule: recorded sha256 is NEVER overwritten
    after = conn.execute(
        "SELECT sha256 FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()["sha256"]
    assert after == recorded


# --- 3. missing raw => defect ---------------------------------------------

def test_preservation_audit_flags_missing_raw_as_defect(tmp_path: Path) -> None:
    repo_root, conn, doc_id, _tr_id = _seed_intact_corpus(tmp_path)
    (repo_root / "Raw-PDFs/2026/alpinewy/a.pdf").unlink()
    overlay = audit.audit_preservation(conn, repo_root=repo_root)
    doc_row = next(
        r for r in overlay["preservation_status"]
        if r["unit_ref"] == {"object_type": "document", "id": doc_id}
    )
    assert doc_row["retained"] is False
    assert doc_row["preservation_state"] == "defect"
    assert overlay["defect_count"] >= 1


# --- 4. overlay passes the transport backstop -----------------------------

def test_preservation_overlay_passes_assert_no_raw_paths(tmp_path: Path) -> None:
    repo_root, conn, _doc_id, _tr_id = _seed_intact_corpus(tmp_path)
    overlay = audit.build_preservation_overlay(conn, repo_root=repo_root)
    # the builder routes the body through the existing backstop and returns it
    assert read_api.assert_no_raw_paths(overlay) is overlay
    # belt-and-braces: no 64-hex sha256 in any PER-UNIT row (only the verdict bool
    # and the single envelope digest may carry hash information)
    for row in overlay["preservation_status"]:
        assert not _SHA256_RE.search(str(row)), row


# --- 5. web-safe projection drops all raw metadata ------------------------

def test_preservation_no_raw_metadata_in_web_safe_projection() -> None:
    record = {
        "source_id": "alpinewy_gov",
        "raw_local_path": "Raw-PDFs/2026/alpinewy/src.pdf",
        "raw_sha256": "a" * 64,
        "raw_preservation_status": "preserved",
        "fetch_time_utc": "2026-01-01T00:00:00Z",
        "scan_date": "2026-01-01",
        "last_validated_utc": "2026-06-01T00:00:00Z",
        "archive_status": "available",
        "ui_status": "archived-source-backed",
    }
    web = audit.public_preservation_view(record)
    for dropped in ("raw_local_path", "raw_sha256", "raw_preservation_status", "fetch_time_utc"):
        assert dropped not in web
    assert set(web) == {"source_id", "scan_date", "last_validated_utc", "archive_status", "ui_status"}


# --- 6. no-fork guard: SSOT constants imported, never re-declared ---------

def test_preservation_audit_publication_read_api_zero_diff() -> None:
    # the auditor consumes the SSOT by reference (extend-not-fork): it must not
    # carry its own copy of the unsafe-field set or the raw-marker list.
    assert audit.pub.WEB_UNSAFE_FIELDS is pub.WEB_UNSAFE_FIELDS
    assert audit.read_api.RAW_PATH_MARKERS is read_api.RAW_PATH_MARKERS
    assert not hasattr(audit, "WEB_UNSAFE_FIELDS")
    assert not hasattr(audit, "RAW_PATH_MARKERS")
    assert not hasattr(audit, "WEB_SAFE_FIELD_ALLOWLIST")


# --- 7. raw-before-parse ordering holds over the corpus -------------------

def test_raw_before_parse_ordering_holds_over_corpus(tmp_path: Path) -> None:
    repo_root, conn, doc_id, _tr_id = _seed_intact_corpus(tmp_path)
    # intact: every derived raw_text row has a hash-verifiable raw predecessor
    assert audit.audit_raw_before_parse(conn, repo_root=repo_root) == []
    assert audit.assert_raw_before_parse_holds(conn, repo_root=repo_root) is True
    # plant an ordering violation: a derived row whose raw predecessor drifted
    (repo_root / "Raw-PDFs/2026/alpinewy/a.pdf").write_bytes(b"tampered after parse")
    violations = audit.audit_raw_before_parse(conn, repo_root=repo_root)
    assert [v["id"] for v in violations] == [doc_id]
    with pytest.raises(audit.RawBeforeParseViolation):
        audit.assert_raw_before_parse_holds(conn, repo_root=repo_root)
