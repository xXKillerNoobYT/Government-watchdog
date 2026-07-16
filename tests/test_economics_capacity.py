"""LED-F6 capacity: deterministic synthetic harness; headroom = max - load."""

from __future__ import annotations

from economics import basis, capacity


def test_synthetic_load_is_deterministic_for_a_seed():
    a = capacity.synthetic_load("alpine", seed="s1")
    b = capacity.synthetic_load("alpine", seed="s1")
    assert a == b  # same (seed, area) -> byte-identical


def test_different_seed_or_area_changes_the_load():
    assert capacity.synthetic_load("alpine", seed="s1") != capacity.synthetic_load("alpine", seed="s2")
    assert capacity.synthetic_load("alpine", seed="s1") != capacity.synthetic_load("etna", seed="s1")


def test_headroom_is_max_minus_load_and_measured():
    fc = capacity.forecast("alpine", seed="s1")
    mx = fc["max_sustainable_throughput"]["value"]
    load = fc["current_load"]["value"]
    assert abs(fc["capacity_headroom"]["value"] - (mx - load)) < 1e-9
    assert fc["capacity_headroom"]["basis"] == basis.MEASURED


def test_forecast_pack_is_basis_labeled():
    fc = capacity.forecast("alpine")
    assert basis.lint_report(fc) == []


def test_load_stays_within_capacity():
    # current load is a fraction (< 0.9) of max -> headroom always positive.
    for area in ("alpine", "etna", "lincoln", "wy"):
        fc = capacity.forecast(area)
        assert fc["capacity_headroom"]["value"] > 0
