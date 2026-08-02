"""GOV-1706 (C1): an area bound to the whole codebase is bound to nothing.

**This defect has now landed three separate times**, and each time it survived for
dozens of iterations because nothing looked wrong:

| area | bound to | fixed |
|---|---|---|
| `ingest-provenance` | `scripts/` | iteration 44 |
| `read-api` | `scripts/` | iteration 60 (#240) |
| `ai-boundary` | `scripts/` | iteration 73 (#244) |

`paths: [scripts/]` is syntactically fine, reads as deliberate, and passes every
existing check. But every check that scopes by path — C1, C1b, C4, C5, C7b, C8,
C9 — then ranges over all 160 modules, so the area "owns" the entire backend and
its reviews mean nothing in particular. **A binding that selects everything
selects nothing**, and it fails silently: the checks still run, still pass, and
still report green.

### Why the threshold is 25% and not "no directories"

Measured 2026-08-01 before writing this guard, because a guard that is red on
arrival gets suppressed rather than obeyed. Directory entries are **normal and
correct** — 8 areas use them:

| entry | .py files | share of `scripts/` |
|---|---|---|
| `scripts/mcp_service/` | 21 | **13.1%**  <- largest legitimate |
| `scripts/beta/` | 15 | 9.4% |
| `scripts/economics/` | 12 | 7.5% |
| `scripts/accounts/` | 6 | 3.8% |
| ... | | all below 10% |
| **`scripts/` (the defect)** | **160** | **100%** |

So "ban directories" would fire on seven correct bindings. The real signal is
*breadth*: the gap between the largest legitimate entry (13.1%) and the defect
(100%) is enormous, and 25% sits in it with 12 points of headroom — room for
`mcp_service/` to nearly double before anyone has to think about this again.

**Skips when the heartbeat is absent.** `docs/auto-go-*.md` is in
`.git/info/exclude` (loop state, and this repo is PUBLIC), so the file exists only
on machines running the loop. That is also exactly where the defect is introduced,
so the guard fires where it matters; it cannot fire in a fresh clone, and pretending
otherwise would be worse than saying so.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEARTBEAT = ROOT / "docs" / "auto-go-heartbeat.md"
SOURCE_ROOT = ROOT / "scripts"

#: An entry may not cover more of the codebase than this. See the docstring for
#: the measurement that chose it; re-measure before changing it.
MAX_SHARE_OF_CODEBASE = 0.25

pytestmark = pytest.mark.skipif(
    not HEARTBEAT.exists(),
    reason="docs/auto-go-heartbeat.md is loop state, gitignored; absent in a fresh clone",
)

_AREA_PATHS = re.compile(
    r"^  ([a-z-]+):\n(?:(?!^  [a-z-]+:).)*?    paths: \[((?:[^\]])*)\]",
    re.M | re.S,
)


def _bindings() -> list[tuple[str, str]]:
    """(area, entry) for every path entry, multi-line lists included.

    The multi-line handling is load-bearing and was got wrong first: a naive
    `paths: \\[(.*?)\\]` stops at the first newline, so a wrapped list reports only
    its first entry and every later one goes unchecked — a guard that inspects
    one seventh of its subject and reports clean.
    """
    text = HEARTBEAT.read_text(encoding="utf-8")
    out = []
    for area, blob in _AREA_PATHS.findall(text):
        for entry in re.split(r",\s*", blob.replace("\n", " ")):
            entry = entry.strip()
            if entry:
                out.append((area, entry))
    return out


def _covered(entry: str) -> int:
    p = ROOT / entry
    if entry.endswith("/"):
        return len(list(p.rglob("*.py"))) if p.is_dir() else 0
    return 1 if p.exists() else 0


def test_the_binding_parser_sees_multi_line_lists():
    """Non-vacuity. Without this, a broken regex makes every test below pass."""
    bindings = _bindings()
    assert bindings, "no area bindings parsed at all — the regex stopped matching"
    per_area: dict[str, int] = {}
    for area, _ in bindings:
        per_area[area] = per_area.get(area, 0) + 1
    widest = max(per_area.values())
    assert widest >= 5, (
        f"no area parsed with 5+ path entries (widest was {widest}). At least one "
        "area binds a long wrapped list, so this means the parser is stopping at "
        "the first line and the breadth check below is inspecting a fraction of "
        "what it claims to.")


def test_no_area_is_bound_to_a_share_of_the_codebase_that_means_nothing():
    total = len(list(SOURCE_ROOT.rglob("*.py")))
    assert total > 50, f"only {total} modules found — SOURCE_ROOT looks wrong"

    offenders = []
    for area, entry in _bindings():
        share = _covered(entry) / total
        if share >= MAX_SHARE_OF_CODEBASE:
            offenders.append(f"{area} -> {entry} ({share:.0%} of {total} modules)")

    assert not offenders, (
        "these area bindings select so much of the codebase that they scope "
        f"nothing: {offenders}. Every path-scoped check (C1, C1b, C4, C5, C7b, C8, "
        "C9) would range over the whole backend and report green regardless. Derive "
        "the area's real ownership by CONTRACT COVERAGE — the modules its bound "
        "contracts name, minus what another area already owns — not by filename "
        "prefix. Precedent: ingest-provenance (iter 44), read-api (#240), "
        "ai-boundary (#244).")


def _parked_areas() -> set[str]:
    """Areas waiting on a decision, from the heartbeat's `parked_areas:` list."""
    text = HEARTBEAT.read_text(encoding="utf-8")
    return set(re.findall(r"^  - name: ([a-z-]+)", text, re.M))


