"""PILOT-2026 §5.3 test 5: decision-pack reproducibility + no-price export (GOV-781)."""

from __future__ import annotations

import pytest

from economics import export as _export
from pilot import pack


def test_pack_content_hash_is_reproducible(pilot_applied):
    """Two builds on the same substrate hash identically (§4.3)."""
    conn, rep = pilot_applied
    h1 = pack.content_hash(pack.build_pack(conn, period=rep["period"]))
    h2 = pack.content_hash(pack.build_pack(conn, period=rep["period"]))
    assert h1 == h2


def test_build_and_record_then_verify(pilot_applied):
    conn, rep = pilot_applied
    built = pack.build_and_record(conn, period=rep["period"])
    v = pack.verify(conn, period=rep["period"], expected_sha256=built["content_sha256"])
    assert v["match"] is True
    # The economics sub-pack hash is anchored in ledger_report_runs.
    row = conn.execute(
        "SELECT content_sha256 FROM ledger_report_runs WHERE report_id = ?",
        (built["economics_report_id"],)).fetchone()
    assert row[0] == built["economics_content_sha256"]


def test_pack_basis_lint_clean(pilot_applied):
    conn, rep = pilot_applied
    p = pack.build_pack(conn, period=rep["period"])
    assert pack.lint(p) == []
    pack.assert_labeled(p)  # does not raise


def test_projection_rows_are_labeled(pilot_applied):
    conn, rep = pilot_applied
    p = pack.build_pack(conn, period=rep["period"])
    projections = [r for r in p["rows"] if r["row_kind"] == "projection"]
    assert len(projections) == 2  # county + state
    for r in projections:
        assert "PROJECTION" in r["header"]


def test_export_has_no_prices(pilot_applied):
    conn, rep = pilot_applied
    p = pack.build_pack(conn, period=rep["period"])
    rows = pack.export_rows(p)  # runs assert_no_prices internally
    assert rows
    for r in rows:
        for token in ("usd", "price", "dollar", "$"):
            assert token not in str(r["field"]).lower()
            assert token not in str(r["unit"] or "").lower()


def test_assert_no_prices_is_a_red_guard():
    """A fabricated price token in an export row is a hard failure (RED)."""
    with pytest.raises(ValueError, match="price token"):
        _export.assert_no_prices([{"field": "cost_usd", "unit": "usd"}])
