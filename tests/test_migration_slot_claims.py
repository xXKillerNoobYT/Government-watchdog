"""GOV-1696: the sibling-PR half of the migration-slot guard.

`tests/test_migration_slots.py` reads the working tree, so it sees exactly one
branch — its own. Both PRs that took `0032` pass it individually; it fires only
after the second lands. `scripts/migration_slot_claims.py` asks the forge instead,
so it can answer the question the contract actually poses (*"is this slot free?"*)
before anyone commits to an answer.

The collision analysis is a pure function over an already-fetched
`{pr: [paths]}` mapping, so everything below runs offline. The one thing that
cannot be pure — enumerating open PRs — is guarded on its **failure** behaviour,
which is the property that matters: it must refuse, never report clean.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import migration_slot_claims as msc  # noqa: E402

MIGRATIONS = msc.MIGRATIONS_PREFIX


# --- the pure collision analysis ----------------------------------------------

class TestCollisionDetection:

    def test_two_open_prs_claiming_one_slot_is_a_collision(self):
        """The live case: #199 and #132 both added a `0032_*.sql`."""
        claims = msc.claims_from_pr_files({
            199: [f"{MIGRATIONS}0032_beta_account_deletion_requests.sql"],
            132: [f"{MIGRATIONS}0032_access_decision_core.sql"],
        })
        assert msc.find_collisions({"0031"}, claims) == {"0032": [132, 199]}

    def test_a_pr_claiming_a_slot_already_on_disk_is_a_collision(self):
        """A stale branch rebased onto a base that consumed its slot."""
        claims = msc.claims_from_pr_files({7: [f"{MIGRATIONS}0031_something.sql"]})
        assert msc.find_collisions({"0030", "0031"}, claims) == {"0031": [7]}

    def test_distinct_slots_are_not_a_collision(self):
        claims = msc.claims_from_pr_files({
            1: [f"{MIGRATIONS}0032_a.sql"],
            2: [f"{MIGRATIONS}0033_b.sql"],
        })
        assert msc.find_collisions({"0031"}, claims) == {}

    def test_a_sql_file_outside_the_migrations_dir_is_not_a_claim(self):
        """Only `Database/migrations/` holds migrations; nothing else counts."""
        claims = msc.claims_from_pr_files({
            1: ["scripts/0032_helper.sql", "Docs/0032_notes.sql", "README.md"],
        })
        assert claims.by_slot == {} and claims.unparseable == []

    def test_one_pr_touching_the_same_slot_twice_counts_once(self):
        """A PR that edits its own migration must not collide with itself."""
        claims = msc.claims_from_pr_files({
            5: [f"{MIGRATIONS}0032_a.sql", f"{MIGRATIONS}0032_a.sql"],
        })
        assert claims.by_slot == {"0032": [5]}
        assert msc.find_collisions({"0031"}, claims) == {}


class TestUnparseablePathsAreSurfacedNotDropped:
    """Fail-closed: a migration path that does not parse is an UNSEEN claim.

    Dropping it would be the quiet failure the house style forbids — the tool
    would report "no collisions" precisely because it could not read the file
    that holds the collision.
    """

    @pytest.mark.parametrize("path", [
        "0032-dashes-not-underscores.sql",   # wrong separator
        "032_short_slot.sql",                # three digits
        "0032_MixedCase.sql",                # slug must be lowercase
        "0032_no_extension",                 # not .sql
    ])
    def test_a_malformed_migration_filename_is_reported(self, path):
        claims = msc.claims_from_pr_files({9: [f"{MIGRATIONS}{path}"]})
        assert claims.unparseable == [(9, f"{MIGRATIONS}{path}")], (
            f"{path!r} sits in the migrations dir but was silently skipped — "
            "an unparsed claim is an unseen claim")
        assert claims.by_slot == {}


# --- the answer the contract's step 1 actually wants --------------------------

class TestNextFreeSlot:

    def test_it_counts_in_flight_prs_which_is_the_whole_point(self):
        """`main` at 0031 + an open PR on 0032 means the next author takes 0033.

        Reading the directory alone answers 0032 and manufactures the collision.
        """
        claims = msc.claims_from_pr_files({199: [f"{MIGRATIONS}0032_x.sql"]})
        taken = {f"{n:04d}" for n in range(1, 32)}
        assert msc.next_free_slot(taken, claims) == 33
        assert max(int(s) for s in taken) + 1 == 32, (
            "the directory-only answer must differ, or this test proves nothing")

    def test_it_refuses_when_the_sequence_has_a_hole(self):
        """With a gap, "next free" means either the hole or the end.

        Two authors resolving that differently is how a collision is made, so
        the tool refuses rather than picking one reading.
        """
        claims = msc.claims_from_pr_files({1: [f"{MIGRATIONS}0009_x.sql"]})
        with pytest.raises(msc.AmbiguousSlotSequence, match="0005"):
            msc.next_free_slot({"0001", "0002", "0003", "0004", "0006"}, claims)

    def test_the_real_repo_has_an_unambiguous_next_slot(self):
        """Non-vacuity: the pure helpers agree with this checkout."""
        taken = msc.existing_slots()
        assert taken, "no migrations found — the helper would be vacuous"
        assert msc.next_free_slot(taken, msc.SlotClaims()) == len(taken) + 1


