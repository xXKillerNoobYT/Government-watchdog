"""F1-F7 + F-ELIG numeric correctness + basis labels (LEDGER-2026 §2)."""

from __future__ import annotations

from economics import basis, formulas as f


def test_f1_variable_cost_is_measured_sum():
    r = f.f1_area_variable_cost([100, 150, 80])
    assert r.value == 330
    assert r.basis == basis.MEASURED
    assert r.formula_id == "LED-F1"


def test_f7_weight_is_document_share_and_assumed():
    r = f.f7_weight(3, 12)
    assert r.value == 0.25
    assert r.basis == basis.ASSUMED
    # zero total -> zero weight, never a divide error
    assert f.f7_weight(5, 0).value == 0.0


def test_f2_allocated_fixed_is_product_and_assumed():
    r = f.f2_area_allocated_fixed(1000, 0.25)
    assert r.value == 250.0
    assert r.basis == basis.ASSUMED
    # owner has not set fixed_total -> value None, still labeled
    assert f.f2_area_allocated_fixed(None, 0.25).value is None


def test_f3_total_is_derived_and_carries_worst_input_basis():
    r = f.f3_area_total_cost(330, 250.0,
                             variable_basis=basis.MEASURED, fixed_basis=basis.ASSUMED)
    assert r.value == 580.0
    assert r.basis == basis.DERIVED
    assert r.worst_input_basis == basis.ASSUMED  # ASSUMED taints the MEASURED sum


def test_f4_and_f5_guard_divide_by_zero():
    assert f.f4_cost_per_active_user(580.0, 0).value is None
    assert f.f4_cost_per_active_user(580.0, None).value is None
    assert f.f4_cost_per_active_user(580.0, 4).value == 145.0
    assert f.f5_cost_per_document(580.0, 0).value is None
    assert f.f5_cost_per_document(580.0, 5).value == 116.0
    assert f.f4_cost_per_active_user(580.0, 4).basis == basis.DERIVED


def test_f6_capacity_headroom_is_measured_difference():
    r = f.f6_capacity_headroom(120.0, 75.0)
    assert r.value == 45.0
    assert r.basis == basis.MEASURED
    assert r.formula_id == "LED-F6"


def test_f_elig_rule():
    # cost 300 <= balance 500 * safety 1.0 -> eligible
    assert f.f_elig(300, 500, 1.0) is True
    # cost 600 <= 500 * 1.0 -> not eligible
    assert f.f_elig(600, 500, 1.0) is False
    # safety factor 1.5 lifts the bar: 600 <= 500 * 1.5 = 750 -> eligible
    assert f.f_elig(600, 500, 1.5) is True


def test_worst_basis_ordering():
    assert basis.worst_basis(basis.MEASURED, basis.MEASURED) == basis.MEASURED
    assert basis.worst_basis(basis.MEASURED, basis.ASSUMED) == basis.ASSUMED
    assert basis.worst_basis(basis.MEASURED, basis.OWNER_SET) == basis.OWNER_SET
    assert basis.worst_basis(basis.ASSUMED, basis.DERIVED) == basis.DERIVED
