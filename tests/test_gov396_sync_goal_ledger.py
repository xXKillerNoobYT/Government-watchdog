"""GOV-396 — goal-ledger <-> board auto-sync (governance / AutomationOps).

Proves the three flip rules of ``scripts/governance/sync_goal_ledger.py`` over
deterministic in-memory fixtures (NO network, NO DB, NO PII). Mirrors the lane
spec in ``CTO_WORKFLOWS.md`` -> "Goal-ledger <-> board auto-sync lane" and the
GOV-396 acceptance criteria:

* AC-1  rule 1: subgoal planned->active when an issue is in_progress; apply +
        idempotent re-run; restore.
* AC-2  rule 2: numbered parent active->achieved when all NON-deferred children
        achieved; deferred children (Isaac-gated banner) do NOT block.
* AC-3  rule 3 is PROPOSE-ONLY: a recommendation row, never a PATCH under --apply.
* AC-4  the live-baseline shape (Stage 3 active w/ planned children) reports ZERO
        rule-1/rule-2 drift.
* allowlist + one-active-stage invariant guardrails.

Each rule is also proven to be able to go RED (a flip is *missed* when its
precondition is removed) so the checks are load-bearing, not vacuous.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS / "governance"))

import sync_goal_ledger as sgl  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixture builders.
# --------------------------------------------------------------------------- #
def goal(gid, title, status, parent=None, desc=""):
    return {"id": gid, "title": title, "status": status, "parentId": parent,
            "description": desc}


def issue(iid, goal_id, status):
    return {"id": iid, "goalId": goal_id, "status": status, "title": f"issue-{iid}"}


DEFERRED_BANNER = "This subgoal is DEFERRED to Stage 9 (Isaac-gated) and not required."


class FakeLedger:
    """In-memory PATCH sink that mutates goals so idempotency can be re-checked."""

    def __init__(self, goals):
        self.by_id = {g["id"]: g for g in goals}
        self.patches = []

    def patch(self, goal_id, status):
        self.patches.append((goal_id, status))
        self.by_id[goal_id]["status"] = status

    def goals(self):
        return list(self.by_id.values())


def _noop_log(level, message):
    pass


# --------------------------------------------------------------------------- #
# Classification helpers.
# --------------------------------------------------------------------------- #
def test_classification_numbered_vs_subgoal():
    assert sgl.is_numbered_stage(goal("g", "Stage 3 — MVP", "active"))
    assert not sgl.is_numbered_stage(goal("g", "Stage 3.07 — verify", "active"))
    assert sgl.is_subgoal(goal("g", "Stage 3.07 — verify", "active"))
    assert not sgl.is_subgoal(goal("g", "Stage 3 — MVP", "active"))
    assert not sgl.is_numbered_stage(goal("g", "00 — HEAD GOAL", "active"))


def test_deferred_banner_detection():
    assert sgl.is_deferred(goal("g", "Stage 2.08 — x", "planned", desc=DEFERRED_BANNER))
    assert not sgl.is_deferred(goal("g", "Stage 2.07 — x", "planned", desc="normal"))


def test_stage_number_ordering():
    assert sgl.stage_number(goal("g", "Stage 4 — x", "planned")) == 4
    assert sgl.stage_number(goal("g", "Stage 12 — x", "planned")) == 12
    assert sgl.stage_number(goal("g", "00 — HEAD", "active")) is None


# --------------------------------------------------------------------------- #
# AC-1 — rule 1: subgoal planned -> active on work start.
# --------------------------------------------------------------------------- #
def test_ac1_rule1_flips_planned_subgoal_when_issue_in_progress():
    goals = [
        goal("p", "Stage 3 — MVP", "active"),
        goal("sub", "Stage 3.08 — newsletter", "planned", parent="p"),
    ]
    issues = [issue("i1", "sub", "in_progress")]
    report = sgl.compute_drift(goals, issues)
    assert len(report.rule1_flips) == 1
    flip = report.rule1_flips[0]
    assert (flip.goal_id, flip.from_status, flip.to_status, flip.rule) == (
        "sub", "planned", "active", 1)


def test_ac1_rule1_in_review_also_counts():
    goals = [goal("sub", "Stage 3.08 — x", "planned")]
    report = sgl.compute_drift(goals, [issue("i1", "sub", "in_review")])
    assert len(report.rule1_flips) == 1


def test_ac1_rule1_red_when_issue_not_started():
    """Load-bearing: a done/blocked issue must NOT trigger the flip."""
    goals = [goal("sub", "Stage 3.08 — x", "planned")]
    for st in ("done", "blocked", "cancelled", "todo"):
        report = sgl.compute_drift(goals, [issue("i1", "sub", st)])
        assert report.rule1_flips == [], f"status {st} must not flip"


def test_ac1_rule1_red_when_subgoal_already_active():
    """Idempotency at the rule level: an active subgoal yields no flip."""
    goals = [goal("sub", "Stage 3.08 — x", "active")]
    report = sgl.compute_drift(goals, [issue("i1", "sub", "in_progress")])
    assert report.rule1_flips == []


def test_ac1_apply_then_idempotent_then_restore():
    goals = [goal("sub", "Stage 3.08 — x", "planned")]
    issues = [issue("i1", "sub", "in_progress")]
    ledger = FakeLedger(goals)

    report = sgl.compute_drift(ledger.goals(), issues)
    sgl.apply_flips(report.auto_flips, ledger.patch, _noop_log)
    assert ledger.by_id["sub"]["status"] == "active"
    assert ledger.patches == [("sub", "active")]

    # Idempotent: a second scan over the now-synced ledger yields ZERO flips.
    report2 = sgl.compute_drift(ledger.goals(), issues)
    assert report2.auto_flips == []
    sgl.apply_flips(report2.auto_flips, ledger.patch, _noop_log)
    assert ledger.patches == [("sub", "active")]  # unchanged -> idempotent

    # Restore (mirrors AC-1 "Restore." step).
    ledger.patch("sub", "planned")
    assert ledger.by_id["sub"]["status"] == "planned"


# --------------------------------------------------------------------------- #
# AC-2 — rule 2: numbered parent active -> achieved; deferred children ignored.
# --------------------------------------------------------------------------- #
def test_ac2_rule2_flips_parent_when_all_nondeferred_children_achieved():
    goals = [
        goal("P", "Stage 2 — transcripts", "active"),
        goal("c1", "Stage 2.01 — a", "achieved", parent="P"),
        goal("c2", "Stage 2.02 — b", "achieved", parent="P"),
        # three Isaac-gated DEFERRED children that must NOT block the flip:
        goal("d1", "Stage 2.08 — n", "planned", parent="P", desc=DEFERRED_BANNER),
        goal("d2", "Stage 2.09 — m", "planned", parent="P", desc=DEFERRED_BANNER),
        goal("d3", "Stage 2.11 — s", "planned", parent="P", desc=DEFERRED_BANNER),
    ]
    report = sgl.compute_drift(goals, [])
    assert len(report.rule2_flips) == 1
    flip = report.rule2_flips[0]
    assert (flip.goal_id, flip.from_status, flip.to_status, flip.rule) == (
        "P", "active", "achieved", 2)
    assert "deferred" in flip.reason.lower()


def test_ac2_rule2_red_when_a_nondeferred_child_not_achieved():
    """Load-bearing: one planned non-deferred child blocks the parent flip."""
    goals = [
        goal("P", "Stage 2 — x", "active"),
        goal("c1", "Stage 2.01 — a", "achieved", parent="P"),
        goal("c2", "Stage 2.02 — b", "planned", parent="P"),  # blocks
    ]
    report = sgl.compute_drift(goals, [])
    assert report.rule2_flips == []


def test_ac2_deferred_only_children_do_not_auto_flip():
    """A parent whose only remaining children are deferred is an anomaly, not a flip."""
    goals = [
        goal("P", "Stage 2 — x", "active"),
        goal("d1", "Stage 2.08 — n", "planned", parent="P", desc=DEFERRED_BANNER),
    ]
    report = sgl.compute_drift(goals, [])
    assert report.rule2_flips == []
    assert any("no non-deferred child" in a for a in report.anomalies)


def test_ac2_allowlist_parent_never_flipped():
    """Belt-and-suspenders: an allowlisted active goal is never achieved by rule 2."""
    # Use an allowlisted id but give it a numbered-stage title + all-achieved kids.
    goals = [
        goal("59fd6f5e-aaaa", "Stage 9 — governance program", "active"),
        goal("c1", "Stage 9.01 — a", "achieved", parent="59fd6f5e-aaaa"),
    ]
    report = sgl.compute_drift(goals, [])
    assert report.rule2_flips == []


# --------------------------------------------------------------------------- #
# AC-3 — rule 3 is PROPOSE-ONLY.
# --------------------------------------------------------------------------- #
def test_ac3_rule3_proposes_next_stage_when_none_active():
    goals = [
        goal("s3", "Stage 3 — MVP", "achieved"),
        goal("s4", "Stage 4 — newsletter", "planned"),
        goal("s5", "Stage 5 — corrections", "planned"),
    ]
    report = sgl.compute_drift(goals, [])
    assert len(report.rule3_recs) == 1
    rec = report.rule3_recs[0]
    assert (rec.goal_id, rec.from_status, rec.to_status) == ("s4", "planned", "active")
    # Lowest-numbered planned stage chosen (Stage 4 before Stage 5).


def test_ac3_rule3_never_enters_auto_flips_and_apply_issues_no_patch():
    """The core AC-3 guarantee: rule 3 is structurally excluded from --apply."""
    goals = [
        goal("s3", "Stage 3 — MVP", "achieved"),
        goal("s4", "Stage 4 — x", "planned"),
    ]
    report = sgl.compute_drift(goals, [])
    assert report.rule3_recs and report.auto_flips == []  # rec present, no auto flips

    ledger = FakeLedger(goals)
    sgl.apply_flips(report.auto_flips, ledger.patch, _noop_log)
    assert ledger.patches == []  # ZERO PATCHes -> Stage 4 stays planned
    assert ledger.by_id["s4"]["status"] == "planned"


def test_ac3_rule3_silent_when_a_stage_is_active():
    goals = [
        goal("s3", "Stage 3 — MVP", "active"),
        goal("s4", "Stage 4 — x", "planned"),
    ]
    report = sgl.compute_drift(goals, [])
    assert report.rule3_recs == []  # exactly one active -> no proposal


def test_ac3_rule2_flip_chains_into_rule3_proposal_without_applying_it():
    """When rule 2 would flip the last active stage achieved, rule 3 proposes the
    next stage — but still propose-only (not in auto_flips)."""
    goals = [
        goal("s3", "Stage 3 — MVP", "active"),
        goal("c1", "Stage 3.01 — a", "achieved", parent="s3"),
        goal("s4", "Stage 4 — x", "planned"),
    ]
    report = sgl.compute_drift(goals, [])
    assert [f.goal_id for f in report.rule2_flips] == ["s3"]   # rule 2 flips Stage 3
    assert [r.goal_id for r in report.rule3_recs] == ["s4"]    # rule 3 proposes Stage 4
    # Stage 4 must not be in the apply set.
    assert all(f.goal_id != "s4" for f in report.auto_flips)


# --------------------------------------------------------------------------- #
# AC-4 — live-baseline shape reports zero rule-1/rule-2 drift.
# --------------------------------------------------------------------------- #
def test_ac4_baseline_shape_zero_drift():
    """Stage 3 active with planned children + in_progress issues already pointing
    at active goals == the live GOV-395 baseline. Expect 0 auto drift."""
    goals = [
        goal("s3", "Stage 3 — MVP", "active"),
        goal("c1", "Stage 3.01 — a", "achieved", parent="s3"),
        goal("c2", "Stage 3.10 — qa", "active", parent="s3"),     # already active
        goal("c3", "Stage 3.11 — sec", "planned", parent="s3"),   # planned, no issue
        goal("gov", "Goal/spec governance", "active"),            # cross-cutting
    ]
    issues = [
        issue("i1", "c2", "in_progress"),   # points at already-active 3.10
        issue("i2", "gov", "in_progress"),  # points at cross-cutting active goal
    ]
    report = sgl.compute_drift(goals, issues)
    assert report.rule1_flips == []
    assert report.rule2_flips == []
    assert report.has_drift is False


# --------------------------------------------------------------------------- #
# One-active-stage invariant.
# --------------------------------------------------------------------------- #
def test_more_than_one_active_numbered_stage_is_anomaly_not_flip():
    goals = [
        goal("s2", "Stage 2 — x", "active"),
        goal("s3", "Stage 3 — y", "active"),
    ]
    report = sgl.compute_drift(goals, [])
    assert any("more than one numbered stage" in a for a in report.anomalies)
    assert report.rule3_recs == []  # no proposal while >1 active


# --------------------------------------------------------------------------- #
# Report rendering / JSON shape smoke.
# --------------------------------------------------------------------------- #
def test_render_and_json_summary_smoke():
    goals = [
        goal("sub", "Stage 3.08 — x", "planned"),
        goal("s3", "Stage 3 — MVP", "active"),
    ]
    report = sgl.compute_drift(goals, [issue("i1", "sub", "in_progress")])
    text = sgl.render_report(report)
    assert "rule-1" in text and "rule-3" in text
    d = sgl._report_to_dict(report)
    assert d["auto_flip_count"] == 1 and d["has_drift"] is True
    assert d["rule1_flips"][0]["to"] == "active"
