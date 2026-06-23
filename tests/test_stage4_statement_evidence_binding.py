"""Stage 4.07 statement->exact-source binding validator RED tests (GOV-467).

The RED tests enumerated in the 4.07 contract
(``docs/stage4-07-statement-evidence-binding-contract.md`` §5). They MUST fail before
``scripts/stage4_statement_evidence_binding.py`` exists (ModuleNotFoundError) and pass after.

They prove, over a seeded reviewer-internal Alpine corpus, that the validator:

* maps every statement-bearing digest item to its real statement record and asserts an
  exact-source pointer (resolving segment edge OR a complete, valid evidence_link pointer),
  one validation-log row per item, zero unrouted orphans (test 1);
* EG-2 every-statement-bound is load-bearing — an *incomplete* served pointer (still served
  by read_api, which never re-checks pointer completeness) is caught as an orphan and routed
  to VSR, proving the defense-in-depth gap over the serve gate is real, not tautological (test 2);
* EG-4 orphan-routing is load-bearing — an unrouted orphan row raises (test 3);
* a non-verified statement keeps its conservative Stage-3 label and is never silently upgraded
  to verified; an out-of-vocabulary claim label raises (test 4);
* paraphrase != verbatim — a verbatim-styled statement lacking a segment / quoted_text anchor
  is flagged (test 5);
* the log is a pure function of the DB — byte-identical across builds (test 6);
* no raw vault path / 64-hex hash crosses the log or overlay; statementId is a slug (test 7);
* extends-not-forks the SSOT — statements/read_api/publication/digest/feed 0-diff, constants by
  reference (test 8);
* exposes a CLI that emits the reviewer-internal log + overlay and exits 0 (test 9).

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency. The seed mirrors
``tests/test_stage4_newsletter_digest_assembler.py`` (GOV-457), adding a verbatim,
quoted_text-anchored statement so the paraphrase!=verbatim guard has a positive subject.
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
import stage3_card_feed as card_feed  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402
import stage4_newsletter_digest_assembler as digest  # noqa: E402
import stage4_statement_evidence_binding as binding  # noqa: E402  (under test — RED until it exists)


# --- seeding (mirrors tests/test_stage4_newsletter_digest_assembler.py) -------


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
    is_verbatim: int = 0,
    evidence: list[dict] | None = None,
) -> None:
    """Insert + reviewer-promote a statement. Every evidence link plants a raw path that
    MUST be stripped upstream — the no-leak test (test 7) proves it never crosses."""
    record = {
        "statement_id": statement_id,
        "agenda_item_id": None,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": produced_by,
        "is_verbatim": is_verbatim,
    }
    if correction is not None:
        record["correction_status"] = correction
    if produced_by == "ai":
        if conn.execute(
            "SELECT 1 FROM ai_extraction_runs WHERE run_id=?", (run_id,)
        ).fetchone() is None:
            ai.create_run(conn, run_id=run_id, input_source_ids=[])
        record["ai_extraction_run_id"] = run_id
    if evidence is None:
        evidence = [
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
        ]
    st.insert_statement(conn, record, evidence)
    gate.promote_statement(
        conn,
        statement_id,
        reviewer_id="reviewer:isaac",
        decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


_VERBATIM_QUOTE = "the motion carries four to one"


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES ('alpine_packet', 'Agenda Packet', "
        "'alpine', 'agenda_packet', 'official', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-467 statement-binding seed",
    )
    conn.commit()
    # paraphrase statements (is_verbatim=0), page-anchored.
    for i in range(1, 5):
        _promote(conn, f"stmt-{i}", page=i, is_verbatim=0)
    # a verbatim statement (is_verbatim=1) bound by a char_span quoted_text anchor.
    _promote(
        conn,
        "stmt-verbatim",
        page=5,
        is_verbatim=1,
        evidence=[
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": "2026-05-05",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "char_span",
                "char_start": 0,
                "char_end": len(_VERBATIM_QUOTE),
                "quoted_text": _VERBATIM_QUOTE,
                "verification_status": "human_verified",
                "confidence": "high",
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
            }
        ],
    )
    _promote(conn, "stmt-ai", produced_by="ai", run_id="run-ai", page=6, is_verbatim=0)
    _promote(conn, "stmt-corrected", correction="corrected", page=7, is_verbatim=0)
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


# --- test 1: intact corpus -> every statement-bearing item is bound -----------


def test_statement_link_validation_intact_corpus(conn: sqlite3.Connection) -> None:
    log = binding.statement_link_validation(conn)
    assert log["scope"] == "alpine"
    assert log["access"] == "reviewer_internal"
    assert log["rows"], "a seeded corpus yields at least one statement-bearing row"
    for row in log["rows"]:
        assert row["resolves"] is True, f"orphan in a clean corpus: {row}"
        assert row["pointerKind"] in ("segment", "page", "char_span", "section",
                                      "paragraph", "timestamp")
        assert row["statementId"]
        assert row["route"] is None
        assert row["label"] in nl.STAGE3_CLAIM_VOCAB
    assert log["routing"] == []
    assert log["passed"] is True
    # the log carries no /alpine/ route links — it must pass the strict transport guard.
    assert read_api.assert_no_raw_paths(log) is log


# --- test 2: EG-2 every-statement-bound is load-bearing (defense-in-depth) -----


def test_every_statement_bound_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    assert binding.assert_every_statement_bound(conn) is True

    # Poison ONE served statement's only pointer to be INCOMPLETE: a page locator with a
    # null `page` and no segment edge. read_api still serves it (its evidence_links list is
    # non-empty — the serve gate never re-checks pointer completeness), the digest still
    # carries it, but statements.validate_pointer rejects the pointer -> orphan. This is the
    # defense-in-depth gap 4.07 closes; a tautological re-check of the serve gate would miss it.
    conn.execute(
        "UPDATE evidence_links SET page = NULL WHERE from_node_id = 'stmt-1'"
    )
    conn.commit()
    # sanity: read_api STILL serves stmt-1 (the bug 4.07 catches), so the digest carries it.
    served_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    assert "stmt-1" in served_ids, "read_api's serve gate does not catch the incomplete pointer"

    with pytest.raises(binding.OrphanStatementError):
        binding.assert_every_statement_bound(conn)

    # the orphan is ROUTED to VSR in the log, never silently dropped.
    log = binding.statement_link_validation(conn)
    orphans = [r for r in log["rows"] if not r["resolves"]]
    assert orphans, "the incomplete pointer must surface as an orphan row"
    assert all(r["route"] == nl.VSR for r in orphans)
    assert any(r["statementId"] == "stmt-1" for r in orphans)


# --- test 3: EG-4 orphan-routing is load-bearing ------------------------------


def test_no_unrouted_orphans_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    log = binding.statement_link_validation(conn)
    assert binding.assert_no_unrouted_orphans(log) is True

    # a hand-built log with an orphan whose route is NOT VSR -> RED.
    poisoned = json.loads(json.dumps(log))
    poisoned["rows"].append({
        "itemId": "alpine-newsletter-item-999",
        "statementId": "stmt-ghost",
        "pointerKind": None,
        "resolves": False,
        "label": "unverified",
        "route": None,  # an orphan that was NOT routed
    })
    with pytest.raises(binding.UnroutedOrphanError):
        binding.assert_no_unrouted_orphans(poisoned)


# --- test 4: label conservatism / no silent upgrade is load-bearing -----------


def test_labels_conservative_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    out = digest.assemble_digests(conn)
    assert binding.assert_labels_conservative(conn, out) is True

    # mutate one item's claimStatus to "verified" while the read surface still recomputes
    # the conservative status -> silent upgrade caught.
    upgraded = json.loads(json.dumps(out))
    upgraded["digests"][0]["items"][0]["labels"]["claimStatus"] = "verified"
    with pytest.raises(binding.LabelUpgradeError):
        binding.assert_labels_conservative(conn, upgraded)

    # a claim label entirely outside the Stage-3 vocabulary -> RED.
    off_vocab = json.loads(json.dumps(out))
    off_vocab["digests"][0]["items"][0]["labels"]["claimStatus"] = "totally_made_up"
    with pytest.raises(binding.LabelUpgradeError):
        binding.assert_labels_conservative(conn, off_vocab)


# --- test 5: paraphrase != verbatim is load-bearing ---------------------------


def test_verbatim_anchored_guard_is_load_bearing(conn: sqlite3.Connection) -> None:
    assert binding.assert_verbatim_anchored(conn) is True

    # flip the verbatim statement's anchor to a bare page pointer: still a VALID pointer
    # (so it stays bound), but no quoted_text and no segment edge -> verbatim-overclaim.
    conn.execute(
        "UPDATE evidence_links SET locator_kind = 'page', page = 1, "
        "char_start = NULL, char_end = NULL, quoted_text = NULL "
        "WHERE from_node_id = 'stmt-verbatim'"
    )
    conn.commit()
    # it is still bound (a page pointer is valid) — the failure is verbatim-specific.
    assert binding.assert_every_statement_bound(conn) is True
    with pytest.raises(binding.VerbatimAnchorError):
        binding.assert_verbatim_anchored(conn)


# --- test 6: reproducibility (pure function of the DB) ------------------------


def test_log_is_reproducible(conn: sqlite3.Connection) -> None:
    fingerprint = binding.assert_reproducible(conn)
    assert fingerprint
    a = json.dumps(binding.statement_link_validation(conn), sort_keys=True)
    b = json.dumps(binding.statement_link_validation(conn), sort_keys=True)
    assert a == b, "the validation log must be byte-identical across builds"


# --- test 7: no raw path / hash crosses the log or overlay -------------------


def test_no_raw_leak(conn: sqlite3.Connection) -> None:
    log = binding.statement_link_validation(conn)
    overlay = binding.build_binding_overlay(conn)
    for body in (log, overlay):
        blob = json.dumps(body)
        for marker in ("file://", "/Users/", ".sha256", "transcript_path", "deep_link",
                       "Source-Data", "Raw-PDFs", "\\"):
            assert marker not in blob, f"leak: {marker!r} reached an artifact"
    fingerprint = overlay["binding_digest"]
    for value in _iter_values(log):
        if isinstance(value, str):
            assert not _is_hex64(value), f"raw 64-hex hash leaked in log: {value!r}"
    for value in _iter_values(overlay):
        if isinstance(value, str) and value != fingerprint:
            assert not _is_hex64(value), f"raw 64-hex hash leaked in overlay: {value!r}"
    # the quoted_text verbatim anchor (reviewer-internal) is NEVER emitted in the log.
    assert _VERBATIM_QUOTE not in json.dumps(log)


# --- test 8: extend-not-fork; SSOT modules 0-diff ----------------------------


def test_extend_not_fork_ssot_zero_diff() -> None:
    # (a) the validator consumes the SSOT modules by reference, re-declares nothing.
    assert binding.read_api.RAW_PATH_MARKERS is read_api.RAW_PATH_MARKERS
    assert binding.st is st
    assert binding.digest is digest
    assert binding.nl is nl
    assert "LOCATOR_REQUIRED_FIELDS" not in vars(binding)
    assert "STAGE3_CLAIM_VOCAB" not in vars(binding)
    assert "RAW_PATH_MARKERS" not in vars(binding)
    assert "assemble_digests" not in vars(binding)

    # (b) git-diff evidence: the SSOT + consumed modules are byte-for-byte unchanged vs main.
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--",
         "scripts/statements.py", "scripts/read_api.py", "scripts/publication.py",
         "scripts/stage4_newsletter_digest_assembler.py", "scripts/stage4_newsletter_feed.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"a consumed SSOT module drifted from origin/main:\n{diff.stdout}"


# --- test 9: CLI smoke -------------------------------------------------------


def test_cli_emits_reviewer_internal_log_and_overlay(conn: sqlite3.Connection, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"  # the fixture seeded this exact path
    script = ROOT / "scripts" / "stage4_statement_evidence_binding.py"

    log_run = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path)],
        capture_output=True, text=True,
    )
    assert log_run.returncode == 0, log_run.stderr
    log = json.loads(log_run.stdout)
    assert log["access"] == "reviewer_internal"
    assert log["rows"]
    assert log["passed"] is True

    overlay_run = subprocess.run(
        [sys.executable, str(script), "--db", str(db_path), "--artifact", "overlay", "--check"],
        capture_output=True, text=True,
    )
    assert overlay_run.returncode == 0, overlay_run.stderr
    overlay = json.loads(overlay_run.stdout)
    assert overlay["access"] == "reviewer_internal"
    assert overlay["all_bound"] is True
    assert overlay["no_unrouted_orphans"] is True
    assert overlay["labels_conservative"] is True
    assert overlay["verbatim_anchored"] is True
