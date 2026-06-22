"""Stage 4.04 newsletter raw-preservation & reproducibility auditor RED tests (GOV-453).

The RED tests enumerated in the 4.04 contract
(``Docs/stage4-04-newsletter-preservation-reproducibility-contract.md`` §5). They MUST
fail before ``scripts/stage4_newsletter_preservation_audit.py`` exists (ImportError) and
pass after.

They prove, over a seeded reviewer-internal Alpine corpus, that the read-time auditor:

* reports NF-1/NF-2/NF-3 true on an intact corpus, in a reviewer-internal overlay that
  passes ``read_api.assert_no_raw_paths`` (test 1);
* catches a non-deterministic projection — NF-1 reproducibility is load-bearing, not a
  tautology (test 2);
* catches a fabricated item and a lossy/invented source linkage — NF-2 provenance is a
  genuine independent cross-check (tests 3, 4);
* catches a write to a raw table and proves the clean build mutates nothing — NF-3 (test 5);
* lets no raw vault path / 64-hex hash cross the overlay; ``localSourcePath`` null (test 6);
* extends-not-forks the SSOT — ``read_api.py`` / ``publication.py`` /
  ``stage4_newsletter_feed.py`` 0-diff and their constants are imported, not re-declared (test 7);
* exposes a CLI that emits the reviewer-internal overlay and exits 0 (test 8).

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import publication  # noqa: E402
import read_api  # noqa: E402
import stage3_card_feed as card_feed  # noqa: E402
import statements as st  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402
import stage4_newsletter_preservation_audit as audit  # noqa: E402  (under test — RED until it exists)


# --- seeding (mirrors tests/test_gov449_newsletter_feed.py) ------------------


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


def _promote(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    produced_by: str = "human",
    correction: str | None = None,
    run_id: str | None = None,
    page: int = 1,
) -> None:
    """Insert + reviewer-promote a statement; every evidence link plants a raw path
    that MUST be stripped upstream — the no-leak test (test 6) proves it never crosses."""
    record = {
        "statement_id": statement_id,
        "agenda_item_id": None,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": produced_by,
    }
    if correction is not None:
        record["correction_status"] = correction
    if produced_by == "ai":
        if conn.execute(
            "SELECT 1 FROM ai_extraction_runs WHERE run_id=?", (run_id,)
        ).fetchone() is None:
            ai.create_run(conn, run_id=run_id, input_source_ids=[])
        record["ai_extraction_run_id"] = run_id
    st.insert_statement(
        conn,
        record,
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "final_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": f"2026-05-{page:02d}",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": page,
                "verification_status": "human_verified",
                "confidence": "high",
                # raw/private locators that MUST be stripped at the boundary:
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=1",
            }
        ],
    )
    gate.promote_statement(
        conn,
        statement_id,
        reviewer_id="reviewer:isaac",
        decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES ('alpine_packet', 'Agenda Packet', "
        "'alpine', 'agenda_packet', 'official', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-453 newsletter-preservation seed",
    )
    conn.commit()
    for i in range(1, 6):
        _promote(conn, f"stmt-{i}", page=i)
    _promote(conn, "stmt-ai", produced_by="ai", run_id="run-ai", page=6)
    _promote(conn, "stmt-corrected", correction="corrected", page=7)
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source", commit=True,
    )


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


# --- test 1: all invariants hold on an intact corpus -------------------------


def test_overlay_reports_all_invariants_on_intact_corpus(conn: sqlite3.Connection) -> None:
    overlay = audit.build_preservation_overlay(conn)
    assert overlay["scope"] == "alpine"
    assert overlay["access"] == "reviewer_internal"
    assert overlay["reproducible"] is True            # NF-1
    assert overlay["provenance_ok"] is True           # NF-2
    assert overlay["raw_mutation_ok"] is True         # NF-3
    assert overlay["item_count"] > 0
    assert overlay["feed_digest"] and isinstance(overlay["feed_digest"], str)
    assert all(not v for v in overlay["violations"].values())
    # the overlay routes through the transport guard; re-assert here
    assert read_api.assert_no_raw_paths(overlay) is overlay


# --- test 2: NF-1 reproducibility is load-bearing ----------------------------


def test_reproducibility_guard_is_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    # clean: the real feed is a pure function — assert_reproducible returns a digest.
    digest = audit.assert_reproducible(conn)
    assert digest

    # neuter: a build that returns a different value each call. A tautological guard
    # (comparing a value to itself) would still pass; the real guard must go RED.
    calls = {"n": 0}
    real = nl.build_newsletter_feed

    def _nondeterministic(c):
        calls["n"] += 1
        feed = real(c)
        feed["nonce"] = calls["n"]  # different every call -> not reproducible
        return feed

    monkeypatch.setattr(nl, "build_newsletter_feed", _nondeterministic)
    with pytest.raises(audit.NewsletterReproducibilityError):
        audit.assert_reproducible(conn)


# --- test 3: NF-2 catches a fabricated item ----------------------------------


def test_provenance_flags_fabricated_item(conn: sqlite3.Connection) -> None:
    assert audit.provenance_violations(conn) == []  # clean corpus traces fully

    fabricated = {
        "id": "alpine-newsletter-item-999",
        "cardIds": ["info_does-not-exist"],        # no served record backs this handle
        "sourceIds": ["ghost_source"],
        "sourceTrail": [{"sourceId": "ghost_source", "localSourcePath": None}],
    }
    violations = audit.provenance_violations(conn, feed={"items": [fabricated]})
    assert violations, "a fabricated item must be flagged"
    assert any(v["item_id"] == "alpine-newsletter-item-999" for v in violations)


# --- test 4: NF-2 catches a lossy / invented source linkage ------------------


def test_provenance_flags_lossy_source_linkage(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    assert audit.provenance_violations(conn, feed=feed) == []  # honest feed traces

    # mutate one real item's source set: add a phantom source the served record
    # never carried. NF-2 (lossless linkage) must go RED.
    poisoned = json.loads(json.dumps(feed))
    poisoned["items"][0]["sourceIds"] = poisoned["items"][0]["sourceIds"] + ["phantom_src"]
    violations = audit.provenance_violations(conn, feed=poisoned)
    assert violations, "an invented source on a real item must be flagged"
    assert violations[0]["item_id"] == poisoned["items"][0]["id"]


# --- test 5: NF-3 zero raw mutation ------------------------------------------


def test_raw_mutation_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    # clean: building all three artifacts mutates none of the raw tables.
    assert audit.raw_mutation_violations(conn) == []

    # neuter: a build that writes to a raw table. NF-3 must go RED naming that table.
    def _writes_raw(c):
        c.execute(
            "INSERT INTO sources (source_id, name, scope, source_class) "
            "VALUES ('injected_src', 'x', 'alpine', 'municipal_primary')"
        )
        c.commit()

    violations = audit.raw_mutation_violations(conn, build=_writes_raw)
    assert any(v["table"] == "sources" for v in violations)


# --- test 6: the overlay leaks no raw path / hash ----------------------------


def test_overlay_no_raw_leak(conn: sqlite3.Connection) -> None:
    overlay = audit.build_preservation_overlay(conn)
    blob = json.dumps(overlay)
    for marker in ("file://", "/Users/", ".sha256", "transcript_path", "deep_link",
                   "Source-Data", "Raw-PDFs", "\\"):
        assert marker not in blob, f"leak: {marker!r} reached the overlay"
    for value in _iter_values(overlay):
        if isinstance(value, str) and value != overlay["feed_digest"]:
            assert not _is_hex64(value), f"raw 64-hex hash leaked: {value!r}"
    # the feed it audits also has localSourcePath null on every trail entry
    feed = nl.build_newsletter_feed(conn)
    for item in feed["items"]:
        for entry in item["sourceTrail"]:
            assert entry["localSourcePath"] is None


# --- test 7: extend-not-fork; SSOT modules 0-diff ----------------------------


def test_extend_not_fork_ssot_zero_diff() -> None:
    # (a) the auditor consumes the feed + guards by reference, re-declares nothing.
    assert audit.read_api.RAW_PATH_MARKERS is read_api.RAW_PATH_MARKERS
    assert audit.nl is nl
    assert "build_newsletter_feed" not in vars(audit)
    assert "RAW_PATH_MARKERS" not in vars(audit)

    # (b) git-diff evidence: the SSOT + feed modules are byte-for-byte unchanged vs main.
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--",
         "scripts/read_api.py", "scripts/publication.py", "scripts/stage4_newsletter_feed.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"a consumed SSOT module drifted from origin/main:\n{diff.stdout}"


# --- test 8: CLI smoke -------------------------------------------------------


def test_cli_emits_reviewer_internal_overlay(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"  # the fixture seeded this exact path
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage4_newsletter_preservation_audit.py"),
         "--db", str(db_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    overlay = json.loads(result.stdout)
    assert overlay["access"] == "reviewer_internal"
    assert overlay["reproducible"] is True
