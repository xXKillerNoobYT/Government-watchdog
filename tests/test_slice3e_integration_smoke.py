"""Slice 3 E — full AI-gateway integration smoke test (GOV-92, Lane 2->3->4->5).

Drives scripts/slice3e_smoke.py over the committed sanitized Alpine fixture and
asserts each end-to-end invariant granularly. This is the Slice-3 capstone: where
the per-lane smokes (GOV-89/90/91) prove one lane in isolation, this proves the
*whole* gateway holds together as one continuous pipeline over a single DB — and
in particular the load-bearing cross-lane guarantee that NOTHING AI-written is
publishable by default, even a row a human reviewer has approved.

Acceptance criteria asserted (GOV-92 done-bar 7-12 in an end-to-end context):
- the pipeline runs raw -> Lane 2 -> Lane 3 -> Lane 4 -> Lane 5 with all lane runs
  on the shared ledger;
- every AI row carries produced_by='ai' + machine_extracted_unreviewed +
  not_publishable + ai_thought_then + is_verbatim=0 + its run provenance;
- a no-pointer AI claim is rejected (not written; the run records it);
- the uncertain AI speaker is name-free (no person_id, no made_statement edge);
- Lanes 3 and 4 never mutate a gating field (statements digest stable across both);
- Lane 4 records a legal no-go on the accusation + a publication review flag per
  unreviewed AI row;
- the reviewer-gate rejects a no-reviewer promotion, blocks an open no-go row and a
  failed-run row, and a valid human promotion reaches reviewed but NEVER flips
  publication_state;
- HEADLINE: after the whole pipeline, EVERY AI row is still
  statement_publication_blocked() — zero AI rows reach a publishable state.

No AI, no network: pure sqlite + the committed fixture + injected deterministic
proposers, in a throwaway sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import slice3e_smoke as smoke  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    sandbox = tmp_path_factory.mktemp("slice3e_smoke")
    return smoke.run_smoke(smoke.DEFAULT_FIXTURE, sandbox)


def _check(result: dict, name: str) -> dict:
    for c in result["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"smoke produced no check named {name!r}")


def test_smoke_overall_ok(result: dict) -> None:
    failed = [c["name"] for c in result["checks"] if not c["passed"]]
    assert result["ok"], f"slice 3 E smoke failed checks: {failed}"


def test_pipeline_ran_end_to_end(result: dict) -> None:
    check = _check(result, "pipeline_ran_end_to_end")
    assert check["passed"], check.get("error")
    assert check["segment_count"] == 8
    assert set(check["written"]) == {smoke._CLEAN_ID, smoke._LEGAL_ID}
    assert check["orphan_rejected"] == 1
    # all four gateway runs (Lane 2 main + orphan + fail, Lane 3, Lane 4) present.
    for run_id in (smoke._LANE2_RUN, smoke._LANE2_ORPHAN_RUN, smoke._LANE2_FAIL_RUN,
                   smoke._LANE3_RUN, smoke._LANE4_RUN):
        assert run_id in check["ledger_runs"]


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
    assert smoke._CANDIDATE_NAME not in (check["label"] or "")


def test_lane3_labels_never_promote(result: dict) -> None:
    check = _check(result, "lane3_labels_never_promote")
    assert check["passed"], check.get("error")
    # Lane 3 labelled every AI row...
    assert set(check["verdicts"]) == {smoke._CLEAN_ID, smoke._LEGAL_ID}
    # ...and did not change the statements gating digest.
    assert check["pre"] == check["post"]


def test_lane4_risk_flags(result: dict) -> None:
    check = _check(result, "lane4_risk_flags")
    assert check["passed"], check.get("error")
    assert check["legal_no_go"] >= 1
    assert check["publication_flags"] >= 2
    assert check["pre"] == check["post"]  # Lane 4 wrote no gating field


def test_all_lanes_logged(result: dict) -> None:
    check = _check(result, "all_lanes_logged")
    assert check["passed"], check.get("error")
    assert check["lanes"][smoke._LANE2_RUN] == "2_extraction"
    assert check["lanes"][smoke._LANE3_RUN] == "3_verification"
    assert check["lanes"][smoke._LANE4_RUN] == "4_risk"


def test_reviewer_gate_rejects_unreviewed(result: dict) -> None:
    check = _check(result, "reviewer_gate_rejects_unreviewed")
    assert check["passed"], check.get("error")
    assert check["rejected"] is True
    assert check["still_unreviewed"] is True
    assert check["no_decision_row"] is True


def test_open_nogo_blocks_promotion(result: dict) -> None:
    check = _check(result, "open_nogo_blocks_promotion")
    assert check["passed"], check.get("error")
    assert check["blocked"] is True
    assert check["open_flag_count"] >= 1


def test_failed_run_blocks_downstream(result: dict) -> None:
    check = _check(result, "failed_run_blocks_downstream")
    assert check["passed"], check.get("error")
    assert check["blocked"] is True


def test_promotion_never_publishes(result: dict) -> None:
    check = _check(result, "promotion_never_publishes")
    assert check["passed"], check.get("error")
    assert check["verification_status"] == "reviewed_source_linked"
    assert check["publication_state"] == "not_publishable"
    assert check["promoted"] is True


def test_nothing_publishable_by_default(result: dict) -> None:
    check = _check(result, "nothing_publishable_by_default")
    assert check["passed"], check.get("error")
    # every AI row — including the human-approved clean one — stays blocked.
    assert check["publishable_rows"] == []
    assert check["ai_row_count"] >= 3
    # the owner DB gate is strictly stronger than the Lane-3 verdict gate.
    assert check["clean_verdict"] == "source_match"
    assert check["clean_verdict_gate_permits"] is True
    assert check["clean_owner_gate_still_blocks"] is True


def test_smoke_strict_does_not_raise_on_clean_run(tmp_path) -> None:
    smoke.run_smoke(smoke.DEFAULT_FIXTURE, tmp_path, strict=True)
