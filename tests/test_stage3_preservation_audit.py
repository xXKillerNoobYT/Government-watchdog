"""Stage 3.04 raw-preservation read-time auditor RED tests (GOV-367).

These are the seven RED tests enumerated verbatim in the 3.04 contract
(``Docs/stage3-04-raw-preservation-contract.md`` §5). They MUST fail before
``scripts/stage3_preservation_audit.py`` exists (ImportError) and pass after.

They prove, over a seeded Alpine corpus, that the read-time auditor:

* reports RP-1..RP-4 + RP-0 on an intact corpus (test 1);
* flags tamper / missing-raw as a preservation DEFECT without re-fetch or
  overwriting the recorded ``sha256`` (tests 2, 3 — GOV-262 absolute drift rule);
* emits a reviewer-internal overlay that passes ``read_api.assert_no_raw_paths``
  carrying no raw path / ``.sha256`` / 64-hex hash per unit (test 4);
* never lets raw metadata cross ``to_web_safe`` (test 5);
* extends-not-forks the SSOT — ``publication.py`` / ``read_api.py`` 0-diff and the
  unsafe/marker constants are imported, not re-declared (test 6);
* verifies the RP-0 raw-before-parse ordering held over the present corpus (test 7).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import publication  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402
import stage3_preservation_audit as audit  # noqa: E402  (module under test — RED until it exists)


# --- seeding helpers (mirror tests/test_gov262_preservation_replay.py) -------

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


def _insert_source(conn, *, source_id: str, status: str, source_class: str = "municipal_primary",
                   raw_local_path: str | None = None, raw_sha256: str | None = None,
                   archive_url: str | None = None, archive_status: str = "not_checked",
                   last_validated_utc: str | None = None) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_class, raw_preservation_status, "
        "raw_local_path, raw_sha256, scan_date, last_validated_utc, archive_url, archive_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, source_id, source_class, status, raw_local_path, raw_sha256,
         _now(), last_validated_utc or _now(), archive_url, archive_status),
    )
    conn.commit()


def _insert_document(conn, *, source_url: str, local_path: str, sha256: str,
                     source_id: str | None = None, raw_text: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, source_id, raw_text) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source_url, local_path, sha256, _now(), source_id, raw_text),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_transcript(conn, *, video_id: str, full_text: str, local_path: str) -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, full_text, local_path, sha256, "
        "fetch_time_utc) VALUES (?, ?, ?, ?, ?, ?)",
        (video_id, f"https://youtu.be/{video_id}", full_text, local_path,
         rp.sha256_text(full_text), _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_intact_corpus(conn, tmp_path: Path) -> None:
    """A source preserved-by-children + a valid document + a valid transcript."""
    sha = _write_raw(tmp_path, "Raw-Corpus/ok.pdf", b"%PDF intact bytes")
    _insert_source(conn, source_id="alpine_town", status="raw_preserved",
                   archive_url="https://web.archive.org/x", archive_status="available")
    _insert_document(conn, source_url="https://alpine/ok", local_path="Raw-Corpus/ok.pdf",
                     sha256=sha, source_id="alpine_town", raw_text="extracted body text")
    _insert_transcript(conn, video_id="vok", full_text="alpine council minutes",
                       local_path="Transcripts/vok.json")


def _by_type(rows, object_type):
    return [r for r in rows if r["unit_ref"]["object_type"] == object_type]


# --- test 1: all invariants on an intact corpus ------------------------------

def test_preservation_audit_reports_all_invariants_on_intact_corpus(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with db.open_db(db_path) as conn:
        _seed_intact_corpus(conn, tmp_path)
        rows = audit.audit_unit_rows(conn, repo_root=tmp_path)

    # one row per preserved unit: document + transcript + source
    assert {r["unit_ref"]["object_type"] for r in rows} == {"document", "transcript", "source"}
    for row in rows:
        assert row["retained"] is True, row                  # RP-1
        assert row["hash_ok"] is True, row                   # RP-2
        assert row["as_of"]["first_captured"], row           # RP-3 as-of #1
        assert row["as_of"]["last_validated"], row           # RP-3 as-of #2
        assert "status" in row["archive"], row               # RP-4 archive state present
        assert row["preservation_state"] == "preserved", row


# --- test 2: tamper is a defect (drift rule, no re-fetch) --------------------

def test_preservation_audit_flags_tamper_as_defect(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/drift.pdf", b"original bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(conn, source_url="u://drift",
                               local_path="Raw-Corpus/drift.pdf", sha256=sha)
        recorded_before = conn.execute(
            "SELECT sha256 FROM documents WHERE id = ?", (did,)).fetchone()[0]
        # inject drift AFTER the hash was recorded
        (tmp_path / "Raw-Corpus/drift.pdf").write_bytes(b"CORRUPTED!!")

        rows = audit.audit_unit_rows(conn, repo_root=tmp_path)
        recorded_after = conn.execute(
            "SELECT sha256 FROM documents WHERE id = ?", (did,)).fetchone()[0]

    doc = _by_type(rows, "document")[0]
    assert doc["hash_ok"] is False                # RP-2 violated
    assert doc["preservation_state"] == "defect"
    # tamper does not erase the artifact: it is present (retained) but corrupt
    assert doc["retained"] is True
    # absolute drift rule (GOV-262): recorded sha256 unchanged; no re-fetch
    assert recorded_before == recorded_after
    assert (tmp_path / "Raw-Corpus/drift.pdf").read_bytes() == b"CORRUPTED!!"


# --- test 3: missing raw is a defect ----------------------------------------

def test_preservation_audit_flags_missing_raw_as_defect(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/gone.pdf", b"to be deleted")
    with db.open_db(db_path) as conn:
        _insert_document(conn, source_url="u://gone",
                         local_path="Raw-Corpus/gone.pdf", sha256=sha)
        (tmp_path / "Raw-Corpus/gone.pdf").unlink()  # delete the stored artifact

        rows = audit.audit_unit_rows(conn, repo_root=tmp_path)

    doc = _by_type(rows, "document")[0]
    assert doc["retained"] is False               # RP-1 violated
    assert doc["hash_ok"] is False
    assert doc["preservation_state"] == "defect"


# --- test 4: the reviewer-internal overlay passes assert_no_raw_paths --------

def test_preservation_overlay_passes_assert_no_raw_paths(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    # a source whose raw locator is a vault-ish path that must NEVER reach the body
    sha = _write_raw(tmp_path, "Source-Data/TownOfAlpine/raw.bin", b"vault raw bytes")
    with db.open_db(db_path) as conn:
        _insert_source(conn, source_id="vault_src", status="raw_preserved",
                       raw_local_path="Source-Data/TownOfAlpine/raw.bin", raw_sha256=sha)
        _seed_intact_corpus(conn, tmp_path)
        overlay = audit.build_preservation_overlay(conn, repo_root=tmp_path)

    # build_preservation_overlay routes through the transport guard; re-assert here
    assert read_api.assert_no_raw_paths(overlay) is overlay
    assert overlay["access"] == "reviewer_internal"
    assert overlay["units"], "overlay must carry per-unit rows"
    # no per-unit row carries a raw path key or a 64-hex sha256 value (verdict only)
    for unit in overlay["units"]:
        assert "raw_local_path" not in unit and "raw_sha256" not in unit
        assert "sha256" not in unit
        for value in _iter_values(unit):
            if isinstance(value, str):
                assert not _is_hex64(value), f"raw hash leaked into unit row: {value!r}"


def _iter_values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_values(v)
    else:
        yield obj


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


# --- test 5: no raw metadata in a web-safe projection -----------------------

def test_preservation_no_raw_metadata_in_web_safe_projection(tmp_path: Path) -> None:
    record = {
        # reviewer-internal raw metadata that MUST be dropped:
        "raw_local_path": "Source-Data/TownOfAlpine/raw.bin",
        "raw_sha256": "a" * 64,
        "raw_preservation_status": "preserved",
        "fetch_time_utc": _now(),
        # already-allowlisted, 3.03-cleared public preservation fields:
        "scan_date": "2026-01-01",
        "last_validated_utc": _now(),
        "archive_status": "available",
    }
    web = audit.web_safe_preservation_projection(record)

    assert set(web) <= {"scan_date", "last_validated_utc", "archive_status", "ui_status"}
    for unsafe in ("raw_local_path", "raw_sha256", "raw_preservation_status", "fetch_time_utc"):
        assert unsafe not in web


# --- test 6: extend-not-fork; publication.py / read_api.py 0-diff ------------

def test_preservation_audit_publication_read_api_zero_diff() -> None:
    # (a) the auditor imports the SSOT constants by reference — never re-declares.
    assert audit.pub.WEB_UNSAFE_FIELDS is publication.WEB_UNSAFE_FIELDS
    assert audit.read_api.RAW_PATH_MARKERS is read_api.RAW_PATH_MARKERS
    assert "WEB_UNSAFE_FIELDS" not in vars(audit)
    assert "RAW_PATH_MARKERS" not in vars(audit)

    # (b) git-diff evidence: the SSOT modules are byte-for-byte unchanged vs main.
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--", "scripts/publication.py", "scripts/read_api.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"publication.py/read_api.py drifted from origin/main:\n{diff.stdout}"


# --- test 7: RP-0 raw-before-parse ordering holds over the corpus -----------

def test_raw_before_parse_ordering_holds_over_corpus(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    sha = _write_raw(tmp_path, "Raw-Corpus/derived.pdf", b"raw predecessor bytes")
    with db.open_db(db_path) as conn:
        did = _insert_document(conn, source_url="u://d", local_path="Raw-Corpus/derived.pdf",
                               sha256=sha, raw_text="this derived row has a hash-verifiable raw")
        # intact corpus: the derived row's raw predecessor re-hashes -> no violation
        assert audit.raw_before_parse_violations(conn, repo_root=tmp_path) == []

        # corrupt the raw predecessor of a derived (raw_text non-null) row
        (tmp_path / "Raw-Corpus/derived.pdf").write_bytes(b"TAMPERED")
        violations = audit.raw_before_parse_violations(conn, repo_root=tmp_path)

    assert [v["id"] for v in violations] == [did]
    assert all(v["object_type"] == "document" for v in violations)