# --- fail-closed: the probe refuses rather than reporting clean ---------------

class TestProbeFailsClosed:
    """A tool that answers "no collisions" when it could not look is worse than
    no tool: it converts an unknown into a false assurance."""

    def test_missing_gh_raises_rather_than_returning_an_empty_map(self, monkeypatch):
        def _boom(*a, **k):
            raise FileNotFoundError("gh")
        monkeypatch.setattr(msc.subprocess, "run", _boom)
        with pytest.raises(msc.SlotProbeUnavailable, match="not installed"):
            msc.fetch_open_pr_files()

    def test_a_failing_gh_call_raises(self, monkeypatch):
        def _fail(*a, **k):
            raise subprocess.CalledProcessError(4, a[0], stderr="not authenticated")
        monkeypatch.setattr(msc.subprocess, "run", _fail)
        with pytest.raises(msc.SlotProbeUnavailable, match="not authenticated"):
            msc.fetch_open_pr_files()

    def test_one_unreadable_pr_diff_fails_the_whole_probe(self, monkeypatch):
        """A partial map under-reports — and the PR that failed is exactly the
        one whose claim is unknown."""
        def _fake(cmd, **k):
            if cmd[1] == "pr" and cmd[2] == "list":
                return subprocess.CompletedProcess(cmd, 0, stdout='[{"number":1},{"number":2}]')
            if cmd[3] == "2":
                raise subprocess.CalledProcessError(1, cmd, stderr="merge conflict")
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{MIGRATIONS}0032_a.sql\n")
        monkeypatch.setattr(msc.subprocess, "run", _fake)
        with pytest.raises(msc.SlotProbeUnavailable, match="merge conflict"):
            msc.fetch_open_pr_files()

    def test_the_cli_exits_2_on_an_unavailable_probe_not_0(self, monkeypatch, capsys):
        """Exit 0 would tell CI the slot is free without anyone having checked."""
        monkeypatch.setattr(msc, "fetch_open_pr_files",
                            lambda repo=None: (_ for _ in ()).throw(
                                msc.SlotProbeUnavailable("rate limited")))
        assert msc.main([]) == 2
        assert "SLOT PROBE UNAVAILABLE" in capsys.readouterr().err

    def test_the_cli_exits_1_on_a_collision(self, monkeypatch, capsys):
        monkeypatch.setattr(msc, "fetch_open_pr_files", lambda repo=None: {
            199: [f"{MIGRATIONS}0032_a.sql"], 132: [f"{MIGRATIONS}0032_b.sql"]})
        assert msc.main([]) == 1
        assert "COLLISION slot 0032" in capsys.readouterr().out

    def test_the_cli_exits_0_and_names_the_next_slot_when_clean(self, monkeypatch, capsys):
        monkeypatch.setattr(msc, "fetch_open_pr_files", lambda repo=None: {7: ["README.md"]})
        assert msc.main([]) == 0
        assert "no slot collisions" in capsys.readouterr().out


# --- the convention is stated twice; pin the copies together ------------------

def test_the_slot_regex_matches_the_offline_guards_copy():
    """`test_migration_slots.py` must stay offline, so it cannot import this
    module — the convention is therefore written in both files. If they drift,
    one guard accepts a filename the other rejects and the pair stops agreeing
    on what a migration even is.

    Compared as source text rather than by import, so this holds regardless of
    pytest's import mode.
    """
    source = (ROOT / "tests" / "test_migration_slots.py").read_text(encoding="utf-8")
    match = re.search(r'MIGRATION_NAME_RE = re\.compile\(r"(?P<pat>[^"]+)"\)', source)
    assert match, "could not find MIGRATION_NAME_RE in tests/test_migration_slots.py"
    assert match["pat"] == msc.MIGRATION_NAME_RE.pattern, (
        "the migration-name convention has drifted between the offline guard and "
        f"{msc.__name__}: {match['pat']!r} vs {msc.MIGRATION_NAME_RE.pattern!r}")
