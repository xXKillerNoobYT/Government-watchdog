"""GOV-332 Stage 2.15 — agent-handoff / owner-escalation routing guard.

Proves :mod:`stage2_escalation` enforces that every Stage-2 reviewer-internal
fail-closed / unresolvable condition class terminates at exactly one named human owner
with a defined escalation action, and never loops back to a detecting agent
(anti-infinite-loop invariant).

The module is a pure governance artifact: no DB, no AI, no network, no migration, no
``--apply``. So this test is pure in-process — it asserts the six checks are CLEAN on
the SSOT manifest, then for ≥2 checks injects a routing defect into a *copy* of the
manifest and proves the guard flips to non-clean (and back to clean when the defect is
removed). A check that cannot go RED is decorative, not load-bearing.

Test-only / read-only: imports the existing ``stage2_escalation`` functions, adds NO
production routing, no read_api/publication touch, no schema, no AI, no network. If a
real routing defect surfaced here it would be a SEPARATE CTO-routed governance fix —
this ticket ships the guard, not a silent self-heal.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import stage2_escalation as esc  # noqa: E402


@pytest.fixture()
def manifest() -> dict[str, dict[str, str]]:
    """A deep copy of the SSOT manifest, so a test mutation never leaks to other tests."""
    return copy.deepcopy(esc.ROUTING_MANIFEST)


# ===========================================================================
# GREEN — every check is clean on the SSOT manifest.
# ===========================================================================


def test_audit_is_clean_on_ssot_manifest() -> None:
    report = esc.audit_escalation()
    assert report["clean"], report


def test_every_enumerated_class_has_exactly_one_route() -> None:
    report = esc.completeness()
    assert report["clean"]
    assert report["mapped_count"] == report["enumerated_count"] == 7
    assert report["unmapped"] == [] and report["unknown"] == []
    # The frozen enumerated set and the manifest keys agree exactly.
    assert set(esc.ROUTING_MANIFEST) == set(esc.CONDITION_CLASSES)


def test_every_owner_is_a_named_human_governance_role() -> None:
    report = esc.named_human_owner()
    assert report["clean"]
    owners = {r["owner_role"] for r in esc.ROUTING_MANIFEST.values()}
    assert owners <= set(esc.HUMAN_OWNER_ROLES)
    # And no human owner role is secretly also a detecting agent.
    assert esc.HUMAN_OWNER_ROLES.isdisjoint(esc.DETECTING_AGENTS)


def test_no_route_terminates_at_a_detecting_agent() -> None:
    report = esc.no_self_handoff()
    assert report["clean"]
    for route in esc.ROUTING_MANIFEST.values():
        assert route["owner_role"] not in esc.DETECTING_AGENTS
        assert route["owner_role"] != route["detected_by"]
        # Sanity: every detector IS a known detecting agent (so the check is meaningful).
        assert route["detected_by"] in esc.DETECTING_AGENTS


def test_manifest_carries_no_pii_or_internal_columns() -> None:
    assert esc.no_leak()["clean"]


def test_determinism_routes_and_serialization_stable() -> None:
    report = esc.determinism()
    assert report["stable_routes"] and report["byte_identical"]


def test_read_only_no_writes_no_serving_imports() -> None:
    report = esc.read_only()
    assert report["clean"], report
    assert report["write_ops"] == [] and report["serving_imports"] == []


def test_route_for_returns_ssot_route_for_mapped_class() -> None:
    route = esc.route_for("provenance_unverified")
    assert route["owner_role"] == "security-privacy-reviewer"
    assert route["detected_by"] in esc.DETECTING_AGENTS


def test_route_for_unmapped_class_is_failclosed_to_cto() -> None:
    """An unmapped class resolves to the fail-closed CTO default — never a silent no-op."""
    route = esc.route_for("some_unknown_future_condition")
    assert route["owner_role"] == "cto"
    assert route["owner_role"] in esc.HUMAN_OWNER_ROLES


# ===========================================================================
# RED — injected routing defects flip the guard. (≥2 required; proven load-bearing:
# each FAILS when the defect is present and PASSES when it is removed.)
# ===========================================================================


def test_red_self_handoff_trips_check3(manifest) -> None:
    """RED (a): an injected self-handoff (owner = the detecting agent) trips check 3.

    Load-bearing proof: clean before, RED with the defect, clean again once removed.
    """
    cls = "coverage_backgap"
    detector = manifest[cls]["detected_by"]
    assert detector in esc.DETECTING_AGENTS

    # Clean baseline on the copy.
    assert esc.no_self_handoff(manifest)["clean"]

    # Inject: route the condition straight back to the agent that detects it (a loop).
    manifest[cls]["owner_role"] = detector
    report = esc.no_self_handoff(manifest)
    assert not report["clean"]
    assert any(h["condition_class"] == cls for h in report["self_handoffs"])
    # The whole audit also goes non-clean.
    assert esc.audit_escalation(manifest)["clean"] is False

    # Remove the defect -> the check passes again (proves it is load-bearing, not stuck).
    manifest[cls]["owner_role"] = "cto"
    assert esc.no_self_handoff(manifest)["clean"]


def test_red_unmapped_condition_class_trips_check1(manifest) -> None:
    """RED (b): an injected unmapped condition class (dropped route) trips check 1.

    Load-bearing proof: clean before, RED with the class unmapped, clean once restored.
    """
    cls = "docdrift_red"
    assert esc.completeness(manifest)["clean"]

    # Inject: drop the route for an enumerated class -> it is now unmapped.
    dropped = manifest.pop(cls)
    report = esc.completeness(manifest)
    assert not report["clean"]
    assert cls in report["unmapped"]
    assert esc.audit_escalation(manifest)["clean"] is False
    # Fail-closed: even unmapped, route_for still resolves it to the CTO default.
    assert esc.route_for(cls, manifest)["owner_role"] == "cto"

    # Restore -> clean again.
    manifest[cls] = dropped
    assert esc.completeness(manifest)["clean"]


def test_red_unknown_route_class_trips_check1(manifest) -> None:
    """RED: a route for a class NOT in the frozen enumerated set trips completeness."""
    manifest["bogus_condition"] = {
        "owner_role": "cto", "escalation_action": "x", "pass_up_trigger": "y",
        "detected_by": "automation-ops-engineer",
    }
    report = esc.completeness(manifest)
    assert not report["clean"]
    assert "bogus_condition" in report["unknown"]


def test_red_empty_owner_trips_check2(manifest) -> None:
    """RED: an empty owner_role trips named-human-owner."""
    manifest["traceability_orphan"]["owner_role"] = ""
    report = esc.named_human_owner(manifest)
    assert not report["clean"]
    assert any(v["condition_class"] == "traceability_orphan" for v in report["violations"])


def test_red_non_human_owner_trips_check2(manifest) -> None:
    """RED: an owner that is not a frozen human/governance role trips check 2."""
    manifest["traceability_orphan"]["owner_role"] = "some-random-agent"
    assert not esc.named_human_owner(manifest)["clean"]


def test_red_pii_email_in_route_trips_check4(manifest) -> None:
    """RED: an injected email address in a route trips the no-leak SecPriv check."""
    manifest["reviewer_access_denied"]["escalation_action"] = "email reviewer@example.com"
    report = esc.no_leak(manifest)
    assert not report["clean"]
    assert any(l["reason"] == "email-like PII" for l in report["leaks"])


def test_red_internal_column_name_in_route_trips_check4(manifest) -> None:
    """RED: an internal column name (record payload) in a route trips no-leak."""
    manifest["provenance_unverified"]["escalation_action"] = "inspect statement_text of the row"
    report = esc.no_leak(manifest)
    assert not report["clean"]
    assert any("statement_text" in l["reason"] for l in report["leaks"])


def test_red_serving_import_trips_check6(tmp_path: Path) -> None:
    """RED: a synthetic module that imports read_api trips the read-only check.

    Proves check 6 is load-bearing without mutating the real module: point ``read_only``
    at a temp source file that imports the production serving module.
    """
    bad = tmp_path / "bad_module.py"
    bad.write_text("import read_api\nx = 1\n", encoding="utf-8")
    report = esc.read_only(bad)
    assert not report["clean"]
    assert "read_api" in report["serving_imports"]


def test_red_write_op_trips_check6(tmp_path: Path) -> None:
    """RED: a synthetic module that performs a write call trips the read-only check."""
    bad = tmp_path / "bad_writer.py"
    bad.write_text("def f(conn):\n    conn.execute('DELETE FROM x')\n", encoding="utf-8")
    report = esc.read_only(bad)
    assert not report["clean"]
    assert ".execute()" in report["write_ops"]


# ===========================================================================
# CLI exit ladder — 0 clean / 1 routing defect.
# ===========================================================================


def test_cli_exit_0_on_clean_manifest() -> None:
    assert esc.main([]) == 0
    assert esc.main(["--json"]) == 0


def test_cli_exit_1_on_injected_defect(monkeypatch) -> None:
    """CLI exit ladder: a self-handoff injected into the live manifest makes main() exit 1."""
    broken = copy.deepcopy(esc.ROUTING_MANIFEST)
    broken["coverage_backgap"]["owner_role"] = broken["coverage_backgap"]["detected_by"]
    monkeypatch.setattr(esc, "ROUTING_MANIFEST", broken)
    assert esc.main([]) == 1
