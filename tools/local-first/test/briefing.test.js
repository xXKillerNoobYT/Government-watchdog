import assert from "node:assert/strict";
import test from "node:test";

import {
  createSourceLink,
  createStatement,
  applyVerificationTransition,
} from "../src/statement-verification.js";
import {
  detectHotTopics,
  buildCorrectionNotices,
  partitionBriefingStatements,
  assembleBriefing,
} from "../src/briefing.js";

const NOW = new Date("2026-06-26T00:00:00.000Z");

// ── fixtures ────────────────────────────────────────────────────────────────

function sourceRecord(id, canonicalUrl, hashValue, lifecycleStatus = "current") {
  return {
    id,
    canonicalUrl,
    lifecycleStatus,
    contentHash: hashValue === null ? null : { algorithm: "sha256", value: hashValue },
  };
}

function publishableFact(id, text, overrides = {}) {
  return createStatement({
    id,
    text,
    status: "verified",
    sourceLinks: [createSourceLink({
      sourceId: overrides.sourceId ?? `toa-${id}`,
      quote: overrides.quote ?? `${text} (source quote)`,
      page: 1,
    })],
    evidenceLimits: "Source proves the quoted line only; no wider claim.",
    ...overrides.statement,
  }, { now: NOW, actor: "reviewer" });
}

// ── F4: hot-topic detection ─────────────────────────────────────────────────

test("detectHotTopics flags changed, new, and missing current sources; skips unchanged", () => {
  const sources = [
    sourceRecord("a", "https://townofalpine.example.gov/a", "hashA2"),  // changed
    sourceRecord("b", "https://townofalpine.example.gov/b", "hashB1"),  // unchanged
    sourceRecord("c", "https://townofalpine.example.gov/c", "hashC1"),  // new (no prior)
    sourceRecord("d", "https://townofalpine.example.gov/d", null),      // missing hash
  ];
  const priorHashes = {
    "https://townofalpine.example.gov/a": "hashA1",
    "https://townofalpine.example.gov/b": "hashB1",
  };

  const triage = detectHotTopics(sources, priorHashes);
  const byId = Object.fromEntries(triage.map((t) => [t.sourceId, t.change]));

  assert.equal(byId.a, "changed");
  assert.equal(byId.c, "new");
  assert.equal(byId.d, "missing");
  assert.equal(byId.b, undefined, "unchanged source must not be surfaced");
  assert.ok(triage.every((t) => t.requiresReviewerAction === true));
});

test("detectHotTopics ignores non-current lifecycle records", () => {
  const sources = [
    sourceRecord("a", "https://townofalpine.example.gov/a", "hashA2", "replaced"),
    sourceRecord("b", "https://townofalpine.example.gov/b", "hashB2", "rejected"),
  ];
  assert.deepEqual(detectHotTopics(sources, {}), []);
});

test("detectHotTopics output is deterministic regardless of input order", () => {
  const a = sourceRecord("a", "https://townofalpine.example.gov/a", "x1");
  const b = sourceRecord("b", "https://townofalpine.example.gov/b", "x2");
  const c = sourceRecord("c", "https://townofalpine.example.gov/c", "x3");
  assert.deepEqual(detectHotTopics([a, b, c], {}), detectHotTopics([c, a, b], {}));
});

// ── §5: correction notices ──────────────────────────────────────────────────

function correctedStatement(id, priorText, newText) {
  let s = publishableFact(id, priorText);
  s = applyVerificationTransition(s, {
    action: "correct_false",
    correctedText: newText,
    reason: "Original figure was wrong; corrected per official record.",
    correctingSourceLink: {
      sourceId: `toa-${id}-correction`,
      quote: `${newText} (correcting source)`,
      page: 2,
    },
  }, { now: NOW, actor: "reviewer" });
  return s;
}

test("buildCorrectionNotices emits a notice only when prior text appeared in a prior digest", () => {
  const s = correctedStatement("s1", "The budget rose 40%.", "The budget rose 4%.");

  const noNotice = buildCorrectionNotices([s], ["An unrelated prior line."]);
  assert.equal(noNotice.notices.length, 0, "no prior publication -> no obligation");

  const withNotice = buildCorrectionNotices([s], ["The budget rose 40%."]);
  assert.equal(withNotice.notices.length, 1);
  const n = withNotice.notices[0];
  assert.equal(n.statementId, "s1");
  assert.equal(n.priorText, "The budget rose 40%.");
  assert.equal(n.newText, "The budget rose 4%.");
  assert.ok(n.correctingSourceLink.traceHash, "notice must cite a correcting source");
});

