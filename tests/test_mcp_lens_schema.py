"""§3.9 lens output-schema regression (§3.6, fail-closed labels).

The lens output schema requires the lens/version/uncertainty labels and rejects
unknown fields. A missing label or a smuggled extra field is ``denied:schema`` —
never a silent pass — so an unlabelled or shape-drifted output can never reach
staging through ``submit_output``.
"""

from __future__ import annotations

import copy

import pytest

from mcp_service import lenses, schemas
from mcp_service.errors import MCPDenied

SID = lenses.LENS_OUTPUT_SCHEMA_ID
VER = lenses.LENS_VERSION


def _valid_body():
    return {
        "lens_id": "lens.libertarian",
        "lens_version": "1.0.0",
        "disclosure": "A labelled interpretation, not a fact.",
        "interpretation": "A reading of the cited record.",
        "claims": [{"text": "The record supports X.", "source_anchor": "stmt1",
                    "confidence": "low", "uncertainty": "reasonable readers differ"}],
        "evidence_refs": ["gov-evidence://job/job1/evidence.statement/stmt1"],
        "uncertainty_summary": "All claims low-confidence.",
        "neutral_comparison_note": "Another reader could weigh differently.",
    }


def test_schema_registered_at_lens_version():
    lenses.register_output_schema()
    assert SID in schemas.registered_ids()
    assert VER in schemas.registered_ids()[SID]


def test_valid_body_passes():
    assert schemas.validate(_valid_body(), SID, VER) is not None


@pytest.mark.parametrize("missing", ["lens_id", "lens_version", "uncertainty_summary",
                                     "claims", "disclosure", "neutral_comparison_note"])
def test_missing_required_label_fails_closed(missing):
    body = _valid_body()
    del body[missing]
    with pytest.raises(MCPDenied) as exc:
        schemas.validate(body, SID, VER)
    assert exc.value.code == "denied:schema"


def test_unknown_top_level_field_rejected():
    body = _valid_body()
    body["editorialize"] = "smuggled extra"
    with pytest.raises(MCPDenied) as exc:
        schemas.validate(body, SID, VER)
    assert exc.value.code == "denied:schema"


def test_unknown_claim_field_rejected():
    body = _valid_body()
    body["claims"][0]["publication_state"] = "publishable"  # cannot ride in a claim
    with pytest.raises(MCPDenied) as exc:
        schemas.validate(body, SID, VER)
    assert exc.value.code == "denied:schema"


def test_claim_missing_uncertainty_fails_closed():
    body = _valid_body()
    del body["claims"][0]["uncertainty"]
    with pytest.raises(MCPDenied) as exc:
        schemas.validate(body, SID, VER)
    assert exc.value.code == "denied:schema"


def test_bad_confidence_enum_rejected():
    body = _valid_body()
    body["claims"][0]["confidence"] = "certain"  # not in high|medium|low
    with pytest.raises(MCPDenied) as exc:
        schemas.validate(body, SID, VER)
    assert exc.value.code == "denied:schema"
