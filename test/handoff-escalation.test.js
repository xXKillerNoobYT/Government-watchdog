import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  evaluateHandoff,
  evaluateEscalation,
  evaluateStageExit,
  evaluateHandoffSlice,
  renderHandoffVerdict,
  HANDOFF_VIOLATION_TYPES,
  OWNER_GATES,
  ROLE_SET,
  TRIGGER_SET,
  HANDOFF_VERDICT_TITLE,
} from "../src/handoff-escalation.js";

const here = dirname(fileURLToPath(import.meta.url));

// A small, legal role-transition model used by the unit tests. The full real
// model is loaded from Docs/handoff-escalation-model.json at the bottom.
function model() {
  return {
    version: "test-model-1",
    transitions: [
      { from: "impl", to: "VSR", trigger: "impl_complete", requiredEvidenceKinds: ["facts", "summaries"], preservesReviewStatus: true },
      { from: "VSR", to: "Security", trigger: "vsr_pass", requiredEvidenceKinds: ["facts"], preservesReviewStatus: true },
      { from: "Owner", to: "CTO", trigger: "owner_decision", requiredEvidenceKinds: [], preservesReviewStatus: false },
    ],
    ownerGates: OWNER_GATES,
  };
}

// A clean, legal handoff request that yields ZERO violations (decision "allow").
function request() {
  return {
    from: "impl",
    to: "VSR",
    trigger: "impl_complete",
    evidenceBundle: {
      facts: [{ id: "f1", sourceRef: "src/x.js" }],
      summaries: [{ id: "s1", text: "done" }],
      aiAssumptions: [],
      laterVerification: [],
    },
    reviewStatus: "impl_self_reviewed",
  };
}

function clone(x) {
  return JSON.parse(JSON.stringify(x));
}

// ── baseline ─────────────────────────────────────────────────────────────────

test("baseline legal handoff is allowed — zero violations, separationOk", () => {
  const h = evaluateHandoff({ model: model(), request: request() });
  assert.equal(h.decision, "allow");
  assert.deepEqual(h.violations, []);
  assert.equal(h.preservedReviewStatus, "impl_self_reviewed");
  assert.equal(h.evidenceContract.separationOk, true);
});

// ── §4.1 every handoff-validation violation type ─────────────────────────────

test("illegal_transition (high) when (from,to,trigger) is not in the model", () => {
  const r = request();
  r.to = "CTO"; // impl --impl_complete--> CTO is not modeled
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "illegal_transition");
  assert.ok(v);
  assert.equal(v.severity, "high");
  assert.equal(h.decision, "deny");
});

test("missing_required_evidence (high) when a required bucket is empty", () => {
  const r = request();
  r.evidenceBundle.summaries = []; // impl_complete requires facts + summaries
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "missing_required_evidence");
  assert.ok(v);
  assert.equal(v.severity, "high");
  assert.deepEqual(v.evidence.missingEvidenceKinds, ["summaries"]);
  assert.equal(h.decision, "deny");
});

test("ai_assumption_as_fact (high) when an aiAssumptions item is promoted to facts", () => {
  const r = request();
  const shared = { id: "a1", sourceRef: "src/x.js", text: "guess" };
  r.evidenceBundle.facts.push(shared);
  r.evidenceBundle.aiAssumptions.push(shared);
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "ai_assumption_as_fact");
  assert.ok(v);
  assert.equal(v.severity, "high");
  assert.equal(h.evidenceContract.separationOk, false);
  assert.equal(h.decision, "deny");
});

test("ai_assumption_as_fact also fires when a fact is AI-labeled in place", () => {
  const r = request();
  r.evidenceBundle.facts.push({ id: "f2", sourceRef: "src/x.js", aiDerived: true });
  const h = evaluateHandoff({ model: model(), request: r });
  assert.ok(h.violations.find((x) => x.type === "ai_assumption_as_fact"));
});

test("fact_without_source (high) when a facts item lacks a sourceRef", () => {
  const r = request();
  r.evidenceBundle.facts.push({ id: "f3" }); // no sourceRef
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "fact_without_source");
  assert.ok(v);
  assert.equal(v.severity, "high");
  assert.equal(h.decision, "deny");
});

test("bucket_cross_contamination (high) when buckets are not disjoint", () => {
  const r = request();
  const shared = { id: "dup1", text: "shared" };
  r.evidenceBundle.summaries.push(shared);
  r.evidenceBundle.laterVerification.push(shared); // summaries <-> laterVerification
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "bucket_cross_contamination");
  assert.ok(v);
  assert.equal(v.severity, "high");
  assert.equal(h.evidenceContract.separationOk, false);
});

