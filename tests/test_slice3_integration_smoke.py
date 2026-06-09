"""Third-slice end-to-end AI-gateway Lane-2 smoke test (GOV-89, Stage 1 Slice 3 B).

Drives scripts/slice3_smoke.py over the committed sanitized Alpine fixture and
asserts each AI-gateway invariant granularly. The smoke proves the *whole*
Lane-2 chain holds together — migrate -> reuse Slice-1 registry -> load fixture
-> segment (Lane-1) -> Lane-2 AI extraction (offline proposer) -> ledger — which
the per-unit tests cannot prove in isolation.

Acceptance criteria asserted (GOV-89 done-bar 7-11):
- every AI-written row carries produced_by='ai' + machine_extracted_unreviewed +
  not_publishable + ai_thought_then + is_verbatim=0 + its run provenance;
- a no-pointer AI claim is rejected (not written; run records the rejection);
- the uncertain AI speaker is name-free (no person_id, no made_statement edge);
- the gateway run-log records input set / model+tool+prompt / outputs / errors /
  reviewer state / retry;
- fail-closed downstream: an OK-but-unreviewed run is blocked, a failed run is
  blocked, and approval unblocks only when error_status is ok.

No AI, no network: pure sqlite + the committed fixture + an injected
deterministic proposer, in a throwaway sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import slice3_smoke as smoke  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    sandbox = tmp_path_factory.mktemp("slice3_smoke")
    return smoke.run_smoke(smoke.DEFAULT_FIXTURE, sandbox)


def _check(result: dict, name: str) -> dict:
    for c in result["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"smoke produced no check named {name!r}")


def test_smoke_overall_ok(result: dict) -> None:
    failed = [c["name"] for c in result["checks"] if not c["passed"]]
    assert result["ok"], f"slice 3 smoke failed checks: {failed}"


def test_pipeline_ran_end_to_end(result: dict) -> None:
    assert result["transcript_source_id"] == "alpine_youtube_channel"
    assert result["segment_count"] == 8
    assert result["output_count"] == 2      # two anchored claims written
    assert result["rejected"] == 1          # the orphan claim rejected
    assert result["error_status"] == "partial"


def test_ai_provenance_failclosed(result: dict) -> None:
    check = _check(result, "ai_provenance_failclosed")
    assert check["passed"], check.get("error")
    assert check["rows"] == 2
    assert not check.get("offenders")


def test_no_orphan_claims(result: dict) -> None:
    check = _check(result, "no_orphan_claims")
    assert check["passed"], check.get("error")
    assert check["orphan_written"] == 0
    assert check["orphan_rejected_count"] >= 1


def test_attribution_safe(result: dict) -> None:
    check = _check(result, "attribution_safe")
    assert check["passed"], check.get("error")
    assert check["attribution_state"] != "attributed"
    assert check["person_id"] is None
    assert check["made_statement_rows"] == 0
    assert "Pat Maxwell" not in (check["label"] or "")


def test_gateway_run_log(result: dict) -> None:
    check = _check(result, "gateway_run_log")
    assert check["passed"], check.get("error")
    assert check["output_count"] == 2
    assert check["input_source_count"] >= 1
    assert check["input_segment_count"] >= 1


def test_failclosed_downstream(result: dict) -> None:
    check = _check(result, "failclosed_downstream")
    assert check["passed"], check.get("error")
    assert check["unreviewed_blocked"] is True
    assert check["failed_blocked"] is True
    assert check["approved_unblocks"] is True


def test_smoke_strict_does_not_raise_on_clean_run(tmp_path) -> None:
    smoke.run_smoke(smoke.DEFAULT_FIXTURE, tmp_path, strict=True)
