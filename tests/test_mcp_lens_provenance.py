"""§3.9 lens provenance regression (D6, LED-1).

Every claim a lens emits must anchor to a source inside the job's authorized
evidence set — a lens cannot cite something it was never granted. And every
generation audit row carries the policy + lens version, so the ledger can
attribute a reading to the exact pack that produced it.
"""

from __future__ import annotations

import json

from conftest import fake_adapter

from mcp_service import analysis, resources


def _authorized_anchor_set(conn, job_id="job1"):
    sel = resources._selector(conn, job_id)
    anchors = set(str(s) for s in sel.get("statement_ids", []) or [])
    anchors |= resources._authorized_source_ids(conn, job_id)
    return anchors


def test_every_claim_anchor_resolves_in_authorized_set(routed):
    token = analysis.mint_submit_token(routed, "job1")
    analysis.run_multi_lens(routed, job_id="job1",
                            adapters={"fake": fake_adapter()}, token=token)
    authorized = _authorized_anchor_set(routed)
    assert authorized  # sanity: the job actually authorizes some evidence
    rows = routed.execute("SELECT body, claims FROM mcp_job_outputs").fetchall()
    assert rows
    for r in rows:
        body = json.loads(r["body"])
        assert body["claims"], "a lens output must carry at least one claim"
        for claim in body["claims"]:
            assert claim["source_anchor"] in authorized, (
                f"claim anchor {claim['source_anchor']!r} escapes authorized set")
        # The staged claims summary mirrors the body's anchors.
        for claim in json.loads(r["claims"]):
            assert claim["source_anchor"] in authorized


def test_audit_rows_carry_policy_and_lens_version(routed):
    token = analysis.mint_submit_token(routed, "job1")
    analysis.run_multi_lens(routed, job_id="job1",
                            adapters={"fake": fake_adapter()}, token=token)
    rows = routed.execute(
        "SELECT policy_version, lens_version FROM mcp_audit_events "
        "WHERE kind='provider' AND outcome='allow'").fetchall()
    assert len(rows) == 3  # one generation per lens
    for r in rows:
        assert r["policy_version"] == "1.0.0"
        assert r["lens_version"] == "1.0.0"


def test_evidence_refs_point_only_at_authorized_uris(routed):
    token = analysis.mint_submit_token(routed, "job1")
    analysis.run_multi_lens(routed, job_id="job1",
                            adapters={"fake": fake_adapter()}, token=token)
    body = json.loads(
        routed.execute("SELECT body FROM mcp_job_outputs LIMIT 1").fetchone()["body"])
    for ref in body["evidence_refs"]:
        assert ref.startswith("gov-evidence://job/job1/")
