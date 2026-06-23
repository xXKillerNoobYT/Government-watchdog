import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { buildSourceCapture, applyReplacementDetection } from "../src/source-registry.js";
import { createStatement, applyVerificationTransition } from "../src/statement-verification.js";
import {
  runWeeklyRefresh,
  renderRunLog,
  formatLogLine,
  ALPINE_HOSTS,
} from "../src/refresh-runner.js";

const NOW = new Date("2026-06-23T01:00:00.000Z");

// ── Helpers: build a real registry + verified statements over temp capture files.

async function makeRoot() {
  return mkdtemp(join(tmpdir(), "gov479-"));
}

async function writeCapture(root, name, body) {
  const path = join(root, name);
  await mkdir(root, { recursive: true });
  await writeFile(path, body);
  return path;
}

function sourceDef(id, name, path) {
  return {
    id,
    sourceUrl: `https://townofalpine.example.gov/records/${name}.pdf`,
    sourceClass: "resolution",
    title: `Town of Alpine ${name}`,
    toaLocalPath: path,
    capture: { method: "local_file", capturedBy: "test", mimeType: "text/markdown" },
  };
}

async function importRegistry(defs) {
  let registry = [];
  for (const def of defs) {
    const capture = await buildSourceCapture(def, { now: NOW });
    registry = applyReplacementDetection(registry, capture, { now: NOW });
  }
  return registry;
}

function verifiedStatement(id, sourceRecord) {
  const statement = createStatement({
    id,
    text: `Fact backed by ${sourceRecord.id}.`,
    kind: "fact_claim",
    evidenceLimits: "Proves the quoted line only.",
    sourceLinks: [
      {
        sourceId: sourceRecord.id,
        quote: `Fact backed by ${sourceRecord.id}.`,
        page: 1,
        sourceContentHash: sourceRecord.contentHash.value,
      },
    ],
  }, { now: NOW });
  return applyVerificationTransition(statement, { action: "verify" }, { now: NOW });
}

// ── AC2: idempotency — unchanged sources produce no new records + identical digest.

test("idempotent pass over unchanged sources adds no records and a byte-identical digest", async () => {
  const root = await makeRoot();
  const pathA = await writeCapture(root, "a.md", "alpha content\n");
  const pathB = await writeCapture(root, "b.md", "beta content\n");
  const defs = [sourceDef("src-a", "a", pathA), sourceDef("src-b", "b", pathB)];
  const registry = await importRegistry(defs);
  const statements = registry.map((r, i) => verifiedStatement(`stmt-${i}`, r));

  // Week-2 re-capture batch: same URLs/paths/content, fresh capture ids.
  const wk2 = [sourceDef("src-a-wk2", "a", pathA), sourceDef("src-b-wk2", "b", pathB)];

  const run1 = await runWeeklyRefresh(
    { priorRegistry: registry, statements, sourceDefs: wk2 },
    { now: NOW },
  );

  // No new records: registry length + ids unchanged from the prior registry.
  assert.equal(run1.registry.length, registry.length);
  assert.deepEqual(run1.registry.map((r) => r.id), registry.map((r) => r.id));
  assert.equal(run1.counts.unchanged, 2);
  assert.equal(run1.counts.replaced, 0);
  assert.equal(run1.counts.statementsReopened, 0);
  assert.equal(run1.digest.included.length, 2);

  // Run again over the result -> byte-identical digest body (the health signal).
  const run2 = await runWeeklyRefresh(
    { priorRegistry: run1.registry, statements: run1.statements, sourceDefs: wk2 },
    { now: NOW },
  );
  assert.equal(run2.registry.length, registry.length);
  assert.equal(run2.digest.body, run1.digest.body);
  assert.equal(run2.counts.unchanged, 2);

  await rm(root, { recursive: true, force: true });
});

// ── AC1: changed hash -> replacement + statement re-open + dropped from digest.

test("changed source hash marks prior replaced, registers current, and re-opens the bound statement", async () => {
  const root = await makeRoot();
  const pathA = await writeCapture(root, "a.md", "alpha v1\n");
  const pathB = await writeCapture(root, "b.md", "beta content\n");
  const defs = [sourceDef("src-a", "a", pathA), sourceDef("src-b", "b", pathB)];
  const registry = await importRegistry(defs);
  const stmtA = verifiedStatement("stmt-a", registry.find((r) => r.id === "src-a"));
  const stmtB = verifiedStatement("stmt-b", registry.find((r) => r.id === "src-b"));

  // Source A content changes; B unchanged.
  await writeFile(pathA, "alpha v2 — amended\n");
  const wk2 = [sourceDef("src-a-wk2", "a", pathA), sourceDef("src-b-wk2", "b", pathB)];

  const run = await runWeeklyRefresh(
    { priorRegistry: registry, statements: [stmtA, stmtB], sourceDefs: wk2 },
    { now: NOW },
  );

  assert.equal(run.counts.replaced, 1);
  assert.equal(run.counts.unchanged, 1);
  // Prior A record is now `replaced`; new `current` for A's URL exists.
  const priorA = run.registry.find((r) => r.id === "src-a");
  assert.equal(priorA.lifecycleStatus, "replaced");
  const currentA = run.registry.find(
    (r) => r.canonicalUrl === priorA.canonicalUrl && r.lifecycleStatus === "current",
  );
  assert.equal(currentA.id, "src-a-wk2");

  // stmt-a re-opened (verified->unverified); stmt-b still verified.
  const outA = run.statements.find((s) => s.id === "stmt-a");
  const outB = run.statements.find((s) => s.id === "stmt-b");
  assert.equal(outA.status, "unverified");
  assert.equal(outB.status, "verified");
  assert.equal(run.counts.statementsReopened, 1);

  // Re-opened statement dropped from the digest; only stmt-b remains, byte-stable.
  assert.equal(run.digest.included.length, 1);
  assert.equal(run.digest.included[0].id, "stmt-b");
  assert.ok(run.log.some((e) => e.level === "WARN" && e.msg.includes("REOPENED statement stmt-a")));

  await rm(root, { recursive: true, force: true });
});

