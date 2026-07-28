import assert from "node:assert/strict";
import test from "node:test";

import {
  analyzeDocContinuity,
  renderContinuityReport,
  FINDING_TYPES,
  CONTINUITY_REPORT_TITLE,
} from "../src/doc-continuity.js";

// A clean baseline that yields ZERO findings (handoffReady === true). Each test
// clones it and perturbs exactly one thing to provoke exactly one finding type.
function baseline() {
  return {
    now: "2026-06-26T00:00:00.000Z",
    manifest: {
      version: "test-1",
      entries: [
        {
          id: "modcont-mod-a",
          path: "Docs/modules/mod-a.md",
          kind: "module_continuity_doc",
          owner: "AutomationOpsEngineer",
          cadence: "per_slice",
          requiredFor: "handoff",
          module: "src/mod-a.js",
          documentedExports: ["BAR", "foo"],
          purpose: true,
          io: true,
          invariants: true,
          testEntry: "node --test test/mod-a.test.js",
        },
      ],
    },
    fileSet: ["src/mod-a.js", "Docs/modules/mod-a.md"],
    moduleExports: { "src/mod-a.js": ["BAR", "foo"] },
  };
}

function clone(x) {
  return JSON.parse(JSON.stringify(x));
}

function typesOf(report) {
  return report.findings.map((f) => f.type).sort();
}

test("baseline repo is clean — zero findings, handoffReady true", () => {
  const report = analyzeDocContinuity(baseline());
  assert.deepEqual(report.findings, []);
  assert.equal(report.handoffReady, true);
  assert.equal(report.counts.total, 0);
  assert.equal(report.manifestVersion, "test-1");
  assert.equal(report.generatedAt, "2026-06-26T00:00:00.000Z");
});

// ── §4.1 documentation-maintenance finding types ─────────────────────────────

test("missing_required_doc (high) when a required artifact has no file", () => {
  const b = baseline();
  b.fileSet = b.fileSet.filter((p) => p !== "Docs/modules/mod-a.md");
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "missing_required_doc");
  assert.ok(f, "expected missing_required_doc");
  assert.equal(f.severity, "high");
  assert.equal(f.subjectId, "modcont-mod-a");
  assert.equal(f.evidence.path, "Docs/modules/mod-a.md");
  assert.equal(report.handoffReady, false);
});

test("missing_module_continuity_doc (high) for a shipped module with no doc entry", () => {
  const b = baseline();
  b.moduleExports["src/mod-b.js"] = ["baz"];
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "missing_module_continuity_doc");
  assert.ok(f);
  assert.equal(f.severity, "high");
  assert.equal(f.subjectId, "src/mod-b.js");
  assert.equal(report.handoffReady, false);
});

test("undocumented_export (medium) when a live export is absent from the doc surface", () => {
  const b = baseline();
  b.moduleExports["src/mod-a.js"] = ["BAR", "foo", "newThing"];
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "undocumented_export");
  assert.ok(f);
  assert.equal(f.severity, "medium");
  assert.deepEqual(f.evidence.missingExports, ["newThing"]);
  assert.equal(report.handoffReady, true); // medium does not block handoff
});

test("documented_nonexistent_export (medium) when the doc lists a removed export", () => {
  const b = baseline();
  b.manifest.entries[0].documentedExports = ["BAR", "foo", "ghost"];
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "documented_nonexistent_export");
  assert.ok(f);
  assert.equal(f.severity, "medium");
  assert.deepEqual(f.evidence.extraExports, ["ghost"]);
});

test("stale_reference (medium) when a referenced path no longer exists", () => {
  const b = baseline();
  b.manifest.entries[0].referencedModules = ["src/gone.js"];
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "stale_reference");
  assert.ok(f);
  assert.equal(f.severity, "medium");
  assert.equal(f.evidence.path, "src/gone.js");
});

test("unreferenced_required_module (medium) when a required reference is omitted", () => {
  const b = baseline();
  b.manifest.entries[0].mustReferenceModules = ["src/dep.js"];
  b.manifest.entries[0].referencedModules = [];
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "unreferenced_required_module");
  assert.ok(f);
  assert.equal(f.severity, "medium");
  assert.equal(f.evidence.path, "src/dep.js");
});

