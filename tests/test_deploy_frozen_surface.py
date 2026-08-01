"""DEPLOY-2026 AC-7: frozen serving surfaces are byte-0 vs origin/main.

GOV-722 is additive-only. The four frozen surfaces (read_api, ai_risk_gate,
stage5_agenda_board, and the whole mcp_service package) must show an empty diff
against ``origin/main``. Also guards the migrations directory: existing
migrations are immutable, and a new migration may land only via a deliberate
allowlist entry (GOV-721 plan amendment #6 rewrote the original zero-diff
assertion into the allowlist form; the guard stays, the allowlist grows).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FROZEN = [
    "scripts/read_api.py",
    "scripts/ai_risk_gate.py",
    "scripts/stage5_agenda_board.py",
    "scripts/mcp_service/",
]

# Migrations a branch is allowed to ADD vs origin/main (amendment #6 form of
# the guard). Grows deliberately, one PR at a time, next free slot only.
MIGRATION_ALLOWLIST = {
    "0025_accounts_cohorts_notifications.sql",
    "0026_beta_gate.sql",  # GOV-801: gated-beta front door (five beta_* tables)
    "0027_beta_magic_code.sql",  # GOV-1538: 6-digit code columns on beta_magic_tokens
    "0028_supplied_file_records.sql",  # GOV-1575 (B2): supplied_files record + provenance
    "0029_supplied_file_links.sql",  # GOV-1577 (B4): supplied_file→subject linkage
    "0030_supplied_file_versioning.sql",  # GOV-1578 (B5): supersede versioning + red-flag
    "0031_supplied_file_provenance_note.sql",  # GOV-1625 (B3 schema evo): free-text provenance_note
}


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


@pytest.mark.skipif(
    _git("rev-parse", "--verify", "origin/main").returncode != 0,
    reason="origin/main not available in this checkout",
)
def test_frozen_surfaces_byte0_vs_origin_main():
    diff = _git("diff", "origin/main", "--", *FROZEN)
    assert diff.returncode == 0
    assert diff.stdout.strip() == "", f"frozen surface modified:\n{diff.stdout}"


@pytest.mark.skipif(
    _git("rev-parse", "--verify", "origin/main").returncode != 0,
    reason="origin/main not available in this checkout",
)
def test_leg2_added_no_new_migration():
    """Migration guard, allowlist form (GOV-721 plan v0.2 amendment #6).

    Originally (GOV-738 plan D7) this asserted a zero migration diff. A branch
    is now allowed to ADD exactly the migrations pinned below — nothing else,
    and never a modification or deletion of an existing migration. A future
    leg that needs a new slot updates MIGRATION_ALLOWLIST deliberately in the
    same PR, so the CTO merge gate always sees the schema change named here."""
    diff = _git("diff", "--name-status", "origin/main", "--", "Database/migrations/")
    assert diff.returncode == 0
    for line in diff.stdout.strip().splitlines():
        status, path = line.split(maxsplit=1)
        name = Path(path).name
        assert status == "A", (
            f"existing migration modified/deleted ({status}): {path}")
        assert name in MIGRATION_ALLOWLIST, (
            f"migration added without an allowlist entry: {path}")


# --- GOV-1671 (C12): CLAUDE.md's frozen-surface list must not rot ------------

def test_claude_md_lists_exactly_the_frozen_surfaces():
    """The repo's CLAUDE.md names the four frozen surfaces; FROZEN defines them.

    Two hand-maintained copies of one list — the same drift shape as the beta
    audit enum (#193). The failure here is quieter than a crash: CLAUDE.md is
    the first thing an agent reads, so a stale list sends someone to edit a
    surface believing it is unfrozen, or to treat a newly frozen one as fair
    game. Documentation that cannot be checked decays silently; this makes the
    claim executable.
    """
    import re
    from pathlib import Path

    claude_md = Path(__file__).resolve().parents[1] / "CLAUDE.md"
    assert claude_md.exists(), "repo-level CLAUDE.md is missing"
    text = claude_md.read_text(encoding="utf-8")

    section = text.split("Four serving surfaces are frozen", 1)
    assert len(section) == 2, "CLAUDE.md no longer has the frozen-surfaces claim"
    # Paths are written as `scripts/...` inline code; take them up to the
    # sentence that follows the list.
    listed = set(re.findall(r"`(scripts/[A-Za-z0-9_/.]+)`", section[1][:400]))

    assert listed == set(FROZEN), (
        f"CLAUDE.md and FROZEN disagree.\n"
        f"  only in CLAUDE.md: {sorted(listed - set(FROZEN))}\n"
        f"  only in FROZEN:    {sorted(set(FROZEN) - listed)}")


def test_claude_md_states_the_required_python_version():
    """3.12 is a hard requirement (PEP-701 nested f-strings), not a preference.

    Pinned because an agent that installs 3.11 gets a confusing failure in
    stage2_traceability.py rather than a clear version error.
    """
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "3.12" in text
    assert "PEP-701" in text or "nested f-string" in text


# --- GOV-1678 (C12): make CLAUDE.md's threading claim executable --------------

#: Loopback HTTP surfaces that MUST be threaded with a request timeout, and the
#: one that must NOT be. Derived from measurement, not preference: a plain
#: ``HTTPServer`` serves one request at a time, so a single client that opens a
#: socket and goes silent denies the whole API — measured at 6.003s on the beta
#: gate (GOV-1669) and again, independently, on notifications (GOV-1677). The
#: second one existed for two days after the first was fixed, which is why this
#: is a list rather than a comment.
THREADED_SURFACES = ("scripts/beta/http_api.py", "scripts/notifications/http_api.py")
#: Deliberately single-threaded: its handler closes over a ``RawObjectStore``
#: whose ``_append_link`` appends to a shared ledger file, not established as
#: thread-safe (#206). This entry is the *exception*, and it is pinned too — an
#: unexamined "consistency" edit threading it would be a real regression.
UNTHREADED_SURFACES = ("scripts/beta/intake_api.py",)


@pytest.mark.parametrize("rel", THREADED_SURFACES)
def test_loopback_http_surfaces_are_threaded_with_a_timeout(rel):
    """Both halves are load-bearing; neither alone is sufficient.

    Threading without a timeout swaps one stalled connection for unbounded
    stalled threads. A timeout without threading still blocks the single worker.
    """
    src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
    assert "ThreadingHTTPServer(" in src, (
        f"{rel} no longer constructs a ThreadingHTTPServer — one silent socket "
        "denies the whole surface")
    assert "daemon_threads" in src, f"{rel} must set daemon_threads"
    assert "REQUEST_TIMEOUT_SECONDS" in src, (
        f"{rel} has no request timeout; a stalled worker never comes back")


@pytest.mark.parametrize("rel", UNTHREADED_SURFACES)
def test_the_documented_single_threaded_exception_stays_single_threaded(rel):
    src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
    assert "ThreadingHTTPServer" not in src, (
        f"{rel} is threaded, but its handler shares a RawObjectStore ledger "
        "that is not established as thread-safe (#206) — see CLAUDE.md")


def test_claude_md_documents_the_threading_rule_and_its_exception():
    """The list above and CLAUDE.md are two hand-maintained copies of one fact.

    Same drift shape as the frozen-surfaces list, and the same fix: if a surface
    is added to one and not the other, this fails rather than decaying quietly.
    """
    text = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    for rel in THREADED_SURFACES + UNTHREADED_SURFACES:
        name = rel.removeprefix("scripts/")
        assert name in text, f"CLAUDE.md does not mention {name}"
    assert "intake_api.py` is deliberately **not** threaded" in text, (
        "CLAUDE.md must state the exception, not just the rule — an undocumented "
        "exception reads as an oversight and invites a 'consistency' fix")


# --- GOV-1685 (C12, data-model): CLAUDE.md's pointers must stay true ----------
#
# CLAUDE.md's whole value is that it is accurate. A pointer to a moved file, or a
# citation of an invariant that has been renumbered, is not a missing fact — it is
# a *confident wrong* one, which is worse, because the reader stops looking.
#
# Deliberately NOT asserting "every Docs/ file is referenced from CLAUDE.md":
# measured 2026-07-31, **63 of 64 are not**, and that is correct — CLAUDE.md is an
# operating manual, not an index. A guard that is red on arrival gets suppressed
# rather than obeyed.

_DOCS_REF = re.compile(r"`(Docs/[A-Za-z0-9._-]+\.md)`")
#: `INV-<n>` citations in CLAUDE.md, which must resolve in the contract.
#:
#: Matches the CITATION, not a formatting convention. The first version required
#: bold (`\*\*INV-8\*\*`) and therefore silently ignored the plain "See INV-8." in
#: the same file — caught by the red proof, which PASSED when it should have
#: failed. Matching prose style rather than the token itself is the recurring
#: trap on this repo; this is that trap inside a guard written to prevent it.
_INV_REF = re.compile(r"\b(INV-\d+)\b")


def test_every_docs_path_claude_md_names_actually_exists():
    """Link rot in the one file everyone is told to read first.

    Currently two references (the access-gate contract and the data-model
    contract), so this is a ratchet at **zero** violations — cheapest possible
    moment to install it.
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    named = sorted(set(_DOCS_REF.findall(text)))
    assert named, (
        "CLAUDE.md names no Docs/ file at all — either the pointers were deleted "
        "or the regex stopped matching; both make this guard vacuous")
    missing = [n for n in named if not (root / n).exists()]
    assert not missing, (
        f"CLAUDE.md points at {missing}, which do not exist. A stale pointer is "
        "worse than none: the reader stops looking.")


def test_claude_md_invariant_citations_resolve_in_the_data_model_contract():
    """A renumbered invariant leaves the citation intact and the meaning wrong.

    CLAUDE.md cites INV-4, INV-5, INV-7 and INV-8 by number to tell a migration
    author which rules bite silently. Renumbering the contract would keep every
    one of those tokens valid-looking while pointing at different rules — the
    presence-vs-effect shape this repo has now hit several times.
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    contract = root / "Docs" / "data-model-contract.md"
    assert contract.exists(), "the contract CLAUDE.md points at is gone"
    body = contract.read_text(encoding="utf-8")

    cited = sorted(set(_INV_REF.findall(text)))
    assert cited, "CLAUDE.md cites no invariant — the pointer lost its specifics"
    unresolved = [c for c in cited if f"### {c} —" not in body]
    assert not unresolved, (
        f"CLAUDE.md cites {unresolved}, which have no `### <INV> —` heading in "
        f"{contract.name}. Either the invariant was renumbered (update CLAUDE.md) "
        "or removed (update both).")


# --- GOV-1694 (C8 hunt, PUBLIC REPO): nothing sensitive may become TRACKED ----
#
# `.gitignore` is this repo's disclosure boundary and every entry states its
# reason. But an ignore rule only helps while it is in place: a file committed
# BEFORE its rule landed stays in public history forever, and `git rm --cached`
# does not remove it.
#
# Measured 2026-08-01 across ALL history: of the paths the ignore file names as
# carrying real civic data, **zero were ever committed**. Two Logs/ files were
# (`acceptance.log`, `phase2-pilot-verification.log`), and the ignore file
# justifies leaving them there as "summary-only evidence ... no raw corpus/PII".
# That claim was VERIFIED rather than trusted: counters and AC pass/fail lines
# only; the single quoted string over 40 chars is a public document TITLE
# ("Annual financial report (2019-06-30)"); zero emails, zero absolute paths and
# zero speech attributions in either file.
#
# This guard exists for the NEXT one. Ratchet at zero violations.

#: Path fragments that, if TRACKED, would publish real civic data or secrets.
#: Each mirrors a `.gitignore` entry whose comment states the same boundary.
_MUST_NEVER_BE_TRACKED = (
    ".db", ".db.bak", ".db.backup",
    "Vault/", "Raw-Corpus/", "Raw-PDFs/", "Transcripts/",
    "ai_provider.local.json",
    "agent_inline_claims", "claims-batch", "batch3_claims", "batch4_claims",
    "batch5_claims",
    "Logs/backfill_", "Logs/control-plane/", "Logs/governance/", "Logs/pilot/",
    ".hermes/", "graphify-out/", "dist/",
)


def test_no_tracked_file_carries_real_civic_data_or_secrets():
    """This repository is PUBLIC. A tracked file is a published file.

    `git rm --cached` unpublishes nothing — it only stops FUTURE commits. So the
    check that matters runs on the index, before the disclosure happens.
    """
    out = subprocess.run(
        ["git", "ls-files"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=False)
    assert out.returncode == 0, f"git ls-files failed: {out.stderr}"
    tracked = [p for p in out.stdout.splitlines() if p]
    assert tracked, "git ls-files returned nothing — the guard would be vacuous"

    offenders = sorted(
        p for p in tracked if any(frag in p for frag in _MUST_NEVER_BE_TRACKED))
    assert not offenders, (
        "A file matching this repo's disclosure boundary is TRACKED. This "
        "repository is PUBLIC, so committing it publishes it permanently — "
        "`git rm --cached` later does NOT undo that. Remove it from the index "
        "before committing:\n  " + "\n  ".join(offenders))
