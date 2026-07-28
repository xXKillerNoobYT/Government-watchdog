import assert from "node:assert/strict";
import test from "node:test";
import { execFile } from "node:child_process";
import { access, readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));

// ── the two-root mapping this package lives under ────────────────────────────
// This package was extracted out of a single-root local-first workspace: code
// landed in tools/local-first/, committed prose + models in Docs/local-first/.
// The manifest still speaks the original vocabulary ("Docs/…", "src/…",
// "README.md"), which stays the stable contract — so every committed-path
// lookup has to re-join the two roots.
//
// The extraction shipped with three paths still pointing at the pre-split
// layout. Two of them were inside CLI main() wrappers that no test exercised,
// so they failed silently with ENOENT; only the third was caught, by a unit
// test that happened to read the same file. This suite pins the mapping and
// exercises the CLI entry points, so the next relocation fails loudly here.
const packageRoot = resolve(here, "..");
const docsRoot = resolve(packageRoot, "../..", "Docs/local-first");

// Manifest paths are keyed in the original workspace's vocabulary; "Docs/x"
// resolves under docsRoot, everything else ("src/x", "README.md") under the
// package root.
function resolveManifestPath(relPath) {
  return relPath.startsWith("Docs/")
    ? resolve(docsRoot, relPath.slice("Docs/".length))
    : resolve(packageRoot, relPath);
}

async function missingOf(relPaths) {
  const missing = [];
  for (const rel of relPaths) {
    try {
      await access(resolveManifestPath(rel));
    } catch {
      missing.push(rel);
    }
  }
  return missing;
}

const readJson = async (absPath) => JSON.parse(await readFile(absPath, "utf8"));

// ── the committed data files each CLI loads at startup ───────────────────────

test("committed handoff-escalation model resolves and parses", async () => {
  const model = await readJson(resolve(docsRoot, "handoff-escalation-model.json"));
  assert.ok(Array.isArray(model.transitions) && model.transitions.length > 0,
    "model must carry at least one transition");
});

test("committed doc-maintenance manifest resolves and parses", async () => {
  const manifest = await readJson(resolve(docsRoot, "doc-maintenance-manifest.json"));
  assert.ok(Array.isArray(manifest.entries) && manifest.entries.length > 0,
    "manifest must carry at least one entry");
});

// ── every path the manifest declares must exist under one of the two roots ───

test("every manifest-declared doc path exists on disk", async () => {
  const manifest = await readJson(resolve(docsRoot, "doc-maintenance-manifest.json"));
  const declared = manifest.entries.map((e) => e.path).filter(Boolean);
  const missing = await missingOf(declared);
  assert.deepEqual(missing, [], `manifest declares doc paths that do not exist: ${missing.join(", ")}`);
});

test("every manifest-declared module path exists on disk", async () => {
  const manifest = await readJson(resolve(docsRoot, "doc-maintenance-manifest.json"));
  const declared = manifest.entries.map((e) => e.module).filter(Boolean);
  const missing = await missingOf(declared);
  assert.deepEqual(missing, [], `manifest declares modules that do not exist: ${missing.join(", ")}`);
});

// ── the CLI wrappers themselves: the surface that failed silently ────────────
// Asserted on "loads its committed data without ENOENT", not on exit code —
// doc-continuity exits non-zero whenever the repo is not handoff-ready, which
// is a legitimate content signal and must not make this path test flaky.

for (const entry of ["handoff-escalation", "doc-continuity"]) {
  test(`${entry} CLI loads its committed data (no ENOENT)`, async () => {
    let stdout = "";
    let stderr = "";
    try {
      ({ stdout, stderr } = await run(process.execPath, [`src/${entry}.js`], { cwd: packageRoot }));
    } catch (err) {
      // Non-zero exit is allowed; a missing committed file is not.
      stdout = err.stdout ?? "";
      stderr = err.stderr ?? "";
    }
    assert.ok(!stderr.includes("ENOENT"),
      `${entry} CLI could not read a committed file:\n${stderr}`);
    assert.match(stdout, /^# /, `${entry} CLI produced no report header`);
  });
}
