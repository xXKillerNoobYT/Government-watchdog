"""LED-6 export surface: field shape + no-fabricated-price scanner (RED)."""

from __future__ import annotations

import csv
import io
import json

import pytest
from conftest import ECON_PERIOD

from economics import export, report

_REQUIRED = {"field", "unit", "value", "basis", "formula_id", "area_id", "period"}


def test_json_rows_have_the_led6_shape(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    rows = json.loads(export.to_json(pack))
    assert rows
    for r in rows:
        assert set(r.keys()) == _REQUIRED
        assert r["basis"] is not None  # every row basis-labeled (LED-5)
        assert r["area_id"] == "alpine"
        assert r["period"] == ECON_PERIOD


def test_csv_header_is_exactly_the_led6_fields(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    reader = csv.DictReader(io.StringIO(export.to_csv(pack)))
    assert reader.fieldnames == list(export.FIELDS)
    rows = list(reader)
    assert rows


def test_no_price_or_dollar_column_anywhere(econ_conn):
    pack = report.build_pack(econ_conn, "alpine", ECON_PERIOD)
    text = export.to_json(pack).lower() + export.to_csv(pack).lower()
    for token in ("price", "usd", "dollar", "$", "invoice"):
        assert token not in text


def test_price_scanner_is_a_hard_red():
    # A hand-forged row carrying a price token must trip the scanner.
    forged = [{"field": "list_price", "unit": "usd", "value": 9.99,
               "basis": "ASSUMED", "formula_id": None, "area_id": "a", "period": "p"}]
    with pytest.raises(ValueError):
        export.assert_no_prices(forged)


def test_shared_pool_export_excludes_estimated_cost_usd(econ_conn):
    # The shared pool holds an ASSUMED estimated_cost_usd annotation; it must never
    # reach the unit-only export.
    pack = report.build_pack(econ_conn, None, ECON_PERIOD)
    text = export.to_json(pack).lower()
    assert "usd" not in text