test("buildCorrectionNotices records a defect (and suppresses notice) when correcting source link is absent", () => {
  // Hand-build a false_corrected statement whose correction entry lacks a usable link.
  const s = {
    id: "s2",
    text: "Corrected text.",
    status: "false_corrected",
    correctionHistory: [{
      priorText: "Wrong text.",
      newText: "Corrected text.",
      reason: "fix",
      correctingSourceLink: null,
    }],
  };
  const { notices, defects } = buildCorrectionNotices([s], ["Wrong text."]);
  assert.equal(notices.length, 0);
  assert.equal(defects.length, 1);
  assert.equal(defects[0].reason, "correction_notice_missing_correcting_source_link");
});

// ── partition ────────────────────────────────────────────────────────────────

test("partition routes disputed, false_corrected, ai_analysis, and gate failures away from the body", () => {
  const fact = publishableFact("ok", "A verified, traceable fact.");

  let disputed = publishableFact("dis", "A now-disputed claim.");
  disputed = applyVerificationTransition(disputed, {
    action: "dispute", reason: "Conflicting record found.",
  }, { now: NOW, actor: "reviewer" });

  const corrected = correctedStatement("cor", "Old wrong claim.", "New right claim.");

  const ai = createStatement({
    id: "ai", text: "AI summary of the above.", kind: "ai_analysis", status: "unverified",
    sourceLinks: [createSourceLink({ sourceId: "toa-ai", quote: "grounding quote", page: 1 })],
    evidenceLimits: "Summary only; no new claims.",
  }, { now: NOW });

  const dnp = publishableFact("dnp", "Hard-excluded claim.", {
    statement: { publication: { doNotPublish: true, reason: "sensitive" } },
  });

  const part = partitionBriefingStatements([fact, disputed, corrected, ai, dnp]);

  assert.deepEqual(part.bodyEligible.map((s) => s.id), ["ok"]);
  assert.deepEqual(part.disputed.map((s) => s.id), ["dis"]);
  assert.deepEqual(part.corrected.map((s) => s.id), ["cor"]);
  assert.deepEqual(part.aiProse.map((s) => s.id), ["ai"]);
  assert.deepEqual(part.excluded.map((s) => s.id), ["dnp"]);
});

// ── end-to-end assembly ───────────────────────────────────────────────────────

test("assembleBriefing keeps false_corrected and disputed claims out of the publishable body", () => {
  const fact = publishableFact("ok", "Council approved the 2026 budget.");
  const corrected = correctedStatement("cor", "The levy doubled.", "The levy rose 3%.");
  let disputed = publishableFact("dis", "The mayor missed every meeting.");
  disputed = applyVerificationTransition(disputed, {
    action: "dispute", reason: "Minutes show attendance.",
  }, { now: NOW, actor: "reviewer" });

  const briefing = assembleBriefing({
    statements: [fact, corrected, disputed],
    currentSources: [sourceRecord("toa-ok", "https://townofalpine.example.gov/budget", "h2")],
    priorSourceHashes: { "https://townofalpine.example.gov/budget": "h1" },
    priorDigestTexts: ["The levy doubled."],
  });

  // Publishable body contains only the clean fact.
  assert.deepEqual(briefing.publishable.map((s) => s.id), ["ok"]);
  assert.ok(briefing.digestBody.includes("Council approved the 2026 budget."));
  assert.ok(!briefing.digestBody.includes("The levy doubled."), "superseded claim must not be in body");
  assert.ok(!briefing.digestBody.includes("The mayor missed every meeting."), "disputed claim must not be in body");

  // Correction surfaced as a notice (its prior text was in a prior digest).
  assert.equal(briefing.corrections.length, 1);
  assert.equal(briefing.corrections[0].statementId, "cor");

  // Disputed surfaced only under its labeled section.
  assert.deepEqual(briefing.disputed.map((d) => d.statementId), ["dis"]);
  assert.ok(briefing.body.includes("[DISPUTED] dis"));

  // Hot-topic + Wayback gate.
  assert.equal(briefing.hotTopics.length, 1);
  assert.equal(briefing.waybackStatus, "unchecked");
});

test("assembleBriefing is deterministic and order-independent", () => {
  const a = publishableFact("a", "Fact A.");
  const b = publishableFact("b", "Fact B.");
  const c = publishableFact("c", "Fact C.");
  const sources = [
    sourceRecord("s1", "https://townofalpine.example.gov/1", "h1"),
    sourceRecord("s2", "https://townofalpine.example.gov/2", "h2"),
  ];

  const first = assembleBriefing({ statements: [a, b, c], currentSources: sources });
  const second = assembleBriefing({ statements: [c, a, b], currentSources: [...sources].reverse() });

  assert.equal(first.body, second.body);
});

test("assembleBriefing makes no external Wayback call even when sources are present", () => {
  const briefing = assembleBriefing({
    statements: [publishableFact("x", "A fact.")],
    currentSources: [sourceRecord("s", "https://townofalpine.example.gov/x", "h")],
  });
  assert.equal(briefing.waybackStatus, "unchecked");
  assert.ok(briefing.body.includes("F3 gated — no external call made"));
});
