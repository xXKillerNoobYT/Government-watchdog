"""Stage 4.05 deterministic digest-assembler RED tests (GOV-457).

The RED tests enumerated in the 4.05 contract
(``docs/stage4-05-newsletter-digest-assembler-contract.md`` §5). They MUST fail before
``scripts/stage4_newsletter_digest_assembler.py`` exists (ImportError) and pass after.

They prove, over a seeded reviewer-internal Alpine corpus, that the assembler:

* groups the GOV-449 item feed into one digest per Alpine coverage period, every digest
  carrying all required GOV-15 sections as structured data (test 1);
* is a pure function — re-assembling is byte-identical, NF-A reproducibility is
  load-bearing not a tautology (test 2);
* enforces section presence (EG-5) and rejects a prose section value (test 3);
* enforces non-decreasing chronology within a digest (EG-3) (test 4);
* carries every item's Stage-3 label + ``sourceTrail`` unchanged — mutation is caught (test 5);
* maps the GOV-15 sections correctly over the existing item vocabulary (test 6);
* lets no raw vault path / 64-hex hash cross the digest object or overlay; ``localSourcePath``
  null (test 7);
* extends-not-forks the SSOT — ``read_api.py`` / ``publication.py`` /
  ``stage4_newsletter_feed.py`` 0-diff and their constants are imported, not re-declared (test 8);
* exposes a CLI that emits the reviewer-internal digest object and overlay and exits 0 (test 9).

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency. The seed mirrors
``tests/test_stage4_newsletter_preservation_audit.py`` (GOV-453).
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
import read_api  # noqa: E402
import statements as st  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402
import stage4_newsletter_digest_assembler as digest  # noqa: E402  (under test — RED until it exists)


# --- seeding (mirrors tests/test_stage4_newsletter_preservation_audit.py) ----


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
    that MUST be stripped upstream — the no-leak test (test 7) proves it never crosses."""
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
        registered_by="owner:isaac", note="GOV-457 newsletter-digest seed",
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


REQUIRED_SECTIONS = {
    "processedRecords", "sourceSetProgress", "timelineChunks", "keyMeetings",
    "keyDocuments", "topics", "corrections", "conflicts", "laterOutcomes",
    "unverifiedItems", "sourceTrail",
}


# --- test 1: intact corpus assembles into reviewer-internal digests -----------


def test_assemble_digests_intact_corpus(conn: sqlite3.Connection) -> None:
    out = digest.assemble_digests(conn)
    assert out["scope"] == "alpine"
    assert out["access"] == "reviewer_internal"
    assert out["digests"], "a seeded corpus yields at least one digest"
    for d in out["digests"]:
        assert d["newsletterId"]
        assert "items" in d and d["items"]
        assert REQUIRED_SECTIONS <= set(d["sections"]), "every GOV-15 section present"
    # the digest object routes through the feed's route-aware transport guard (it embeds
    # the feed items' /alpine/ route links); a raw path would still fail loudly.
    assert nl._assert_local_safe(out) is out


# --- test 2: NF-A reproducibility is load-bearing ----------------------------


def test_reproducibility_guard_is_load_bearing(conn: sqlite3.Connection, monkeypatch) -> None:
    fingerprint = digest.assert_reproducible(conn)
    assert fingerprint

    calls = {"n": 0}
    real = nl.build_newsletter_feed

    def _nondeterministic(c):
        calls["n"] += 1
        feed = real(c)
        feed["items"][0] = dict(feed["items"][0], nonce=calls["n"])  # varies per call
        return feed

    monkeypatch.setattr(nl, "build_newsletter_feed", _nondeterministic)
    with pytest.raises(digest.DigestReproducibilityError):
        digest.assert_reproducible(conn)


# --- test 3: EG-5 section presence is load-bearing ---------------------------


def test_section_presence_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    digests = digest.assemble_digests(conn)["digests"]
    assert digest.assert_section_presence(digests) is True

    # drop a required section -> RED
    dropped = json.loads(json.dumps(digests))
    dropped[0]["sections"].pop("timelineChunks")
    with pytest.raises(digest.DigestSectionError):
        digest.assert_section_presence(dropped)

    # a prose string where structured data belongs -> RED (sections are DATA, not prose)
    prosey = json.loads(json.dumps(digests))
    prosey[0]["sections"]["topics"] = "This week Alpine discussed several topics."
    with pytest.raises(digest.DigestSectionError):
        digest.assert_section_presence(prosey)


# --- test 4: EG-3 chronology is load-bearing ---------------------------------


def test_chronology_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    digests = digest.assemble_digests(conn)["digests"]
    assert digest.assert_digest_chronology(digests) is True

    # find a digest with >=2 dated items and reverse them -> RED
    target = next(
        (d for d in json.loads(json.dumps(digests))
         if len([it for it in d["items"] if it.get("recordDate")]) >= 2),
        None,
    )
    assert target is not None, "seed must produce a digest with >=2 dated items"
    target["items"] = list(reversed(target["items"]))
    with pytest.raises(digest.DigestChronologyError):
        digest.assert_digest_chronology([target])


# --- test 5: labels + sourceTrail carried unchanged --------------------------