def test_every_bound_path_of_an_ACTIVE_area_exists():
    """A binding that points at nothing is the quieter half of the same failure.

    **Parked areas are exempt, and that exemption is not a convenience.** This
    guard was red on arrival with exactly one violation — `local-first-tooling`
    binding `tools/local-first/`, a Node substrate that *arrives with PR #171*,
    the very PR that area is parked on. A forward-looking binding on a parked
    area is correct: it is what lets the area resume untouched when the decision
    lands.

    The failure this still catches is the one that matters — an **active** area
    scoped to a path that is not there, whose checks therefore cannot fail and
    are not evidence (the bind-or-retire rule).
    """
    parked = _parked_areas()
    assert parked, "no parked areas parsed — the exemption below would be vacuous"
    missing = [
        f"{a} -> {e}" for a, e in _bindings()
        if a not in parked and not (ROOT / e).exists()
    ]
    assert not missing, (
        f"ACTIVE area bindings point at paths that do not exist: {missing}. A "
        "check scoped to a missing path can neither pass nor fail, so it is not "
        "evidence — bind it to the real path or retire it. (Parked areas are "
        "exempt: their bindings may legitimately await an unmerged PR.)")


# --- GOV-1713 (C5): the `tests:` half of the binding was missing everywhere ----
#
# `area_bindings` has four keys — label / paths / contracts / tests — and C4
# ("coverage >= 90% for this area's files") and C5 ("green on this area's test
# suite") both scope on `tests:`. Measured 2026-08-02: **8 of 11 areas had no
# `tests:` key at all**, so both checks fell back to the whole suite.
#
# A whole-suite green says nothing about whether THIS area is covered — the same
# "selects everything, selects nothing" defect the breadth guard above catches for
# `paths:`, one dimension over. And it is not hypothetical: `ingest-provenance`
# GRADUATED with C4 and C5 marked done and no `tests:` binding.
#
# Guarded as a RATCHET, not a requirement. Requiring `tests:` on every area would
# be red on arrival against seven areas whose bindings are their own C1 work; a
# ratchet means the number can only go down.

