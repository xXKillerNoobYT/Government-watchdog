"""PILOT-2026 §5.3 test 2: snapshot completeness + basis lint (GOV-781)."""

from __future__ import annotations

from economics import basis as _basis
from pilot import snapshot


def test_snapshot_basis_lint_clean(pilot_applied):
    """Every value cell carries a valid basis label (AM-7)."""
    conn, rep = pilot_applied
    snap = snapshot.extract(conn, "alpine", rep["period"])
    assert snapshot.lint(snap) == []


def test_snapshot_has_all_section_keys(pilot_applied):
    conn, rep = pilot_applied
    snap = snapshot.extract(conn, "alpine", rep["period"])
    for section in ("cost", "quality", "latency", "safety", "support",
                    "notification", "capacity"):
        assert section in snap, section
    # §2.7: synthetic baseline and observed are separate sub-columns.
    assert "synthetic_baseline" in snap["capacity"]
    assert "observed" in snap["capacity"]


def test_measured_cells_reflect_the_run(pilot_applied):
    conn, rep = pilot_applied
    snap = snapshot.extract(conn, "alpine", rep["period"])
    # WL-1 (5 reads) + WL-2 (6 lens jobs) all produced allow audit rows for alpine.
    assert snap["quality"]["mcp_allow"]["value"] >= 11
    assert snap["quality"]["mcp_allow"]["basis"] == _basis.MEASURED
    # WL-4(b) produced exactly one redaction denial.
    assert snap["safety"]["redaction_events"]["value"] == 1
    # WL-5 sent 3 consented emails via the null adapter.
    assert snap["notification"]["consented_sends"]["value"] == 3


def test_uninstrumented_support_is_a_labeled_hole(pilot_applied):
    conn, rep = pilot_applied
    snap = snapshot.extract(conn, "alpine", rep["period"])  # no support log
    assert snap["support"]["tickets"]["basis"] == _basis.NOT_INSTRUMENTED
    assert snap["support"]["tickets"]["value"] is None


def test_support_log_aggregates_when_present(pilot_applied, tmp_path):
    conn, rep = pilot_applied
    log = tmp_path / "C1.jsonl"
    log.write_text(
        '{"ts":"t","cohort_step":"C1","user_ref":"u1","channel":"email",'
        '"category":"howto","minutes_spent":10,"resolution":"answered","owner_minutes":4}\n'
        '{"ts":"t","cohort_step":"C1","user_ref":"u2","channel":"email",'
        '"category":"bug","minutes_spent":5,"resolution":"logged","owner_minutes":1}\n',
        encoding="utf-8")
    snap = snapshot.extract(conn, "alpine", rep["period"], support_log_path=str(log))
    assert snap["support"]["tickets"]["value"] == 2
    assert snap["support"]["total_minutes"]["value"] == 15
    assert snap["support"]["owner_minutes"]["value"] == 5
    assert snap["support"]["tickets"]["basis"] == _basis.MEASURED
