"""Slice 3 C end-to-end Lane-3 verification smoke test (GOV-90, Stage 1 Slice 3 C).

Drives scripts/slice3c_smoke.py over the committed sanitized Alpine fixture and
asserts each Lane-3 invariant granularly. The smoke proves the *whole* Lane-2 ->
Lane-3 chain holds together — migrate -> reuse Slice-1 registry -> load fixture
-> segment (Lane-1) -> Lane-2 AI extraction -> Lane-3 verification — which the
per-unit tests cannot prove in isolation.

Acceptance criteria asserted (GOV-90):
- a verification label is written per AI statement (grounded -> source_match,
  off-source -> source_mismatch + contested);
- the mismatch path flags rather than promotes: every AI row stays
  machine_extracted_unreviewed + not_publishable after Lane 3;
- Lane 3 writes NO gating field (statements digest unchanged pre/post);
- the Lane-3 run is recorded on ai_extraction_runs with lane='3_verification';
- fail-closed downstream: a verdict blocks publication unless it is a source_match
  a human separately approved.

No AI, no network: pure sqlite + the committed fixture + a deterministic Lane-2
proposer + a deterministic Lane-3 compare, in a throwaway sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import pytest  # noqa: E402

import slice3c_smoke as smoke  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    sandbox = tmp_path_factory.mktemp("slice3c_smoke")
    return smoke.run_smoke(smoke.DEFAULT_FIXTURE, sandbox)


def _check(result: dict, name: str) -> dict:
    for c in result["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"smoke produced no check named {name!r}")


def test_smoke_overall_ok(result: dict) -> None:
    failed = [c["name"] for c in result["checks"] if not c["passed"]]
    assert result["ok"], f"slice 3 C smoke failed checks: {failed}"


def test_pipeline_ran_end_to_end(result: dict) -> None:
    assert result["segment_count"] == 8
    assert result["written_count"] == 2       # two anchored AI claims
    assert result["verified_count"] == 2
    assert result["contested_count"] == 1     # the off-source claim
    assert result["error_status"] == "ok"


def test_labels_assigned(result: dict) -> None:
    check = _check(result, "labels_assigned")
    assert check["passed"], check.get("error")
    assert check["result_rows"] == 2
    assert check["verdicts"][smoke._MATCH_ID] == "source_match"
    assert check["verdicts"][smoke._MISMATCH_ID] == "source_mismatch"


def test_never_promoted(result: dict) -> None:
    check = _check(result, "never_promoted")
    assert check["passed"], check.get("error")
    assert not check.get("offenders")


def test_no_gating_write(result: dict) -> None:
    check = _check(result, "no_gating_write")
    assert check["passed"], check.get("error")


def test_gateway_run_log(result: dict) -> None:
    check = _check(result, "gateway_run_log")
    assert check["passed"], check.get("error")
    assert check["lane"] == "3_verification"


def test_failclosed_downstream(result: dict) -> None:
    check = _check(result, "failclosed_downstream")
    assert check["passed"], check.get("error")
    assert check["match_blocked_unapproved"] is True
    assert check["match_unblocks_approved"] is True
    assert check["mismatch_blocked"] is True
    assert check["none_blocked"] is True


def test_smoke_strict_does_not_raise_on_clean_run(tmp_path) -> None:
    smoke.run_smoke(smoke.DEFAULT_FIXTURE, tmp_path, strict=True)
