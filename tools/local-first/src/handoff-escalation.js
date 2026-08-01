// Stage 5.15 / GOV-585 — deterministic, reviewer-internal agent-handoff &
// owner-escalation evaluator.
//
// Mirrors the substrate posture (doc-continuity / refresh-runner): pure,
// read-only, deterministic, fail-closed, no network, no AI in the decision path,
// no public output. The exported `evaluate*` functions are pure functions of
// their explicit inputs — they never walk the filesystem, never call
// Date.now()/Math.random(), never mutate their arguments, and never contact an
// owner channel. The (impure) CLI wrapper at the bottom does the I/O.
//
// Scope boundary (contract §4 note): 5.13 audits *verification records*, 5.14
// audits *documentation/module state*, 5.15 audits *role transitions, evidence
// custody, and owner gates*. Disjoint inputs — intentionally NOT merged.
//
// Governing principle (GOV-471): deterministic logic owns routing, validation,
// trust-separation, and the escalation decision. AI output, if surfaced at all,
// is an optional `ai_analysis` summary OVER an already-produced verdict — it can
// never be primary evidence and never flips a deny/required to allow.

import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const HANDOFF_VERDICT_TITLE = "Stage 5.15 Handoff & Escalation Verdict";
export const MODEL_VERSION = "5.15.0";

// ── enumerated constants (severity is a property of the type, never AI-decided) ─

// §4.1 handoff-validation violation types → fixed severity.
export const HANDOFF_VIOLATION_TYPES = Object.freeze({
  illegal_transition: "high",
  missing_required_evidence: "high",
  ai_assumption_as_fact: "high",
  fact_without_source: "high",
  bucket_cross_contamination: "high",
  review_status_dropped: "medium",
  unknown_trigger: "medium",
  unrecognized_role: "medium",
});

// §3.1 role set. `impl` is the canonical role; Backend/Automation engineers fill
// it, so they are recognized aliases (the model uses them on real transitions).
export const ROLE_SET = Object.freeze([
  "impl",
  "Backend",
  "Automation",
  "VSR",
  "Security",
  "CTO",
  "CEO",
  "Owner",
]);

// §3.1 trigger set (the only legal handoff triggers).
export const TRIGGER_SET = Object.freeze([
  "impl_complete",
  "vsr_pass",
  "sec_pass",
  "cto_merge",
  "correction_filed",
  "hot_topic_flag",
  "source_change_detected",
  "future_fact_verified",
  "ledger_flip_ready",
  "owner_decision",
]);

// The four trust-separated evidence buckets. They must stay disjoint (§4.1
// bucket_cross_contamination) and individually labeled (§5.4).
export const EVIDENCE_BUCKETS = Object.freeze([
  "facts",
  "summaries",
  "aiAssumptions",
  "laterVerification",
]);

// §3.4 the five owner gates — the ONLY conditions that require Isaac to decide.
// Frozen canonical set; the committed model JSON encodes the same gates.
export const OWNER_GATE_CONDITIONS = Object.freeze([
  "publication",
  "scope_expansion_beyond_alpine",
  "ai_label_or_verification_policy_change",
  "legal_privacy_on_individual",
  "budget",
]);

export const OWNER_GATES = Object.freeze([
  Object.freeze({ id: "gate_publication", conditionKind: "publication", severity: "high", requiresOwner: true }),
  Object.freeze({ id: "gate_scope_expansion", conditionKind: "scope_expansion_beyond_alpine", severity: "high", requiresOwner: true }),
  Object.freeze({ id: "gate_ai_policy", conditionKind: "ai_label_or_verification_policy_change", severity: "high", requiresOwner: true }),
  Object.freeze({ id: "gate_legal_privacy", conditionKind: "legal_privacy_on_individual", severity: "high", requiresOwner: true }),
  Object.freeze({ id: "gate_budget", conditionKind: "budget", severity: "high", requiresOwner: true }),
]);

