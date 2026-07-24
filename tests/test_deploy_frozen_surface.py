"""DEPLOY-2026 AC-7: frozen serving surfaces are byte-0 vs origin/main.

GOV-722 is additive-only. The four frozen surfaces (read_api, ai_risk_gate,
stage5_agenda_board, and the whole mcp_service package) must show an empty diff
against ``origin/main``. Also guards the migrations directory: existing
migrations are immutable, and a new migration may land only via a deliberate
allowlist entry (GOV-721 plan amendment #6 rewrote the original zero-diff
assertion into the allowlist form; the guard stays, the allowlist grows).
"""

from __future__ import annotations

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
    "0027_access_decision_core.sql",  # ACCESS-2026: inert explicit access facts
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
