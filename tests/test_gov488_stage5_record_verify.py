"""Stage 5.04 record-verifier/producer RED tests (GOV-488).

Prove, over a seeded reviewer-internal Alpine corpus that mirrors the real
"existing reviewer-internal data" shape (one reviewed, source-backed-but-NOT-yet-
grounded record + six unsourced ``ai_presented`` observations — i.e. the GOV-477
"0 verified items" starting state), that ``scripts/stage5_record_verifier.py``:

* drives >=1 Alpine record to ``verified`` with all four evidence elements —
  resolvable primary ``originalUrl`` + near-scan ``archiveUrl`` snapshot + a real
  ISO coverage week + the ai_presented unsourced count dropping (I6, test 1);
* is deterministic + idempotent — re-running yields a byte-identical envelope and no
  churn (I7, test 2);
* the grounding write is RED-proof load-bearing through the REAL 4.05 digest pipeline,
  non-tautologically (I5, test 3);
* lets no raw vault path / 64-hex / email / phone / ``file://`` cross the emitted
  envelope; ``localSourcePath`` never appears (I1/I2, test 4);
* exposes exactly one envelope digest — no per-source raw-content hash (I3, test 5);
* drops the unsourced ``ai_presented`` count below the 6-observation baseline (test 6);
* never leaks the reviewer-internal verified record onto the public lane (I4, test 7);
* exposes a CLI that emits the reviewer-internal verified record and exits 0 (test 8);
* the resolvability predicates are sound (test 9).

Pure sqlite + tmp files: no network, no real-corpus dependency. The seed mirrors
``tests/test_stage4_newsletter_digest_assembler.py`` (GOV-457).
"""

from __future__ import annotations

import hashlib
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
import db  # noqa: E402
import read_api  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402
import statements as st  # noqa: E402
import stage5_record_verifier as v  # noqa: E402  (under test — RED until it exists)

# --- resolved-evidence constants (real-shaped Alpine government locators) ------

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
EVENT_DATE = "2026-04-13"  # the real Alpine meeting/record date
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-04-13.pdf"
# A genuine Wayback snapshot two days after the meeting -> near the scan date.
ARCHIVE_URL = "https://web.archive.org/web/20260415000000/https://www.alpinewy.gov/minutes/2026-04-13.pdf"
RAW_SHA256 = hashlib.sha256(b"preserved alpine minutes raw bytes").hexdigest()
# A raw backend-only path (carries vault markers) — must NEVER reach a served body.
RAW_LOCAL_PATH = "/Users/IA/Obsidian Vault/Source-Data/minutes-2026-04-13.pdf"


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
    to_source_id: str,
    produced_by: str = "human",
    run_id: str | None = None,
    original_url: str | None = None,
    archive_url: str | None = None,
    archive_status: str = "not_checked",
    scan_date: str = EVENT_DATE,
) -> None:
    """Insert + reviewer-promote a source-linked statement (the GOV-146 serve gate)."""
    record = {
        "statement_id": statement_id,
        "agenda_item_id": None,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": produced_by,
    }
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
                "to_source_id": to_source_id,
                "relation": "substantiates",
                "original_url": original_url,
                "final_url": original_url,
                "archive_url": archive_url,
                "archive_status": archive_status,
                "scan_date": scan_date,
                "captured_at_utc": "2026-04-15T12:00:00Z",
                "locator_kind": "page",
                "page": 1,
                "verification_status": "human_verified",
                "confidence": "high",
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
    """The starting state: a source-backed-but-unverified record + 6 unsourced AI obs."""
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Town Council Minutes', "
        "'alpine', 'minutes', 'official', 'official', ?)",
        (MINUTES_SOURCE, ORIGINAL_URL),
    )
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Council Agenda', "
        "'alpine', 'agenda', 'official', 'official', ?)",
        (AGENDA_SOURCE, AGENDA_URL),
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-488 record-verify seed",
    )
    conn.commit()
    # The verification candidate: reviewed + source-backed, resolvable originalUrl,
    # but NOT yet archived and NOT yet raw-preserved -> serves as ``unverified``.
    _promote(
        conn, "stmt-verified", to_source_id=MINUTES_SOURCE,
        original_url=ORIGINAL_URL, archive_status="not_checked",
    )
    # The six ai_presented observations — served (anchored to a source) but UNSOURCED
    # (no resolvable primary originalUrl: a file:// vault URI is stripped at the boundary).
    for i in range(1, v.AI_PRESENTED_BASELINE + 1):
        _promote(
            conn, f"stmt-ai-{i}", to_source_id=MINUTES_SOURCE, produced_by="ai",
            run_id=f"run-ai-{i}",
            original_url="file:///Users/IA/Obsidian%20Vault/Source-Data/ai-note.txt",
        )