// descriptorFlags an action may raise → the owner gate each one trips. A flag set
// true fires its gate; a descriptorFlags object carrying an UNKNOWN key is
// treated as unrecognized → fail-closed escalate (§4.2 fail-closed rule).
const DESCRIPTOR_FLAG_GATES = Object.freeze({
  publishesToPublic: "publication",
  expandsScopeBeyondAlpine: "scope_expansion_beyond_alpine",
  changesAiOrVerificationPolicy: "ai_label_or_verification_policy_change",
  touchesIndividualLegalPrivacy: "legal_privacy_on_individual",
  affectsBudget: "budget",
});

// Condition kinds that are explicitly inside the Alpine buildable envelope. An
// action must positively declare one of these (and raise no gate flag) to earn
// `not_required`; anything else fails closed to `required`.
const KNOWN_SAFE_CONDITIONS = Object.freeze([
  "none",
  "internal_handoff",
  "reviewer_verdict",
  "buildable_envelope_op",
  "merge_to_main",
]);

const SEVERITY_RANK = Object.freeze({ high: 0, medium: 1, low: 2 });

// Banner that marks a Stage-5 subgoal as deferred (5.08/5.09/5.11, Isaac-gated).
const DEFERRED_BANNER = /DEFERRED .*\(Isaac-gated\)/;

// ── (1) handoff validation ──────────────────────────────────────────────────

export function evaluateHandoff({ model, request } = {}) {
  // Structural validation throws (programming error), mirroring analyzeDocContinuity.
  if (!model || typeof model !== "object" || !Array.isArray(model.transitions)) {
    throw new Error("evaluateHandoff requires model:{ version, transitions:[...] }");
  }
  if (!request || typeof request !== "object") {
    throw new Error("evaluateHandoff requires request:{ from, to, trigger, evidenceBundle, reviewStatus }");
  }

  const { from, to, trigger, evidenceBundle, reviewStatus } = request;
  const violations = [];
  const add = (type, subjectId, detail, evidence) =>
    violations.push({
      type,
      severity: HANDOFF_VIOLATION_TYPES[type],
      subjectId,
      detail,
      evidence: evidence ?? {},
    });

  const transitionKey = `${from ?? "?"}--${trigger ?? "?"}-->${to ?? "?"}`;

  // unrecognized_role — `from` or `to` outside the Stage 5 role set.
  const roleSet = new Set(ROLE_SET);
  const badRoles = [];
  if (!roleSet.has(from)) badRoles.push(`from:${from ?? "(absent)"}`);
  if (!roleSet.has(to)) badRoles.push(`to:${to ?? "(absent)"}`);
  if (badRoles.length > 0) {
    add("unrecognized_role", transitionKey, `role(s) outside the Stage 5 role set: ${badRoles.join(", ")}`, {
      from: from ?? null,
      to: to ?? null,
    });
  }

  // unknown_trigger — `trigger` outside the enumerated set.
  if (!TRIGGER_SET.includes(trigger)) {
    add("unknown_trigger", transitionKey, `trigger '${trigger ?? "(absent)"}' is not in the enumerated trigger set`, {
      trigger: trigger ?? null,
    });
  }

  // illegal_transition — no model entry matches (from, to, trigger). Fail-closed:
  // an unknown transition denies; the evaluator never invents transitions.
  const matched = model.transitions.find(
    (t) => t && t.from === from && t.to === to && t.trigger === trigger,
  );
  if (!matched) {
    add("illegal_transition", transitionKey, "no role-transition-model entry permits this (from, to, trigger)", {
      from: from ?? null,
      to: to ?? null,
      trigger: trigger ?? null,
    });
  }

  // ── evidence-bundle validation (trust separation is hard, §5.4) ────────────
  const bundle = normalizeBundle(evidenceBundle);

  // missing_required_evidence — the matched transition requires a bucket the
  // bundle leaves empty. Only checkable when the transition is legal.
  if (matched) {
    const required = asArray(matched.requiredEvidenceKinds);
    const missing = required.filter((kind) => !(EVIDENCE_BUCKETS.includes(kind) && bundle[kind].length > 0));
    if (missing.length > 0) {
      add("missing_required_evidence", transitionKey, `bundle omits required evidence kind(s): ${missing.join(", ")}`, {
        from: from ?? null,
        to: to ?? null,
        trigger: trigger ?? null,
        missingEvidenceKinds: missing,
      });
    }
  }

  // fact_without_source — every `facts` item must carry a sourceRef (§3.3).
  const unsourced = bundle.facts.filter((f) => !hasSourceRef(f));
  if (unsourced.length > 0) {
    add("fact_without_source", transitionKey, `${unsourced.length} fact(s) lack a sourceRef`, {
      count: unsourced.length,
      unsourcedKeys: unsourced.map(itemKey).sort(),
    });
  }

  // ai_assumption_as_fact — an aiAssumptions item appears in (or is labeled as)
  // `facts`. No AI assumption is ever promotable to a fact (§5.4).
  const assumptionKeys = new Set(bundle.aiAssumptions.map(itemKey));
  const promoted = bundle.facts.filter((f) => assumptionKeys.has(itemKey(f)) || isAiLabeled(f));
  if (promoted.length > 0) {
    add("ai_assumption_as_fact", transitionKey, `${promoted.length} fact(s) are AI assumptions promoted to fact`, {
      crossContaminatedBucket: "facts<-aiAssumptions",
      promotedKeys: promoted.map(itemKey).sort(),
    });
  }

  // bucket_cross_contamination — the four buckets must be disjoint. (The
  // aiAssumptions↔facts overlap is reported by the more specific type above; this
  // catches every OTHER overlapping pair.)
  const contaminated = findCrossContamination(bundle);
  if (contaminated.length > 0) {
    add("bucket_cross_contamination", transitionKey, `${contaminated.length} item(s) appear in more than one trust bucket`, {
      crossContaminatedBucket: contaminated.map((c) => c.pair).sort().join(","),
      sharedKeys: contaminated.map((c) => c.key).sort(),
    });
  }

  // review_status_dropped — transition declares preservesReviewStatus but the
  // request drops/empties it.
  const preserves = matched ? matched.preservesReviewStatus === true : false;
  const carried = nonEmptyString(reviewStatus) ? reviewStatus : null;
  if (preserves && carried === null) {
    add("review_status_dropped", transitionKey, "transition preserves review status but the request dropped it", {
      from: from ?? null,
      to: to ?? null,
    });
  }

  violations.sort(compareViolations);

  const separationOk =
    !violations.some((v) => v.type === "ai_assumption_as_fact" || v.type === "bucket_cross_contamination");

  return {
    // §4: violations empty IFF decision === "allow" (fail-closed — any violation denies).
    decision: violations.length === 0 ? "allow" : "deny",
    violations,
    preservedReviewStatus: carried, // echo what the request carries (may be null)
    evidenceContract: {
      facts: bundle.facts,
      summaries: bundle.summaries,
      aiAssumptions: bundle.aiAssumptions,
      laterVerification: bundle.laterVerification,
      separationOk,
    },
  };
}

