// Stage 5.14 / GOV-581 — deterministic, reviewer-internal documentation-maintenance
// & project-state-continuity detector.
//
// Mirrors the substrate's posture (digest-assembler / refresh-runner): pure,
// read-only, deterministic, fail-closed, no network, no AI in the detection path,
// no public output. `analyzeDocContinuity` is a pure function of its explicit
// inputs — it never walks the filesystem, never calls Date.now()/Math.random(),
// and never mutates its arguments. The (impure) caller enumerates the repo file
// set + the live module export surface and passes them in; see `main()` below.
//
// Scope boundary vs the 5.13 back-gap analyzer (Python substrate): that analyzer
// audits *verification records*. This detector audits *documentation + module
// state*. They share the read-only/deterministic/fail-closed report shape but
// operate on disjoint inputs and are intentionally NOT merged (contract §4 note).

import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const CONTINUITY_REPORT_TITLE = "Stage 5.14 Doc-Continuity Report";

// Enumerated finding types (contract §4.1 / §4.2) with their fixed severities.
// Severity is a property of the *type*, never AI-decided.
export const FINDING_TYPES = Object.freeze({
  // §4.1 documentation-maintenance
  missing_required_doc: "high",
  missing_module_continuity_doc: "high",
  undocumented_export: "medium",
  documented_nonexistent_export: "medium",
  stale_reference: "medium",
  unreferenced_required_module: "medium",
  cadence_unowned: "low",
  orphan_doc: "low",
  // §4.2 project-state-continuity
  handoff_gap: "high",
  missing_test_entry: "medium",
  ledger_state_unknown: "low",
});

const SEVERITY_RANK = Object.freeze({ high: 0, medium: 1, low: 2 });

// Doc-prose continuity fields a module_continuity_doc entry must assert so a fresh
// agent can resume with no out-of-band knowledge (contract §4.2 handoff_gap). The
// `node --test` entry is checked separately as `missing_test_entry` (medium), so
// handoff_gap (high) covers only the four non-test fields — every required field
// is covered exactly once, at the severity the contract assigns it.
const HANDOFF_FIELDS = Object.freeze(["purpose", "publicSurface", "io", "invariants"]);

// Only prose docs participate in orphan-doc drift; data/manifest files do not.
const ORPHAN_DOC_PREFIX = "Docs/";
const ORPHAN_DOC_SUFFIX = ".md";

