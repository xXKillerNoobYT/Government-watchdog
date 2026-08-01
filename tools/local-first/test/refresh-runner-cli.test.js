import assert from "node:assert/strict";
import test from "node:test";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);
const here = dirname(fileURLToPath(import.meta.url));
const packageRoot = resolve(here, "..");

// ── the CLI wrapper itself: the surface the doc-continuity/handoff-escalation
// incident showed no other test exercises. runWeeklyRefresh (the pure function)
// is covered thoroughly by refresh-runner.test.js; this file instead runs the
// real `main()` entry point — argv parsing, the fixtures bootstrap, and the
// --apply persistence path — so a broken outward-reaching file lookup (the
// GOV-1566/PR#171 defect class) fails loudly here instead of silently.

test("refresh-runner CLI (bare invocation) bootstraps from its own package fixtures with no ENOENT", async () => {
  let stdout = "";
  let stderr = "";
  try {
    ({ stdout, stderr } = await run(process.execPath, ["src/refresh-runner.js"], { cwd: packageRoot }));
  } catch (err) {
    stdout = err.stdout ?? "";
    stderr = err.stderr ?? "";
  }
  assert.ok(!stderr.includes("ENOENT"), `refresh-runner CLI could not read a committed fixture:\n${stderr}`);
  assert.match(stdout, /=== Stage 4\.F2 weekly refresh \(DRY-RUN\) ===/, "CLI produced no dry-run banner");
  assert.match(stdout, /reviewer-internal digest \(\d+ publishable\)/, "CLI produced no digest section");
  assert.match(stdout, /SUMMARY checked=\d+/, "CLI run log carries no SUMMARY line");
});

test("refresh-runner CLI --apply persists the digest + run log to the requested --out/--log paths", async () => {
  const dir = await mkdtemp(join(tmpdir(), "gov1640-refresh-cli-"));
  const outPath = join(dir, "digest.md");
  const logPath = join(dir, "run.log");

  try {
    // No --state: exercises the same fixtures-bootstrap path as the bare
    // invocation above, but with --apply so the write side (--out/--log) runs.
    const { stdout, stderr } = await run(
      process.execPath,
      ["src/refresh-runner.js", "--apply", "--out", outPath, "--log", logPath],
      { cwd: packageRoot },
    );

    assert.ok(!stderr.includes("ENOENT"), `refresh-runner --apply hit ENOENT:\n${stderr}`);
    assert.match(stdout, /=== Stage 4\.F2 weekly refresh \(APPLY\) ===/);
    assert.match(stdout, /\[APPLIED\] state \+ digest persisted\./);

    const digestBody = await readFile(outPath, "utf8");
    assert.match(digestBody, /# Reviewer-Internal Weekly Digest/);

    const runLog = await readFile(logPath, "utf8");
    assert.match(runLog, /^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[(INFO|WARN|ERROR)\] /m);
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});
