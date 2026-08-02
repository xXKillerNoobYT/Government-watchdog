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