def test_label_and_source_trail_preservation_is_load_bearing(conn: sqlite3.Connection) -> None:
    feed = nl.build_newsletter_feed(conn)
    digests = digest.assemble_digests(conn, feed=feed)["digests"]
    assert digest.assert_labels_preserved(conn, digests, feed=feed) is True
    assert digest.assert_source_trail_preserved(conn, digests, feed=feed) is True

    # mutate a label on a digest item -> label guard RED
    poisoned_labels = json.loads(json.dumps(digests))
    poisoned_labels[0]["items"][0]["labels"]["claimStatus"] = "verified"
    with pytest.raises(digest.DigestPreservationError):
        digest.assert_labels_preserved(conn, poisoned_labels, feed=feed)

    # mutate a sourceTrail entry on a digest item -> trail guard RED
    poisoned_trail = json.loads(json.dumps(digests))
    poisoned_trail[0]["items"][0]["sourceTrail"].append({"sourceId": "phantom"})
    with pytest.raises(digest.DigestPreservationError):
        digest.assert_source_trail_preserved(conn, poisoned_trail, feed=feed)


# --- test 6: GOV-15 section mapping is correct over the corpus ----------------


def test_gov15_section_mapping(conn: sqlite3.Connection) -> None:
    out = digest.assemble_digests(conn)
    all_item_ids = {it["id"] for d in out["digests"] for it in d["items"]}
    assert all_item_ids, "feed produced items"

    # every item is in some digest's processedRecords
    processed = {
        iid for d in out["digests"] for iid in d["sections"]["processedRecords"]["itemIds"]
    }
    assert processed == all_item_ids
    for d in out["digests"]:
        assert d["sections"]["processedRecords"]["count"] == len(d["items"])

    # the corrected record surfaces in corrections; AI/unverified records surface
    # in unverifiedItems; the reviewed source set surfaces in keyDocuments/sourceTrail.
    corrections = [iid for d in out["digests"] for iid in d["sections"]["corrections"]]
    unverified = [iid for d in out["digests"] for iid in d["sections"]["unverifiedItems"]]
    documents = {sid for d in out["digests"] for sid in d["sections"]["keyDocuments"]}
    trail_sources = {
        e["sourceId"] for d in out["digests"] for e in d["sections"]["sourceTrail"]
    }
    assert corrections, "the corrected record must land in the corrections section"
    assert unverified, "unreviewed/AI records must land in unverifiedItems"
    assert "alpine_packet" in documents
    assert "alpine_packet" in trail_sources

    # sections are id-lists / structured dicts, never prose strings
    for d in out["digests"]:
        for key, value in d["sections"].items():
            assert not isinstance(value, str), f"section {key} must be data, not prose"


# --- test 7: no raw path / hash crosses the digest object or overlay ---------


def test_no_raw_leak(conn: sqlite3.Connection) -> None:
    out = digest.assemble_digests(conn)
    overlay = digest.build_digest_overlay(conn)
    for body in (out, overlay):
        blob = json.dumps(body)
        for marker in ("file://", "/Users/", ".sha256", "transcript_path", "deep_link",
                       "Source-Data", "Raw-PDFs", "\\"):
            assert marker not in blob, f"leak: {marker!r} reached an artifact"
    fingerprint = overlay["digest_digest"]
    for value in _iter_values(out):
        if isinstance(value, str):
            assert not _is_hex64(value), f"raw 64-hex hash leaked in digest: {value!r}"
    for value in _iter_values(overlay):
        if isinstance(value, str) and value != fingerprint:
            assert not _is_hex64(value), f"raw 64-hex hash leaked in overlay: {value!r}"
    # every carried sourceTrail entry keeps localSourcePath null
    for d in out["digests"]:
        for item in d["items"]:
            for entry in item["sourceTrail"]:
                assert entry["localSourcePath"] is None
        for entry in d["sections"]["sourceTrail"]:
            assert entry["localSourcePath"] is None


# --- test 8: extend-not-fork; SSOT modules 0-diff ----------------------------


def test_extend_not_fork_ssot_zero_diff() -> None:
    # (a) the assembler consumes the feed + guards by reference, re-declares nothing.
    assert digest.read_api.RAW_PATH_MARKERS is read_api.RAW_PATH_MARKERS
    assert digest.nl is nl
    assert "build_newsletter_feed" not in vars(digest)
    assert "RAW_PATH_MARKERS" not in vars(digest)
    assert "STAGE3_CLAIM_VOCAB" not in vars(digest)

    # (b) git-diff evidence: the SSOT + feed modules are byte-for-byte unchanged vs main.
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--",
         "scripts/read_api.py", "scripts/publication.py", "scripts/stage4_newsletter_feed.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"a consumed SSOT module drifted from origin/main:\n{diff.stdout}"


# --- test 9: CLI smoke -------------------------------------------------------


def test_cli_emits_reviewer_internal_digest_and_overlay(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"  # the fixture seeded this exact path
    script = ROOT / "scripts" / "stage4_newsletter_digest_assembler.py"

    digest_run = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path)],
        capture_output=True, text=True,
    )
    assert digest_run.returncode == 0, digest_run.stderr
    obj = json.loads(digest_run.stdout)
    assert obj["access"] == "reviewer_internal"
    assert obj["digests"]

    overlay_run = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--artifact", "overlay", "--check"],
        capture_output=True, text=True,
    )
    assert overlay_run.returncode == 0, overlay_run.stderr
    overlay = json.loads(overlay_run.stdout)
    assert overlay["access"] == "reviewer_internal"
    assert overlay["sections_complete"] is True
    assert overlay["chronology_ok"] is True
