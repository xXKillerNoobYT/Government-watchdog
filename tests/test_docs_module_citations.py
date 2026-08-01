"""GOV-1699 (C1, read-api): a contract that cites a module which does not exist.

Found by C1 while binding `read-api`: `Docs/gov601-agenda-board-backend-contract.md`
cited `frontend_surface.py` **three times with line numbers**
(`frontend_surface.py:202-290`, `§3, :320-338`) — and no such file exists. The
same document names `stage5_frontend_surface.py` correctly four times, so it was
a partial rename: a reader following the precise-looking citation finds nothing,
and the precision is what makes it convincing.

This is the same failure class as `test_every_docs_path_claude_md_names_actually_exists`
(CLAUDE.md pointing at a missing `Docs/` file) turned around: a contract pointing
at a missing `scripts/` module. **A stale pointer is worse than none — the reader
stops looking.**

**LIMIT, STATED BECAUSE THE RED PROOF FOUND IT: this guard would NOT have caught
the gov601 defect that prompted it.** Those three citations were written bare —
`frontend_surface.py:202-290`, not `` `scripts/frontend_surface.py` `` — and this
check only sees the backticked, repo-relative form.

Widening to bare names was measured and rejected: 249 distinct `*.py` tokens are
mentioned across tracked `Docs/`, of which **62 do not resolve as `scripts/<name>`
— and most of those are correct**, naming real modules nested a level deeper
(`cohorts.py` → `scripts/accounts/`, `gate.py` → `scripts/beta/`) or under
`tests/`. Resolving a bare basename anywhere in the repo would make almost
everything resolve and leave the check toothless. So the bare form is not
reliably checkable, and this guard covers the form that is: **9 genuinely dangling
backticked citations existed when it was installed.**

Matched on SYNTAX, not vocabulary: only a backticked ``scripts/….py`` counts.
Prose that merely mentions a module name is deliberately out of scope — every
guard on this repo that greps for a *phrase* has eventually fired on the prose
describing it (GOV-1665, GOV-1672, and this file's own docstring, which names a
module that does not exist and must not trip it).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A backticked repo-relative module path. Anchored to the backticks so prose,
#: this docstring, and the correction notes inside contracts cannot trip it.
_MODULE_CITE = re.compile(r"`(scripts/[A-Za-z0-9_/]+\.py)`")

#: Citations that were ALREADY dangling when this guard was installed
#: (2026-08-01, measured: 9 across 4 docs out of 166 total citations).
#:
#: All are stage-1 era references to scripts that no longer exist —
#: `validate_concept_map_export.py` alone is cited by four separate contracts.
#: They are recorded rather than fixed because correcting them means deciding, per
#: script, whether it was renamed or deleted, and that is documentation archaeology
#: rather than this check's job. Filed as an issue.
#:
#: **This set may only SHRINK.** A ratchet installed at its true value beats one
#: installed at zero by pretending — a guard that is red on arrival gets suppressed
#: rather than obeyed, and one that is green by exclusion never had teeth.
KNOWN_DANGLING = {
    ("Docs/stage1-automation-ai-boundary-matrix-contract.md",
     "scripts/validate_concept_map_export.py"),
    ("Docs/stage1-backend-gap-analysis.md", "scripts/crawl_summary.py"),
    ("Docs/stage1-backend-gap-analysis.md", "scripts/data_boundary_check.py"),
    ("Docs/stage1-backend-gap-analysis.md", "scripts/stage1_check.py"),
    ("Docs/stage1-backend-gap-analysis.md", "scripts/validate_concept_map_export.py"),
    ("Docs/stage1-backend-gap-analysis.md", "scripts/validate_sources.py"),
    ("Docs/stage1-backend-gap-analysis.md", "scripts/wayback_check.py"),
    ("Docs/stage1-security-privacy-publication-gates-contract.md",
     "scripts/validate_concept_map_export.py"),
    ("Docs/stage1-transcript-evidence-statement-contract.md",
     "scripts/validate_concept_map_export.py"),
}


def _tracked_docs() -> list[str]:
    """Tracked `Docs/*.md` only, and that is REQUIRED, not merely tidy.

    `Docs/` and `docs/` are **the same directory** on this checkout — measured
    2026-08-01, same inode; git's canonical case is `Docs/`, and macOS APFS is
    case-insensitive. So the local-only `docs/auto-go-*.md` trackers live here too,
    and they cite `scripts/….py` in backticks constantly. A filesystem glob would
    scan them, fail on tracker prose, and behave differently on CI (where those
    files do not exist at all) than it does locally.

    `git ls-files` lists staged files too, so a contract added in the same commit
    IS checked by the PR that adds it — which is the case that matters.
    """
    out = subprocess.run(["git", "ls-files", "Docs"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [p for p in out.stdout.splitlines() if p.endswith(".md")]


def _citations() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for doc in _tracked_docs():
        text = (ROOT / doc).read_text(encoding="utf-8")
        for module in _MODULE_CITE.findall(text):
            found.add((doc, module))
    return found


def test_no_doc_cites_a_module_that_does_not_exist():
    """Every backticked `scripts/….py` in a tracked contract must resolve."""
    dangling = {(d, m) for d, m in _citations() if not (ROOT / m).exists()}
    new = sorted(dangling - KNOWN_DANGLING)
    assert not new, (
        "these docs cite a module that does not exist:\n  "
        + "\n  ".join(f"{d} -> {m}" for d, m in new)
        + "\nEither the module was renamed (fix the citation) or removed (fix both). "
        "A stale pointer is worse than none: the reader stops looking.")


def test_the_known_dangling_set_may_only_shrink():
    """A fixed citation must be removed from the allowlist, or it rots there.

    Without this, `KNOWN_DANGLING` becomes a list of things that USED to be
    broken — indistinguishable from things that still are, which is how an
    allowlist quietly stops describing reality.
    """
    citations = _citations()
    stale = sorted(entry for entry in KNOWN_DANGLING
                   if entry not in citations or (ROOT / entry[1]).exists())
    assert not stale, (
        "these KNOWN_DANGLING entries are no longer dangling (fixed or the "
        f"citation was deleted) — remove them from the allowlist: {stale}")


def test_the_citation_scan_is_not_vacuous():
    """A broken regex would make both checks above pass by finding nothing.

    The count is asserted against a floor rather than an exact number so ordinary
    documentation work does not fail the suite — the point is that the scan still
    SEES the corpus, not that the corpus is frozen.
    """
    citations = _citations()
    assert len(citations) > 100, (
        f"only {len(citations)} module citations found across tracked Docs/ — "
        "expected >100 (166 when installed). The regex has probably stopped "
        "matching, which would make the dangling check pass for the wrong reason")
    assert any(m == "scripts/file_read_api.py" for _, m in citations), (
        "the read-api contract's citation of file_read_api.py is not being seen")