export function analyzeDocContinuity(input = {}) {
  const { manifest, fileSet, moduleExports, priorSnapshot, now } = input;

  // Structural validation throws (programming error), mirroring assembleDigest.
  // Data ambiguity *within* a valid structure degrades to findings (fail-closed).
  if (!manifest || typeof manifest !== "object" || !Array.isArray(manifest.entries)) {
    throw new Error("analyzeDocContinuity requires manifest:{ version, entries:[...] }");
  }
  if (!Array.isArray(fileSet)) {
    throw new Error("analyzeDocContinuity requires fileSet to be an array of paths");
  }
  if (!moduleExports || typeof moduleExports !== "object") {
    throw new Error("analyzeDocContinuity requires moduleExports to be an object");
  }

  const files = new Set(fileSet);
  const exportsByModule = moduleExports; // { "src/x.js": ["a","b"] }
  const entries = manifest.entries;

  // Modules documented by a module_continuity_doc entry -> { module: entry }.
  const docByModule = new Map();
  for (const e of entries) {
    if (e && e.kind === "module_continuity_doc" && nonEmptyString(e.module)) {
      docByModule.set(e.module, e);
    }
  }

  const findings = [];
  const add = (type, subjectId, detail, evidence) =>
    findings.push({
      type,
      severity: FINDING_TYPES[type],
      subjectId,
      detail,
      carried: false, // back-filled below from priorSnapshot
      evidence: evidence ?? {},
    });

  // ── §4.1 documentation-maintenance ────────────────────────────────────────
  for (const e of entries) {
    if (!e || !nonEmptyString(e.id)) continue;

    // missing_required_doc — every manifest-required artifact must have a file.
    if (nonEmptyString(e.path) && !files.has(e.path)) {
      add("missing_required_doc", e.id, `no file at required path ${e.path}`, { path: e.path });
    }

    // cadence_unowned — an entry with no owner or no cadence cannot be maintained.
    if (!nonEmptyString(e.owner) || !nonEmptyString(e.cadence)) {
      add("cadence_unowned", e.id, "manifest entry missing owner and/or cadence", {
        owner: e.owner ?? null,
        cadence: e.cadence ?? null,
      });
    }

    // stale_reference — a referenced path that no longer exists in the repo.
    for (const ref of asArray(e.referencedModules)) {
      if (!files.has(ref) && !(ref in exportsByModule)) {
        add("stale_reference", e.id, `references missing path ${ref}`, { path: ref });
      }
    }

    // unreferenced_required_module — a required reference the doc omits.
    const referenced = new Set(asArray(e.referencedModules));
    for (const req of asArray(e.mustReferenceModules)) {
      if (!referenced.has(req)) {
        add("unreferenced_required_module", e.id, `required reference ${req} not present`, {
          path: req,
        });
      }
    }

    // ledger_state_unknown — fail-closed: a stage-ledger ref we cannot resolve to
    // a goal status is reported as explicitly unknown, never as a confirmed gap.
    if (e.kind === "stage_ledger_ref" && !nonEmptyString(e.stageLedgerStatus)) {
      add("ledger_state_unknown", e.id, "stage-ledger goal status could not be resolved", {
        goalRef: e.goalRef ?? null,
      });
    }
  }

  // missing_module_continuity_doc — every shipped src module needs a doc entry.
  for (const modulePath of Object.keys(exportsByModule).sort()) {
    if (!docByModule.has(modulePath)) {
      add("missing_module_continuity_doc", modulePath, `shipped module ${modulePath} has no continuity doc`, {
        path: modulePath,
      });
    }
  }

  // Export drift + handoff completeness, per documented module.
  for (const [modulePath, e] of docByModule) {
    const live = exportsByModule[modulePath];

    // Fail-closed: if the caller could not supply the live export surface we do
    // NOT assert undocumented/nonexistent drift (would be a false gap). Export
    // checks run only when a real surface is present.
    if (Array.isArray(live)) {
      const documented = new Set(asArray(e.documentedExports));
      const liveSet = new Set(live);

      const missingExports = live.filter((n) => !documented.has(n)).sort();
      if (missingExports.length > 0) {
        add("undocumented_export", e.id, `${missingExports.length} export(s) absent from doc public surface`, {
          path: e.path ?? null,
          missingExports,
        });
      }

      const extraExports = asArray(e.documentedExports).filter((n) => !liveSet.has(n)).sort();
      if (extraExports.length > 0) {
        add("documented_nonexistent_export", e.id, `${extraExports.length} documented export(s) no longer exist`, {
          path: e.path ?? null,
          extraExports,
        });
      }
    }

    // handoff_gap — the four non-test continuity fields must all be present.
    const missingFields = HANDOFF_FIELDS.filter((f) => !hasHandoffField(e, f));
    if (missingFields.length > 0) {
      add("handoff_gap", e.id, `continuity doc missing: ${missingFields.join(", ")}`, {
        path: e.path ?? null,
        missingFields,
      });
    }

    // missing_test_entry — a runnable `node --test` entry point is required.
    if (!nonEmptyString(e.testEntry)) {
      add("missing_test_entry", e.id, "continuity doc names no node --test entry point", {
        path: e.path ?? null,
      });
    }
  }

  // orphan_doc — a prose Docs/*.md file no manifest entry covers (possible drift).
  const coveredPaths = new Set(entries.filter((e) => e && nonEmptyString(e.path)).map((e) => e.path));
  for (const path of [...files].sort()) {
    if (path.startsWith(ORPHAN_DOC_PREFIX) && path.endsWith(ORPHAN_DOC_SUFFIX) && !coveredPaths.has(path)) {
      add("orphan_doc", path, `Docs file ${path} is not covered by any manifest entry`, { path });
    }
  }

  // ── carried flag (triage ordering only — never an error if snapshot absent) ──
  const priorKeys = priorSnapshotKeys(priorSnapshot);
  for (const f of findings) {
    f.carried = priorKeys.has(findingKey(f));
  }

  // ── deterministic order: severity desc, then type, then subjectId ───────────
  findings.sort(compareFindings);

  const counts = tallyCounts(findings);
  const handoffReady = counts.bySeverity.high === 0;
  const generatedAt = normalizeNow(now);

  const report = {
    generatedAt,
    manifestVersion: manifest.version ?? null,
    findings,
    counts,
    handoffReady,
    summary:
      `continuity: ${counts.total} finding(s) ` +
      `(${counts.bySeverity.high} high, ${counts.bySeverity.medium} medium, ${counts.bySeverity.low} low) ` +
      `— handoffReady=${handoffReady}`,
  };
  return report;
}

