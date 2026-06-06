import assert from "node:assert/strict";
import test from "node:test";

import {
  applyVerificationTransition,
  computeSourceTraceHash,
  createSourceLink,
  createStatement,
  evaluatePublicationGate,
} from "../src/statement-verification.js";

test("source links include deterministic trace hashes for quote and location", () => {
  const input = {
    sourceId: "toa-minutes-2026-06-05",
    quote: " The town manager reported the grant had been received. ",
    page: 7,
    timestamp: "00:42:10",
    location: "meeting-minutes paragraph 12",
    sourceContentHash: "sha256:abc123",
  };

  const link = createSourceLink(input);
  const repeated = computeSourceTraceHash({
    ...input,
    quote: "The town manager reported   the grant had been received.",
  });

  assert.equal(link.traceHash, repeated);
  assert.match(link.traceHash, /^[a-f0-9]{64}$/);
});

test("verification transitions cover unverified to verified and verified to disputed", () => {
  const sourceLink = createSourceLink({
    sourceId: "toa-agenda-2026-06-05",
    quote: "Agenda item 4 authorizes the purchase.",
    page: 2,
    location: "item 4",
  });
  const unverified = createStatement({
    id: "stmt-purchase-authorized",
    text: "The agenda includes a purchase authorization.",
    sourceLinks: [sourceLink],
    evidenceLimits: "The source proves agenda inclusion, not final approval.",
  });

  const verified = applyVerificationTransition(unverified, { action: "verify" }, {
    actor: "BackendCoder",
    now: new Date("2026-06-06T03:45:00.000Z"),
  });

  assert.equal(verified.status, "verified");
  assert.equal(verified.verification.verifiedBy, "BackendCoder");
  assert.equal(verified.verification.verifiedAt, "2026-06-06T03:45:00.000Z");

  const disputed = applyVerificationTransition(verified, {
    action: "dispute",
    reason: "Later minutes show the item was tabled.",
  }, {
    actor: "LocalFirstReviewer",
    now: new Date("2026-06-06T04:00:00.000Z"),
  });

  assert.equal(disputed.status, "disputed");
  assert.equal(disputed.verification.disputedBy, "LocalFirstReviewer");
  assert.equal(disputed.verification.disputeReason, "Later minutes show the item was tabled.");
});

test("correction to false_corrected records prior and new statement history", () => {
  const verified = createStatement({
    id: "stmt-award-amount",
    text: "The award amount was $500,000.",
    status: "verified",
    sourceLinks: [createSourceLink({
      sourceId: "toa-resolution-v1",
      quote: "Award amount: $500,000",
      page: 3,
    })],
    evidenceLimits: "Resolution excerpt only supports the amount listed on page 3.",
  });

  const corrected = applyVerificationTransition(verified, {
    action: "correct_false",
    correctedText: "The award amount was $50,000.",
    reason: "Original statement added a zero not present in the correcting source.",
    correctingSourceLink: {
      sourceId: "toa-resolution-v2",
      quote: "Award amount: $50,000",
      page: 3,
      sourceContentHash: "sha256:def456",
    },
    publicNote: "Corrected amount from updated resolution.",
    internalNote: "Keep both source traces for audit.",
  }, {
    actor: "BackendCoder",
    now: new Date("2026-06-06T04:15:00.000Z"),
  });

  assert.equal(corrected.status, "false_corrected");
  assert.equal(corrected.text, "The award amount was $50,000.");
  assert.equal(corrected.correctionHistory.length, 1);
  assert.deepEqual(
    {
      priorText: corrected.correctionHistory[0].priorText,
      newText: corrected.correctionHistory[0].newText,
      priorStatus: corrected.correctionHistory[0].priorStatus,
      newStatus: corrected.correctionHistory[0].newStatus,
      actor: corrected.correctionHistory[0].actor,
      publicNote: corrected.correctionHistory[0].publicNote,
      internalNote: corrected.correctionHistory[0].internalNote,
    },
    {
      priorText: "The award amount was $500,000.",
      newText: "The award amount was $50,000.",
      priorStatus: "verified",
      newStatus: "false_corrected",
      actor: "BackendCoder",
      publicNote: "Corrected amount from updated resolution.",
      internalNote: "Keep both source traces for audit.",
    },
  );
  assert.equal(corrected.correctionHistory[0].correctingSourceLink.sourceId, "toa-resolution-v2");
  assert.match(corrected.correctionHistory[0].correctingSourceLink.traceHash, /^[a-f0-9]{64}$/);
});

test("AI analysis cannot be verified as fact", () => {
  const analysis = createStatement({
    id: "stmt-analysis",
    text: "This pattern may indicate weak procurement controls.",
    kind: "ai_analysis",
    sourceLinks: [createSourceLink({
      sourceId: "toa-audit",
      quote: "Three bids were received.",
      page: 9,
    })],
    evidenceLimits: "AI analysis only; not a factual conclusion.",
  });

  assert.throws(
    () => applyVerificationTransition(analysis, { action: "verify" }),
    /AI analysis cannot be verified as fact/,
  );
});

test("publication gate fails missing source links, evidence limits, AI-as-fact, and do-not-publish flags", () => {
  const missingSource = createStatement({
    id: "stmt-no-source",
    text: "The meeting started at 7 p.m.",
    evidenceLimits: "No source attached yet.",
  });
  assert.deepEqual(evaluatePublicationGate(missingSource), {
    publishable: false,
    failures: ["missing_source_links"],
  });

  const missingLimits = createStatement({
    id: "stmt-no-limits",
    text: "The packet contains a contract amendment.",
    sourceLinks: [createSourceLink({
      sourceId: "toa-packet",
      quote: "Contract Amendment",
      page: 14,
    })],
  });
  assert.deepEqual(evaluatePublicationGate(missingLimits), {
    publishable: false,
    failures: ["missing_evidence_limits"],
  });

  const analysisAsFact = createStatement({
    id: "stmt-analysis-as-fact",
    text: "The pattern may require further review.",
    kind: "ai_analysis",
    status: "verified",
    sourceLinks: [createSourceLink({
      sourceId: "toa-minutes",
      quote: "Staff will review the pattern.",
      page: 5,
    })],
    evidenceLimits: "AI analysis label must stay separate from verified facts.",
  });
  assert.deepEqual(evaluatePublicationGate(analysisAsFact), {
    publishable: false,
    failures: ["ai_analysis_as_fact"],
  });

  const doNotPublish = applyVerificationTransition(createStatement({
    id: "stmt-sensitive",
    text: "Internal review note.",
    sourceLinks: [createSourceLink({
      sourceId: "toa-correspondence",
      quote: "Internal review note",
      location: "email body",
    })],
    evidenceLimits: "Internal note is not for publication.",
    sensitivityFlags: ["do_not_publish"],
  }), {
    action: "do_not_publish",
    reason: "Sensitive internal note.",
  });

  assert.deepEqual(evaluatePublicationGate(doNotPublish), {
    publishable: false,
    failures: ["do_not_publish", "do_not_publish_sensitive_flag"],
  });
});