#: Areas still missing a `tests:` binding. **This number may only DECREASE.**
#: Lower it when you bind an area; never raise it to make a new area pass.
#:
#: CORRECTED 7 -> 6 on 2026-08-02 (GOV-1719). It was set to 7 when ai-boundary was
#: bound, but the bindings block declares **10** areas and 4 of them carry `tests:`,
#: so the true figure was 6 from that moment. The off-by-one survived because
#: nothing pinned the DENOMINATOR: a ratchet over "areas without X" silently
#: absorbs any change in how many areas the parser sees. `local-first-tooling`,
#: for instance, is a parked area with NO `area_bindings:` entry at all — whether
#: a parser counts it changes this number without anyone touching a binding.
#: `_DECLARED_AREA_COUNT` below now pins the denominator so that shift fails loudly
#: instead of moving the ratchet.
_AREAS_WITHOUT_TESTS_BINDING = 6

#: How many areas the bindings block declares. Pinned so a parser or file change
#: cannot quietly redefine every per-area ratchet in this file.
_DECLARED_AREA_COUNT = 10

_AREA_TESTS = re.compile(
    r"^  ([a-z-]+):\n(?:(?!^  [a-z-]+:$).)*?    tests: \[((?:[^\]])*)\]",
    re.M | re.S,
)


def _test_bindings() -> dict[str, list[str]]:
    text = HEARTBEAT.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for area, blob in _AREA_TESTS.findall(text):
        out[area] = [e.strip() for e in re.split(r",\s*", blob.replace("\n", " ")) if e.strip()]
    return out


def _declared_areas() -> set[str]:
    text = HEARTBEAT.read_text(encoding="utf-8")
    start = text.index("area_bindings:")
    end = text.index("\nareas:", start) if "\nareas:" in text[start:] else len(text)
    return set(re.findall(r"^  ([a-z-]+):$", text[start:end], re.M))


def test_the_declared_area_count_is_what_every_ratchet_assumes():
    """The denominator, pinned.

    Ratchets in this file count areas *lacking* something. That shape is blind to
    the total moving: if the parser stops seeing an area, "areas without a tests
    binding" falls by one and reads as progress. This is the same
    two-lists-agreeing trap CLAUDE.md records for the frozen-surface list — the
    fix is to pin the thing both sides assume rather than compare them.
    """
    areas = _declared_areas()
    assert len(areas) == _DECLARED_AREA_COUNT, (
        f"the bindings block declares {len(areas)} areas ({sorted(areas)}), not "
        f"{_DECLARED_AREA_COUNT}. Every per-area ratchet here is measured against "
        "that total, so a change in it silently re-baselines them. If an area was "
        "genuinely added or removed, update _DECLARED_AREA_COUNT **and** re-derive "
        "_AREAS_WITHOUT_TESTS_BINDING in the same commit.")


def test_areas_missing_a_tests_binding_only_ever_decreases():
    """A ratchet. C4/C5 silently mean "the whole suite" for every area listed here."""
    areas = _declared_areas()
    assert areas, "no areas parsed from area_bindings"
    bound = set(_test_bindings())
    missing = sorted(areas - bound)

    assert len(missing) <= _AREAS_WITHOUT_TESTS_BINDING, (
        f"{len(missing)} areas now lack a `tests:` binding ({missing}), up from "
        f"{_AREAS_WITHOUT_TESTS_BINDING}. C4 and C5 scope on that key, so an "
        "unbound area runs them against the entire suite and reports green "
        "regardless of its own coverage. Bind the area, or if this count is meant "
        "to drop, lower _AREAS_WITHOUT_TESTS_BINDING to match.")
    assert len(missing) == _AREAS_WITHOUT_TESTS_BINDING, (
        f"only {len(missing)} areas lack a `tests:` binding, but the ratchet still "
        f"says {_AREAS_WITHOUT_TESTS_BINDING}. Someone bound an area without "
        "lowering the number — tighten it so the next regression is caught.")