test("review_status_dropped (medium) when a preserving transition drops review status", () => {
  const r = request();
  r.reviewStatus = ""; // transition preservesReviewStatus
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "review_status_dropped");
  assert.ok(v);
  assert.equal(v.severity, "medium");
  assert.equal(h.decision, "deny"); // any violation denies (§4)
});

test("unknown_trigger (medium) when trigger is outside the enumerated set", () => {
  const r = request();
  r.trigger = "totally_made_up";
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "unknown_trigger");
  assert.ok(v);
  assert.equal(v.severity, "medium");
  // also illegal_transition (no model match) → still deny
  assert.equal(h.decision, "deny");
});

test("unrecognized_role (medium) when from/to is outside the role set", () => {
  const r = request();
  r.from = "Marketing";
  const h = evaluateHandoff({ model: model(), request: r });
  const v = h.violations.find((x) => x.type === "unrecognized_role");
  assert.ok(v);
  assert.equal(v.severity, "medium");
  assert.ok(v.detail.includes("Marketing"));
});

test("owner_decision transition does not require a carried review status", () => {
  const r = {
    from: "Owner",
    to: "CTO",
    trigger: "owner_decision",
    evidenceBundle: { facts: [], summaries: [], aiAssumptions: [], laterVerification: [] },
    reviewStatus: "",
  };
  const h = evaluateHandoff({ model: model(), request: r });
  assert.equal(h.decision, "allow");
  assert.deepEqual(h.violations, []);
});

// ── §4.2 escalation — both outcomes incl. fail-closed ────────────────────────

test("escalation required when conditionKind matches an owner gate", () => {
  const e = evaluateEscalation({ triggerSet: OWNER_GATES, action: { conditionKind: "publication" } });
  assert.equal(e.decision, "required");
  assert.deepEqual(e.gatesTriggered, ["gate_publication"]);
});

test("escalation required when a descriptorFlag trips a gate", () => {
  const e = evaluateEscalation({
    triggerSet: OWNER_GATES,
    action: { conditionKind: "internal_handoff", descriptorFlags: { expandsScopeBeyondAlpine: true } },
  });
  assert.equal(e.decision, "required");
  assert.deepEqual(e.gatesTriggered, ["gate_scope_expansion"]);
});

test("escalation not_required for a recognized safe action with no gate flags", () => {
  const e = evaluateEscalation({
    triggerSet: OWNER_GATES,
    action: { conditionKind: "internal_handoff", descriptorFlags: {} },
  });
  assert.equal(e.decision, "not_required");
  assert.deepEqual(e.gatesTriggered, []);
});

test("escalation FAIL-CLOSED to required on an unknown conditionKind", () => {
  const e = evaluateEscalation({ triggerSet: OWNER_GATES, action: { conditionKind: "something_weird" } });
  assert.equal(e.decision, "required");
  assert.ok(e.reason.includes("fail-closed"));
});

test("escalation FAIL-CLOSED to required on an unrecognized descriptorFlag", () => {
  const e = evaluateEscalation({
    triggerSet: OWNER_GATES,
    action: { conditionKind: "internal_handoff", descriptorFlags: { mysteryFlag: true } },
  });
  assert.equal(e.decision, "required");
  assert.ok(e.reason.includes("unrecognized descriptorFlag"));
});

test("escalation FAIL-CLOSED to required when no action descriptor is supplied", () => {
  const e = evaluateEscalation({ triggerSet: OWNER_GATES });
  assert.equal(e.decision, "required");
  assert.deepEqual(e.gatesTriggered, []);
});

test("escalation defaults to canonical OWNER_GATES when no triggerSet supplied", () => {
  const e = evaluateEscalation({ action: { conditionKind: "budget" } });
  assert.equal(e.decision, "required");
  assert.deepEqual(e.gatesTriggered, ["gate_budget"]);
});

// ── §4.3 stage-exit predicate ────────────────────────────────────────────────

test("stageExit ready when all non-deferred subgoals achieved, no open blocker", () => {
  const s = evaluateStageExit({
    snapshot: {
      subgoals: [
        { id: "5.13", status: "achieved" },
        { id: "5.14", status: "achieved" },
        { id: "5.15", status: "achieved" },
      ],
      handoffLedger: [],
    },
  });
  assert.equal(s.decision, "ready");
  assert.deepEqual(s.blockingSubgoals, []);
});

