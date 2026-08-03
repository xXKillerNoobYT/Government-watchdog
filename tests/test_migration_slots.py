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


# --- GOV-1672 (C1): documentation must not advertise a stale slot ------------
#
# The file-level guards above stop two MIGRATIONS colliding. This stops the
# DOCUMENT that sends someone to a slot from being wrong — the upstream cause.
# Found by C1 on the accounts plan, whose header read "Next migration slot: 0025"
# long after 0025 merged. PRs #199 and #132 both took 0032 the same week.

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[1] / "Docs"

# Both patterns are anchored to the START OF A LINE with optional bold markers,
# i.e. the header form only. That is deliberate: the first version matched
# anywhere, and immediately fired on this very PR's correction note, which
# QUOTES the old header line while explaining that it was wrong. Same trap as
# GOV-1665, where an append-only sweep tripped on the docstring promising there
# was no update path. **Match on syntax and position, not on vocabulary** —
# any guard that greps for a phrase will eventually hit the prose describing it.
#: `**Next migration slot:** NNNN` — a forward claim; the slot must still be FREE.
_NEXT_CLAIM = re.compile(r"^\**Next migration slot:\**\s*(\d{4})", re.I | re.M)
#: `**Migration slot (consumed):** NNNN` — historical; it must EXIST.
_CONSUMED_CLAIM = re.compile(r"^\**Migration slot \(consumed\):\**\s*(\d{4})",
                             re.I | re.M)


def _doc_claims(pattern):
    """Every (doc, slot) pair matching `pattern` across Docs/*.md."""
    found = []
    for doc in sorted(DOCS.rglob("*.md")):
        for slot in pattern.findall(doc.read_text(encoding="utf-8")):
            found.append((doc.name, slot))
    return found


def test_no_doc_advertises_an_already_consumed_slot():
    """A doc saying "take slot NNNN" must name one that is genuinely free.

    This is the upstream half of the collision guard. `test_migration_slots.py`'s
    other tests fail the branch that lands a duplicate; this fails the *document*
    that would send two people there in the first place.
    """
    existing = {f.name[:4] for f in _migration_files()}

    stale = [(doc, slot) for doc, slot in _doc_claims(_NEXT_CLAIM)
             if slot in existing]

    assert not stale, (
        "these docs point at a slot that is already taken: "
        + ", ".join(f"{doc} -> {slot}" for doc, slot in stale))


def test_consumed_slot_claims_name_a_migration_that_exists():
    """The historical form must be true too, or it is just a different lie.

    Also keeps this pair NON-VACUOUS: a guard that scans for a pattern and finds
    nothing passes for the wrong reason. At least one claim must be present, so
    deleting every claim to make the suite green fails instead.
    """
    existing = {f.name[:4] for f in _migration_files()}
    claims = _doc_claims(_CONSUMED_CLAIM)

    assert claims, ("no `Migration slot (consumed): NNNN` claim found in Docs/ — "
                    "if the convention was dropped, drop these tests with it "
                    "rather than leaving them to pass vacuously")

    missing = [(doc, slot) for doc, slot in claims if slot not in existing]
    assert not missing, (
        "these docs record a consumed slot with no migration behind it: "
        + ", ".join(f"{doc} -> {slot}" for doc, slot in missing))
