"""LED-6 export surface: unit-denominated rows, no prices (LEDGER-2026 §2).

Flattens a report pack into the exact surface the frontend / business-plan lane
consumes: rows of ``{field, unit, value, basis, formula_id, area_id, period}``,
as CSV and JSON. The ledger's currency is metered UNITS, never dollars — so the
export carries no price/dollar column at all. :func:`assert_no_prices` is the RED
guard (the fabricated-price scanner) that the export test runs.
"""

from __future__ import annotations

import csv
import io
import json

from . import basis as _basis

FIELDS = ("field", "unit", "value", "basis", "formula_id", "area_id", "period")

# Tokens that would signal a price/dollar figure leaking into the unit-only
# ledger export. The scanner rejects any of these in a field name or unit.
_PRICE_TOKENS = ("price", "usd", "dollar", "$", "cost_usd", "pricing", "invoice", "rate_usd")


def _rows_from_cells(node, area_id, period, prefix=""):
    """Walk a pack, emit one LED-6 row per value cell (dict with a ``value`` key)."""
    rows = []
    if isinstance(node, dict):
        if "value" in node:
            rows.append({
                "field": prefix or "<root>",
                "unit": node.get("unit"),
                "value": node.get("value"),
                "basis": node.get("basis"),
                "formula_id": node.get("formula_id"),
                "area_id": area_id,
                "period": period,
            })
            return rows
        for key, child in node.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_rows_from_cells(child, area_id, period, child_prefix))
    elif isinstance(node, (list, tuple)):
        for i, child in enumerate(node):
            rows.extend(_rows_from_cells(child, area_id, period, f"{prefix}[{i}]"))
    return rows


def to_rows(pack: dict) -> list[dict]:
    """LED-6 rows for a pack. Every row carries a basis label (LED-5)."""
    area_id = pack.get("area_id")
    period = pack.get("period")
    rows = _rows_from_cells(pack, area_id, period)
    assert_no_prices(rows)
    return rows


def assert_no_prices(rows: list[dict]) -> None:
    """RED guard: reject any field/unit that names a price or dollar figure.

    The ledger meters units and only ever multiplies by an OWNER-SET rate; it
    asserts no customer pricing and invents no operating-cost dollars. A price
    token in an exported row is a hard build failure (no-fabricated-price scanner).
    """
    for r in rows:
        for key in ("field", "unit"):
            text = str(r.get(key) or "").lower()
            for tok in _PRICE_TOKENS:
                if tok in text:
                    raise ValueError(
                        f"LED-6 export carries a forbidden price token {tok!r} in "
                        f"{key}={r.get(key)!r} (no-fabricated-price RED)"
                    )


def to_csv(pack: dict) -> str:
    """LED-6 CSV. Header is exactly :data:`FIELDS`; no price column."""
    rows = to_rows(pack)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def to_json(pack: dict) -> str:
    """LED-6 JSON: a list of ``{field, unit, value, basis, formula_id, area_id, period}``."""
    rows = to_rows(pack)
    return json.dumps(rows, ensure_ascii=False, indent=2)
