"""GOV-1651: the migration slot convention is enforced, not just documented.

The house rule is "next free slot only" — a new migration takes the next
unused 4-digit prefix, and a collision means renumber and update every
reference. Until now that rule lived only in prose, and nothing rejected two
files sharing a slot.

Why a collision is silent rather than loud: ``scripts/db.py`` keys the
``schema_migrations`` ledger on ``path.stem`` — the *whole* filename, not the
numeric prefix (``db.py`` ``apply_migrations``). So ``0032_alpha.sql`` and
``0032_beta.sql`` are two distinct ledger rows; the runner applies both, in
``sorted()`` order over full filenames, which is alphabetical and bears no
relation to the order the two branches merged. Two branches that each took
"the next free slot" against the same base therefore produce a schema whose
final shape depends on how the second filename happens to sort.

``tests/test_deploy_frozen_surface.py`` catches this only incidentally: its
allowlist compares added filenames against ``origin/main``, so it fires only
when the losing branch is stale at CI time, and it reports the collision as a
confusing frozen-surface diff rather than as a slot conflict.

These checks are deliberately local — they read the directory and nothing
else. Unlike the frozen-surface tests they carry no ``origin/main`` skipif, so
they still run in a shallow clone, in a worktree, and offline.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "Database" / "migrations"

# ``0031_supplied_file_provenance_note.sql`` — four digits, underscore,
# lowercase snake_case slug, ``.sql``. Every migration 0001-0031 conforms.
MIGRATION_NAME_RE = re.compile(r"^(?P<slot>\d{4})_(?P<slug>[a-z0-9_]+)\.sql$")


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def test_migration_filenames_follow_the_slot_convention():
    """Every migration parses as NNNN_slug.sql.

    A file the convention cannot parse has no slot, so the collision and
    contiguity checks below would silently skip it.
    """
    files = _migration_files()
    assert files, f"no migrations found in {MIGRATIONS_DIR}"

    malformed = [p.name for p in files if not MIGRATION_NAME_RE.match(p.name)]
    assert not malformed, (
        "migration filenames must be NNNN_lower_snake_case.sql; "
        f"malformed: {sorted(malformed)}"
    )


def test_no_two_migrations_share_a_slot():
    """The house rule, enforced: one migration per 4-digit slot.

    Fails the branch that would introduce the collision, naming both files,
    rather than letting ``apply_migrations`` run them in filename order.
    """
    by_slot: dict[str, list[str]] = defaultdict(list)
    for path in _migration_files():
        match = MIGRATION_NAME_RE.match(path.name)
        if match:
            by_slot[match["slot"]].append(path.name)

    collisions = {slot: sorted(names) for slot, names in by_slot.items() if len(names) > 1}
    assert not collisions, (
        "migration slot collision — two files claim the same slot. Renumber the "
        "later one to the next free slot, update every reference to it, and "
        "re-derive MIGRATION_ALLOWLIST in tests/test_deploy_frozen_surface.py. "
        f"Collisions: {collisions}"
    )


def test_migration_slots_are_contiguous_from_0001():
    """No gaps: slots run 0001..N with nothing skipped.

    A skipped slot makes "next free slot" ambiguous — the next author has to
    guess whether the hole is reserved or abandoned, which is how two branches
    end up choosing differently and colliding.
    """
    slots = sorted(
        int(match["slot"])
        for path in _migration_files()
        if (match := MIGRATION_NAME_RE.match(path.name))
    )
    assert slots[0] == 1, f"migrations must start at slot 0001, found {slots[0]:04d}"

    missing = [n for n in range(slots[0], slots[-1] + 1) if n not in set(slots)]
    assert not missing, (
        "migration slots must be contiguous; missing "
        f"{[f'{n:04d}' for n in missing]} between 0001 and {slots[-1]:04d}"
    )


def test_next_free_slot_is_discoverable():
    """The rule an author actually follows is computable, not eyeballed.

    Pins the arithmetic so "next free slot" has one answer that a human and a
    script agree on.
    """
    slots = [
        int(match["slot"])
        for path in _migration_files()
        if (match := MIGRATION_NAME_RE.match(path.name))
    ]
    next_free = max(slots) + 1
    assert next_free == len(slots) + 1, (
        "next free slot disagrees with the migration count, which means the "
        "slots are not contiguous — see test_migration_slots_are_contiguous_from_0001"
    )
    assert not (MIGRATIONS_DIR / f"{next_free:04d}").exists()
