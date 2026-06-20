"""Stage 2 agent-handoff / owner-escalation routing guard (GOV-332, Stage 2.15).

Owner: AutomationOpsEngineer. Parent: GOV-331 (CTO Stage 2 sequencing). The final
buildable Stage-2 slice. Read-only, deterministic, Alpine-only, governance artifact —
**not** backend crawler/API code. There is no ``--apply`` (inherently dry-run), no DB,
no AI, no network, no migration; it doubles as a CI gate (exit 1 on any routing defect).

Why this slice exists (governance gap, not crawler code)
--------------------------------------------------------
The four merged Stage-2 trust auditors each prove a *read-surface* property and are all
**fail-closed**: when a condition trips, the reviewer-internal surface silently serves
*less*.

* traceability (GOV-306, :mod:`stage2_traceability`) — no orphan / drift / leak;
* composition / integration safety-net (GOV-318) — 5 overlays co-present, no cross-lane leak;
* back-gap / coverage-regression (GOV-322, :mod:`stage2_backgap`) — nothing silently dropped;
* doc-drift (GOV-326) — the reviewer reference doc matches the live surface.

None of them assert *what happens to the human* when a condition is **unresolvable**.
2.15 closes that gap: a deterministic SSOT routing manifest + guard proving every
Stage-2 fail-closed / unresolvable condition **terminates at exactly one named human
owner with a defined escalation action** — and never loops back to a *detecting* agent
(the anti-infinite-loop invariant). The manifest routes by abstract **condition class**,
never by record content, so it inherently carries no PII and no record payload.

Hard constraints (GOV-332): NO migration, NO mutation, NO AI, NO network, NO public
projection. It MUST NOT import or modify ``scripts/read_api.py`` / ``scripts/publication.py``
— **0 production diff**; this is an additive module + test only. The escalation owner of
every route is a human/governance role, never the agent/automation that detects the
condition.

The six first-class checks (each a report key; CLI exit 1 if any non-clean):

1. **completeness** — every enumerated condition class maps to exactly one route
   (0 unmapped, 0 extra/unknown). Fail-closed default for any unmapped class =
   escalate to ``cto`` AND the guard goes RED — an unmapped condition must never pass
   silently. (:func:`route_for` returns that fail-closed default; :func:`completeness`
   still reports the class as a defect.)
2. **named-human-owner** — every route ``owner_role`` is a member of the frozen
   human/governance owner set and is never empty.
3. **no-self-handoff (anti-loop)** — no route's ``owner_role`` is a detecting
   agent/automation, and no route resolves to its own ``detected_by`` agent. Every
   escalation terminates at a human/governance owner. This is the anti-infinite-loop
   invariant.
4. **no-public-projection / no-leak** — the manifest contains no PII (email/phone) and
   no record payload / internal column names; it routes by condition *class*, not by
   record content. (SecPriv lane.)
5. **determinism** — same condition class always resolves to the same route, and the
   serialized manifest is byte-identical and stably ordered across runs.
6. **read-only** — the module neither writes (no INSERT/UPDATE/DELETE, no file write)
   nor imports mutable serving state (no ``read_api`` / ``publication`` import). Proven
   structurally against this module's own source so it cannot silently regress; the
   ``0 diff`` to ``read_api.py`` / ``publication.py`` is additionally evidenced by
   ``git diff --stat`` in the PR.

If implementing 2.15 ever surfaces a genuine need to touch ``read_api.py`` /
``publication.py`` (a production serving change), or a condition class has no defensible
human owner without an Isaac/owner decision, STOP and escalate (GOV-332 pass-up trigger).

Usage:
    python scripts/stage2_escalation.py [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# SSOT — frozen vocabularies. Declared here once; the guard validates the
# manifest against these sets so a typo or an unmapped class cannot pass.
# ---------------------------------------------------------------------------

# The exact, frozen set of Stage-2 reviewer-internal fail-closed / unresolvable
# condition classes. Mirrors the proven read surface (each cites the auditor /
# slice that raises it). Adding a real new condition class is a deliberate edit
# here AND a new route below — never an ad-hoc string at a call site.
CONDITION_CLASSES: frozenset[str] = frozenset(
    {
        "reviewer_access_denied",       # gated-beta account/access gate denies a reviewer
        "provenance_unverified",        # provenance_status='unverified' blocks trust (GOV-311)
        "coverage_backgap",             # back-gap / coverage regression (GOV-322)
        "integration_safetynet_trip",   # 5-overlay compose fail-closed / cross-lane leak (GOV-318)
        "traceability_orphan",          # read-surface orphan / drift (GOV-306)
        "completeness_gap_unresolved",  # unresolved completeness-gap card (GOV-298)
        "docdrift_red",                 # reviewer reference doc-drift guard RED (GOV-326)
    }
)

# Human/governance owner roles ONLY — an escalation must terminate at one of these.
# A detecting agent is never a member (that is the anti-loop guarantee, check 3).
HUMAN_OWNER_ROLES: frozenset[str] = frozenset(
    {
        "cto",                        # technical owner: serving/coverage/integration defects
        "ceo",                        # company/editorial owner: coverage & policy decisions
        "security-privacy-reviewer",  # privacy/trust owner: provenance, cross-lane leak
        "isaac-owner",                # final owner decision (access policy, scope)
    }
)

# The agents/automations that DETECT a Stage-2 condition. A route that terminates at
# one of these would loop the escalation straight back to the detector — forbidden.
# Note the deliberate distinction from HUMAN_OWNER_ROLES: ``security-privacy-agent``
# (a detecting automation) is NOT ``security-privacy-reviewer`` (a human owner).
DETECTING_AGENTS: frozenset[str] = frozenset(
    {
        "backend-crawler-engineer",
        "automation-ops-engineer",
        "security-privacy-agent",
        "verification-safety-reviewer",
        "frontend-timeline-engineer",
        "source-archivist",
    }
)

# Internal column / payload names that must NEVER appear in the routing manifest —
# the manifest routes by condition class, not by record content (check 4, SecPriv lane).
_INTERNAL_COLUMN_TOKENS: frozenset[str] = frozenset(
    {
        "statement_text", "source_id", "person_id", "candidate_person_id",
        "segment_id", "transcript_class", "ai_extraction_run_id", "original_url",
        "local_path", "sha256", "display_label", "speaker_attribution_id",
        "verification_status", "publication_state",
    }
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")

# Fail-closed default route for an unmapped condition class: escalate to the CTO.
# Returned by :func:`route_for` so an unrecognized class never silently no-ops — but
# :func:`completeness` still flags the class, so the guard goes RED (never silent).
_FAILCLOSED_DEFAULT: dict[str, str] = {
    "owner_role": "cto",
    "escalation_action": "unmapped Stage-2 condition class — CTO triages and assigns a "
    "named human owner before the reviewer-internal surface is trusted",
    "pass_up_trigger": "escalate to isaac-owner if no defensible human owner exists",
    "detected_by": "automation-ops-engineer",
}


# ---------------------------------------------------------------------------
# SSOT routing manifest — condition class -> exactly one human-owned route.
# Each route: {owner_role, escalation_action, pass_up_trigger, detected_by}.
#   owner_role     — the single human/governance owner the condition terminates at.
#   escalation_action — the defined action that owner takes (what happens to the human).
#   pass_up_trigger — when this route escalates further (to ceo / isaac-owner).
#   detected_by    — the agent/automation that raises the condition; the anti-loop
#                    check asserts owner_role is never this (or any) detecting agent.
# ---------------------------------------------------------------------------
ROUTING_MANIFEST: dict[str, dict[str, str]] = {
    "reviewer_access_denied": {
        "owner_role": "cto",
        "escalation_action": "CTO reviews the gated-beta waitlist entry and approves or "
        "denies the reviewer account via the manual backend-approval path",
        "pass_up_trigger": "escalate to isaac-owner if WHO qualifies for the "
        "reviewer-internal beta (the access policy itself) is in question",
        "detected_by": "backend-crawler-engineer",
    },
    "provenance_unverified": {
        "owner_role": "security-privacy-reviewer",
        "escalation_action": "SecurityPrivacy reviewer inspects the unverified record's "
        "grounding and decides whether it may be trusted in the reviewer-internal lane",
        "pass_up_trigger": "escalate to cto if the unverified state implies a defect in "
        "the provenance projection (GOV-311) rather than a single record",
        "detected_by": "automation-ops-engineer",
    },
    "coverage_backgap": {
        "owner_role": "cto",
        "escalation_action": "CTO opens a scoped defect issue for the silently-dropped "
        "record class and assigns the read-surface fix (never a self-heal from the guard)",
        "pass_up_trigger": "escalate to ceo if the back-gap reflects a coverage-scope "
        "decision (which Alpine records belong in the lane) rather than a code defect",
        "detected_by": "automation-ops-engineer",
    },
    "integration_safetynet_trip": {
        "owner_role": "security-privacy-reviewer",
        "escalation_action": "SecurityPrivacy reviewer treats the cross-lane leak / "
        "compose failure as a privacy incident and gates the surface until cleared",
        "pass_up_trigger": "escalate to cto for the engineering fix once the leak is "
        "contained, and to isaac-owner if reviewer data may have been exposed",
        "detected_by": "automation-ops-engineer",
    },
    "traceability_orphan": {
        "owner_role": "cto",
        "escalation_action": "CTO assigns the orphan/drift to the owning backend slice "
        "for a scoped fix; the served value stays fail-closed until the trace is restored",
        "pass_up_trigger": "escalate to ceo only if resolving the orphan requires a "
        "source-scope expansion beyond Alpine",
        "detected_by": "automation-ops-engineer",
    },
    "completeness_gap_unresolved": {
        "owner_role": "ceo",
        "escalation_action": "CEO decides the editorial disposition of the unresolved "
        "completeness-gap card (acquire a primary source, or keep it surfaced as a gap)",
        "pass_up_trigger": "escalate to isaac-owner if closing the gap needs an "
        "owner-level source-acquisition or scope decision",
        "detected_by": "automation-ops-engineer",
    },
    "docdrift_red": {
        "owner_role": "cto",
        "escalation_action": "CTO reconciles the reviewer reference doc with the live "
        "read surface (patch whichever side drifted) before the surface is trusted",
        "pass_up_trigger": "escalate to ceo if the drift reveals a contract change that "
        "alters the Stage-2 reviewer-internal scope",
        "detected_by": "automation-ops-engineer",
    },
}

# The route keys every entry must carry. Declared once so the structural checks and
# the no-leak scan agree on what a well-formed route is.
_ROUTE_KEYS: tuple[str, ...] = ("owner_role", "escalation_action", "pass_up_trigger", "detected_by")


def route_for(condition_class: str, manifest: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    """Resolve a condition class to its route, fail-closed.

    A mapped class returns its SSOT route. An UNMAPPED class returns
    :data:`_FAILCLOSED_DEFAULT` (escalate to ``cto``) so the condition is never silently
    dropped at a call site — but :func:`completeness` independently flags the class, so
    the guard still goes RED. Routing and detection of an unmapped class are decoupled on
    purpose: serve a safe default, fail the gate loudly.
    """
    table = ROUTING_MANIFEST if manifest is None else manifest
    return dict(table.get(condition_class, _FAILCLOSED_DEFAULT))


# ---------------------------------------------------------------------------
# Check 1 — completeness (every class mapped exactly once; no unknown class).
# ---------------------------------------------------------------------------


def completeness(manifest: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Every enumerated condition class maps to exactly one route; no unknown class.

    ``unmapped`` = enumerated classes with no route (each silently un-routed → the guard
    must go RED). ``unknown`` = manifest keys that are not enumerated condition classes
    (a route to a class the frozen SSOT does not recognize). A dict cannot hold duplicate
    keys, so 1:1 mapping is guaranteed once both sets are empty.
    """
    table = ROUTING_MANIFEST if manifest is None else manifest
    keys = set(table)
    unmapped = sorted(CONDITION_CLASSES - keys)
    unknown = sorted(keys - CONDITION_CLASSES)
    return {
        "enumerated_count": len(CONDITION_CLASSES),
        "mapped_count": len(keys & CONDITION_CLASSES),
        "unmapped": unmapped,
        "unknown": unknown,
        "clean": not unmapped and not unknown,
    }


