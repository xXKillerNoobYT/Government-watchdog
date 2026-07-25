import assert from "node:assert/strict";
import test from "node:test";

import {
  applyVerificationTransition,
  createSourceLink,
  createStatement,
  evaluatePublicationGate,
} from "../src/statement-verification.js";
import {
  assembleBriefing,
  buildCorrectionNotices,
  detectHotTopics,
  partitionBriefingStatements,
} from "../src/briefing.js";

// Independent VSR verification harness for Stage 5.08 (GOV-569). Complements the
// impl author's `briefing.test.js` with reviewer-owned checks the AC names but the
// author's suite does not assert directly:
//   AC4  the rendered AI label "[AI ANALYSIS — NOT VERIFIED]" is present, and an
//        ai_analysis claimed as a verified fact is gate-excluded (not just routed).
//   AC1  the correction notice cites a real sha256 trace, and a notice missing its
//        source surfaces as a §9 issue candidate at the briefing level.
//   §6   do_not_publish hard-excludes a *corrected* and a *disputed* statement
//        (not only a plain fact) from every output bucket.
// Verifies the canonical committed implementation `src/briefing.js` (GOV-568).

function verifiedFact(id, text, opts = {}) {
  return createStatement({
    id,
    text,
    kind: "fact_claim",
    status: "verified",
    sourceLinks: [createSourceLink({
      sourceId: opts.sourceId ?? `toa-${id}`,
      quote: opts.quote ?? `${text} (source quote)`,
      page: opts.page ?? 1,
    })],
    evidenceLimits: opts.evidenceLimits
      ?? "Source proves the quoted line only; no wider claim.",
  }, { now: new Date(opts.now ?? "2026-06-10T00:00:00.000Z"), actor: "reviewer" });
}

function correctFalse(stmt, opts) {
  return applyVerificationTransition(stmt, {
    action: "correct_false",
    reason: opts.reason,
    correctedText: opts.correctedText,
    correctingSourceLink: opts.correctingSourceLink,
  }, { now: new Date(opts.now ?? "2026-06-12T00:00:00.000Z"), actor: "reviewer" });
}

// ── AC1: correction notice provenance + defect surfacing ─────────────────────

test("AC1: a published correction emits a retraction notice citing a real sha256 trace", () => {
  const corrected = correctFalse(
    verifiedFact("stmt-corr", "The grant was $80,000."),
    {
      reason: "Original figure misread; the resolution states $50,000.",
      correctedText: "The grant was $50,000.",
      correctingSourceLink: { sourceId: "toa-reso", quote: "award set at $50,000", page: 2 },
    },
  );

  const { notices, defects } = buildCorrectionNotices([corrected], ["The grant was $80,000."]);
  assert.equal(defects.length, 0);
  assert.equal(notices.length, 1);
  const n = notices[0];
  assert.equal(n.priorText, "The grant was $80,000.");
  assert.equal(n.newText, "The grant was $50,000.");
  assert.equal(n.correctingSourceLink.sourceId, "toa-reso");
  assert.match(n.correctingSourceLink.traceHash, /^[a-f0-9]{64}$/);
});

test("AC1: a correction missing its correcting source surfaces as a §9 issue candidate", () => {
  const corrected = correctFalse(
    verifiedFact("stmt-corr", "Figure was wrong."),
    {
      reason: "Bad figure.",
      correctedText: "Figure corrected.",
      correctingSourceLink: { sourceId: "toa-reso", quote: "corrected figure", page: 2 },
    },
  );
  const last = corrected.correctionHistory[corrected.correctionHistory.length - 1];
  last.correctingSourceLink = { ...last.correctingSourceLink, traceHash: null };

  const result = assembleBriefing({ statements: [corrected], priorDigestTexts: ["Figure was wrong."] });
  assert.equal(result.corrections.length, 0, "uncited correction is never emitted as a notice");
  assert.ok(result.issueCandidates.some(
    (c) => c.type === "correction_notice_missing_source" && c.statementId === "stmt-corr",
  ));
});

