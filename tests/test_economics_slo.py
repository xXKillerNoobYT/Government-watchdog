"""AM-12 / SLO-7: every SLO metric emitted per-area in the ledger surface."""

from __future__ import annotations

from conftest import ECON_PERIOD

from economics import basis, ledger, report

_ALL_SLOS = {"SLO-1", "SLO-2", "SLO-3", "SLO-4", "SLO-5", "SLO-6"}


def test_all_six_slos_present_per_area(econ_conn):
    for area in ("alpine", "etna"):
        metrics = ledger.slo_metrics(econ_conn, area, ECON_PERIOD)
        assert {m["slo_id"] for m in metrics} == _ALL_SLOS


def test_every_slo_cell_is_basis_labeled(econ_conn):
    metrics = ledger.slo_metrics(econ_conn, "alpine", ECON_PERIOD)
    # lint the whole SLO block (measured + target cells).
    assert basis.lint_report(metrics) == []
    for m in metrics:
        assert m["measured"]["basis"] in basis.ACCEPTED_BASIS_LABELS
        assert m["target"]["basis"] in basis.ACCEPTED_BASIS_LABELS


def test_read_latency_p95_is_measured_from_substrate(econ_conn):
    metrics = {m["slo_id"]: m for m in ledger.slo_metrics(econ_conn, "alpine", ECON_PERIOD)}
    slo3 = metrics["SLO-3"]  # read_latency_p95 from alpine audits (120, 200)
    assert slo3["measured"]["basis"] == basis.MEASURED
    assert slo3["measured"]["value"] == 200.0  # nearest-rank p95 of [120, 200]


def test_slo5_has_no_target_labeled_hole(econ_conn):
    metrics = {m["slo_id"]: m for m in ledger.slo_metrics(econ_conn, "alpine", ECON_PERIOD)}
    slo5 = metrics["SLO-5"]  # review_turnaround: no target until pilot
    assert slo5["target"]["value"] is None
    assert slo5["target"]["basis"] == basis.OWNER_SET_UNSET


def test_slo_block_present_in_report_pack(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    assert {m["slo_id"] for m in pack["slo"]} == _ALL_SLOS
