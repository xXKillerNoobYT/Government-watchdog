import assert from "node:assert/strict";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  applyReplacementDetection,
  buildSourceCapture,
  hashFileSha256,
  importSources,
} from "../src/source-registry.js";

test("hashFileSha256 returns stable SHA-256 content hashes", async () => {
  const root = await makeFixtureRoot();
  const file = join(root, "Documents/TOA/TownOfAlpine/directives/source.md");
  await mkdir(join(root, "Documents/TOA/TownOfAlpine/directives"), { recursive: true });
  await writeFile(file, "official source content\n");

  const hash = await hashFileSha256(file);

  assert.equal(hash, "a6f2d58656d1fc0511169738394234f78bc6a27cb67e3d8f84946872394fb95d");
  await rm(root, { recursive: true, force: true });
});

test("same canonical URL with changed hash creates replacement records", async () => {
  const v1 = sourceRecord({
    id: "toa-directive-v1",
    hash: "aaa",
    sourceUrl: "https://townofalpine.example.gov/directives/watchdog.pdf#download",
  });
  const v2 = sourceRecord({
    id: "toa-directive-v2",
    hash: "bbb",
    sourceUrl: "https://townofalpine.example.gov/directives/watchdog.pdf",
  });

  const records = applyReplacementDetection([v1], v2, {
    now: new Date("2026-06-05T12:00:00.000Z"),
  });

  assert.equal(records[0].lifecycleStatus, "replaced");
  assert.equal(records[0].replacement.replacedBySourceId, "toa-directive-v2");
  assert.equal(records[0].replacement.reason, "same_url_changed_hash");
  assert.equal(records[1].lifecycleStatus, "current");
  assert.equal(records[1].replacement.replacesSourceId, "toa-directive-v1");
  assert.equal(records[1].replacement.reason, "same_url_changed_hash");
});

test("missing local capture is preserved as missing_after_capture", async () => {
  const root = await makeFixtureRoot();
  const missing = await buildSourceCapture({
    id: "toa-missing-agenda-after-capture",
    sourceUrl: "https://townofalpine.example.gov/agendas/2026-06-05.pdf",
    sourceClass: "agenda_packet",
    title: "Town of Alpine Agenda Packet",
    toaLocalPath: join(root, "Documents/TOA/TownOfAlpine/agendas/2026-06-05.pdf"),
    capturedAt: "2026-06-05T09:00:00.000Z",
  }, {
    actor: "BackendCoder",
    now: new Date("2026-06-05T09:30:00.000Z"),
  });

  assert.equal(missing.lifecycleStatus, "missing_after_capture");
  assert.equal(missing.contentHash, null);
  assert.match(missing.audit.notes.at(-1), /Local capture was not found/);
  await rm(root, { recursive: true, force: true });
});

test("TOA import fixtures prove changed-hash replacement and missing preservation", async () => {
  const root = await makeFixtureRoot();
  const directiveDir = join(root, "Documents/TOA/TownOfAlpine/directives");
  await mkdir(directiveDir, { recursive: true });
  await writeFile(join(directiveDir, "government-watchdog-verification-plan.md"), "version one\n");
  await writeFile(join(directiveDir, "government-watchdog-verification-plan-recaptured.md"), "version two\n");

  const fixtureBytes = await readFile(new URL("../fixtures/toa-sources.json", import.meta.url), "utf8");
  const fixtures = JSON.parse(fixtureBytes).map((fixture) => ({
    ...fixture,
    toaLocalPath: fixture.toaLocalPath.replace("~/", `${root}/`),
  }));

  const records = await importSources(fixtures, {
    actor: "BackendCoder",
    now: new Date("2026-06-05T12:00:00.000Z"),
  });

  assert.equal(records.length, 3);
  assert.equal(records[0].lifecycleStatus, "replaced");
  assert.equal(records[0].replacement.replacedBySourceId, "toa-directive-verification-plan-v2");
  assert.equal(records[1].lifecycleStatus, "current");
  assert.equal(records[1].replacement.replacesSourceId, "toa-directive-verification-plan-v1");
  assert.equal(records[2].lifecycleStatus, "missing_after_capture");
  assert.equal(records[2].toaLocalPath.endsWith("/Documents/TOA/TownOfAlpine/agendas/2026-06-05-missing-after-capture.pdf"), true);
  await rm(root, { recursive: true, force: true });
});

async function makeFixtureRoot() {
  const root = join(tmpdir(), `government-watchdog-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  await mkdir(root, { recursive: true });
  return root;
}

function sourceRecord({ id, hash, sourceUrl }) {
  const now = "2026-06-05T00:00:00.000Z";
  return {
    id,
    sourceUrl,
    canonicalUrl: new URL(sourceUrl).origin + new URL(sourceUrl).pathname,
    sourceClass: "directive",
    title: id,
    jurisdiction: "Town of Alpine",
    issuingBody: "Town of Alpine",
    publishedAt: null,
    capturedAt: now,
    toaLocalPath: "~/Documents/TOA/TownOfAlpine/directives/watchdog.pdf",
    capture: {
      method: "local_file",
      capturedBy: "BackendCoder",
      originalPath: "/tmp/watchdog.pdf",
      mimeType: "application/pdf",
      sizeBytes: 10,
    },
    contentHash: {
      algorithm: "sha256",
      value: hash,
    },
    lifecycleStatus: "current",
    replacement: {
      replacesSourceId: null,
      replacedBySourceId: null,
      replacementDetectedAt: null,
      reason: null,
    },
    audit: {
      createdAt: now,
      createdBy: "BackendCoder",
      updatedAt: now,
      updatedBy: "BackendCoder",
      notes: [],
    },
  };
}