// ── AC2: disputed surfaces only under a visible label ────────────────────────

test("AC2: disputed claim appears only under the labeled Disputed section, with its reason", () => {
  const disputed = applyVerificationTransition(
    verifiedFact("stmt-disp", "The rezoning vote passed unanimously."),
    { action: "dispute", reason: "Minutes and meeting video conflict on the outcome." },
    { now: new Date("2026-06-12T00:00:00.000Z"), actor: "reviewer" },
  );

  const result = assembleBriefing({ statements: [verifiedFact("stmt-ok", "Clean fact."), disputed] });

  assert.ok(!result.publishable.some((i) => i.id === "stmt-disp"));
  const dispStart = result.body.indexOf("## Disputed");
  const pubStart = result.body.indexOf("## Publishable facts");
  const disputedSection = result.body.slice(dispStart, pubStart);
  const publishableSection = result.body.slice(pubStart, result.body.indexOf("## AI-assisted prose"));

  assert.ok(disputedSection.includes("NOT settled fact"));
  assert.ok(disputedSection.includes("[DISPUTED] stmt-disp"));
  assert.ok(disputedSection.includes("Minutes and meeting video conflict"));
  assert.ok(!publishableSection.includes("stmt-disp"));
});

// ── AC3: hot-topic triage requires reviewer action; non-current ignored ──────

test("AC3: triage flags changed/new/missing current sources, requires reviewer action, skips non-current", () => {
  const currentSources = [
    { id: "src-a", canonicalUrl: "https://alpine.gov/a", lifecycleStatus: "current", contentHash: { value: "hashA" } },
    { id: "src-b", canonicalUrl: "https://alpine.gov/b", lifecycleStatus: "current", contentHash: { value: "hashB2" } },
    { id: "src-c", canonicalUrl: "https://alpine.gov/c", lifecycleStatus: "current", contentHash: { value: "hashC" } },
    { id: "src-d", canonicalUrl: "https://alpine.gov/d", lifecycleStatus: "current", contentHash: { value: null } },
    { id: "src-e", canonicalUrl: "https://alpine.gov/e", lifecycleStatus: "replaced", contentHash: { value: "hashE" } },
  ];
  const priorHashes = { "https://alpine.gov/a": "hashA", "https://alpine.gov/b": "hashB" };

  const triage = detectHotTopics(currentSources, priorHashes);
  const byType = new Map(triage.map((t) => [t.change, t.sourceId]));
  assert.equal(triage.length, 3);
  assert.equal(byType.get("changed"), "src-b");
  assert.equal(byType.get("new"), "src-c");
  assert.equal(byType.get("missing"), "src-d");
  assert.ok(!triage.some((t) => t.sourceId === "src-a" || t.sourceId === "src-e"));
  assert.ok(triage.every((t) => t.requiresReviewerAction === true));

  const result = assembleBriefing({ statements: [], currentSources, priorSourceHashes: priorHashes });
  assert.equal(result.publishable.length, 0, "hot sources never auto-enter the publishable pool");
  assert.ok(result.body.slice(0, result.body.indexOf("## Corrections")).includes("reviewer action required"));
});

// ── AC4: AI prose labeling + AI-as-fact exclusion ────────────────────────────

test("AC4: a sourced ai_analysis statement renders under the visible AI label, never in the body", () => {
  const aiProse = createStatement({
    id: "stmt-ai",
    text: "Across the cited minutes, the board trended toward approving the purchase.",
    kind: "ai_analysis",
    status: "unverified",
    sourceLinks: [createSourceLink({ sourceId: "toa-min", quote: "minutes line about the purchase", page: 3 })],
    evidenceLimits: "AI summary of the cited lines only; not independent verification.",
  }, { now: new Date("2026-06-10T00:00:00.000Z"), actor: "reviewer" });

  // Gate allows it (not claimed as a verified fact) and it is source-linked (AC4).
  assert.equal(evaluatePublicationGate(aiProse).publishable, true);
  assert.ok(aiProse.sourceLinks.length >= 1);

  const part = partitionBriefingStatements([aiProse]);
  assert.deepEqual(part.aiProse.map((s) => s.id), ["stmt-ai"]);
  assert.equal(part.bodyEligible.length, 0);

  const result = assembleBriefing({ statements: [aiProse] });
  const aiStart = result.body.indexOf("## AI-assisted prose");
  const aiSection = result.body.slice(aiStart);
  assert.ok(aiSection.includes("[AI ANALYSIS — NOT VERIFIED]"));
  assert.ok(aiSection.includes("[AI] stmt-ai"));
  assert.ok(!result.body.slice(result.body.indexOf("## Publishable facts"), aiStart).includes("stmt-ai"));
});