def test_every_bound_test_file_exists_and_is_a_test():
    """A `tests:` entry pointing at nothing makes C5 pass by selecting zero tests.

    `pytest` exits 0 with "no tests ran" for a path that matches nothing in some
    invocations, so a typo'd binding is the quietest possible way to turn C5 into
    a no-op.
    """
    bindings = _test_bindings()
    assert bindings, "no `tests:` bindings parsed at all — the regex stopped matching"
    problems = []
    for area, entries in sorted(bindings.items()):
        for entry in entries:
            path = ROOT / entry
            if not path.exists():
                problems.append(f"{area} -> {entry} (does not exist)")
            elif not path.name.startswith("test_"):
                problems.append(f"{area} -> {entry} (not a test file)")
    assert not problems, (
        f"`tests:` bindings that cannot select a real test: {problems}. C5 would "
        "report green having run nothing.")


# --- GOV-1719 (C1, governance): the THIRD variant — what no area owns at all ---
#
# The breadth guard above catches an area bound to too MUCH. The existence guard
# catches an area bound to NOTHING REAL. Neither asks the question that turns out
# to matter most: **is every module owned by some area?**
#
# Measured 2026-08-02, on entering `governance`: **60 of 160 modules in `scripts/`
# are owned by NO area — 38% of the backend.** 56 of those 60 are referenced by the
# test suite, so this is live, exercised code, not scaffolding.
#
# What that costs: every path-scoped check (C1, C1b, C4, C5, C7b, C8, C9) ranges
# over `paths:`. A module no area binds is never planned against a contract, never
# security-reviewed, never performance-reviewed, and never counted in an area's
# coverage — and the rotation still reports areas GRADUATING. The loop's own
# accounting says "2 of 11 areas production-ready" while more than a third of the
# tree has never been looked at by any check.
#
# It is a RATCHET rather than a requirement, for the same reason as the `tests:`
# one below-but-above: demanding full coverage today would be red on arrival
# against 60 modules and get suppressed rather than obeyed. The number may only
# fall.

#: Modules under `scripts/` that no area's `paths:` binding covers, as of
#: 2026-08-02. **This number may only DECREASE.** Lower it when you widen an
#: area or add one; never raise it to make a new module pass.
_UNOWNED_MODULES = 60


def _owned_by() -> dict[str, str | None]:
    """{module path: owning area or None} for every module under `scripts/`."""
    entries = _bindings()
    out: dict[str, str | None] = {}
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(ROOT))
        owner = None
        for area, entry in entries:
            if (entry.endswith("/") and rel.startswith(entry)) or entry == rel:
                owner = area
                break
        out[rel] = owner
    return out


def test_the_ownership_map_actually_resolves_owners():
    """Non-vacuity. If nothing resolved, "unowned" would be every module and the
    ratchet below would look catastrophic for the wrong reason — or, if the
    matching were inverted, nothing would ever be unowned and the guard would be
    decoration."""
    owned = _owned_by()
    assert len(owned) > 50, f"only {len(owned)} modules found — SOURCE_ROOT looks wrong"
    resolved = sum(1 for v in owned.values() if v)
    assert resolved > 0, (
        "no module resolved to an area at all — the prefix match is broken, so "
        "the count below would report the whole tree as unowned")
    areas = {v for v in owned.values() if v}
    assert len(areas) >= 3, (
        f"only {sorted(areas)} claimed any module. A single area matching "
        "everything is the 'selects everything, selects nothing' defect one "
        "level up, and it would make the ratchet meaningless.")


def test_modules_owned_by_no_area_only_ever_decreases():
    """A module no area binds is invisible to every path-scoped check."""
    owned = _owned_by()
    unowned = sorted(k for k, v in owned.items() if v is None)

    assert len(unowned) <= _UNOWNED_MODULES, (
        f"{len(unowned)} modules are owned by no area, up from "
        f"{_UNOWNED_MODULES}. New: nothing in the rotation will ever plan, "
        f"security-review or performance-review these. Bind them to an area (or "
        f"add one), or if this growth is deliberate, say why here.\n"
        f"{unowned}")
    assert len(unowned) == _UNOWNED_MODULES, (
        f"only {len(unowned)} modules are unowned, but the ratchet still says "
        f"{_UNOWNED_MODULES}. Someone widened an area without tightening the "
        "number — lower it so the next regression is caught.")
