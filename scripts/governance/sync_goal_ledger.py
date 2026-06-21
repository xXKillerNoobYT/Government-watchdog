"""Goal-ledger <-> board auto-sync (GOV-396, governance / AutomationOps).

Owner: AutomationOpsEngineer. Lane spec: ``CTO_WORKFLOWS.md`` -> "Goal-ledger
<-> board auto-sync lane" (extends goal-flip-at-merge / GOV-325, sequenced in
GOV-395). Run cadence + log/failure/retry contract live in
``AUTOMATION_OPS_WORKFLOWS.md`` -> "Goal-ledger <-> board auto-sync run".

Why this exists (GOV-394 incident)
-----------------------------------
The goal ledger (``goal.status``) drifts from the board (``issue.status``)
because nothing links an issue transition back to its ``goalId``. Isaac caught
the active-goals view showing Stage 2 active while work had reached Stage 3.10.
The CEO reconciled by hand; this deterministic control-plane script makes the
*safe* flips automatic and *proposes* (never auto-applies) the owner-gated ones.

This is a Paperclip control-plane tool (it reads/PATCHes goals + reads issues).
It touches NO Government-Watchdog crawler data, NO public surface, NO DB, NO AI.
It carries no PII and no record payload -- it reasons over goal/issue *status*
and *title* only.

The three flip rules
--------------------
1. **Subgoal ``planned`` -> ``active``** (AUTO): flip when >=1 issue with that
   ``goalId`` is ``in_progress`` or ``in_review``. Mirrors "work started."
2. **Numbered-stage parent ``active`` -> ``achieved``** (AUTO): flip when every
   *non-deferred* child subgoal is ``achieved``. A child is **deferred** iff its
   description matches ``DEFERRED to Stage \\d+ \\(Isaac-gated\\)`` -- deferred
   children are not-required and MUST NOT block the parent flip (Stage
   2.08/2.09/2.11). Numbered-stage parents in the always-active allowlist are
   never flipped (belt-and-suspenders; the allowlist holds cross-cutting goals,
   not numbered stages).
3. **Next numbered-stage parent ``planned`` -> ``active``** (PROPOSE-ONLY --
   *never* auto-PATCH): stage unlock is a CEO/owner gate and an Alpine-first hard
   stop. When the projected ledger (after rules 1+2) has ZERO active numbered
   stages, the lowest-numbered ``planned`` numbered stage is emitted as a "ready
   to unlock" recommendation for the CEO. ``--apply`` never touches rule 3.

Guardrails (CTO-owned, non-negotiable)
--------------------------------------
* **Always-active allowlist** never flipped to ``achieved`` by rule 2:
  HEAD ``5e8b8006``, governance ``59fd6f5e``, premium ``6834f0dd``,
  obsidian/backup ``a44b4936``, security-testing ``31744b17``,
  security-control ``527b9486``.
* **Dry-run is the default.** Mutation requires ``--apply``. ``--apply`` is a
  CEO-approved-plan gate (CTO hard stop): running it on the live ledger requires
  CEO approval routed by the CTO. Rule 3 is *never* executed under ``--apply``.
* **One numbered stage active at a time.** Rule 3 only proposes when zero
  numbered stages would be active; >1 active numbered stage is reported as an
  anomaly, never auto-resolved.
* **Idempotent.** A re-run over a synced ledger yields ZERO PATCHes.

Usage::

    # dry-run drift scan over the live ledger (default; no mutation)
    python scripts/governance/sync_goal_ledger.py

    # same, but exit 1 if any rule-1/rule-2 drift exists (CEO heartbeat audit gate)
    python scripts/governance/sync_goal_ledger.py --fail-on-drift

    # CEO-APPROVED apply (CTO hard stop -- never run unreviewed):
    python scripts/governance/sync_goal_ledger.py --apply

    # offline drift scan over captured JSON (no network)
    python scripts/governance/sync_goal_ledger.py --goals-file g.json --issues-file i.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# Control-plane constants (verified live, GOV-395 baseline).
# --------------------------------------------------------------------------- #
COMPANY_ID = "bcac096e-4aff-4ce3-ad33-c4e0b693b36f"
DEFAULT_BASE_URL = "http://127.0.0.1:3100"
DEFAULT_LOG_PATH = "Logs/governance/sync_goal_ledger.log"

# Cross-cutting / company-level program goals: never flipped to achieved by
# rule 2. Numbered stages are never members; this is a belt-and-suspenders guard.
# Matched against the first id segment (see ``_short``) so the list stays exact
# and greppable.
ALWAYS_ACTIVE = frozenset(
    {
        "5e8b8006",  # HEAD goal
        "59fd6f5e",  # goal/spec governance
        "6834f0dd",  # premium
        "a44b4936",  # obsidian / backup
        "31744b17",  # security testing
        "527b9486",  # security / publication control
    }
)

# An issue in one of these board states means "work started" on its goal.
WORK_STARTED = frozenset({"in_progress", "in_review"})

NUMBERED_STAGE_RE = re.compile(r"^Stage\s+(\d+)\b")
SUBGOAL_RE = re.compile(r"^Stage\s+(\d+)\.(\d+)\b")
DEFERRED_RE = re.compile(r"DEFERRED to Stage \d+ \(Isaac-gated\)")


def _short(goal_id: str) -> str:
    """First allowlist-comparable segment of a goal id."""
    return (goal_id or "").split("-")[0]


# --------------------------------------------------------------------------- #
# Goal classification (pure).
# --------------------------------------------------------------------------- #
def is_numbered_stage(goal: dict) -> bool:
    """A numbered-stage *parent* goal: ``Stage N — ...`` but not ``Stage N.M``."""
    title = goal.get("title") or ""
    return bool(NUMBERED_STAGE_RE.match(title)) and not SUBGOAL_RE.match(title)


def is_subgoal(goal: dict) -> bool:
    """A numbered subgoal: ``Stage N.M — ...``."""
    return bool(SUBGOAL_RE.match(goal.get("title") or ""))


def is_deferred(goal: dict) -> bool:
    """True iff the goal description carries the Isaac-gated DEFERRED banner."""
    return bool(DEFERRED_RE.search(goal.get("description") or ""))


def is_always_active(goal: dict) -> bool:
    return _short(goal.get("id", "")) in ALWAYS_ACTIVE


def stage_number(goal: dict) -> Optional[int]:
    """Leading stage integer for a numbered stage / subgoal, else ``None``."""
    m = NUMBERED_STAGE_RE.match(goal.get("title") or "")
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Report data model.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Flip:
    """An intended ``goal.status`` transition (rules 1 and 2 only)."""

    goal_id: str
    title: str
    from_status: str
    to_status: str
    rule: int
    reason: str

    def line(self) -> str:
        return (
            f"[rule {self.rule}] {_short(self.goal_id)} "
            f"{self.from_status} -> {self.to_status}  {self.title!r}  ({self.reason})"
        )


@dataclass(frozen=True)
class Recommendation:
    """A rule-3 propose-only "next stage ready to unlock" row. NEVER applied."""

    goal_id: str
    title: str
    from_status: str
    to_status: str
    reason: str

    def line(self) -> str:
        return (
            f"[rule 3 PROPOSE] {_short(self.goal_id)} "
            f"{self.from_status} -> {self.to_status}  {self.title!r}  ({self.reason})"
        )


@dataclass
class DriftReport:
    rule1_flips: list = field(default_factory=list)  # subgoal planned->active
    rule2_flips: list = field(default_factory=list)  # parent active->achieved
    rule3_recs: list = field(default_factory=list)  # propose-only
    anomalies: list = field(default_factory=list)  # human-readable warnings

    @property
    def auto_flips(self) -> list:
        """All flips eligible for ``--apply`` (rules 1+2). Rule 3 excluded."""
        return list(self.rule1_flips) + list(self.rule2_flips)

    @property
    def has_drift(self) -> bool:
        """True iff any AUTO (rule-1/rule-2) drift exists. Rule-3 recs do not count."""
        return bool(self.rule1_flips or self.rule2_flips)


# --------------------------------------------------------------------------- #
# Pure drift computation.
# --------------------------------------------------------------------------- #
def compute_drift(goals: Iterable[dict], issues: Iterable[dict]) -> DriftReport:
    """Compute all three rules over a ``(goals, issues)`` snapshot. No I/O."""
    goals = list(goals)
    issues = list(issues)
    report = DriftReport()

    # Index goals that have >=1 "work started" issue.
    goal_started: set = set()
    for issue in issues:
        gid = issue.get("goalId")
        if gid and issue.get("status") in WORK_STARTED:
            goal_started.add(gid)

    # ---- Rule 1: subgoal planned -> active when work started ---------------- #
    for goal in goals:
        if not is_subgoal(goal):
            continue
        if goal.get("status") != "planned":
            continue
        if goal["id"] in goal_started:
            report.rule1_flips.append(
                Flip(
                    goal_id=goal["id"],
                    title=goal.get("title", ""),
                    from_status="planned",
                    to_status="active",
                    rule=1,
                    reason=">=1 issue in_progress/in_review",
                )
            )

    # ---- Rule 2: numbered-stage parent active -> achieved ------------------- #
    children_by_parent: dict = {}
    for goal in goals:
        pid = goal.get("parentId")
        if pid:
            children_by_parent.setdefault(pid, []).append(goal)

    rule2_flipped_ids: set = set()
    for goal in goals:
        if not is_numbered_stage(goal):
            continue
        if goal.get("status") != "active":
            continue
        if is_always_active(goal):
            # Cross-cutting program goal -- never auto-achieved (belt+braces).
            continue
        children = [c for c in children_by_parent.get(goal["id"], []) if is_subgoal(c)]
        non_deferred = [c for c in children if not is_deferred(c)]
        if not non_deferred:
            # No required children to gate on -- do not auto-flip; surface it.
            report.anomalies.append(
                f"numbered stage {_short(goal['id'])} {goal.get('title','')!r} is "
                f"active with no non-deferred child subgoals; not auto-flipping."
            )
            continue
        if all(c.get("status") == "achieved" for c in non_deferred):
            n_def = len(children) - len(non_deferred)
            report.rule2_flips.append(
                Flip(
                    goal_id=goal["id"],
                    title=goal.get("title", ""),
                    from_status="active",
                    to_status="achieved",
                    rule=2,
                    reason=(
                        f"all {len(non_deferred)} non-deferred children achieved"
                        + (f"; {n_def} deferred child(ren) ignored" if n_def else "")
                    ),
                )
            )
            rule2_flipped_ids.add(goal["id"])

    # ---- Rule 3: propose next stage (NEVER applied) ------------------------- #
    numbered = [g for g in goals if is_numbered_stage(g)]
    # Active numbered stages in the *projected* ledger (after rule-2 flips).
    projected_active = [
        g
        for g in numbered
        if g.get("status") == "active" and g["id"] not in rule2_flipped_ids
    ]
    if len(projected_active) > 1:
        report.anomalies.append(
            "more than one numbered stage would be active: "
            + ", ".join(f"{_short(g['id'])} {g.get('title','')!r}" for g in projected_active)
            + " -- one-active-stage invariant violated; not auto-resolved."
        )
    elif len(projected_active) == 0:
        planned_numbered = sorted(
            (g for g in numbered if g.get("status") == "planned"),
            key=lambda g: (stage_number(g) if stage_number(g) is not None else 1 << 30),
        )
        if planned_numbered:
            nxt = planned_numbered[0]
            report.rule3_recs.append(
                Recommendation(
                    goal_id=nxt["id"],
                    title=nxt.get("title", ""),
                    from_status="planned",
                    to_status="active",
                    reason=(
                        "no numbered stage active after projected rule-1/rule-2 "
                        "flips; lowest-numbered planned stage is next. CEO unlock gate."
                    ),
                )
            )

    return report


# --------------------------------------------------------------------------- #
# Apply layer. Receives ONLY rules 1+2 (report.auto_flips); rule 3 never reaches it.
# --------------------------------------------------------------------------- #
def apply_flips(
    flips: Iterable[Flip],
    patch_fn: Callable[[str, str], None],
    log: Callable[[str, str], None],
) -> list:
    """Execute each AUTO flip via ``patch_fn(goal_id, to_status)``.

    Returns the list of applied flips. ``patch_fn`` raising aborts that flip but
    is recorded by the caller's logger; we re-raise so a transient failure is
    visible (the run is idempotent, so a retry is safe).
    """
    applied = []
    for flip in flips:
        log("INFO", f"PATCH goal {_short(flip.goal_id)} status={flip.to_status}")
        patch_fn(flip.goal_id, flip.to_status)
        applied.append(flip)
    return applied


# --------------------------------------------------------------------------- #
# HTTP layer (stdlib urllib -- zero third-party deps so it runs under bare python3).
# --------------------------------------------------------------------------- #
def _get_json(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
        return json.loads(resp.read().decode("utf-8"))


def fetch_goals(base_url: str, company_id: str) -> list:
    return _get_json(f"{base_url}/api/companies/{company_id}/goals")


def fetch_issues(base_url: str, company_id: str) -> list:
    # NOTE: /api/issues returns [] -- the company-scoped endpoint is correct.
    return _get_json(f"{base_url}/api/companies/{company_id}/issues")


def patch_goal_status(base_url: str, goal_id: str, status: str, timeout: float = 30.0):
    payload = json.dumps({"status": status}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/goals/{goal_id}",
        data=payload,
        method="PATCH",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# Logging.
# --------------------------------------------------------------------------- #
def make_logger(log_path: Optional[Path], echo: bool = True) -> Callable[[str, str], None]:
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(level: str, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {message}"
        if log_path is not None:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if echo:
            print(line)

    return log


# --------------------------------------------------------------------------- #
# Report rendering.
# --------------------------------------------------------------------------- #
def render_report(report: DriftReport) -> str:
    lines = []
    lines.append("=== Goal-ledger <-> board drift report ===")
    lines.append(f"rule-1 (subgoal planned->active)   : {len(report.rule1_flips)}")
    for f in report.rule1_flips:
        lines.append("    " + f.line())
    lines.append(f"rule-2 (parent active->achieved)   : {len(report.rule2_flips)}")
    for f in report.rule2_flips:
        lines.append("    " + f.line())
    lines.append(f"rule-3 (propose next stage unlock) : {len(report.rule3_recs)}  [PROPOSE-ONLY]")
    for r in report.rule3_recs:
        lines.append("    " + r.line())
    if report.anomalies:
        lines.append(f"anomalies                          : {len(report.anomalies)}")
        for a in report.anomalies:
            lines.append("    [!] " + a)
    auto = len(report.auto_flips)
    lines.append(
        f"--> {auto} AUTO flip(s) "
        + ("would be applied with --apply" if auto else "(ledger in sync)")
        + f"; {len(report.rule3_recs)} CEO recommendation(s)."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Goal-ledger <-> board auto-sync (GOV-396). Dry-run by default."
    )
    p.add_argument("--apply", action="store_true",
                   help="Execute rule-1/rule-2 PATCHes. CEO-approved-plan gate (CTO hard stop). "
                        "Rule 3 is never applied.")
    p.add_argument("--fail-on-drift", action="store_true",
                   help="Exit 1 if any rule-1/rule-2 drift exists (CEO heartbeat audit gate). "
                        "Rule-3 recommendations do not count as drift.")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help=f"Paperclip control-plane base URL (default {DEFAULT_BASE_URL}).")
    p.add_argument("--company-id", default=COMPANY_ID,
                   help="Company id (default Government Watchdog).")
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH,
                   help=f"Run log path (default {DEFAULT_LOG_PATH}). Use '' to disable file logging.")
    p.add_argument("--goals-file", default=None,
                   help="Read goals from this JSON file instead of the network (offline scan).")
    p.add_argument("--issues-file", default=None,
                   help="Read issues from this JSON file instead of the network (offline scan).")
    p.add_argument("--json", action="store_true",
                   help="Emit a machine-readable JSON summary instead of the text report.")
    return p


def _report_to_dict(report: DriftReport) -> dict:
    def flip_d(f):
        return {"goal_id": f.goal_id, "title": f.title, "from": f.from_status,
                "to": f.to_status, "rule": f.rule, "reason": f.reason}

    def rec_d(r):
        return {"goal_id": r.goal_id, "title": r.title, "from": r.from_status,
                "to": r.to_status, "reason": r.reason}

    return {
        "rule1_flips": [flip_d(f) for f in report.rule1_flips],
        "rule2_flips": [flip_d(f) for f in report.rule2_flips],
        "rule3_recommendations": [rec_d(r) for r in report.rule3_recs],
        "anomalies": list(report.anomalies),
        "auto_flip_count": len(report.auto_flips),
        "has_drift": report.has_drift,
    }


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    log_path = Path(args.log_path) if args.log_path else None
    # In --json mode keep stdout clean for the JSON; still write the file log.
    log = make_logger(log_path, echo=not args.json)

    try:
        if args.goals_file and args.issues_file:
            goals = json.loads(Path(args.goals_file).read_text(encoding="utf-8"))
            issues = json.loads(Path(args.issues_file).read_text(encoding="utf-8"))
            log("INFO", f"loaded {len(goals)} goals / {len(issues)} issues from files")
        else:
            goals = fetch_goals(args.base_url, args.company_id)
            issues = fetch_issues(args.base_url, args.company_id)
            log("INFO", f"fetched {len(goals)} goals / {len(issues)} issues from {args.base_url}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log("ERROR", f"failed to load control-plane data: {exc}")
        return 2

    report = compute_drift(goals, issues)

    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2))
    else:
        print(render_report(report))
    log("INFO",
        f"drift: rule1={len(report.rule1_flips)} rule2={len(report.rule2_flips)} "
        f"rule3_recs={len(report.rule3_recs)} anomalies={len(report.anomalies)}")

    if args.apply:
        if not report.auto_flips:
            log("INFO", "--apply: no AUTO flips to apply; ledger already in sync.")
        else:
            log("WARN", f"--apply: executing {len(report.auto_flips)} AUTO flip(s). "
                        "(CEO-approved-plan gate.)")
            apply_flips(
                report.auto_flips,
                lambda gid, status: patch_goal_status(args.base_url, gid, status),
                log,
            )
            log("INFO", "--apply complete. Re-run to confirm idempotency (expect 0 flips).")
        if report.rule3_recs:
            log("INFO", f"{len(report.rule3_recs)} rule-3 recommendation(s) NOT applied "
                        "(propose-only; CEO unlock gate).")

    if args.fail_on_drift and report.has_drift:
        log("ERROR", "drift detected (rule-1/rule-2); ledger out of sync.")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
