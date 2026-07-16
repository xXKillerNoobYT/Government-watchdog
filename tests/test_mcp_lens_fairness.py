"""§3.9 lens fairness regression (D6).

Fairness is structural: the runner assembles the evidence context ONCE and hashes
it, so every lens consumes byte-identical evidence (hash equality); the three lens
packs carry identical requirement/prohibition constraint sets (symmetry); and
every staged output is labelled with its lens id + version.
"""

from __future__ import annotations

import json

from conftest import fake_adapter

from mcp_service import analysis, lenses


def _run(conn):
    token = analysis.mint_submit_token(conn, "job1")
    return analysis.run_multi_lens(
        conn, job_id="job1", adapters={"fake": fake_adapter()}, token=token)


def test_evidence_hash_identical_across_all_three_lenses(routed):
    summary = _run(routed)
    assert summary["lens_count"] == 3
    hashes = {r["evidence_hash"] for r in summary["runs"]}
    assert len(hashes) == 1  # one shared context, hashed once (D6)


def test_pack_constraints_are_symmetric(mcp_conn):
    constraints = [lenses.pack_constraints(pid) for pid in lenses.LENSES]
    first = constraints[0]
    for c in constraints[1:]:
        assert c["requirements"] == first["requirements"]
        assert c["prohibitions"] == first["prohibitions"]
    # The prohibitions cover the accepted no-gos verbatim.
    joined = " ".join(first["prohibitions"]).lower()
    assert "stereotyp" in joined and "campaign" in joined
    assert "all members" in joined and "verification" in joined


def test_only_the_interpretive_frame_differs(mcp_conn):
    frames = {pid: lenses.LENSES[pid]["frame"] for pid in lenses.LENSES}
    assert len(set(frames.values())) == 3  # each lens has a distinct frame
    # But the rendered rules share the identical requirement/prohibition tail.
    tails = set()
    for pid in lenses.LENSES:
        rules = lenses.rules_template(pid)
        tails.add(rules.split("Requirements:", 1)[1])
    assert len(tails) == 1  # everything after the frame is identical


def test_every_staged_output_is_labelled_lens_and_version(routed):
    _run(routed)
    rows = routed.execute(
        "SELECT policy_pack_id, policy_pack_version, body FROM mcp_job_outputs").fetchall()
    assert len(rows) == 3
    seen = set()
    for r in rows:
        body = json.loads(r["body"])
        assert body["lens_id"] == r["policy_pack_id"]
        assert body["lens_version"] == r["policy_pack_version"] == lenses.LENS_VERSION
        seen.add(body["lens_id"])
    assert seen == set(lenses.LENSES)