// ── (2) owner-escalation evaluation ─────────────────────────────────────────

export function evaluateEscalation({ triggerSet, action } = {}) {
  // triggerSet supplied as data (§3.4); default to the canonical OWNER_GATES.
  const gates = Array.isArray(triggerSet) && triggerSet.length > 0 ? triggerSet : OWNER_GATES;
  const gateByCondition = new Map();
  for (const g of gates) {
    if (g && nonEmptyString(g.conditionKind) && nonEmptyString(g.id)) {
      gateByCondition.set(g.conditionKind, g);
    }
  }

  if (!action || typeof action !== "object") {
    // Fail-closed: an absent/malformed action escalates, never silently passes.
    return {
      decision: "required",
      gatesTriggered: [],
      reason: "fail-closed: no action descriptor supplied — escalation required",
    };
  }

  const { conditionKind, descriptorFlags } = action;
  const fired = [];

  // (a) explicit conditionKind that matches an owner gate.
  if (nonEmptyString(conditionKind) && gateByCondition.has(conditionKind)) {
    fired.push(gateByCondition.get(conditionKind).id);
  }

  // (b) descriptorFlags: a recognized flag set true fires its gate; an
  //     UNRECOGNIZED flag key fails closed to escalate.
  let unrecognizedFlag = null;
  if (descriptorFlags !== undefined && descriptorFlags !== null) {
    if (typeof descriptorFlags !== "object") {
      unrecognizedFlag = "(descriptorFlags not an object)";
    } else {
      for (const [key, val] of Object.entries(descriptorFlags)) {
        if (!(key in DESCRIPTOR_FLAG_GATES)) {
          unrecognizedFlag = key;
          continue;
        }
        if (val === true) {
          const cond = DESCRIPTOR_FLAG_GATES[key];
          const gate = gateByCondition.get(cond);
          if (gate) fired.push(gate.id);
          else fired.push(`gate_unmapped:${cond}`); // gate not in supplied set → still escalate
        }
      }
    }
  }

  if (fired.length > 0) {
    return {
      decision: "required",
      gatesTriggered: [...new Set(fired)].sort(),
      reason: `owner gate(s) fired: ${[...new Set(fired)].sort().join(", ")}`,
    };
  }

  // (c) fail-closed on ambiguity: unknown conditionKind or unrecognized flag.
  if (unrecognizedFlag !== null) {
    return {
      decision: "required",
      gatesTriggered: [],
      reason: `fail-closed: unrecognized descriptorFlag '${unrecognizedFlag}' — escalation required`,
    };
  }
  if (!nonEmptyString(conditionKind) || !KNOWN_SAFE_CONDITIONS.includes(conditionKind)) {
    return {
      decision: "required",
      gatesTriggered: [],
      reason: `fail-closed: conditionKind '${conditionKind ?? "(absent)"}' is not a recognized buildable-envelope action — escalation required`,
    };
  }

  // (d) not_required — positively recognized safe action, no gate raised.
  return {
    decision: "not_required",
    gatesTriggered: [],
    reason: `no owner gate; '${conditionKind}' is inside the Alpine buildable envelope`,
  };
}

