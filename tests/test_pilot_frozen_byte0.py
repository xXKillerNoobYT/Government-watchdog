"""PILOT-2026 §5.3 test 6: byte-0 guard on the frozen serving modules (GOV-781).

The harness IMPORTS the three frozen serving surfaces (transitively, via the MCP
redaction choke-point) but must never mutate them. This asserts the source files
are byte-identical before and after a full workload + snapshot + pack build —
runtime immutability. The PR/CI additionally diffs them against ``origin/main``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FROZEN = ("read_api.py", "ai_risk_gate.py", "stage5_agenda_board.py")


def _digests() -> dict[str, str]:
    out = {}
    for name in FROZEN:
        data = (_SCRIPTS / name).read_bytes()
        out[name] = hashlib.sha256(data).hexdigest()
    return out


def test_frozen_modules_unchanged_by_harness(pilot_conn):
    from pilot import pack, snapshot, workload

    before = _digests()
    rep = workload.run(pilot_conn, seed="byte0", apply=True,
                       bounds={"WL-1": 3, "WL-2": 1, "WL-3": 2,
                               "WL-4": 1, "WL-5": 2, "WL-6": 1})
    snapshot.extract(pilot_conn, "alpine", rep["period"])
    pack.build_and_record(pilot_conn, period=rep["period"])
    after = _digests()
    assert before == after


def test_harness_imports_frozen_scanners_not_copies():
    """The redaction choke-point imports the frozen scanners (single source)."""
    from mcp_service import redaction

    assert redaction.RawPathLeak is __import__("read_api").RawPathLeak