// Deterministic text render, mirroring refresh-runner.renderRunLog /
// digest-assembler.renderBody (lines array joined with "\n", trailing newline).
export function renderContinuityReport(report) {
  if (!report || typeof report !== "object" || !Array.isArray(report.findings)) {
    throw new Error("renderContinuityReport requires an analyzeDocContinuity report");
  }
  const c = report.counts;
  const lines = [
    `# ${CONTINUITY_REPORT_TITLE}`,
    `# generatedAt: ${report.generatedAt}`,
    `# manifestVersion: ${report.manifestVersion ?? "(none)"}`,
    `# findings: total=${c.total} high=${c.bySeverity.high} medium=${c.bySeverity.medium} low=${c.bySeverity.low}`,
    `# handoffReady: ${report.handoffReady}`,
    "",
  ];
  if (report.findings.length === 0) {
    lines.push("(no findings — documentation is current and handoff-ready)");
  }
  for (const f of report.findings) {
    const carried = f.carried ? " [carried]" : "";
    lines.push(`- [${f.severity.toUpperCase()}] ${f.type} | ${f.subjectId} | ${f.detail}${carried}`);
  }
  lines.push("");
  lines.push(`# ${report.summary}`);
  return lines.join("\n") + "\n";
}

// ── internal helpers (no AI, no clock, no network) ──────────────────────────

function hasHandoffField(entry, field) {
  if (field === "publicSurface") {
    // Present iff the doc enumerates a public surface (an array, possibly empty
    // for an intentionally side-effect-only module — emptiness is a deliberate
    // documented choice, absence is the gap).
    return Array.isArray(entry.documentedExports);
  }
  const v = entry[field];
  return v === true || nonEmptyString(v);
}

function tallyCounts(findings) {
  const bySeverity = { high: 0, medium: 0, low: 0 };
  const byType = {};
  for (const f of findings) {
    bySeverity[f.severity] += 1;
    byType[f.type] = (byType[f.type] ?? 0) + 1;
  }
  return { total: findings.length, bySeverity, byType };
}

function compareFindings(a, b) {
  const sr = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
  if (sr !== 0) return sr;
  if (a.type !== b.type) return a.type < b.type ? -1 : 1;
  if (a.subjectId !== b.subjectId) return a.subjectId < b.subjectId ? -1 : 1;
  return 0;
}

function findingKey(f) {
  return `${f.type}::${f.subjectId}`;
}

function priorSnapshotKeys(priorSnapshot) {
  const keys = new Set();
  if (!priorSnapshot) return keys; // absent ⇒ all carried=false, never an error
  const priorFindings = Array.isArray(priorSnapshot)
    ? priorSnapshot
    : Array.isArray(priorSnapshot.findings)
      ? priorSnapshot.findings
      : [];
  for (const f of priorFindings) {
    if (f && nonEmptyString(f.type) && f.subjectId !== undefined) {
      keys.add(findingKey(f));
    }
  }
  return keys;
}

