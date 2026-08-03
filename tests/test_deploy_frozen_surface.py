"""DEPLOY-2026 AC-7: frozen serving surfaces are byte-0 vs origin/main.

GOV-722 is additive-only. The four surfaces in ``FROZEN`` (read_api, ai_risk_gate,
stage5_agenda_board, and the whole mcp_service package) must show an empty diff
against ``origin/main``.

**``FROZEN`` is not the whole frozen set, and reading it as such is the mistake
this module now guards against.** Four further paths — ``publication.py`` (pinned
by seven separate test files), ``stage4_newsletter_feed.py``,
``stage4_newsletter_digest_assembler.py`` and ``statements.py`` — are pinned byte-0
by scattered ``git diff origin/main`` assertions inside unrelated stage tests,
belonging to no list at all. See
``test_claude_md_names_every_byte_frozen_path_the_suite_enforces``.

Also guards the migrations directory: existing
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
    "0032_beta_account_deletion_requests.sql",  # GOV-1565: user-initiated account-deletion request lifecycle
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

#: A `git diff origin/main -- <paths>` assertion, in the idiom the suite uses.
#: Deliberately anchored to that call shape rather than to any list name: the
#: whole point is to find freezes that belong to NO list.
_BYTE0_CALL = re.compile(
    r'"diff",\s*"origin/main",\s*"--",?(?P<args>[^\]]*)\]', re.S)


def discovered_frozen_paths() -> set[str]:
    """Every path the SUITE actually pins byte-0, not every path a list claims.

    Union of `FROZEN` (the central list, expanded wherever a test splats it) and
    each literal `scripts/...` argument to a git-diff-vs-origin/main assertion
    anywhere under `tests/`.

    **Known limit, stated rather than hidden:** this reads test source text, so a
    freeze written in some other shape — a helper, an f-string, a path built at
    run time — is invisible to it. That makes it under-report, never over-report,
    which is the safe direction: a missed freeze leaves the status quo, whereas a
    phantom one would demand CLAUDE.md document something that is not real.
    """
    tests_dir = Path(__file__).resolve().parent
    found = set(FROZEN)
    for path in sorted(tests_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for call in _BYTE0_CALL.finditer(source):
            found.update(re.findall(r'"(scripts/[A-Za-z0-9_/.]+)"',
                                    call.group("args")))
    return found


def test_claude_md_names_every_byte_frozen_path_the_suite_enforces():
    """CLAUDE.md must name every frozen path — measured, not cross-copied.

    **This replaced a guard that was green and wrong.** The previous version
    compared CLAUDE.md against `FROZEN` — two hand-maintained copies of one list,
    which agreed with each other and were both incomplete. Measured 2026-08-01:
    FOUR more paths are pinned byte-0 by scattered `git diff origin/main`
    assertions inside unrelated stage tests, `scripts/publication.py` by **seven**
    separate files, and neither list mentioned any of them. So an agent read
    CLAUDE.md, edited `publication.py`, and got seven failures from files whose
    names never mention it.

    Two lists agreeing is not ground truth. The fix is the same one this repo
    reaches for elsewhere — **ask the thing that enforces, not the thing that
    documents** (`EXPLAIN QUERY PLAN` over grepping `CREATE INDEX`; the planner
    over the schema). Here that means deriving the set from the assertions.
    """
    claude_md = Path(__file__).resolve().parents[1] / "CLAUDE.md"
    assert claude_md.exists(), "repo-level CLAUDE.md is missing"
    text = claude_md.read_text(encoding="utf-8")

    enforced = discovered_frozen_paths()
    assert enforced >= set(FROZEN), "the central FROZEN list stopped being discovered"
    assert len(enforced) > len(FROZEN), (
        "no scattered per-test freeze was discovered — either they were all "
        "removed (delete this guard with them) or _BYTE0_CALL stopped matching, "
        "which would make this pass for the wrong reason")

    section = text.split("byte-frozen against `origin/main`", 1)
    assert len(section) == 2, "CLAUDE.md no longer has the frozen-surfaces claim"
    listed = set(re.findall(r"`(scripts/[A-Za-z0-9_/.]+)`", section[1][:1400]))

    unnamed = sorted(enforced - listed)
    assert not unnamed, (
        f"the suite pins {unnamed} byte-0 against origin/main, and CLAUDE.md does "
        "not name them. An agent will edit one and get failures from test files "
        "whose names do not mention it — which is exactly how this guard's "
        "predecessor came to be green and wrong.")


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

#: The numbered-citation series CLAUDE.md carries — one row per contract.
#:
#: MERGED 2026-08-01 (GOV-1705, C12). This was two near-identical test functions
#: and adding the B6 series would have made three. The trap is the SAME in every
#: series — renumbering a contract leaves the citation token looking valid while
#: aiming at a different rule — so the series belongs in data, not in another
#: copied function body. A fourth contract is now one row rather than 25 lines.
#:
#: Each pattern matches the CITATION, not a formatting convention. The `INV-`
#: version originally required bold (`\*\*INV-8\*\*`) and so silently ignored the
#: plain "See INV-8." in the same file — caught by a red proof that PASSED when
#: it should have failed. Matching prose style rather than the token itself is
#: the recurring trap on this repo.
#:
#: The leading `\b` is load-bearing and was checked, not assumed: without it,
#: `P-\d+` matches the `P-701` inside `PEP-701`, which CLAUDE.md names in its
#: very first section. Verified empirically — `PEP-701`, `HTTP-404`: no match.
_CITATION_SERIES = (
    ("INV-", re.compile(r"\bINV-\d+\b"), "data-model-contract.md"),
    ("P-", re.compile(r"\bP-\d+\b"), "supplied-file-provenance-contract.md"),
    ("W-", re.compile(r"\bW-\d+\b"),
     "gov1579-web-safe-read-projection-contract.md"),
    # The fourth contract, added 2026-08-02 (GOV-1718, C12) — one row, as the
    # merge above predicted. `\bA-\d+\b` was checked for false positives before
    # being added, not assumed safe: it matches exactly `A-5` and `A-8` in
    # CLAUDE.md and nothing else.
    ("A-", re.compile(r"\bA-\d+\b"),
     "gov278-ai-provenance-audit-contract.md"),
)


def test_every_docs_path_claude_md_names_actually_exists():
    """Link rot in the one file everyone is told to read first.

    A ratchet at **zero** violations, installed when CLAUDE.md named only two
    contracts and still at zero with four (2026-08-02).
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