// ── (3) Stage-5 → Stage-6 exit predicate ────────────────────────────────────

export function evaluateStageExit({ snapshot } = {}) {
  // §5.3: missing stage state degrades to an explicit ABSENT stageExit block,
  // never a silent `ready`. The composer omits the block when this returns null.
  if (!snapshot || typeof snapshot !== "object") return null;

  if (!Array.isArray(snapshot.subgoals)) {
    return {
      decision: "blocked",
      blockingSubgoals: [],
      reason: "fail-closed: stage-state snapshot has no subgoals array",
    };
  }

  const blockingSubgoals = [];
  for (const sg of snapshot.subgoals) {
    if (!sg || !nonEmptyString(sg.id)) continue;
    if (isDeferred(sg)) continue; // deferred (5.08/5.09/5.11) does not block the gate
    if (sg.status !== "achieved") blockingSubgoals.push(sg.id);
  }
  blockingSubgoals.sort();

  // Open handoff-ledger blockers: any deny/required handoff still open also blocks.
  const ledger = asArray(snapshot.handoffLedger);
  const openBlockers = ledger
    .filter((r) => r && r.open !== false && (r.handoffDecision === "deny" || r.escalationDecision === "required"))
    .map((r) => (nonEmptyString(r.id) ? r.id : "(unnamed-handoff)"))
    .sort();

  const ready = blockingSubgoals.length === 0 && openBlockers.length === 0;
  const reasonParts = [];
  if (blockingSubgoals.length > 0) reasonParts.push(`non-deferred subgoal(s) not achieved: ${blockingSubgoals.join(", ")}`);
  if (openBlockers.length > 0) reasonParts.push(`open deny/required handoff(s): ${openBlockers.join(", ")}`);

  return {
    decision: ready ? "ready" : "blocked",
    blockingSubgoals,
    openHandoffBlockers: openBlockers,
    reason: ready
      ? "all non-deferred Stage 5 subgoals achieved; no open deny/required handoff"
      : reasonParts.join("; "),
  };
}

// ── (4) full §4 verdict (composes the three above) ──────────────────────────