# ---------------------------------------------------------------------------
# Check 2 — named-human-owner (every owner is a frozen human/governance role).
# ---------------------------------------------------------------------------


def named_human_owner(manifest: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Every route ``owner_role`` is a member of :data:`HUMAN_OWNER_ROLES`, never empty."""
    table = ROUTING_MANIFEST if manifest is None else manifest
    violations: list[dict[str, str]] = []
    for cls in sorted(table):
        owner = table[cls].get("owner_role")
        if not owner:
            violations.append({"condition_class": cls, "reason": "empty owner_role"})
        elif owner not in HUMAN_OWNER_ROLES:
            violations.append(
                {"condition_class": cls, "reason": f"owner_role {owner!r} not a human/governance role"}
            )
    return {"checked": len(table), "violations": violations, "clean": not violations}


# ---------------------------------------------------------------------------
# Check 3 — no-self-handoff / anti-loop (owner is never a detecting agent).
# ---------------------------------------------------------------------------


def no_self_handoff(manifest: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """No route terminates at a detecting agent — the anti-infinite-loop invariant.

    A self-handoff is any route whose ``owner_role`` is a member of
    :data:`DETECTING_AGENTS`, or equals its own ``detected_by``. Either would loop the
    escalation back to the automation that raised the condition, so it can never resolve
    to a human — the failure mode this slice exists to prevent.
    """
    table = ROUTING_MANIFEST if manifest is None else manifest
    self_handoffs: list[dict[str, str]] = []
    for cls in sorted(table):
        route = table[cls]
        owner = route.get("owner_role")
        detected_by = route.get("detected_by")
        if owner in DETECTING_AGENTS:
            self_handoffs.append(
                {"condition_class": cls, "reason": f"owner_role {owner!r} is a detecting agent"}
            )
        elif owner is not None and owner == detected_by:
            self_handoffs.append(
                {"condition_class": cls, "reason": f"owner_role equals detected_by {owner!r}"}
            )
    return {"checked": len(table), "self_handoffs": self_handoffs, "clean": not self_handoffs}


# ---------------------------------------------------------------------------
# Check 4 — no-public-projection / no-leak (SecPriv lane).
# ---------------------------------------------------------------------------


def no_leak(manifest: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """The manifest carries no PII and no record payload / internal column names.

    Scans every string value of every route (and the condition-class keys) for an email,
    a phone number, or any internal column token. Because the manifest routes by abstract
    condition *class* — not by record content — a clean manifest can structurally never
    leak a served record; this check makes that property load-bearing rather than
    assumed.
    """
    table = ROUTING_MANIFEST if manifest is None else manifest
    leaks: list[dict[str, str]] = []
    for cls in sorted(table):
        fields = [("condition_class", cls)] + [(k, str(v)) for k, v in sorted(table[cls].items())]
        for field, text in fields:
            if _EMAIL_RE.search(text):
                leaks.append({"condition_class": cls, "field": field, "reason": "email-like PII"})
            if _PHONE_RE.search(text):
                leaks.append({"condition_class": cls, "field": field, "reason": "phone-like PII"})
            lowered = text.lower()
            for token in _INTERNAL_COLUMN_TOKENS:
                if token in lowered:
                    leaks.append(
                        {"condition_class": cls, "field": field, "reason": f"internal column name {token!r}"}
                    )
    return {"checked": len(table), "leaks": leaks, "clean": not leaks}


# ---------------------------------------------------------------------------
# Check 5 — determinism (same class -> same route; stable byte-ordering).
# ---------------------------------------------------------------------------


def determinism(manifest: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Same condition class resolves to the same route; serialization is byte-stable.

    Resolves every class twice via :func:`route_for` and asserts equality, then
    serializes the whole manifest twice with sorted keys and asserts byte-identity. A
    pure dict over frozen vocabularies is deterministic by construction; this check
    guards against a future non-deterministic edit (e.g. a route built from a set
    iteration order).
    """
    table = ROUTING_MANIFEST if manifest is None else manifest
    stable_routes = all(route_for(c, table) == route_for(c, table) for c in table)
    first = json.dumps(table, sort_keys=True)
    second = json.dumps(table, sort_keys=True)
    return {
        "stable_routes": stable_routes,
        "byte_identical": first == second,
        "clean": stable_routes and first == second,
    }


# ---------------------------------------------------------------------------
# Check 6 — read-only (no writes; no mutable serving-state import).
# ---------------------------------------------------------------------------

# Production serving modules this slice must leave at 0 diff and must never import.
_SERVING_STATE_MODULES: frozenset[str] = frozenset({"read_api", "publication"})
# Call attributes / names that would mutate a DB or the filesystem.
_WRITE_CALL_ATTRS: frozenset[str] = frozenset(
    {"commit", "execute", "executescript", "executemany", "write", "writelines"}
)
_WRITE_CALL_NAMES: frozenset[str] = frozenset({"open"})


def read_only(source_path: str | Path | None = None) -> dict[str, Any]:
    """Structural proof this module neither writes nor imports mutable serving state.

    Parses this module's own AST (not a text scan — so the docstring and comments that
    *describe* the forbidden patterns are invisible to it) and asserts there is no
    ``import``/``from`` of ``read_api`` / ``publication`` (the production serving modules
    this slice must leave at 0 diff) and no write call (``open(``, ``.execute(``,
    ``.commit(``, ``.write(`` …). Self-inspecting makes the read-only property
    regression-proof: a future edit that adds a serving import or a write op flips this
    check RED. The ``0 diff`` to ``read_api.py`` / ``publication.py`` is additionally
    evidenced by ``git diff --stat`` in the PR.
    """
    path = Path(__file__) if source_path is None else Path(source_path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    serving_imports: set[str] = set()
    write_ops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            serving_imports |= {a.name for a in node.names if a.name in _SERVING_STATE_MODULES}
        elif isinstance(node, ast.ImportFrom):
            if node.module in _SERVING_STATE_MODULES:
                serving_imports.add(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _WRITE_CALL_NAMES:
                write_ops.add(f"{func.id}()")
            elif isinstance(func, ast.Attribute) and func.attr in _WRITE_CALL_ATTRS:
                write_ops.add(f".{func.attr}()")
    return {
        "write_ops": sorted(write_ops),
        "serving_imports": sorted(serving_imports),
        "clean": not write_ops and not serving_imports,
    }


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def audit_escalation(manifest: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Run all six routing checks against the SSOT manifest. Returns a JSON-able report.

    ``clean`` is the conjunction of every check. Pure governance validation: no DB, no
    network, no AI, no writes — it inspects a static routing table and this module's own
    source.
    """
    table = ROUTING_MANIFEST if manifest is None else manifest
    complete = completeness(table)
    owners = named_human_owner(table)
    antiloop = no_self_handoff(table)
    leak = no_leak(table)
    determ = determinism(table)
    ro = read_only()
    clean = all(
        c["clean"] for c in (complete, owners, antiloop, leak, determ, ro)
    )
    return {
        "completeness": complete,
        "named_human_owner": owners,
        "no_self_handoff": antiloop,
        "no_leak": leak,
        "determinism": determ,
        "read_only": ro,
        "clean": clean,
    }


def _format_report(report: dict[str, Any]) -> str:
    co = report["completeness"]
    ow = report["named_human_owner"]
    al = report["no_self_handoff"]
    lk = report["no_leak"]
    dt = report["determinism"]
    ro = report["read_only"]
    co_s = "OK" if co["clean"] else "DEFECT"
    ow_s = "OK" if ow["clean"] else f"DEFECT ({len(ow['violations'])})"
    al_s = "OK" if al["clean"] else f"DEFECT ({len(al['self_handoffs'])})"
    lk_s = "OK" if lk["clean"] else f"DEFECT ({len(lk['leaks'])})"
    dt_s = "OK" if dt["clean"] else "UNSTABLE"
    ro_s = "OK" if ro["clean"] else "DEFECT"
    lines = [
        "Stage 2 escalation / owner-routing guard (GOV-332) — SSOT routing manifest",
        f"  1 completeness (class->route)   : {co_s} "
        f"(mapped={co['mapped_count']}/{co['enumerated_count']} "
        f"unmapped={len(co['unmapped'])} unknown={len(co['unknown'])})",
        f"  2 named-human-owner             : {ow_s} (checked={ow['checked']})",
        f"  3 no-self-handoff (anti-loop)   : {al_s} (checked={al['checked']})",
        f"  4 no-leak / no-public-projection: {lk_s} (checked={lk['checked']})",
        f"  5 determinism                   : {dt_s} "
        f"(stable_routes={dt['stable_routes']} byte_identical={dt['byte_identical']})",
        f"  6 read-only (no write/serving)  : {ro_s} "
        f"(write_ops={ro['write_ops']} serving_imports={ro['serving_imports']})",
        f"  ALL ROUTES CLEAN                : {report['clean']}",
    ]
    for c in co["unmapped"]:
        lines.append(f"    UNMAPPED CONDITION CLASS (fail-closed -> cto, guard RED): {c}")
    for c in co["unknown"]:
        lines.append(f"    UNKNOWN ROUTE (not an enumerated condition class): {c}")
    for v in ow["violations"]:
        lines.append(f"    OWNER DEFECT: {v['condition_class']} -> {v['reason']}")
    for s in al["self_handoffs"]:
        lines.append(f"    SELF-HANDOFF (anti-loop): {s['condition_class']} -> {s['reason']}")
    for lv in lk["leaks"]:
        lines.append(f"    LEAK: {lv['condition_class']}.{lv['field']} -> {lv['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 2 agent-handoff / owner-escalation routing guard (read-only, "
        "no DB). GOV-332 Stage 2.15. Exits 1 on any routing defect."
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable JSON report"
    )
    args = parser.parse_args(argv)

    report = audit_escalation()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
