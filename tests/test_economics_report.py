"""AM-7 / LED-5 basis lint + report reproducibility (content_sha256)."""

from __future__ import annotations

import pytest
from conftest import ECON_PERIOD

from economics import basis, report


def test_full_pack_passes_lint(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    assert basis.lint_report(pack) == []


def test_lint_fails_on_an_unlabeled_value():
    bad = {"total_cost": {"value": 123}}  # no basis
    violations = basis.lint_report(bad)
    assert violations and "basis" in violations[0]
    with pytest.raises(ValueError):
        basis.assert_labeled(bad)


def test_lint_fails_on_unrecognized_basis():
    bad = {"x": {"value": 1, "basis": "VIBES"}}
    assert basis.lint_report(bad)


def test_labeled_hole_passes_lint():
    ok = {"fixed": {"value": None, "basis": basis.OWNER_SET_UNSET}}
    assert basis.lint_report(ok) == []


def test_content_hash_is_stable_across_two_builds(econ_conn):
    p1 = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    p2 = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    assert report.content_hash(p1) == report.content_hash(p2)


def test_record_run_then_verify_hash_matches(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    report_id, digest = report.record_run(econ_conn, pack)
    result = report.verify_hash(econ_conn, report_id)
    assert result["match"] is True
    assert result["stored"] == digest


def test_record_run_refuses_unlabeled_pack(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    pack["total_cost"].pop("basis")  # sabotage a value cell
    with pytest.raises(ValueError):
        report.record_run(econ_conn, pack)


def test_rollup_pack_is_pure_aggregation_and_labeled(econ_conn):
    rollup = report.build_rollup(econ_conn, "county_rollup", "lincoln", ECON_PERIOD)
    assert basis.lint_report(rollup) == []
    # alpine variable 250 + etna 80 = 330 (shared pool excluded).
    assert rollup["variable_cost"]["value"] == 330
    assert set(rollup["member_areas"]) == {"lincoln", "alpine", "etna"}
    rid, digest = report.record_run(econ_conn, rollup)
    assert report.verify_hash(econ_conn, rid)["match"] is True
