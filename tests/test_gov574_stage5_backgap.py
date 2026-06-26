"""Stage 5.13 back-gap / regression auditor tests (GOV-574).

Prove, over a seeded reviewer-internal Alpine corpus that mirrors the merged registry
shape (records across layers; a corrected known_then + its superseding record; an
unchanged/archived source, a changed/archived source, a changed/UNCHECKED source, a
disappeared/no-archive source; a recorded completeness gap), that
``scripts/stage5_backgap.py``:

* Axis A — independently recomputes should-be-served membership and reconciles it against
  the OUTERMOST Stage-5 surface, surfacing every point-in-time back-gap type
  (``untraced_statement`` / ``orphan_source`` / ``dangling_trace`` / ``coverage_hole`` /
  ``coverage_unknown`` / ``archive_unchecked`` / ``archive_missing``);
* Axis B — reconciles the current surface against a pinned golden baseline, surfacing
  every regression type (``verification_regressed`` / ``publish_regressed`` /
  ``capture_lost`` / ``digest_item_dropped`` / ``correction_not_propagated``) and
  fail-closing (``baseline_absent``) when no baseline is parsed;
* is read-only (zero row delta), deterministic/idempotent (run-twice deep-equal), exposes
  exactly one envelope digest, sweeps the body for raw paths (I1/I3), keeps the Wayback
  branch default-closed + mock-only (no live call), and has a load-bearing, non-tautological
  RED-proof (neuter the membership reconciliation -> a real back-gap vanishes while the read
  surface still serves the same records).

Pure sqlite + tmp files: no network, no real-corpus dependency. The seed mirrors
``tests/test_gov531_stage5_trust_model.py`` (GOV-531).
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

import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402
import stage5_source_inventory as inv  # noqa: E402
import stage5_backgap as bg  # noqa: E402  (under test)

FIXTURE = ROOT / "tests" / "fixtures" / "stage5_backgap_baseline.json"

MINUTES_SOURCE = "alpine_minutes"
AGENDA_SOURCE = "alpine_agenda"
CHANGED_ARCHIVED = "alpine_changed_archived"
CHANGED_UNCHECKED = "alpine_changed_unchecked"
DISAPPEARED_SOURCE = "alpine_disappeared"
ORIGINAL_URL = "https://www.alpinewy.gov/minutes/2026-04-13.pdf"
AGENDA_URL = "https://www.alpinewy.gov/agenda/2026-05-11.pdf"
CHANGED_URL = "https://www.alpinewy.gov/notice/2026-03-01.html"
UNCHECKED_URL = "https://www.alpinewy.gov/notice/2026-03-15.html"
DISAPPEARED_URL = "https://www.alpinewy.gov/notice/2026-02-01.html"
WAYBACK_URL = "https://web.archive.org/web/20260413000000/" + ORIGINAL_URL
WAYBACK_CHANGED = "https://web.archive.org/web/20260301000000/" + CHANGED_URL

EARLY_DATE = "2026-01-05"
RECENT_DATE = "2026-04-13"


def _promote(
    conn: sqlite3.Connection, statement_id: str, *, to_source_id: str,
    original_url: str, scan_date: str,
) -> None:
    """Insert + reviewer-promote a source-linked statement (the GOV-146 serve gate)."""
    record = {
        "statement_id": statement_id,
        "statement_text": f"Reviewed Alpine civic claim {statement_id}.",
        "verification_status": "machine_extracted_unreviewed",
        "produced_by": "human",
    }
    link = {
        "to_source_id": to_source_id, "relation": "substantiates",
        "original_url": original_url, "final_url": original_url,
        "archive_status": "not_checked", "scan_date": scan_date,
        "captured_at_utc": "2026-04-15T12:00:00Z", "locator_kind": "page", "page": 1,
        "verification_status": "human_verified", "confidence": "high",
    }
    st.insert_statement(conn, record, [link])
    gate.promote_statement(
        conn, statement_id, reviewer_id="reviewer:isaac", decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


def _seed_sources(conn: sqlite3.Connection) -> None:
    # unchanged + archived (cited)
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status) "
        "VALUES (?, 'Minutes', 'alpine', 'minutes', 'official', 'official', ?, ?, ?, 'available')",
        (MINUTES_SOURCE, ORIGINAL_URL, RECENT_DATE, WAYBACK_URL),
    )
    # unchanged (cited)
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url) VALUES (?, 'Agenda', 'alpine', 'agenda', "
        "'official', 'official', ?)",
        (AGENDA_SOURCE, AGENDA_URL),
    )
    # changed + archived -> archive_backed (no archive gap)
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status, "
        "source_changed) VALUES (?, 'Notice (changed, archived)', 'alpine', 'notice', "
        "'official', 'official', ?, ?, ?, 'available', 1)",
        (CHANGED_ARCHIVED, CHANGED_URL, RECENT_DATE, WAYBACK_CHANGED),
    )
    # changed + NOT checked -> archive_unchecked (Wayback default-closed)
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_status, source_changed) "
        "VALUES (?, 'Notice (changed, unchecked)', 'alpine', 'notice', 'official', "
        "'official', ?, ?, 'not_checked', 1)",
        (CHANGED_UNCHECKED, UNCHECKED_URL, RECENT_DATE),
    )
    # disappeared + no archive -> archive_gap -> archive_missing
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_status) "
        "VALUES (?, 'Notice (gone)', 'alpine', 'notice', 'official', 'official', ?, ?, 'unavailable')",
        (DISAPPEARED_SOURCE, DISAPPEARED_URL, EARLY_DATE),
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-574 back-gap seed",
    )
    conn.commit()


def _seed(conn: sqlite3.Connection) -> None:
    """Rich corpus: a corrected->superseding edge + the five-source archive matrix + a gap."""
    _seed_sources(conn)
    _promote(conn, "stmt-corrected", to_source_id=MINUTES_SOURCE,
             original_url=ORIGINAL_URL, scan_date=EARLY_DATE)
    _promote(conn, "stmt-superseding", to_source_id=MINUTES_SOURCE,
             original_url=ORIGINAL_URL, scan_date=RECENT_DATE)
    _promote(conn, "stmt-plain", to_source_id=AGENDA_SOURCE,
             original_url=AGENDA_URL, scan_date=RECENT_DATE)
    conn.execute("UPDATE statements SET correction_status='corrected' WHERE statement_id='stmt-corrected'")
    conn.execute(
        "UPDATE statements SET layer='corrected_later', updates_statement_id='stmt-corrected' "
        "WHERE statement_id='stmt-superseding'"
    )
    # a recorded completeness gap (carried up as coverage_hole)
    comp.record_gap(
        conn, subject_node_id="meeting-7", subject_node_type="meeting",
        gap_type="no_primary_source", severity="warn",
    )
    conn.commit()


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


def _types(findings, axis=None):
    return {f["type"] for f in findings if axis is None or f["axis"] == axis}


def _subjects(findings, ftype):
    return {f["subjectId"] for f in findings if f["type"] == ftype}


# --- Axis A -----------------------------------------------------------------


def test_orphan_source_detected(conn):
    findings = bg.build_backgap(conn)
    orphans = _subjects(findings, bg.T_ORPHAN_SOURCE)
    # the three uncited registered sources are surfaced; the two cited ones are not.
    assert {CHANGED_ARCHIVED, CHANGED_UNCHECKED, DISAPPEARED_SOURCE} <= orphans
    assert MINUTES_SOURCE not in orphans and AGENDA_SOURCE not in orphans


def test_archive_unchecked_detected(conn):
    findings = bg.build_backgap(conn)
    assert CHANGED_UNCHECKED in _subjects(findings, bg.T_ARCHIVE_UNCHECKED)
    # an archived changed source is NOT flagged unchecked.
    assert CHANGED_ARCHIVED not in _subjects(findings, bg.T_ARCHIVE_UNCHECKED)


def test_archive_missing_detected(conn):
    findings = bg.build_backgap(conn)
    assert DISAPPEARED_SOURCE in _subjects(findings, bg.T_ARCHIVE_MISSING)


def test_coverage_hole_carries_recorded_gap(conn):
    findings = bg.build_backgap(conn)
    holes = [f for f in findings if f["type"] == bg.T_COVERAGE_HOLE]
    assert len(holes) == 1
    assert "no_primary_source" in holes[0]["subjectId"]
    assert holes[0]["severity"] == "warn"  # inherits the gap card's severity


def test_untraced_statement_detected(conn, monkeypatch):
    eligible = bg.oracle.reviewer_eligible_ids(conn)
    assert eligible, "seed must produce at least one reviewer-eligible statement"
    # a clean corpus serves every eligible record (no untraced).
    assert not _subjects(bg.build_backgap(conn), bg.T_UNTRACED_STATEMENT)
    # simulate the OUTERMOST surface silently dropping one eligible record.
    dropped = sorted(eligible)[0]
    served = set(eligible) - {dropped}
    monkeypatch.setattr(bg, "served_statement_ids", lambda c: served)
    assert dropped in _subjects(bg.build_backgap(conn), bg.T_UNTRACED_STATEMENT)


def test_dangling_trace_detected(conn, monkeypatch):
    # a served record that references a source absent from the canonical registry.
    real = bg.referenced_source_ids(conn)
    monkeypatch.setattr(bg, "referenced_source_ids", lambda c: real | {"ghost-source"})
    assert "ghost-source" in _subjects(bg.build_backgap(conn), bg.T_DANGLING_TRACE)


def test_coverage_unknown_failclosed(conn, monkeypatch):
    # the substrate normally fail-closes to valid enums; force an unresolvable envelope.
    real = inv.build_inventory(conn)

    def _broken(c):
        body = json.loads(json.dumps(real))
        body["sources"][0]["lifecycle"] = {}  # missing state -> unresolvable
        return body

    monkeypatch.setattr(bg.inv, "build_inventory", _broken)
    assert _subjects(bg.build_backgap(conn), bg.T_COVERAGE_UNKNOWN)


def test_wayback_probe_mock_only_no_live_call(conn):
    # default-closed: the unchecked changed source stays archive_unchecked.
    assert CHANGED_UNCHECKED in _subjects(bg.build_backgap(conn), bg.T_ARCHIVE_UNCHECKED)

    calls = []

    def probe_available(entry):
        calls.append(entry["sourceId"])
        return inv.SNAPSHOT_AVAILABLE

    resolved = bg.build_backgap(conn, wayback_probe=probe_available)
    assert CHANGED_UNCHECKED not in _subjects(resolved, bg.T_ARCHIVE_UNCHECKED)
    assert CHANGED_UNCHECKED in calls  # the injected mock was consulted (no network)

    def probe_unavailable(entry):
        return inv.SNAPSHOT_NOT_AVAILABLE

    missing = bg.build_backgap(conn, wayback_probe=probe_unavailable)
    assert CHANGED_UNCHECKED in _subjects(missing, bg.T_ARCHIVE_MISSING)


# --- Axis B -----------------------------------------------------------------


def test_baseline_absent_failclosed(conn):
    report = bg.analyze_backgap(conn, baseline=None)
    assert not report["baselinePresent"]
    assert not report["clean"]
    assert bg.T_BASELINE_ABSENT in _types(report["findings"], axis=bg.AXIS_REGRESSION)


def test_matching_baseline_no_regression(conn):
    baseline = bg.capture_snapshot(conn)
    report = bg.analyze_backgap(conn, baseline=baseline)
    assert report["baselinePresent"]
    assert not _types(report["findings"], axis=bg.AXIS_REGRESSION)


def _baseline_plus(conn, key, extra):
    baseline = bg.capture_snapshot(conn)
    baseline[key] = sorted(set(baseline[key]) | {extra})
    return baseline


@pytest.mark.parametrize(
    "key, extra, ftype",
    [
        ("verifiedStatementIds", "stmt-ghost", bg.T_VERIFICATION_REGRESSED),
        ("publishEligibleIds", "stmt-ghost", bg.T_PUBLISH_REGRESSED),
        ("captureSourceIds", "src-ghost", bg.T_CAPTURE_LOST),
        ("inventorySourceIds", "src-ghost", bg.T_DIGEST_ITEM_DROPPED),
        ("correctionEdges", "a->b", bg.T_CORRECTION_NOT_PROPAGATED),
    ],
)
def test_regression_types_detected(conn, key, extra, ftype):
    # a baseline claiming an id the current surface no longer serves => regression.
    baseline = _baseline_plus(conn, key, extra)
    findings = bg.build_regression(conn, baseline)
    assert extra in _subjects(findings, ftype)


# --- Invariants -------------------------------------------------------------


def test_read_only_zero_row_delta(conn):
    def _counts():
        return {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("statements", "sources", "evidence_links", "completeness_gaps")
        }

    before = _counts()
    bg.analyze_backgap(conn, baseline=bg.capture_snapshot(conn))
    assert _counts() == before


def test_deterministic_idempotent(conn):
    baseline = bg.capture_snapshot(conn)
    first = bg.analyze_backgap(conn, baseline=baseline)
    second = bg.analyze_backgap(conn, baseline=baseline)
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_single_envelope_digest_and_well_formed(conn):
    report = bg.analyze_backgap(conn, baseline=bg.capture_snapshot(conn))
    assert bg.assert_single_envelope_digest(report)
    assert bg.assert_findings_well_formed(report)
    # exactly one 64-hex string in the whole body (the digest).
    hexes = [t for t in read_api._iter_strings(report) if bg._is_hex64(t)]
    assert hexes == [report["backgapDigest"]]


def test_body_swept_for_raw_paths(conn, monkeypatch):
    # a finding carrying a filesystem path must fail LOUDLY at the transport boundary (I1).
    real = bg.build_backgap

    def _leaky(c, **kw):
        out = real(c, **kw)
        out.append(bg._finding(bg.AXIS_BACK_GAP, bg.T_ORPHAN_SOURCE, "x",
                               "/Users/IA/secret/vault/raw.pdf"))
        return out

    monkeypatch.setattr(bg, "build_backgap", _leaky)
    with pytest.raises(read_api.RawPathLeak):
        bg.analyze_backgap(conn, baseline=bg.capture_snapshot(conn))


def test_red_proof_membership_resolver_load_bearing(conn, monkeypatch):
    """Neuter the reconciliation -> a real back-gap vanishes while the surface is unchanged."""
    eligible = bg.oracle.reviewer_eligible_ids(conn)
    dropped = sorted(eligible)[0]
    served = set(eligible) - {dropped}
    monkeypatch.setattr(bg, "served_statement_ids", lambda c: served)
    # GREEN: the real auditor catches the dropped (untraced) record.
    assert dropped in _subjects(bg.build_backgap(conn), bg.T_UNTRACED_STATEMENT)
    # the read surface STILL serves the same records (the input is unchanged) — the RED
    # comes from the auditor logic, not the data.
    assert len(served) == len(eligible) - 1
    # neuter the membership reconciliation -> the back-gap is no longer detected (RED).
    monkeypatch.setattr(bg, "_membership_backgap", lambda e, s: [])
    assert dropped not in _subjects(bg.build_backgap(conn), bg.T_UNTRACED_STATEMENT)


# --- committed golden baseline + CLI gate -----------------------------------


def test_committed_baseline_fixture_present_and_clean(conn, tmp_path):
    # the committed fixture is a real golden baseline; loading it must parse + report present.
    assert FIXTURE.exists(), "committed golden baseline fixture must be present"
    loaded = bg.load_baseline(FIXTURE)
    assert isinstance(loaded, dict) and loaded.get("schemaVersion") == bg.BASELINE_SCHEMA_VERSION
    # a malformed/missing baseline fails closed to None (-> baseline_absent).
    assert bg.load_baseline(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert bg.load_baseline(bad) is None


def _clean_db(tmp_path: Path) -> Path:
    """A minimal fully-clean corpus: one cited unchanged+archived source, no gaps."""
    db_path = tmp_path / "clean.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    c.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, original_url, scan_date, archive_url, archive_status) "
        "VALUES (?, 'Minutes', 'alpine', 'minutes', 'official', 'official', ?, ?, ?, 'available')",
        (MINUTES_SOURCE, ORIGINAL_URL, RECENT_DATE, WAYBACK_URL),
    )
    gate.register_reviewer(c, "reviewer:isaac", display_name="Isaac",
                           registered_by="owner:isaac", note="clean")
    c.commit()
    _promote(c, "stmt-clean", to_source_id=MINUTES_SOURCE,
             original_url=ORIGINAL_URL, scan_date=RECENT_DATE)
    c.commit()
    c.close()
    return db_path


def test_cli_gate_exit_codes(tmp_path):
    clean_db = _clean_db(tmp_path)
    with db.open_db(clean_db) as c:
        assert bg.analyze_backgap(c, baseline=bg.capture_snapshot(c))["clean"], "clean seed must audit clean"
        baseline_path = tmp_path / "clean_baseline.json"
        baseline_path.write_text(json.dumps(bg.capture_snapshot(c)), encoding="utf-8")

    # clean audit WITH a parsed baseline -> exit 0
    assert bg.main(["--db", str(clean_db), "--baseline", str(baseline_path)]) == 0
    # no baseline -> fail-closed baseline_absent -> exit 1
    assert bg.main(["--db", str(clean_db)]) == 1
    # missing DB -> exit 2
    assert bg.main(["--db", str(tmp_path / "missing.db")]) == 2


def test_cli_json_smoke(tmp_path, capsys):
    clean_db = _clean_db(tmp_path)
    rc = bg.main(["--db", str(clean_db), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1  # no baseline supplied
    assert out["scope"] == "alpine" and out["access"] == "reviewer_internal"
    assert bg._is_hex64(out["backgapDigest"])


def test_runs_as_subprocess_no_network(tmp_path):
    # end-to-end: the module runs as a script (read-only CLI gate).
    clean_db = _clean_db(tmp_path)
    baseline_path = tmp_path / "b.json"
    with db.open_db(clean_db) as c:
        baseline_path.write_text(json.dumps(bg.capture_snapshot(c)), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "stage5_backgap.py"),
         "--db", str(clean_db), "--baseline", str(baseline_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CLEAN              : True" in proc.stdout
