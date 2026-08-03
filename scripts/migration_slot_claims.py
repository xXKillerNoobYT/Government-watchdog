"""GOV-1696: which migration slot is *actually* free, counting sibling PRs.

`Docs/data-model-contract.md` §3 step 1 tells an author to do two things before
picking a slot:

    Read `Database/migrations/` on current `main` and take the next free slot.
    Check open PRs too — `main` alone cannot show you a sibling branch's claim.

The first half is automated three times over (`tests/test_migration_slots.py`).
**The second half is a manual instruction and nothing checks it**, which is not a
theoretical gap: PRs #199 and #132 each took `0032` against the same base in the
same week, and as of this writing that single collision blocks **six** work items
(#145, #153, #160, #217's two indexes, part of #130) with #154 transitively behind
#145. This module is that second half, written as code instead of a sentence.

**Why no test can do this job.** `test_no_two_migrations_share_a_slot` reads the
migrations directory, so it sees exactly one branch: its own. Both colliding PRs
pass it individually and it only fires *after* the second one lands. Per-PR CI is
structurally blind here — every PR is tested against `main`, never against its
siblings — so the check has to query the forge, which means a script rather than a
test. The two are complements, not duplicates:

| guard | sees | fires |
|---|---|---|
| `tests/test_migration_slots.py` | the working tree | after a collision lands |
| this module | every open PR | before a slot is chosen |

**Fail-closed, and this is the part that matters.** If the probe cannot enumerate
every open PR — `gh` missing, unauthenticated, rate-limited, one `pr diff` failing
— it raises `SlotProbeUnavailable` and the CLI exits 2. It never degrades to "no
collisions found", because a tool that reports clean when it could not look is
worse than no tool: it converts an unknown into a false assurance. A *partial*
answer is refused for the same reason — the one PR that failed to fetch is exactly
where the collision would be.

Usage:

    python scripts/migration_slot_claims.py            # report; exit 1 on a finding
    python scripts/migration_slot_claims.py --next     # print the next genuinely free slot

Exit codes: 0 clean · 1 collision or unparseable migration path · 2 probe unavailable.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "Database" / "migrations"

#: The directory a claim must live in. A `0032_x.sql` added anywhere else is not a
#: migration and must not be counted as one.
MIGRATIONS_PREFIX = "Database/migrations/"

#: Kept byte-identical to `tests/test_migration_slots.py`'s copy on purpose. The
#: test must not import this module — it is offline by design and this one shells
#: out to `gh` — so the convention is stated twice and pinned by
#: `test_the_slot_regex_matches_the_offline_guards_copy`.
MIGRATION_NAME_RE = re.compile(r"^(?P<slot>\d{4})_(?P<slug>[a-z0-9_]+)\.sql$")


class SlotProbeUnavailable(RuntimeError):
    """The set of open PRs could not be established. Never treat as "clean"."""


class AmbiguousSlotSequence(RuntimeError):
    """Slots have a hole, so "the next free one" has more than one answer."""


@dataclass(frozen=True)
class SlotClaims:
    """What every open PR claims, plus what it was not possible to parse."""

    #: slot -> sorted PR numbers adding a migration in that slot
    by_slot: dict[str, list[int]] = field(default_factory=dict)
    #: (pr_number, path) for files under the migrations dir that do not parse.
    #: Surfaced rather than dropped — a skipped file is an unseen claim.
    unparseable: list[tuple[int, str]] = field(default_factory=list)


def slot_of(path: str) -> str | None:
    """The 4-digit slot a repo-relative path claims, or None if it claims none.

    Only files directly under `Database/migrations/` count. A path elsewhere
    returns None (not a claim); a path *inside* that does not parse returns None
    too, and callers must treat that case as unparseable rather than absent —
    which is why `claims_from_pr_files` checks the prefix itself.
    """
    if not path.startswith(MIGRATIONS_PREFIX):
        return None
    match = MIGRATION_NAME_RE.match(path[len(MIGRATIONS_PREFIX):])
    return match["slot"] if match else None


def claims_from_pr_files(pr_files: Mapping[int, Iterable[str]]) -> SlotClaims:
    """Map open PRs to the migration slots they claim.

    Pure: takes an already-fetched {pr_number: [changed paths]} mapping so the
    whole collision analysis is testable with no network and no `gh`.
    """
    by_slot: dict[str, list[int]] = defaultdict(list)
    unparseable: list[tuple[int, str]] = []

    for pr, paths in pr_files.items():
        for path in paths:
            if not path.startswith(MIGRATIONS_PREFIX):
                continue
            slot = slot_of(path)
            if slot is None:
                unparseable.append((pr, path))
            elif pr not in by_slot[slot]:
                by_slot[slot].append(pr)

    return SlotClaims(
        by_slot={slot: sorted(prs) for slot, prs in sorted(by_slot.items())},
        unparseable=sorted(unparseable),
    )


def existing_slots(migrations_dir: Path | None = None) -> set[str]:
    """Slots already taken on the checked-out tree."""
    directory = MIGRATIONS_DIR if migrations_dir is None else migrations_dir
    return {
        match["slot"]
        for path in sorted(directory.glob("*.sql"))
        if (match := MIGRATION_NAME_RE.match(path.name))
    }


def find_collisions(taken: set[str], claims: SlotClaims) -> dict[str, list[int]]:
    """Slots claimed by two or more open PRs, or by a PR when already on disk.

    Both shapes are the same defect from an author's seat — the slot they were
    about to take is not free — so they are reported together, keyed by slot.
    A slot already on disk is recorded with the claiming PRs; the disk itself
    has no PR number, hence the list holds only the PRs.
    """
    return {
        slot: prs
        for slot, prs in claims.by_slot.items()
        if len(prs) > 1 or slot in taken
    }


def next_free_slot(taken: set[str], claims: SlotClaims) -> int:
    """The slot an author should actually take, counting in-flight PRs.

    Refuses rather than guesses when the union of on-disk and claimed slots has a
    hole: with a gap, "next free" means either the hole or the end, and two
    authors choosing differently is precisely how a collision is manufactured.
    """
    numbers = {int(s) for s in taken} | {int(s) for s in claims.by_slot}
    if not numbers:
        return 1

    low, high = min(numbers), max(numbers)
    missing = [n for n in range(low, high + 1) if n not in numbers]
    if missing:
        raise AmbiguousSlotSequence(
            "slots have a hole, so the next free one is ambiguous; missing "
            f"{[f'{n:04d}' for n in missing]} between {low:04d} and {high:04d}"
        )
    return high + 1


# --- the I/O half: everything below this line touches the network -------------


def _gh(args: list[str]) -> str:
    """Run `gh`, converting every failure mode into SlotProbeUnavailable."""
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=True,
        )
    except FileNotFoundError as exc:  # gh not installed
        raise SlotProbeUnavailable("`gh` is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise SlotProbeUnavailable(
            f"`gh {' '.join(args)}` failed ({exc.returncode}): "
            f"{(exc.stderr or '').strip()}"
        ) from exc
    return proc.stdout


def fetch_open_pr_files(repo: str | None = None) -> dict[int, list[str]]:
    """{pr_number: changed paths} for every open PR. All or nothing.

    If any single PR's diff cannot be read the whole call raises, because a
    partial map silently under-reports collisions — and the PR that failed to
    fetch is exactly the one whose claim is unknown.
    """
    scope = ["--repo", repo] if repo else []
    raw = _gh(["pr", "list", "--state", "open", "--limit", "200",
               "--json", "number", *scope])
    try:
        numbers = [int(item["number"]) for item in json.loads(raw)]
    except (ValueError, KeyError, TypeError) as exc:
        raise SlotProbeUnavailable(f"could not parse `gh pr list` output: {exc}") from exc

    return {
        pr: [line for line in _gh(["pr", "diff", str(pr), "--name-only", *scope])
             .splitlines() if line.strip()]
        for pr in numbers
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", help="owner/name; defaults to the current remote")
    parser.add_argument("--next", action="store_true",
                        help="print the next genuinely free slot and exit")
    args = parser.parse_args(argv)

    try:
        pr_files = fetch_open_pr_files(args.repo)
    except SlotProbeUnavailable as exc:
        print(f"SLOT PROBE UNAVAILABLE: {exc}", file=sys.stderr)
        print("Refusing to report 'no collisions' without having looked.",
              file=sys.stderr)
        return 2

    taken = existing_slots()
    claims = claims_from_pr_files(pr_files)

    if args.next:
        try:
            print(f"{next_free_slot(taken, claims):04d}")
        except AmbiguousSlotSequence as exc:
            print(f"AMBIGUOUS: {exc}", file=sys.stderr)
            return 1
        return 0

    collisions = find_collisions(taken, claims)
    for slot, prs in collisions.items():
        where = "already on disk and " if slot in taken else ""
        print(f"COLLISION slot {slot}: {where}claimed by "
              f"{', '.join(f'#{p}' for p in prs)}")
    for pr, path in claims.unparseable:
        print(f"UNPARSEABLE #{pr}: {path} is under {MIGRATIONS_PREFIX} but does "
              "not match NNNN_lower_snake_case.sql")

    if collisions or claims.unparseable:
        print("\nRenumber the later claim to the next free slot, update every "
              "reference to it, and re-derive MIGRATION_ALLOWLIST in "
              "tests/test_deploy_frozen_surface.py.")
        return 1

    print(f"no slot collisions across {len(pr_files)} open PR(s); "
          f"next free slot is {next_free_slot(taken, claims):04d}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