test("cadence_unowned (low) when an entry has no owner or no cadence", () => {
  const b = baseline();
  b.manifest.entries[0].owner = "";
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "cadence_unowned");
  assert.ok(f);
  assert.equal(f.severity, "low");
  assert.equal(report.handoffReady, true);
});

test("orphan_doc (low) for a Docs/*.md file no manifest entry covers", () => {
  const b = baseline();
  b.fileSet.push("Docs/uncovered-note.md");
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "orphan_doc");
  assert.ok(f);
  assert.equal(f.severity, "low");
  assert.equal(f.subjectId, "Docs/uncovered-note.md");
});

// ── §4.2 project-state-continuity finding types ──────────────────────────────

test("handoff_gap (high) when a continuity doc lacks a required field", () => {
  const b = baseline();
  b.manifest.entries[0].purpose = false;
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "handoff_gap");
  assert.ok(f);
  assert.equal(f.severity, "high");
  assert.deepEqual(f.evidence.missingFields, ["purpose"]);
  assert.equal(report.handoffReady, false);
});

test("handoff_gap fires when the public surface list is absent (not merely empty)", () => {
  const b = baseline();
  delete b.manifest.entries[0].documentedExports;
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "handoff_gap");
  assert.ok(f);
  assert.ok(f.evidence.missingFields.includes("publicSurface"));
});

test("missing_test_entry (medium) when the doc names no node --test entry", () => {
  const b = baseline();
  b.manifest.entries[0].testEntry = "";
  const report = analyzeDocContinuity(b);
  const f = report.findings.find((x) => x.type === "missing_test_entry");
  assert.ok(f);
  assert.equal(f.severity, "medium");
  assert.equal(report.handoffReady, true); // medium, per the contract severity table
});

test("ledger_state_unknown (low) is fail-closed, and resolvable when status supplied", () => {
  const b = baseline();
  b.manifest.entries.push({
    id: "ledger-5-14",
    path: "Docs/modules/mod-a.md",
    kind: "stage_ledger_ref",
    owner: "CEO",
    cadence: "on_stage_close",
    requiredFor: "ledger",
    goalRef: "43937de7",
  });
  const unresolved = analyzeDocContinuity(b);
  const f = unresolved.findings.find((x) => x.type === "ledger_state_unknown");
  assert.ok(f, "absent status degrades to ledger_state_unknown");
  assert.equal(f.severity, "low");
  assert.equal(unresolved.handoffReady, true); // fail-closed = low, never a false high

  b.manifest.entries[1].stageLedgerStatus = "active";
  const resolved = analyzeDocContinuity(b);
  assert.equal(resolved.findings.find((x) => x.type === "ledger_state_unknown"), undefined);
});

// ── §5 behavioral invariants ─────────────────────────────────────────────────

test("read-only + idempotent: identical inputs yield deep-equal reports, inputs unmutated", () => {
  const b = baseline();
  b.moduleExports["src/mod-a.js"] = ["BAR", "foo", "newThing"]; // provoke a finding
  const before = JSON.stringify(b);
  const r1 = analyzeDocContinuity(b);
  const r2 = analyzeDocContinuity(b);
  assert.deepEqual(r1, r2);
  assert.equal(JSON.stringify(b), before, "inputs must not be mutated");
});

test("deterministic order: severity desc, then type, then subjectId", () => {
  const b = baseline();
  // Provoke several findings of mixed severity at once.
  b.manifest.entries[0].purpose = false;                  // handoff_gap (high)
  b.moduleExports["src/mod-a.js"] = ["BAR", "foo", "zzz"]; // undocumented_export (medium)
  b.manifest.entries[0].owner = "";                       // cadence_unowned (low)
  b.fileSet.push("Docs/zzz.md");                          // orphan_doc (low)
  const report = analyzeDocContinuity(b);

  const ranks = report.findings.map((f) => ({ high: 0, medium: 1, low: 2 }[f.severity]));
  for (let i = 1; i < ranks.length; i += 1) {
    assert.ok(ranks[i - 1] <= ranks[i], "severity must be non-increasing");
  }
  // Within the two lows, types are alphabetical: cadence_unowned before orphan_doc.
  const lows = report.findings.filter((f) => f.severity === "low").map((f) => f.type);
  assert.deepEqual(lows, ["cadence_unowned", "orphan_doc"]);
});

