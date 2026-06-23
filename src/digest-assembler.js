import { evaluatePublicationGate } from "./statement-verification.js";

// Step D10 / GOV-471 §7 F1 — deterministic, reviewer-internal digest assembler.
//
// Selection rule: a statement enters the digest ONLY when
// `evaluatePublicationGate(statement).publishable === true` AND it carries at
// least one source link with a trace hash. Selection uses NO AI and NO
// generation-time state, so the same inputs always produce a byte-identical body.
//
// Failure rule (contract §5/§8): excluded statements are never silently dropped.
// Every exclusion is recorded in `result.excluded` with its gate failures and a
// matching structured `result.log` entry so the F2 weekly runner can write a
// timestamped run log.

export const DIGEST_TITLE = "Reviewer-Internal Weekly Digest";

export function assembleDigest(statements, options = {}) {
  if (!Array.isArray(statements)) {
    throw new Error("assembleDigest requires an array of statements");
  }
  const title = options.title ?? DIGEST_TITLE;

  const included = [];
  const excluded = [];
  const log = [];

  for (const statement of statements) {
    const id = nonEmptyString(statement?.id) ? statement.id : "(unknown)";

    // Reuse the single publication gate — do not re-implement its rules here.
    const gate = evaluatePublicationGate(statement);
    if (!gate.publishable) {
      excluded.push({ id, status: statement?.status ?? null, failures: gate.failures });
      log.push({ level: "EXCLUDE", statementId: id, reason: gate.failures.join(",") });
      continue;
    }

    // Defect guard for the D10 trace-back rule: a publishable statement must
    // still expose at least one source link with a usable trace hash. A line we
    // cannot trace back is a defect, not a stylistic choice — exclude + log it.
    const traceableLinks = (statement.sourceLinks ?? [])
      .filter((link) => nonEmptyString(link?.traceHash))
      .map((link) => ({
        sourceId: link.sourceId,
        traceHash: link.traceHash,
        page: link.page ?? null,
        timestamp: link.timestamp ?? null,
        location: link.location ?? null,
      }))
      .sort(compareSourceLinks);

    if (traceableLinks.length === 0) {
      excluded.push({ id, status: statement.status, failures: ["missing_trace_hash"] });
      log.push({ level: "EXCLUDE", statementId: id, reason: "missing_trace_hash" });
      continue;
    }

    included.push({
      id,
      status: statement.status,
      text: statement.text,
      evidenceLimits: statement.evidenceLimits,
      createdAt: statement.audit?.createdAt ?? "",
      sourceLinks: traceableLinks,
    });
  }

  // Total, content-only sort key: createdAt then unique id. Independent of input
  // array order and of any Map/object iteration order -> byte-identical output.
  included.sort(compareIncluded);

  const body = renderBody(title, included);

  return { title, body, included, excluded, log };
}

function renderBody(title, included) {
  const lines = [
    `# ${title}`,
    `# publishable statements: ${included.length}`,
    "",
  ];

  for (const item of included) {
    lines.push(`- ${item.id} | ${item.status} | ${normalizeInline(item.text)}`);
    for (const link of item.sourceLinks) {
      lines.push(`  source: ${link.sourceId} trace=${link.traceHash}${formatAnchor(link)}`);
    }
    lines.push(`  limits: ${normalizeInline(item.evidenceLimits)}`);
  }

  return lines.join("\n") + "\n";
}

function formatAnchor(link) {
  let anchor = "";
  if (link.page !== null && link.page !== undefined) anchor += ` p${link.page}`;
  if (nonEmptyString(link.timestamp)) anchor += ` @${link.timestamp}`;
  if (nonEmptyString(link.location)) anchor += ` loc=${link.location}`;
  return anchor;
}

function compareIncluded(a, b) {
  if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? -1 : 1;
  if (a.id !== b.id) return a.id < b.id ? -1 : 1;
  return 0;
}

function compareSourceLinks(a, b) {
  if (a.traceHash !== b.traceHash) return a.traceHash < b.traceHash ? -1 : 1;
  return 0;
}

function normalizeInline(value) {
  if (!nonEmptyString(value)) return "";
  return value.trim().replace(/\s+/g, " ");
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}