def _verify_candidate(conn: sqlite3.Connection) -> dict:
    """Run the 5.04 producer over the seeded candidate; return the resolution."""
    return v.verify_record(
        conn, "stmt-verified", source_id=MINUTES_SOURCE, sha256=RAW_SHA256,
        fetched_url=ORIGINAL_URL, local_path=RAW_LOCAL_PATH,
        fetch_time_utc="2026-04-15T09:00:00Z", archive_url=ARCHIVE_URL, scan_date=EVENT_DATE,
    )


def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, val in obj.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(val)
    elif isinstance(obj, (list, tuple)):
        for val in obj:
            yield from _iter_strings(val)


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


# --- test 1: drive >=1 record to verified with all four evidence elements (I6) -


def test_drives_record_to_verified(conn: sqlite3.Connection) -> None:
    before = v.resolve_verification(conn, "stmt-verified")
    assert before["status"] == "unverified", "starts unverified (no raw, no archive)"
    assert not before["verified"]

    resolution = _verify_candidate(conn)
    assert resolution["verified"] is True
    assert resolution["status"] == "verified"

    # element 1: resolvable primary originalUrl
    assert resolution["originalUrl"] == ORIGINAL_URL
    assert resolution["originalUrlResolvable"] is True
    # element 2: resolvable near-scan archive snapshot
    assert resolution["archiveUrl"] == ARCHIVE_URL
    assert resolution["archiveSnapshot"]["nearScanDate"] is True
    assert resolution["archiveSnapshot"]["snapshotDate"] == "2026-04-15"
    # element 4: real ISO coverage week (NOT undated)
    assert resolution["recordDate"] == EVENT_DATE
    assert resolution["newsletterId"] == "alpine-historical-2026-16"
    assert resolution["undated"] is False

    # the genuine downstream check: the REAL 4.05 digest composes it as verified
    item = v.assert_record_verified(conn, "stmt-verified")
    assert item["status"] == "verified"


# --- test 2: deterministic + idempotent (I7) ---------------------------------


def test_idempotent_no_churn(conn: sqlite3.Connection) -> None:
    _verify_candidate(conn)
    first = json.dumps(v.build_verified_record(conn, "stmt-verified"), sort_keys=True)
    docs_after_first = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    # re-run the producer: no re-fetch, no new rows, byte-identical envelope
    _verify_candidate(conn)
    second = json.dumps(v.build_verified_record(conn, "stmt-verified"), sort_keys=True)
    docs_after_second = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    assert first == second, "re-projection is byte-identical (NF idempotent)"
    assert docs_after_first == docs_after_second == 1, "no preserved-raw churn"


# --- test 3: RED-proof load-bearing, non-tautological (I5) --------------------


def test_grounding_is_red_proof_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    # Neuter the grounding write (record_preserved_raw -> no-op): the record never
    # gets a preserved raw predecessor, so raw_linked fails, provenance stays
    # ``unverified`` and the REAL digest composes the item as ``unverified``.
    monkeypatch.setattr(v, "record_preserved_raw", lambda *a, **k: False)
    _verify_candidate(conn)  # archive added, but raw NOT preserved
    with pytest.raises(v.RecordVerifyError):
        v.assert_record_verified(conn, "stmt-verified")  # RED via the real pipeline

    # Restore the grounding write -> the record reaches verified -> green.
    monkeypatch.undo()
    v.record_preserved_raw(
        conn, MINUTES_SOURCE, sha256=RAW_SHA256, fetched_url=ORIGINAL_URL,
        local_path=RAW_LOCAL_PATH, fetch_time_utc="2026-04-15T09:00:00Z", doc_date=EVENT_DATE,
    )
    item = v.assert_record_verified(conn, "stmt-verified")
    assert item["status"] == "verified"


def test_archive_resolver_is_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    _verify_candidate(conn)
    assert v.assert_record_verified(conn, "stmt-verified")["status"] == "verified"
    # Neuter the archive resolver -> no near-scan snapshot resolves -> RED.
    monkeypatch.setattr(v, "resolve_archive_snapshot", lambda *a, **k: None)
    with pytest.raises(v.RecordVerifyError):
        v.assert_record_verified(conn, "stmt-verified")


# --- test 4: zero raw-path/PII leak; localSourcePath null (I1/I2) -------------


def test_no_leak_and_local_source_path_null(conn: sqlite3.Connection) -> None:
    # plant raw vault path / 64-hex / email / phone / file:// into every evidence
    # column the resolver reads, on the candidate's grounding link.
    conn.execute(
        "UPDATE evidence_links SET section = ?, transcript_path = ?, deep_link = ? "
        "WHERE from_node_id = 'stmt-verified'",
        (
            "contact j.doe@alpinewy.gov / 307-555-0102",
            "/Users/IA/Obsidian Vault/Source-Data/raw.pdf",
            "file:///Volumes/secret/raw.pdf#page=1",
        ),
    )
    conn.commit()
    _verify_candidate(conn)

    body = v.build_verified_record(conn, "stmt-verified", before_status="unverified")
    # the transport sweep already ran inside build_verified_record; assert no marker
    # and no localSourcePath survived into the emitted body.
    blob = json.dumps(body)
    for marker in read_api.RAW_PATH_MARKERS:
        assert marker not in blob, f"raw marker {marker!r} leaked"
    assert "localSourcePath" not in blob
    assert "@alpinewy.gov" not in blob and "307-555-0102" not in blob
    # the backend-only preserved-raw path stays out of the served body...
    assert RAW_LOCAL_PATH not in blob
    # ...but is still recorded in the (never-served) documents table.
    assert conn.execute(
        "SELECT local_path FROM documents WHERE source_id = ?", (MINUTES_SOURCE,)
    ).fetchone()["local_path"] == RAW_LOCAL_PATH


