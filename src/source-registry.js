import { createHash } from "node:crypto";
import { stat, readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { resolve } from "node:path";

export const SOURCE_CLASSES = Object.freeze([
  "official_record",
  "agenda_packet",
  "meeting_minutes",
  "ordinance",
  "resolution",
  "directive",
  "correspondence",
  "other",
]);

export const LIFECYCLE_STATUSES = Object.freeze([
  "current",
  "replaced",
  "missing_after_capture",
  "rejected",
]);

export function expandHomePath(path) {
  if (typeof path !== "string" || path.length === 0) {
    throw new Error("Source local path is required");
  }
  if (path === "~") return homedir();
  if (path.startsWith("~/")) return resolve(homedir(), path.slice(2));
  return resolve(path);
}

export function canonicalizeUrl(url) {
  const parsed = new URL(url);
  parsed.hash = "";
  parsed.hostname = parsed.hostname.toLowerCase();
  if (parsed.pathname !== "/" && parsed.pathname.endsWith("/")) {
    parsed.pathname = parsed.pathname.slice(0, -1);
  }
  return parsed.toString();
}

export async function hashFileSha256(path) {
  const bytes = await readFile(path);
  return createHash("sha256").update(bytes).digest("hex");
}

export async function buildSourceCapture(input, options = {}) {
  const now = options.now ?? new Date();
  const actor = options.actor ?? "local-tooling";
  const expandedPath = expandHomePath(input.toaLocalPath);

  const base = {
    id: input.id,
    sourceUrl: input.sourceUrl,
    canonicalUrl: canonicalizeUrl(input.sourceUrl),
    sourceClass: validateSourceClass(input.sourceClass),
    title: input.title,
    jurisdiction: input.jurisdiction ?? "Town of Alpine",
    issuingBody: input.issuingBody ?? "Town of Alpine",
    publishedAt: input.publishedAt ?? null,
    capturedAt: input.capturedAt ?? now.toISOString(),
    toaLocalPath: input.toaLocalPath,
    capture: {
      method: input.capture?.method ?? "local_file",
      capturedBy: input.capture?.capturedBy ?? actor,
      originalPath: expandedPath,
      mimeType: input.capture?.mimeType ?? null,
      sizeBytes: null,
    },
    contentHash: null,
    lifecycleStatus: "current",
    replacement: {
      replacesSourceId: null,
      replacedBySourceId: null,
      replacementDetectedAt: null,
      reason: null,
    },
    audit: {
      createdAt: now.toISOString(),
      createdBy: actor,
      updatedAt: now.toISOString(),
      updatedBy: actor,
      notes: input.audit?.notes ?? [],
    },
  };

  try {
    const fileStat = await stat(expandedPath);
    base.capture.sizeBytes = fileStat.size;
    base.contentHash = {
      algorithm: "sha256",
      value: await hashFileSha256(expandedPath),
    };
    return base;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    return {
      ...base,
      lifecycleStatus: "missing_after_capture",
      audit: {
        ...base.audit,
        notes: [
          ...base.audit.notes,
          `Local capture was not found at import time: ${input.toaLocalPath}`,
        ],
      },
    };
  }
}

export function applyReplacementDetection(records, candidate, options = {}) {
  if (candidate.lifecycleStatus === "missing_after_capture" || !candidate.contentHash) {
    return [...records, candidate];
  }

  const now = options.now ?? new Date();
  const prior = [...records]
    .reverse()
    .find((record) =>
      record.canonicalUrl === candidate.canonicalUrl
      && record.lifecycleStatus === "current"
      && record.contentHash
      && record.contentHash.value !== candidate.contentHash.value
    );

  if (!prior) return [...records, candidate];

  const replacementDetectedAt = now.toISOString();
  const replacedPrior = {
    ...prior,
    lifecycleStatus: "replaced",
    replacement: {
      ...prior.replacement,
      replacedBySourceId: candidate.id,
      replacementDetectedAt,
      reason: "same_url_changed_hash",
    },
    audit: {
      ...prior.audit,
      updatedAt: replacementDetectedAt,
      notes: [...prior.audit.notes, `Replaced by ${candidate.id}: same URL changed SHA-256.`],
    },
  };

  const replacementCandidate = {
    ...candidate,
    replacement: {
      ...candidate.replacement,
      replacesSourceId: prior.id,
      replacementDetectedAt,
      reason: "same_url_changed_hash",
    },
    audit: {
      ...candidate.audit,
      updatedAt: replacementDetectedAt,
      notes: [...candidate.audit.notes, `Replaces ${prior.id}: same URL changed SHA-256.`],
    },
  };

  return records
    .map((record) => (record.id === prior.id ? replacedPrior : record))
    .concat(replacementCandidate);
}

export async function importSources(fixtures, options = {}) {
  let records = [];
  for (const fixture of fixtures) {
    const candidate = await buildSourceCapture(fixture, options);
    records = applyReplacementDetection(records, candidate, options);
  }
  return records;
}

function validateSourceClass(sourceClass) {
  if (!SOURCE_CLASSES.includes(sourceClass)) {
    throw new Error(`Unsupported source class: ${sourceClass}`);
  }
  return sourceClass;
}
