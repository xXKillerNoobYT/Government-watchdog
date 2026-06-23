import assert from "node:assert/strict";
import test from "node:test";

import {
  applyVerificationTransition,
  createSourceLink,
  createStatement,
  evaluatePublicationGate,
} from "../src/statement-verification.js";
import { assembleDigest } from "../src/digest-assembler.js";

function publishableStatement(id, text, overrides = {}) {
  return createStatement({
    id,
    text,
    status: "verified",
    sourceLinks: [createSourceLink({
      sourceId: overrides.sourceId ?? `toa-${id}`,
      quote: overrides.quote ?? `${text} (source quote)`,
      page: overrides.page ?? 1,
    })],
    evidenceLimits: overrides.evidenceLimits
      ?? "Source proves the quoted line only; no wider claim.",
    ...overrides.statement,
  }, { now: new Date(overrides.now ?? "2026-06-10T00:00:00.000Z"), actor: "reviewer" });
}

test("digest body is byte-identical regardless of input order (stable order)", () => {
  const a = publishableStatement("stmt-a", "Agenda item 4 authorizes a purchase.", {
    now: "2026-06-10T00:00:00.000Z",
  });
  const b = publishableStatement("stmt-b", "Minutes record the grant was received.", {
    now: "2026-06-11T00:00:00.000Z",
  });
  const c = publishableStatement("stmt-c", "Resolution sets the award at $50,000.", {
    now: "2026-06-12T00:00:00.000Z",
  });

  const forward = assembleDigest([a, b, c]).body;
  const reversed = assembleDigest([c, b, a]).body;
  const repeated = assembleDigest([b, a, c]).body;

  assert.equal(forward, reversed);
  assert.equal(forward, repeated);
  // Chronological createdAt ordering is honored, not the input array order.
  assert.ok(forward.indexOf("stmt-a") < forward.indexOf("stmt-b"));
  assert.ok(forward.indexOf("stmt-b") < forward.indexOf("stmt-c"));
});

test("every digest line traces back to a publishable statement with a source trace", () => {
  const a = publishableStatement("stmt-a", "Agenda item 4 authorizes a purchase.");
  const b = publishableStatement("stmt-b", "Minutes record the grant was received.", {
    now: "2026-06-11T00:00:00.000Z",
  });

  const result = assembleDigest([a, b]);

  assert.equal(result.included.length, 2);
  for (const item of result.included) {
    // The selected statement really passes the reused gate...
    const source = item.id === "stmt-a" ? a : b;
    assert.equal(evaluatePublicationGate(source).publishable, true);
    // ...carries at least one trace hash...
    assert.ok(item.sourceLinks.length >= 1);
    assert.match(item.sourceLinks[0].traceHash, /^[a-f0-9]{64}$/);
    // ...and its id + trace hash appear in the rendered body.
    assert.ok(result.body.includes(`- ${item.id} |`));
    assert.ok(result.body.includes(`trace=${item.sourceLinks[0].traceHash}`));
  }
});

test("non-publishable statements are excluded with a logged reason (no silent drops)", () => {
  const ok = publishableStatement("stmt-ok", "Packet contains a contract amendment.");

  const missingLimits = createStatement({
    id: "stmt-no-limits",
    text: "Meeting started at 7 p.m.",
    status: "verified",
    sourceLinks: [createSourceLink({ sourceId: "toa-x", quote: "7:00 p.m. call to order", page: 1 })],
  });

  const doNotPublish = applyVerificationTransition(publishableStatement(
    "stmt-sensitive",
    "Internal review note.",
  ), { action: "do_not_publish", reason: "Sensitive internal note." });

  const result = assembleDigest([ok, missingLimits, doNotPublish]);

  // Only the clean statement is published.
  assert.deepEqual(result.included.map((i) => i.id), ["stmt-ok"]);

  // Both failures are recorded, never silently dropped.
  const excludedById = new Map(result.excluded.map((e) => [e.id, e]));
  assert.deepEqual(excludedById.get("stmt-no-limits").failures, ["missing_evidence_limits"]);
  assert.ok(excludedById.get("stmt-sensitive").failures.includes("do_not_publish"));

  // Every exclusion has a matching log entry (parity = no silent drops).
  assert.equal(result.log.length, result.excluded.length);
  assert.deepEqual(
    result.log.map((l) => l.statementId).sort(),
    ["stmt-no-limits", "stmt-sensitive"],
  );
  assert.ok(result.log.every((l) => l.level === "EXCLUDE" && l.reason.length > 0));

  // Excluded ids never leak into the digest body.
  assert.ok(!result.body.includes("stmt-no-limits"));
  assert.ok(!result.body.includes("stmt-sensitive"));
});

test("publishable statement whose source link lost its trace hash is excluded as a defect", () => {
  // A statement that passes the gate (has a source link + limits) but whose link
  // carries no trace hash must not reach the digest — it would be untraceable.
  const untraceable = {
    ...publishableStatement("stmt-untraceable", "Claim without a usable trace."),
    sourceLinks: [{ sourceId: "toa-x", quote: "q", page: 1, traceHash: null }],
  };

  assert.equal(evaluatePublicationGate(untraceable).publishable, true);

  const result = assembleDigest([untraceable]);
  assert.equal(result.included.length, 0);
  assert.deepEqual(result.excluded[0].failures, ["missing_trace_hash"]);
  assert.equal(result.log[0].reason, "missing_trace_hash");
});
