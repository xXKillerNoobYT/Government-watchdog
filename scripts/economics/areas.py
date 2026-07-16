"""Area rollup spine + owner-gated state machine (LEDGER-2026 §2, AREA-1..5, AM-1).

Two responsibilities:

1. **Rollup spine (AREA-1..3).** ``areas`` is the canonical town -> county ->
   state tree the loose ``area_id`` tags point at. :func:`descendants` /
   :func:`ancestors` walk it for pure-aggregation rollups.

2. **State machine (AREA-4/5, "define, not activate").** :func:`transition` is
   the ONE code path that writes ``area_state``. It is inert without an
   ``owner_decision_ref`` and refuses any edge not in
   :data:`LEGAL_TRANSITIONS` — *before* any write, so AM-1 can assert that an
   illegal or ownerless transition leaves zero rows behind. Every applied
   transition writes exactly one ``area_transitions`` audit row.

The legal-transition table lives here in code (not a DB trigger) precisely so
the unit tests can enumerate and reject every illegal edge deterministically.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

STATES = frozenset(
    {"locked", "free_home", "free_beta", "funded", "paid", "limited"}
)

# Legal (from_state -> to_state) edges. Design intent:
#   * from 'locked' an owner may open an area to any FREE or FUNDED serving state,
#     or park it as 'limited' — but NOT jump straight to 'paid' (paid requires a
#     prior 'funded' footing; GATE-P readiness). This gives AM-1 a concrete
#     illegal edge (locked -> paid) to assert on.
#   * every state may return to 'locked' (owner can always pull an area).
#   * 'paid' is only reachable from 'funded' or 'limited' (an owner decision that
#     an area is entitlement-ready), never from a free tier directly.
LEGAL_TRANSITIONS = frozenset({
    ("locked", "free_home"),
    ("locked", "free_beta"),
    ("locked", "funded"),
    ("locked", "limited"),
    ("free_home", "free_beta"),
    ("free_home", "funded"),
    ("free_home", "limited"),
    ("free_home", "locked"),
    ("free_beta", "free_home"),
    ("free_beta", "funded"),
    ("free_beta", "limited"),
    ("free_beta", "locked"),
    ("funded", "free_home"),
    ("funded", "paid"),
    ("funded", "limited"),
    ("funded", "locked"),
    ("paid", "funded"),
    ("paid", "limited"),
    ("paid", "locked"),
    ("limited", "free_home"),
    ("limited", "free_beta"),
    ("limited", "funded"),
    ("limited", "paid"),
    ("limited", "locked"),
})


class IllegalTransition(ValueError):
    """Raised when an edge is not in :data:`LEGAL_TRANSITIONS` (zero writes)."""


class OwnerlessTransition(ValueError):
    """Raised when :func:`transition` is called without an owner decision ref."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def create_area(conn: sqlite3.Connection, *, area_id: str, kind: str, name: str,
                parent_area_id: str | None = None) -> None:
    """Insert an area into the rollup spine (idempotent on area_id)."""
    conn.execute(
        "INSERT OR IGNORE INTO areas (area_id, kind, name, parent_area_id, created_utc)"
        " VALUES (?, ?, ?, ?, ?)",
        (area_id, kind, name, parent_area_id, _utcnow()),
    )


def get_state(conn: sqlite3.Connection, area_id: str) -> str:
    """Return an area's current state, defaulting to ``'locked'`` if unset (AREA-4)."""
    row = conn.execute(
        "SELECT state FROM area_state WHERE area_id = ?", (area_id,)
    ).fetchone()
    return row[0] if row else "locked"


def child_areas(conn: sqlite3.Connection, parent_area_id: str) -> list[str]:
    """Direct children of an area in the spine."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT area_id FROM areas WHERE parent_area_id = ? ORDER BY area_id",
            (parent_area_id,),
        )
    ]


def descendants(conn: sqlite3.Connection, area_id: str) -> list[str]:
    """All transitive descendants (for a county/state rollup). Excludes ``area_id``.

    Pure spine walk; cycle-safe via a visited set (the spine is a tree, but the
    guard keeps a mis-seeded parent pointer from looping forever).
    """
    out: list[str] = []
    seen = {area_id}
    frontier = child_areas(conn, area_id)
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        out.append(node)
        frontier.extend(child_areas(conn, node))
    return sorted(out)


def ancestors(conn: sqlite3.Connection, area_id: str) -> list[str]:
    """Rollup walk town -> county -> state (immediate-parent-first)."""
    out: list[str] = []
    seen = {area_id}
    cur = area_id
    while True:
        row = conn.execute(
            "SELECT parent_area_id FROM areas WHERE area_id = ?", (cur,)
        ).fetchone()
        if not row or row[0] is None or row[0] in seen:
            break
        parent = row[0]
        out.append(parent)
        seen.add(parent)
        cur = parent
    return out


def transition(conn: sqlite3.Connection, *, area_id: str, to_state: str,
               owner_decision_ref: str, rule: str | None = None) -> int:
    """The ONLY writer of ``area_state`` (AREA-4/5, AM-1).

    Refuses — with zero writes — when:
      * ``owner_decision_ref`` is missing/blank (define-not-activate);
      * ``to_state`` is not a known state;
      * ``(from_state, to_state)`` is not a legal edge.

    On success: upserts ``area_state`` and appends exactly one
    ``area_transitions`` audit row, then commits. Returns the new
    ``transition_id``.
    """
    if not owner_decision_ref or not str(owner_decision_ref).strip():
        raise OwnerlessTransition(
            "transition requires an owner_decision_ref (AREA-5); refused"
        )
    if to_state not in STATES:
        raise IllegalTransition(f"unknown target state {to_state!r}")

    from_state = get_state(conn, area_id)
    if from_state == to_state:
        raise IllegalTransition(f"no-op transition {from_state!r} -> {to_state!r}")
    if (from_state, to_state) not in LEGAL_TRANSITIONS:
        raise IllegalTransition(
            f"illegal transition {from_state!r} -> {to_state!r}"
        )

    now = _utcnow()
    conn.execute(
        "INSERT INTO area_state (area_id, state, updated_utc) VALUES (?, ?, ?)"
        " ON CONFLICT(area_id) DO UPDATE SET state = excluded.state,"
        " updated_utc = excluded.updated_utc",
        (area_id, to_state, now),
    )
    cur = conn.execute(
        "INSERT INTO area_transitions"
        " (area_id, from_state, to_state, owner_decision_ref, rule_evaluated, at_utc)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (area_id, from_state, to_state, owner_decision_ref, rule, now),
    )
    conn.commit()
    return int(cur.lastrowid)