test("stageExit blocked when a non-deferred subgoal is not achieved", () => {
  const s = evaluateStageExit({
    snapshot: { subgoals: [{ id: "5.15", status: "in_progress" }], handoffLedger: [] },
  });
  assert.equal(s.decision, "blocked");
  assert.deepEqual(s.blockingSubgoals, ["5.15"]);
});

test("stageExit excludes deferred subgoals (5.08/5.09/5.11, Isaac-gated)", () => {
  const s = evaluateStageExit({
    snapshot: {
      subgoals: [
        { id: "5.15", status: "achieved" },
        { id: "5.08", status: "planned", deferred: true },
        { id: "5.09", status: "planned", note: "DEFERRED to Stage 6 (Isaac-gated)" },
      ],
      handoffLedger: [],
    },
  });
  assert.equal(s.decision, "ready", "deferred subgoals must not block the exit gate");
  assert.deepEqual(s.blockingSubgoals, []);
});

test("stageExit blocked by an open deny/required handoff in the ledger", () => {
  const s = evaluateStageExit({
    snapshot: {
      subgoals: [{ id: "5.15", status: "achieved" }],
      handoffLedger: [{ id: "h-9", handoffDecision: "deny", open: true }],
    },
  });
  assert.equal(s.decision, "blocked");
  assert.deepEqual(s.openHandoffBlockers, ["h-9"]);
});

test("evaluateStageExit returns null when no snapshot is supplied (never a silent ready)", () => {
  assert.equal(evaluateStageExit({}), null);
  assert.equal(evaluateStageExit({ snapshot: null }), null);
});

test("stageExit fail-closed to blocked when snapshot lacks a subgoals array", () => {
  const s = evaluateStageExit({ snapshot: { handoffLedger: [] } });
  assert.equal(s.decision, "blocked");
  assert.ok(s.reason.includes("fail-closed"));
});

// ── §4 full verdict composer ─────────────────────────────────────────────────

test("evaluateHandoffSlice composes handoff + escalation + stageExit", () => {
  const v = evaluateHandoffSlice({
    model: model(),
    request: request(),
    triggerSet: OWNER_GATES,
    action: { conditionKind: "internal_handoff", descriptorFlags: {} },
    snapshot: { subgoals: [{ id: "5.15", status: "achieved" }], handoffLedger: [] },
    now: "2026-06-26T00:00:00.000Z",
  });
  assert.equal(v.handoff.decision, "allow");
  assert.equal(v.escalation.decision, "not_required");
  assert.equal(v.stageExit.decision, "ready");
  assert.equal(v.generatedAt, "2026-06-26T00:00:00.000Z");
  assert.ok(v.summary.includes("handoff=allow"));
});

test("evaluateHandoffSlice omits stageExit when no snapshot is supplied", () => {
  const v = evaluateHandoffSlice({
    model: model(),
    request: request(),
    triggerSet: OWNER_GATES,
    action: { conditionKind: "internal_handoff" },
    now: "2026-06-26T00:00:00.000Z",
  });
  assert.equal("stageExit" in v, false);
  assert.ok(v.summary.includes("stageExit=(absent)"));
});

// ── §5 behavioral invariants ─────────────────────────────────────────────────

test("read-only + idempotent: identical inputs → deep-equal verdicts, inputs unmutated", () => {
  const m = model();
  const r = request();
  r.evidenceBundle.facts.push({ id: "f9" }); // provoke fact_without_source
  const before = JSON.stringify({ m, r });
  const v1 = evaluateHandoffSlice({ model: m, request: r, triggerSet: OWNER_GATES, action: { conditionKind: "publication" }, now: "T" });
  const v2 = evaluateHandoffSlice({ model: m, request: r, triggerSet: OWNER_GATES, action: { conditionKind: "publication" }, now: "T" });
  assert.deepEqual(v1, v2);
  assert.equal(JSON.stringify({ m, r }), before, "inputs must not be mutated");
});

test("deterministic order: severity desc, then type, then subjectId", () => {
  const r = request();
  r.reviewStatus = "";                                  // review_status_dropped (medium)
  r.evidenceBundle.facts.push({ id: "f4" });            // fact_without_source (high)
  const h = evaluateHandoff({ model: model(), request: r });
  const ranks = h.violations.map((v) => ({ high: 0, medium: 1, low: 2 }[v.severity]));
  for (let i = 1; i < ranks.length; i += 1) {
    assert.ok(ranks[i - 1] <= ranks[i], "severity must be non-increasing");
  }
  assert.equal(h.violations[0].severity, "high");
});