export function evaluateHandoffSlice({ model, request, triggerSet, action, snapshot, now } = {}) {
  const handoff = evaluateHandoff({ model, request });
  const escalation = evaluateEscalation({ triggerSet, action });
  const stageExit = evaluateStageExit({ snapshot });

  const verdict = {
    generatedAt: normalizeNow(now),
    modelVersion: (model && nonEmptyString(model.version)) ? model.version : MODEL_VERSION,
    handoff,
    escalation,
  };
  // §4: stageExit present ONLY when a stage-state snapshot is supplied.
  if (stageExit !== null) verdict.stageExit = stageExit;

  verdict.summary =
    `handoff=${handoff.decision} (${handoff.violations.length} violation(s)) | ` +
    `escalation=${escalation.decision} (${escalation.gatesTriggered.length} gate(s)) | ` +
    `stageExit=${stageExit ? stageExit.decision : "(absent)"}`;

  return verdict;
}

// ── deterministic text render (mirrors refresh-runner.renderRunLog) ──────────

export function renderHandoffVerdict(verdict) {
  if (!verdict || typeof verdict !== "object" || !verdict.handoff || !verdict.escalation) {
    throw new Error("renderHandoffVerdict requires an evaluateHandoffSlice verdict");
  }
  const { handoff, escalation, stageExit } = verdict;
  const lines = [
    `# ${HANDOFF_VERDICT_TITLE}`,
    `# generatedAt: ${verdict.generatedAt}`,
    `# modelVersion: ${verdict.modelVersion ?? "(none)"}`,
    "",
    `## handoff: ${handoff.decision}`,
    `# preservedReviewStatus: ${handoff.preservedReviewStatus ?? "(none)"}`,
    `# separationOk: ${handoff.evidenceContract.separationOk}`,
  ];
  if (handoff.violations.length === 0) {
    lines.push("(no violations — handoff permitted)");
  }
  for (const v of handoff.violations) {
    lines.push(`- [${v.severity.toUpperCase()}] ${v.type} | ${v.subjectId} | ${v.detail}`);
  }
  lines.push("");
  lines.push(`## escalation: ${escalation.decision}`);
  lines.push(`# gatesTriggered: ${escalation.gatesTriggered.length ? escalation.gatesTriggered.join(", ") : "(none)"}`);
  lines.push(`# reason: ${escalation.reason}`);
  lines.push("");
  if (stageExit) {
    lines.push(`## stageExit: ${stageExit.decision}`);
    lines.push(`# blockingSubgoals: ${stageExit.blockingSubgoals.length ? stageExit.blockingSubgoals.join(", ") : "(none)"}`);
    lines.push(`# reason: ${stageExit.reason}`);
  } else {
    lines.push("## stageExit: (absent — no stage-state snapshot supplied)");
  }
  lines.push("");
  lines.push(`# ${verdict.summary}`);
  return lines.join("\n") + "\n";
}

// ── internal helpers (no AI, no clock, no network) ──────────────────────────

function normalizeBundle(evidenceBundle) {
  const b = evidenceBundle && typeof evidenceBundle === "object" ? evidenceBundle : {};
  return {
    facts: asArray(b.facts),
    summaries: asArray(b.summaries),
    aiAssumptions: asArray(b.aiAssumptions),
    laterVerification: asArray(b.laterVerification),
  };
}

// Stable identity for a bundle item: prefer an explicit id, else a canonical
// stringification. Used for disjointness/cross-contamination detection.
function itemKey(item) {
  if (typeof item === "string") return `s:${item}`;
  if (item && typeof item === "object") {
    if (nonEmptyString(item.id)) return `id:${item.id}`;
    return `j:${stableStringify(item)}`;
  }
  return `v:${String(item)}`;
}

function hasSourceRef(fact) {
  if (typeof fact === "string") return false; // a bare string fact carries no sourceRef
  return !!(fact && typeof fact === "object" && nonEmptyString(fact.sourceRef));
}

function isAiLabeled(fact) {
  if (!fact || typeof fact !== "object") return false;
  return fact.aiDerived === true || fact.kind === "ai_assumption" || fact.assumption === true;
}

