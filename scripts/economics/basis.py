"""Basis vocabulary + report lint (LEDGER-2026 §0/§2, LED-5, AM-7).

Every reported value in the ledger carries a *basis label* declaring where the
number came from. The vocabulary is closed and fail-closed:

* ``MEASURED``  — a figure summed directly from a run ledger (provider/compute
  units, timestamps). No assumption introduced.
* ``ASSUMED``   — an estimate or a declared assumption (``estimated_cost_usd``,
  SLO initial targets, the F7 document-share weight).
* ``DERIVED``   — a formula output combining other cells (F3/F4/F5); it carries
  the *worst* basis of its inputs as taint.
* ``OWNER-SET`` — a figure that exists only because an owner set it
  (``safety_factor``, ``fixed_total_units``, ``per_decision_units``, funding
  entries). ``OWNER-SET (unset)`` is emitted when the owner has *not* set it — a
  labeled hole, never a fabricated zero.

A *value cell* is any dict carrying a ``"value"`` key. :func:`lint_report` walks
the whole pack and returns a list of violations — any value cell missing a basis
from the accepted set. AM-7 fails the build if that list is non-empty.

Pure module: stdlib only, no I/O, no DB. Import-safe from tests and report.py.
"""

from __future__ import annotations

from typing import Any

MEASURED = "MEASURED"
ASSUMED = "ASSUMED"
DERIVED = "DERIVED"
OWNER_SET = "OWNER-SET"

# The four canonical bases.
VALID_BASES = frozenset({MEASURED, ASSUMED, DERIVED, OWNER_SET})

# Labeled-hole bases: still a valid, honest label (LEDGER-2026 §7). A value cell
# may carry one of these instead of a fabricated number.
OWNER_SET_UNSET = "OWNER-SET (unset)"
NOT_INSTRUMENTED = "n/a (not yet instrumented)"

# The full set a value cell may carry to pass lint.
ACCEPTED_BASIS_LABELS = VALID_BASES | {OWNER_SET_UNSET, NOT_INSTRUMENTED}

# Trust ranking for DERIVED taint: a formula output inherits the *worst*
# (least-trustworthy) basis among its inputs. Lower rank = more trustworthy.
_TRUST_RANK = {
    MEASURED: 0,
    OWNER_SET: 1,
    OWNER_SET_UNSET: 1,
    ASSUMED: 2,
    NOT_INSTRUMENTED: 2,
    DERIVED: 3,
}


def worst_basis(*bases: str) -> str:
    """Return the least-trustworthy basis among ``bases`` (DERIVED taint carry).

    Used by combining formulas (F3/F4/F5): the label the *output* reports is
    always ``DERIVED``, but the worst input basis is threaded through so a
    reviewer can see an ASSUMED weight tainted the total. Unknown labels rank as
    worst-possible (fail-closed).
    """
    if not bases:
        return MEASURED
    return max(bases, key=lambda b: _TRUST_RANK.get(b, 99))


def cell(value: Any, basis: str, *, unit: str | None = None,
         formula_id: str | None = None) -> dict:
    """Build a value cell: a dict guaranteed to carry a ``value`` + ``basis``.

    Every number that enters a report pack should go through here so it cannot
    reach the lint step unlabeled.
    """
    out: dict[str, Any] = {"value": value, "basis": basis}
    if unit is not None:
        out["unit"] = unit
    if formula_id is not None:
        out["formula_id"] = formula_id
    return out


def _walk_cells(node: Any, path: str = "") -> list[tuple[str, dict]]:
    """Yield ``(path, cell)`` for every dict in ``node`` carrying a ``value`` key."""
    found: list[tuple[str, dict]] = []
    if isinstance(node, dict):
        if "value" in node:
            found.append((path or "<root>", node))
        for key, child in node.items():
            found.extend(_walk_cells(child, f"{path}.{key}" if path else str(key)))
    elif isinstance(node, (list, tuple)):
        for i, child in enumerate(node):
            found.extend(_walk_cells(child, f"{path}[{i}]"))
    return found


def lint_report(pack: Any) -> list[str]:
    """Return a list of lint violations (LED-5 / AM-7).

    A violation is any value cell whose ``basis`` is missing or not in
    :data:`ACCEPTED_BASIS_LABELS`. An empty list means the pack is fully
    basis-labeled and passes AM-7.
    """
    violations: list[str] = []
    for path, c in _walk_cells(pack):
        b = c.get("basis")
        if b is None:
            violations.append(f"{path}: value without a basis label")
        elif b not in ACCEPTED_BASIS_LABELS:
            violations.append(f"{path}: unrecognized basis {b!r}")
    return violations


def assert_labeled(pack: Any) -> None:
    """Raise ``ValueError`` if any value cell is unlabeled (RED guard for AM-7)."""
    violations = lint_report(pack)
    if violations:
        raise ValueError(
            "report failed basis lint (LED-5/AM-7): " + "; ".join(violations)
        )