test("fail-closed: absent clock is echoed explicitly, never the wall clock", () => {
  const v = evaluateHandoffSlice({ model: model(), request: request(), triggerSet: OWNER_GATES, action: { conditionKind: "none" } });
  assert.equal(v.generatedAt, "(now-not-supplied)");
});

test("no AI assumption is ever promotable to a fact (separation hardness)", () => {
  const r = request();
  // Even with a confident-looking AI label, it must NOT become an allowed fact.
  r.evidenceBundle.facts.push({ id: "fX", sourceRef: "src/x.js", kind: "ai_assumption" });
  const h = evaluateHandoff({ model: model(), request: r });
  assert.equal(h.decision, "deny");
  assert.ok(h.violations.find((x) => x.type === "ai_assumption_as_fact"));
  assert.equal(h.evidenceContract.separationOk, false);
});

test("every enumerated violation type has a fixed, sane severity", () => {
  const allowed = new Set(["high", "medium", "low"]);
  for (const [type, sev] of Object.entries(HANDOFF_VIOLATION_TYPES)) {
    assert.ok(allowed.has(sev), `${type} severity must be high|medium|low`);
  }
});

test("role + trigger constant sets match the contract enumerations", () => {
  assert.ok(ROLE_SET.includes("impl") && ROLE_SET.includes("Owner"));
  assert.equal(TRIGGER_SET.length, 10);
  assert.ok(TRIGGER_SET.includes("future_fact_verified"));
});

// ── renderer ─────────────────────────────────────────────────────────────────

test("renderHandoffVerdict is deterministic and reflects the verdict", () => {
  const v = evaluateHandoffSlice({
    model: model(),
    request: request(),
    triggerSet: OWNER_GATES,
    action: { conditionKind: "publication" },
    snapshot: { subgoals: [{ id: "5.15", status: "achieved" }], handoffLedger: [] },
    now: "2026-06-26T00:00:00.000Z",
  });
  const t1 = renderHandoffVerdict(v);
  const t2 = renderHandoffVerdict(v);
  assert.equal(t1, t2);
  assert.ok(t1.startsWith(`# ${HANDOFF_VERDICT_TITLE}`));
  assert.ok(t1.includes("## handoff: allow"));
  assert.ok(t1.includes("## escalation: required"));
  assert.ok(t1.includes("## stageExit: ready"));
  assert.ok(t1.endsWith("\n"));
});

test("renderHandoffVerdict throws on a non-verdict", () => {
  assert.throws(() => renderHandoffVerdict(null), /verdict/);
});

// ── structural guards (programming errors throw) ─────────────────────────────

test("invalid structural input throws (not a silent empty verdict)", () => {
  assert.throws(() => evaluateHandoff({}), /model/);
  assert.throws(() => evaluateHandoff({ model: { transitions: [] } }), /request/);
});

// ── the REAL committed model drives the legitimate chain to allow/not_required ─

test("real Docs/handoff-escalation-model.json: legit chain → allow / not_required", async () => {
  const realModel = JSON.parse(await readFile(resolve(here, "../Docs/handoff-escalation-model.json"), "utf8"));

  // Every modeled transition, given a minimal compliant bundle, is allowed.
  for (const t of realModel.transitions) {
    const bundle = { facts: [], summaries: [], aiAssumptions: [], laterVerification: [] };
    for (const kind of t.requiredEvidenceKinds) {
      bundle[kind] = kind === "facts" ? [{ id: `f-${t.trigger}`, sourceRef: "src/x.js" }] : [{ id: `${kind}-${t.trigger}` }];
    }
    const h = evaluateHandoff({
      model: realModel,
      request: {
        from: t.from,
        to: t.to,
        trigger: t.trigger,
        evidenceBundle: bundle,
        reviewStatus: t.preservesReviewStatus ? "carried" : "",
      },
    });
    assert.equal(h.decision, "allow", `transition ${t.from}--${t.trigger}-->${t.to} should be allowed`);
  }

  // A buildable-envelope action escalates not_required against the real gates.
  const e = evaluateEscalation({ triggerSet: realModel.ownerGates, action: { conditionKind: "internal_handoff", descriptorFlags: {} } });
  assert.equal(e.decision, "not_required");

  // Each real owner gate condition escalates required.
  for (const g of realModel.ownerGates) {
    const er = evaluateEscalation({ triggerSet: realModel.ownerGates, action: { conditionKind: g.conditionKind } });
    assert.equal(er.decision, "required");
    assert.deepEqual(er.gatesTriggered, [g.id]);
  }
});