// Every overlapping bucket pair EXCEPT facts↔aiAssumptions (reported by the more
// specific ai_assumption_as_fact type).
function findCrossContamination(bundle) {
  const pairs = [];
  for (let i = 0; i < EVIDENCE_BUCKETS.length; i += 1) {
    for (let j = i + 1; j < EVIDENCE_BUCKETS.length; j += 1) {
      const a = EVIDENCE_BUCKETS[i];
      const c = EVIDENCE_BUCKETS[j];
      if ((a === "facts" && c === "aiAssumptions") || (a === "aiAssumptions" && c === "facts")) continue;
      pairs.push([a, c]);
    }
  }
  const out = [];
  for (const [a, c] of pairs) {
    const keysA = new Set(bundle[a].map(itemKey));
    for (const item of bundle[c]) {
      const k = itemKey(item);
      if (keysA.has(k)) out.push({ pair: `${a}<->${c}`, key: k });
    }
  }
  return out;
}

function isDeferred(subgoal) {
  if (subgoal.deferred === true) return true;
  for (const field of ["descriptor", "note", "title", "banner"]) {
    if (nonEmptyString(subgoal[field]) && DEFERRED_BANNER.test(subgoal[field])) return true;
  }
  return false;
}

function compareViolations(a, b) {
  const sr = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
  if (sr !== 0) return sr;
  if (a.type !== b.type) return a.type < b.type ? -1 : 1;
  if (a.subjectId !== b.subjectId) return a.subjectId < b.subjectId ? -1 : 1;
  return 0;
}

// Deterministic key-sorted JSON (objects only); arrays keep order.
function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function normalizeNow(now) {
  if (now instanceof Date) return now.toISOString();
  if (nonEmptyString(now)) return now;
  // Fail-closed: an absent clock is echoed explicitly, never silently defaulted
  // to the wall clock (that would break determinism).
  return "(now-not-supplied)";
}

function asArray(v) {
  return Array.isArray(v) ? v : [];
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

// ── CLI: load the committed model, run the slice over a fixture, print verdict ─
// The pure functions above take data; this wrapper does the impure I/O (reading
// the model JSON + an optional request/action/snapshot fixture file) and exits
// non-zero when the handoff is denied OR escalation is required, so a reviewer /
// scheduler notices. Read-only: it writes nothing.

async function main() {
  const { readFile } = await import("node:fs/promises");
  const { dirname } = await import("node:path");

  // This package was extracted out of a single-root local-first workspace, so
  // its code and its committed docs now live under two different roots: code at
  // tools/local-first/, prose + committed models at <repo>/Docs/local-first/.
  const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const docsRoot = resolve(packageRoot, "../..", "Docs/local-first");
  const modelPath = resolve(docsRoot, "handoff-escalation-model.json");
  const model = JSON.parse(await readFile(modelPath, "utf8"));

  // Optional fixture: { request, action, snapshot } supplied via --fixture <path>.
  let fixture = {};
  const fxIdx = process.argv.indexOf("--fixture");
  if (fxIdx !== -1 && process.argv[fxIdx + 1]) {
    fixture = JSON.parse(await readFile(process.argv[fxIdx + 1], "utf8"));
  }

  // Default to the canonical legitimate first chain handoff (impl --impl_complete--> VSR)
  // so a bare `node src/handoff-escalation.js` run is self-demonstrating.
  const request = fixture.request ?? {
    from: "impl",
    to: "VSR",
    trigger: "impl_complete",
    evidenceBundle: {
      facts: [{ id: "f1", sourceRef: "src/handoff-escalation.js" }],
      summaries: [{ id: "s1", text: "impl complete" }],
      aiAssumptions: [],
      laterVerification: [],
    },
    reviewStatus: "impl_self_reviewed",
  };
  const action = fixture.action ?? { conditionKind: "internal_handoff", descriptorFlags: {} };
  const snapshot = fixture.snapshot ?? null;

  const now = new Date(); // CLI may use the wall clock; the pure fns never do.
  const verdict = evaluateHandoffSlice({
    model,
    request,
    triggerSet: model.ownerGates,
    action,
    snapshot,
    now,
  });
  process.stdout.write(renderHandoffVerdict(verdict));

  const denied = verdict.handoff.decision === "deny";
  const escalated = verdict.escalation.decision === "required";
  return denied || escalated ? 1 : 0;
}

const invokedDirectly = process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main()
    .then((code) => process.exit(code))
    .catch((error) => {
      process.stderr.write(`handoff-escalation failed: ${error?.stack ?? error}\n`);
      process.exit(1);
    });
}
