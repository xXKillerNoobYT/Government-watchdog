"""DEPLOY-2026 AC-7: frozen serving surfaces are byte-0 vs origin/main.

GOV-722 is additive-only. The four frozen surfaces (read_api, ai_risk_gate,
stage5_agenda_board, and the whole mcp_service package) must show an empty diff
against ``origin/main``. Also asserts this leg added no new migration (no domain
schema change was needed — plan D7).
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
    """Plan D7: the drill uses a synthetic fixture; no domain migration was added.

    If a future change needs one it takes the next free slot (0023) and stays
    additive — this test would then be updated deliberately."""
    diff = _git("diff", "--name-only", "origin/main", "--", "Database/migrations/")
    assert diff.stdout.strip() == "", (
        f"unexpected migration change in an additive leg:\n{diff.stdout}")