test("AC4: an ai_analysis claimed as a verified fact is gate-excluded, never routed to AI prose", () => {
  const aiAsFact = createStatement({
    id: "stmt-ai-fact",
    text: "AI concludes the board acted improperly.",
    kind: "ai_analysis",
    status: "verified",
    sourceLinks: [createSourceLink({ sourceId: "toa-packet", quote: "packet line", page: 3 })],
    evidenceLimits: "AI summary only.",
  }, { now: new Date("2026-06-10T00:00:00.000Z"), actor: "reviewer" });

  assert.ok(evaluatePublicationGate(aiAsFact).failures.includes("ai_analysis_as_fact"));

  const result = assembleBriefing({ statements: [aiAsFact] });
  assert.ok(!result.publishable.some((i) => i.id === "stmt-ai-fact"));
  assert.ok(!result.aiProse.some((i) => i.statementId === "stmt-ai-fact"));
  assert.ok(result.excluded.some((e) => e.id === "stmt-ai-fact" && e.failures.includes("ai_analysis_as_fact")));
});

// ── §6 hard-stop on corrected + disputed statements ──────────────────────────

test("§6: do_not_publish hard-excludes a CORRECTED statement from every bucket", () => {
  const corrected = correctFalse(
    verifiedFact("stmt-corr", "Figure was wrong."),
    {
      reason: "Bad figure.",
      correctedText: "Figure corrected.",
      correctingSourceLink: { sourceId: "toa-reso", quote: "corrected figure", page: 2 },
    },
  );
  const hardStopped = applyVerificationTransition(corrected, {
    action: "do_not_publish", reason: "Names a private individual.",
  }, { now: new Date("2026-06-13T00:00:00.000Z"), actor: "reviewer" });

  const part = partitionBriefingStatements([hardStopped]);
  assert.equal(part.corrected.length, 0);
  assert.equal(part.bodyEligible.length, 0);
  assert.ok(part.excluded.some((e) => e.id === "stmt-corr" && e.failures.includes("do_not_publish")));

  const result = assembleBriefing({ statements: [hardStopped], priorDigestTexts: ["Figure was wrong."] });
  const pubSection = result.body.slice(
    result.body.indexOf("## Publishable facts"), result.body.indexOf("## AI-assisted prose"),
  );
  assert.ok(!pubSection.includes("stmt-corr"));
});

test("§6: do_not_publish hard-excludes a DISPUTED statement from the Disputed bucket", () => {
  const disputed = applyVerificationTransition(
    verifiedFact("stmt-disp", "Disputed claim about a private person."),
    { action: "dispute", reason: "Sources conflict." },
    { now: new Date("2026-06-12T00:00:00.000Z"), actor: "reviewer" },
  );
  const hardStopped = applyVerificationTransition(disputed, {
    action: "do_not_publish", reason: "Names a private individual.",
  }, { now: new Date("2026-06-13T00:00:00.000Z"), actor: "reviewer" });

  const part = partitionBriefingStatements([hardStopped]);
  assert.equal(part.disputed.length, 0);
  assert.ok(part.excluded.some((e) => e.id === "stmt-disp" && e.failures.includes("do_not_publish")));

  const result = assembleBriefing({ statements: [hardStopped] });
  assert.equal(result.disputed.length, 0);
});
