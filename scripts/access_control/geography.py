"""Validation for the existing ``areas`` town→county→state spine.

ACCESS-2026 v0.1 deliberately supports exact grants only. These checks make a
new location fail review if its parent chain is malformed, while avoiding any
claim about border-town adjacency or automatic descendant access.
"""

from __future__ import annotations

import sqlite3


class InvalidArea(ValueError):
    """The requested area is missing or its hierarchy is malformed."""


EXPECTED_PARENT_KIND = {
    "town": "county",
    "county": "state",
    "state": None,
}


def validate_area(conn: sqlite3.Connection, area_id: str) -> dict:
    """Return the exact area row after validating the full parent chain."""
    if not area_id or not area_id.strip():
        raise InvalidArea("area_id is required")

    rows: list[dict] = []
    seen: set[str] = set()
    current = area_id
    while current is not None:
        if current in seen:
            raise InvalidArea(f"area hierarchy cycle at {current!r}")
        seen.add(current)
        row = conn.execute(
            "SELECT area_id, kind, name, parent_area_id"
            " FROM areas WHERE area_id = ?",
            (current,),
        ).fetchone()
        if row is None:
            raise InvalidArea(f"unknown area {current!r}")
        item = dict(row)
        if item["kind"] not in EXPECTED_PARENT_KIND:
            raise InvalidArea(f"unsupported area kind {item['kind']!r}")
        rows.append(item)
        current = item["parent_area_id"]

    for index, item in enumerate(rows):
        expected = EXPECTED_PARENT_KIND[item["kind"]]
        actual_parent_kind = rows[index + 1]["kind"] if index + 1 < len(rows) else None
        if actual_parent_kind != expected:
            raise InvalidArea(
                f"{item['kind']} {item['area_id']!r} requires parent kind"
                f" {expected!r}, found {actual_parent_kind!r}"
            )
    return rows[0]
