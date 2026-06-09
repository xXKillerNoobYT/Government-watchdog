"""Second-slice end-to-end integration smoke test (GOV-84, Stage 1 Slice 2 E).

Drives scripts/slice2_smoke.py over the committed sanitized Alpine fixture (the
2026-05-08 WWTP-financing meeting) and asserts each Contract 1.07 invariant
granularly. The smoke proves the *whole* Slice-2 chain holds together —
migrate -> reuse Slice-1 registry -> load fixture -> segment -> statements +
evidence_links -> speaker-attribution safety — which the per-module unit tests
cannot prove in isolation.

Acceptance criteria asserted (GOV-84):
- no orphan claims (every statement resolves; an orphan insert is rejected);
- every statement defaults not-publishable with a gated ui_status;
- every evidence_link pointer is valid (and an unresolved pointer is rejected);
- speaker attribution is safe (low-confidence -> name-free/no person edge, no
  wrong-name; on-record-public naming is a hard stop; a justified official can
  still be named so the gate is not trivially passing).

No AI, no network: pure sqlite + the committed fixture, in a throwaway sandbox.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import slice2_smoke as smoke  # noqa: E402


@pytest.fixture(scope="module")
def result(tmp_path_factory) -> dict:
    sandbox = tmp_path_factory.mktemp("slice2_smoke")
    return smoke.run_smoke(smoke.DEFAULT_FIXTURE, sandbox)


def _check(result: dict, name: str) -> dict:
    for c in result["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError(f"smoke produced no check named {name!r}")


def test_smoke_overall_ok(result: dict) -> None:
    failed = [c["name"] for c in result["checks"] if not c["passed"]]
    assert result["ok"], f"slice 2 smoke failed checks: {failed}"


def test_pipeline_ran_end_to_end(result: dict) -> None:
    # fixture -> meeting + transcript reconciled to the Slice-1 video source,
    # 8 segments, 2 statements.
    assert result["transcript_source_id"] == "alpine_youtube_channel"
    assert result["segment_count"] == 8
    assert len(result["statement_ids"]) == 2


def test_no_orphan_claims(result: dict) -> None:
    check = _check(result, "no_orphan_claims")
    assert check["passed"], check.get("error")
    assert check["statements_checked"] == 2
    assert check["orphan_insert_rejected"] is True


def test_default_not_publishable(result: dict) -> None:
    check = _check(result, "default_not_publishable")
    assert check["passed"], check.get("error")
    assert check["rows"] == 2
    assert not check.get("offenders")


def test_every_evidence_pointer_valid(result: dict) -> None:
    check = _check(result, "evidence_pointers_valid")
    assert check["passed"], check.get("error")
    assert check["rows"] == 2  # one timestamp pointer per statement
    assert check["unresolved_pointer_rejected"] is True


def test_speaker_attribution_safe(result: dict) -> None:
    check = _check(result, "speaker_attribution_safe")
    assert check["passed"], check.get("error")
    findings = check["findings"]
    # low-confidence attributed -> downgraded, name-free, no person edge.
    weak = findings["weak_downgraded_name_free"]
    assert weak["state"] != "attributed"
    assert weak["person_id"] is None
    assert weak["made_statement_rows"] == 0
    assert "Pat Maxwell" not in (weak["label"] or "")
    # on-record-public naming is a hard stop.
    assert findings["public_naming_hard_stop"]["raised"] is True
    # a justified official CAN be named (gate not trivially failing).
    assert findings["justified_official_named"]["state"] == "attributed"


def test_smoke_strict_raises_on_regression(tmp_path) -> None:
    # strict=True is what the CLI uses for a loud non-zero exit; a clean run must
    # NOT raise.
    smoke.run_smoke(smoke.DEFAULT_FIXTURE, tmp_path, strict=True)