@pytest.mark.parametrize(
    "prefix,pattern,contract_name", _CITATION_SERIES,
    ids=[row[0].rstrip("-") for row in _CITATION_SERIES])
def test_claude_md_numbered_citations_resolve_in_their_contract(
        prefix, pattern, contract_name):
    """A renumbered invariant leaves the citation intact and the meaning wrong.

    CLAUDE.md cites invariants BY NUMBER to tell a reader which rules bite
    silently — so the number is the entire value of the pointer. Renumbering a
    contract keeps every token valid-looking while aiming it somewhere else:
    the presence-vs-effect shape this repo has hit several times.

    Each series is checked against ITS OWN contract. Resolving a `P-` citation
    against the data-model contract would fail for the wrong reason, which is
    why the mapping is explicit data rather than one regex over one document.
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    contract = root / "Docs" / contract_name
    assert contract.exists(), (
        f"the contract CLAUDE.md points at is gone: Docs/{contract_name}")
    body = contract.read_text(encoding="utf-8")

    cited = sorted(set(pattern.findall(text)))
    assert cited, (
        f"CLAUDE.md cites no {prefix}<n> invariant. Either the pointer lost its "
        "specifics (the numbers are what make it useful) or the regex stopped "
        "matching — both make this guard vacuous.")
    unresolved = [c for c in cited if f"### {c} \u2014" not in body]
    assert not unresolved, (
        f"CLAUDE.md cites {unresolved}, which have no `### <{prefix}n> \u2014` "
        f"heading in {contract.name}. Either the invariant was renumbered "
        "(update CLAUDE.md) or removed (update both).")


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