// ── AC4/§5: missing-after-capture is flagged, never silent, and raises an issue.

test("missing-after-capture on a published source is flagged and raises an issue candidate", async () => {
  const root = await makeRoot();
  const pathA = await writeCapture(root, "a.md", "alpha content\n");
  const defs = [sourceDef("src-a", "a", pathA)];
  const registry = await importRegistry(defs);
  const stmtA = verifiedStatement("stmt-a", registry[0]);

  // Delete the local capture before the weekly pass.
  await rm(pathA, { force: true });
  const wk2 = [sourceDef("src-a-wk2", "a", pathA)];

  const run = await runWeeklyRefresh(
    { priorRegistry: registry, statements: [stmtA], sourceDefs: wk2 },
    { now: NOW },
  );

  assert.equal(run.counts.missing, 1);
  assert.ok(run.log.some((e) => e.level === "WARN" && e.msg.startsWith("MISSING source")));
  // Statement backed by the now-missing source is re-opened and excluded.
  assert.equal(run.statements.find((s) => s.id === "stmt-a").status, "unverified");
  assert.equal(run.digest.included.length, 0);
  assert.ok(run.issueCandidates.some((i) => i.type === "missing_after_capture_published"));

  await rm(root, { recursive: true, force: true });
});

// ── AC/§6: scope leak — non-Alpine source is rejected, never processed, hard stop.

test("non-Alpine source is rejected as a scope leak and never registered", async () => {
  const root = await makeRoot();
  const pathA = await writeCapture(root, "a.md", "alpha content\n");
  const leak = {
    id: "evil-src",
    sourceUrl: "https://not-alpine.example.com/records/x.pdf",
    sourceClass: "resolution",
    title: "Out of scope",
    toaLocalPath: pathA,
    capture: { method: "local_file", capturedBy: "test", mimeType: "text/markdown" },
  };

  const run = await runWeeklyRefresh(
    { priorRegistry: [], statements: [], sourceDefs: [leak] },
    { now: NOW },
  );

  assert.equal(run.counts.scopeRejected, 1);
  assert.equal(run.counts.sourcesChecked, 0);
  assert.equal(run.registry.length, 0); // never registered
  assert.ok(run.issueCandidates.some((i) => i.type === "scope_leak"));
  assert.equal(run.ok, false); // hard stop -> non-zero exit
  assert.ok(run.log.some((e) => e.level === "ERROR" && e.msg.includes("SCOPE_LEAK")));

  await rm(root, { recursive: true, force: true });
});

// ── AC4/§8: run-log writer format `[YYYY-MM-DD HH:MM:SS] [LEVEL] msg`.

test("run-log lines use the contract timestamp+level format and carry the summary counts", async () => {
  const root = await makeRoot();
  const pathA = await writeCapture(root, "a.md", "alpha content\n");
  const registry = await importRegistry([sourceDef("src-a", "a", pathA)]);
  const stmtA = verifiedStatement("stmt-a", registry[0]);

  const run = await runWeeklyRefresh(
    { priorRegistry: registry, statements: [stmtA], sourceDefs: [sourceDef("src-a-wk2", "a", pathA)] },
    { now: NOW },
  );

  const rendered = renderRunLog(run);
  const lineRe = /^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[(INFO|WARN|ERROR)\] .+$/;
  for (const line of rendered.split("\n").filter((l) => l.length > 0)) {
    assert.match(line, lineRe);
  }
  assert.ok(rendered.includes("SUMMARY checked=1 unchanged=1"));
  assert.ok(rendered.includes("digestLines="));

  // formatLogLine is deterministic for a fixed timestamp.
  assert.equal(
    formatLogLine(NOW, "INFO", "hello"),
    "[2026-06-23 01:00:00] [INFO] hello",
  );

  await rm(root, { recursive: true, force: true });
});

test("ALPINE_HOSTS is the documented allowlist", () => {
  assert.ok(ALPINE_HOSTS.includes("townofalpine.example.gov"));
});