# --- test 5: single envelope digest (I3) -------------------------------------


def test_single_envelope_digest(conn: sqlite3.Connection) -> None:
    _verify_candidate(conn)
    body = v.build_verified_record(conn, "stmt-verified")
    assert v.assert_single_envelope_digest(body) is True
    assert _is_hex64(body["verificationDigest"])
    # exactly one 64-hex string in the whole body (the envelope digest)
    hexes = [s for s in _iter_strings(body) if _is_hex64(s)]
    assert hexes == [body["verificationDigest"]]

    # a planted per-source raw-content hash -> RED
    poisoned = json.loads(json.dumps(body))
    poisoned["evidence"]["rawHash"] = RAW_SHA256
    with pytest.raises(v.RecordVerifyError):
        v.assert_single_envelope_digest(poisoned)


# --- test 6: unsourced ai_presented count drops below baseline (element 3) ----


def test_ai_presented_sourcing_drops_unsourced_count(conn: sqlite3.Connection) -> None:
    assert v.count_unsourced_ai_presented(conn) == v.AI_PRESENTED_BASELINE
    # the six AI observations are all still ai_presented and unsourced
    with pytest.raises(v.RecordVerifyError):
        v.assert_unsourced_ai_presented_dropped(conn)

    v.source_ai_observation(
        conn, "stmt-ai-1", source_id=AGENDA_SOURCE, original_url=AGENDA_URL,
        archive_url=None, scan_date=EVENT_DATE,
    )
    assert v.count_unsourced_ai_presented(conn) == v.AI_PRESENTED_BASELINE - 1
    assert v.assert_unsourced_ai_presented_dropped(conn) is True

    # the AI observation stays honestly ai_presented (no laundering into verified)
    item = v._feed_item_for(conn, "stmt-ai-1")
    assert item["labels"]["claimStatus"] == "ai_presented"


# --- test 7: never leaks onto the public lane (I4) ----------------------------


def test_verified_record_stays_reviewer_internal(conn: sqlite3.Connection) -> None:
    _verify_candidate(conn)
    v.source_ai_observation(
        conn, "stmt-ai-1", source_id=AGENDA_SOURCE, original_url=AGENDA_URL,
        archive_url=None, scan_date=EVENT_DATE,
    )
    body = v.build_verified_record(conn, "stmt-verified")
    assert body["access"] == "reviewer_internal" and body["scope"] == "alpine"
    # the public lane stays empty — verifying a reviewer-internal record must not
    # publish anything (publication_state never flipped).
    assert read_api.published_records(conn) == []
    # the record IS present on the reviewer-internal lane as verified.
    assert any(r.get("statement_id") == "stmt-verified" for r in read_api.reviewer_internal_records(conn))


# --- test 8: CLI emits the reviewer-internal verified record, exits 0 ---------


def test_cli_emits_verified_record(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _verify_candidate(conn)
    v.source_ai_observation(
        conn, "stmt-ai-1", source_id=AGENDA_SOURCE, original_url=AGENDA_URL,
        archive_url=None, scan_date=EVENT_DATE,
    )
    conn.commit()
    db_path = tmp_path / "test.db"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage5_record_verifier.py"),
         "--db", str(db_path), "--statement-id", "stmt-verified", "--check"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["access"] == "reviewer_internal"
    assert out["verified"] is True
    assert out["evidence"]["unsourcedAiPresented"] == v.AI_PRESENTED_BASELINE - 1


# --- test 9: resolvability predicates are sound ------------------------------


def test_resolvability_predicates() -> None:
    assert v.is_resolvable_primary_url("https://www.alpinewy.gov/minutes.pdf")
    assert not v.is_resolvable_primary_url("file:///Users/IA/Source-Data/raw.pdf")
    assert not v.is_resolvable_primary_url("https://")  # no host
    assert not v.is_resolvable_primary_url(None)
    assert not v.is_resolvable_primary_url("/Users/IA/raw.pdf")

    near = v.resolve_archive_snapshot("2026-04-13", ARCHIVE_URL)
    assert near and near["nearScanDate"] and near["deltaDays"] == 2
    far = v.resolve_archive_snapshot(
        "2026-04-13", "https://web.archive.org/web/20250101000000/https://x.gov/a"
    )
    assert far and not far["nearScanDate"]
    assert v.resolve_archive_snapshot("2026-04-13", "https://not-wayback.example/a") is None