test("fail-closed: absent module export surface produces no false export-drift finding", () => {
  const b = baseline();
  delete b.moduleExports["src/mod-a.js"]; // caller could not read the surface
  const report = analyzeDocContinuity(b);
  // No undocumented_/documented_nonexistent_ finding, and no false high.
  assert.equal(report.findings.find((x) => x.type === "undocumented_export"), undefined);
  assert.equal(report.findings.find((x) => x.type === "documented_nonexistent_export"), undefined);
  assert.equal(report.counts.bySeverity.high, 0);
});

test("priorSnapshot marks carried findings; absent snapshot is never an error", () => {
  const b = baseline();
  b.moduleExports["src/mod-a.js"] = ["BAR", "foo", "newThing"];
  const first = analyzeDocContinuity(b); // no prior -> carried false
  assert.equal(first.findings[0].carried, false);

  const second = analyzeDocContinuity({ ...b, priorSnapshot: first });
  assert.equal(second.findings[0].carried, true);
});

test("handoffReady is true iff zero high-severity findings", () => {
  const b = baseline();
  b.manifest.entries[0].testEntry = ""; // medium only
  assert.equal(analyzeDocContinuity(b).handoffReady, true);
  b.manifest.entries[0].purpose = false; // add a high
  assert.equal(analyzeDocContinuity(b).handoffReady, false);
});

test("counts tally total / bySeverity / byType consistently", () => {
  const b = baseline();
  b.manifest.entries[0].purpose = false;                  // high
  b.moduleExports["src/mod-a.js"] = ["BAR", "foo", "zzz"]; // medium
  const report = analyzeDocContinuity(b);
  const sum = report.counts.bySeverity.high + report.counts.bySeverity.medium + report.counts.bySeverity.low;
  assert.equal(sum, report.counts.total);
  assert.equal(report.counts.total, report.findings.length);
  const byTypeSum = Object.values(report.counts.byType).reduce((a, c) => a + c, 0);
  assert.equal(byTypeSum, report.counts.total);
});

test("every enumerated finding type has a fixed, sane severity", () => {
  const allowed = new Set(["high", "medium", "low"]);
  for (const [type, sev] of Object.entries(FINDING_TYPES)) {
    assert.ok(allowed.has(sev), `${type} severity must be high|medium|low`);
  }
});

// ── renderer ─────────────────────────────────────────────────────────────────

test("renderContinuityReport is deterministic and reflects the report", () => {
  const b = baseline();
  b.manifest.entries[0].purpose = false;
  const report = analyzeDocContinuity(b);
  const t1 = renderContinuityReport(report);
  const t2 = renderContinuityReport(report);
  assert.equal(t1, t2);
  assert.ok(t1.startsWith(`# ${CONTINUITY_REPORT_TITLE}`));
  assert.ok(t1.includes("# handoffReady: false"));
  assert.ok(t1.includes("[HIGH] handoff_gap | modcont-mod-a"));
  assert.ok(t1.endsWith("\n"));
});

test("renderContinuityReport renders the clean case explicitly", () => {
  const text = renderContinuityReport(analyzeDocContinuity(baseline()));
  assert.ok(text.includes("# handoffReady: true"));
  assert.ok(text.includes("(no findings"));
});

// ── structural guards (programming errors throw) ─────────────────────────────

test("invalid structural input throws (not a silent empty report)", () => {
  assert.throws(() => analyzeDocContinuity({}), /manifest/);
  assert.throws(() => analyzeDocContinuity({ manifest: { entries: [] }, fileSet: "x", moduleExports: {} }), /fileSet/);
  assert.throws(() => analyzeDocContinuity({ manifest: { entries: [] }, fileSet: [], moduleExports: null }), /moduleExports/);
  assert.throws(() => renderContinuityReport(null), /report/);
});
