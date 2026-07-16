"""LED-1 aggregation correctness + AREA-2 shared-pool isolation."""

from __future__ import annotations

from conftest import ECON_PERIOD

from economics import ledger


def test_job_cost_units_summed_per_area(econ_conn):
    # alpine audits: direct_cost 100 + 150 = 250; etna: 80.
    assert sum(ledger.job_cost_units(econ_conn, "alpine", ECON_PERIOD)) == 250
    assert sum(ledger.job_cost_units(econ_conn, "etna", ECON_PERIOD)) == 80


def test_documents_processed_is_event_job_count(econ_conn):
    assert ledger.documents_processed(econ_conn, "alpine", ECON_PERIOD) == 3
    assert ledger.documents_processed(econ_conn, "etna", ECON_PERIOD) == 2
    counts = ledger.document_counts(econ_conn, ECON_PERIOD)
    assert counts["alpine"] == 3 and counts["etna"] == 2 and counts[None] == 1


def test_lane_rollup_buckets_by_lane(econ_conn):
    lanes = ledger.lane_rollup(econ_conn, "alpine", ECON_PERIOD)
    assert lanes["2_extraction"]["job_count"] == 2
    assert lanes["5_review"]["job_count"] == 1
    assert lanes["2_extraction"]["cpu_s"] == 3.0  # 1.0 + 2.0


def test_provider_rollup_sums_measured_units(econ_conn):
    prov = ledger.provider_rollup(econ_conn, "alpine", ECON_PERIOD)
    assert prov["direct_cost_units"] == 250
    assert prov["call_count"] == 2


def test_shared_pool_is_isolated_never_smeared(econ_conn):
    # The area_id IS NULL row (cost 300) lands in the shared pool and is NEVER
    # folded into a named area (AREA-2).
    shared = ledger.job_cost_units(econ_conn, None, ECON_PERIOD)
    assert sum(shared) == 300
    # Named-area totals exclude the shared-pool cost.
    assert sum(ledger.job_cost_units(econ_conn, "alpine", ECON_PERIOD)) == 250
    named_total = sum(
        sum(ledger.job_cost_units(econ_conn, a, ECON_PERIOD)) for a in ("alpine", "etna")
    )
    assert named_total == 330  # 250 + 80, the 300 shared cost is not included


def test_shared_pool_surfaces_unattributable_substrate(econ_conn):
    agg = ledger.aggregate(econ_conn, None, ECON_PERIOD)
    assert "shared_pool_extras" in agg
    extraction = agg["shared_pool_extras"]["extraction"]
    # estimated_cost_usd is disclosed as an ASSUMED annotation, never a value-cell.
    assert extraction["estimated_cost_usd_basis"] == "ASSUMED"
    assert "value" not in extraction  # not a lint value-cell
