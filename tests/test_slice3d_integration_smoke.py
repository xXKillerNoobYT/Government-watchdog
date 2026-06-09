"""Slice 3 D end-to-end Lane-4 risk + Lane-5 reviewer-gate smoke test (GOV-91).

Drives scripts/slice3d_smoke.py over the committed sanitized Alpine fixture and
asserts each Lane-4/5 invariant granularly. The smoke proves the *whole* Lane-2
-> Lane-4 -> Lane-5 chain holds together — migrate -> reuse Slice-1 registry ->
load fixture -> segment (Lane-1) -> Lane-2 AI extraction -> Lane-4 risk screen ->
Lane-5 reviewer-gate — which the per-unit tests cannot prove in isolation.

Acceptance criteria asserted (GOV-91):
- 1.11 risk flags are recorded (the accusation claim gets a legal no-go flag);
- the risk layer writes NO gating field (statements digest unchanged pre/post);
- the Lane-4 run is recorded on ai_extraction_runs with lane='4_risk';
- promoting an AI row WITHOUT a reviewer decision is rejected (nothing written);
- a failed gateway run blocks downstream promotion;
- a valid human promotion reaches a reviewed status but NEVER flips
  publication_state — nothing AI-written is publishable by default.

No AI, no network: pure sqlite + the committed fixture + a deterministic Lane-2
proposer + a deterministic Lane-4 screen, in a throwaway sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402

import slice3d_smoke as smoke  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    sandbox = tmp_path_factory.mktemp("slice3d_smoke")
    return smoke.run_smoke(smoke.DEFAULT_FIXTURE, sandbox)


def _check(result: dict, name: str) -> dict:
    for c in result["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"smoke produced no check named {name!r}")


def test_smoke_overall_ok(result: dict) -> None:
    failed = [c["name"] for c in result["checks"] if not c["passed"]]
    assert result["ok"], f"slice 3 D smoke failed checks: {failed}"


def test_pipeline_ran_end_to_end(result: dict) -> None:
    assert result["segment_count"] == 8
    assert result["written_count"] == 2       # two anchored AI claims
    assert result["flag_count"] >= 1
    assert result["error_status"] == "ok"


def test_risk_flags_recorded(result: dict) -> None:
    check = _check(result, "risk_flags_recorded")
    assert check["passed"], check.get("error")
    assert check["legal_no_go_flags"] >= 1


def test_no_gating_write(result: dict) -> None:
    check = _check(result, "no_gating_write")
    assert check["passed"], check.get("error")


def test_gateway_run_log(result: dict) -> None:
    check = _check(result, "gateway_run_log")
    assert check["passed"], check.get("error")
    assert check["lane"] == "4_risk"


def test_reviewer_gate_rejects_unreviewed(result: dict) -> None:
    check = _check(result, "reviewer_gate_rejects_unreviewed")
    assert check["passed"], check.get("error")
    assert check["rejected"] is True
    assert check["still_unreviewed"] is True
    assert check["no_decision_row"] is True


def test_failed_run_blocks_downstream(result: dict) -> None:
    check = _check(result, "failed_run_blocks_downstream")
    assert check["passed"], check.get("error")
    assert check["blocked"] is True


def test_promotion_never_publishes(result: dict) -> None:
    check = _check(result, "promotion_never_publishes")
    assert check["passed"], check.get("error")
    assert check["verification_status"] == "reviewed_source_linked"
    assert check["publication_state"] == "not_publishable"


def test_smoke_strict_does_not_raise_on_clean_run(tmp_path) -> None:
    smoke.run_smoke(smoke.DEFAULT_FIXTURE, tmp_path, strict=True)