function normalizeNow(now) {
  if (now instanceof Date) return now.toISOString();
  if (nonEmptyString(now)) return now;
  // Fail-closed: an absent clock is echoed explicitly, never silently defaulted
  // to the real wall clock (that would break determinism).
  return "(now-not-supplied)";
}

function asArray(v) {
  return Array.isArray(v) ? v : [];
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

// ── CLI: enumerate the real repo, run the detector, print the report ─────────
// The pure detector above takes data; this wrapper does the impure I/O (reading
// the manifest, enumerating the file set, importing modules for their live export
// surface) and exits non-zero when the repo is NOT handoff-ready so a scheduler /
// reviewer notices. Read-only: it writes nothing.

async function main() {
  const { readFile, readdir } = await import("node:fs/promises");
  const { dirname } = await import("node:path");

  // This package was extracted out of a single-root local-first workspace, so
  // its code and its committed docs now live under two different roots: code at
  // tools/local-first/, prose + committed models at <repo>/Docs/local-first/.
  // The manifest still speaks the original workspace's vocabulary ("Docs/…",
  // "src/…", "README.md"), which stays the stable contract — so the two roots
  // are re-joined here, at the I/O boundary, and the pure detector below never
  // learns that the split happened.
  const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const docsRoot = resolve(packageRoot, "../..", "Docs/local-first");
  const manifestPath = resolve(docsRoot, "doc-maintenance-manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));

  // fileSet: every committed prose doc + every src/test module, keyed by the
  // manifest's relative paths regardless of which root actually holds them.
  const fileSet = [];
  // dir = where to read; prefix = the manifest key that directory maps onto.
  // Keeping those separate is what lets one manifest vocabulary span two roots.
  const collect = async (dir, prefix, filterExt) => {
    let dirents;
    try {
      dirents = await readdir(dir, { withFileTypes: true });
    } catch {
      return; // dir absent ⇒ contributes nothing (fail-closed)
    }
    for (const d of dirents) {
      const rel = prefix ? `${prefix}/${d.name}` : d.name;
      if (d.isDirectory()) await collect(resolve(dir, d.name), rel, filterExt);
      else if (!filterExt || d.name.endsWith(filterExt)) fileSet.push(rel);
    }
  };
  // "Docs/<x>" entries live under docsRoot; code entries under packageRoot.
  await collect(docsRoot, "Docs", null);
  await collect(resolve(packageRoot, "src"), "src", ".js");
  await collect(resolve(packageRoot, "test"), "test", ".js");

  // Root-level required artifacts (e.g. README.md) — direct files only, so a
  // manifest entry whose path has no directory prefix resolves correctly.
  try {
    const rootDirents = await readdir(packageRoot, { withFileTypes: true });
    for (const d of rootDirents) {
      if (d.isFile()) fileSet.push(d.name);
    }
  } catch {
    /* root unreadable ⇒ contributes nothing (fail-closed) */
  }

  // moduleExports: live named exports of each src/*.js (dynamic import = ground
  // truth, no regex drift). Modules are pure at import time.
  const moduleExports = {};
  for (const path of fileSet.filter((p) => p.startsWith("src/") && p.endsWith(".js"))) {
    const mod = await import(resolve(packageRoot, path));
    moduleExports[path] = Object.keys(mod).filter((k) => k !== "default").sort();
  }

  let priorSnapshot = null;
  const snapArgIdx = process.argv.indexOf("--prior");
  if (snapArgIdx !== -1 && process.argv[snapArgIdx + 1]) {
    priorSnapshot = JSON.parse(await readFile(process.argv[snapArgIdx + 1], "utf8"));
  }

  const now = new Date(); // CLI may use the wall clock; the pure fn never does.
  const report = analyzeDocContinuity({ manifest, fileSet, moduleExports, priorSnapshot, now });
  process.stdout.write(renderContinuityReport(report));
  return report.handoffReady ? 0 : 1;
}

const invokedDirectly = process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main()
    .then((code) => process.exit(code))
    .catch((error) => {
      process.stderr.write(`doc-continuity failed: ${error?.stack ?? error}\n`);
      process.exit(1);
    });
}
