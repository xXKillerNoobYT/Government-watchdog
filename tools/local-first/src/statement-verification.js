import { createHash } from "node:crypto";

export const STATEMENT_STATUSES = Object.freeze([
  "unverified",
  "verified",
  "disputed",
  "false_corrected",
]);

export const STATEMENT_KINDS = Object.freeze([
  "fact_claim",
  "ai_analysis",
]);

export function createStatement(input, options = {}) {
  const now = (options.now ?? new Date()).toISOString();
  const actor = options.actor ?? "local-tooling";
  const sourceLinks = (input.sourceLinks ?? []).map(createSourceLink);

  return {
    id: requireNonEmptyString(input.id, "Statement id is required"),
    text: requireNonEmptyString(input.text, "Statement text is required"),
    kind: validateOneOf(input.kind ?? "fact_claim", STATEMENT_KINDS, "statement kind"),
    status: validateOneOf(input.status ?? "unverified", STATEMENT_STATUSES, "statement status"),
    sourceLinks,
    evidenceLimits: input.evidenceLimits ?? null,
    sensitivityFlags: [...(input.sensitivityFlags ?? [])],
    publication: {
      doNotPublish: Boolean(input.publication?.doNotPublish),
      reason: input.publication?.reason ?? null,
      updatedAt: input.publication?.updatedAt ?? null,
      updatedBy: input.publication?.updatedBy ?? null,
    },
    verification: {
      verifiedAt: input.verification?.verifiedAt ?? null,
      verifiedBy: input.verification?.verifiedBy ?? null,
      disputedAt: input.verification?.disputedAt ?? null,
      disputedBy: input.verification?.disputedBy ?? null,
      disputeReason: input.verification?.disputeReason ?? null,
    },
    correctionHistory: [...(input.correctionHistory ?? [])],
    audit: {
      createdAt: input.audit?.createdAt ?? now,
      createdBy: input.audit?.createdBy ?? actor,
      updatedAt: input.audit?.updatedAt ?? now,
      updatedBy: input.audit?.updatedBy ?? actor,
    },
  };
}

export function createSourceLink(input) {
  const link = {
    sourceId: requireNonEmptyString(input.sourceId, "Source link sourceId is required"),
    quote: requireNonEmptyString(input.quote, "Source link quote is required"),
    page: input.page ?? null,
    timestamp: input.timestamp ?? null,
    location: input.location ?? null,
    sourceContentHash: input.sourceContentHash ?? null,
  };

  return {
    ...link,
    traceHash: input.traceHash ?? computeSourceTraceHash(link),
  };
}

export function computeSourceTraceHash(link) {
  return createHash("sha256")
    .update(JSON.stringify({
      sourceId: requireNonEmptyString(link.sourceId, "Source link sourceId is required"),
      quote: normalizeWhitespace(link.quote),
      page: link.page ?? null,
      timestamp: link.timestamp ?? null,
      location: link.location ?? null,
      sourceContentHash: link.sourceContentHash ?? null,
    }))
    .digest("hex");
}

export function applyVerificationTransition(statement, transition, options = {}) {
  const now = (options.now ?? new Date()).toISOString();
  const actor = options.actor ?? "local-tooling";
  const action = transition.action;

  if (action === "verify") {
    assertStatus(statement, ["unverified"], "verify");
    assertFactClaim(statement, "AI analysis cannot be verified as fact");
    assertHasSourceTrace(statement);

    return touch({
      ...statement,
      status: "verified",
      verification: {
        ...statement.verification,
        verifiedAt: now,
        verifiedBy: actor,
      },
    }, actor, now);
  }

  if (action === "dispute") {
    assertStatus(statement, ["verified"], "dispute");
    return touch({
      ...statement,
      status: "disputed",
      verification: {
        ...statement.verification,
        disputedAt: now,
        disputedBy: actor,
        disputeReason: requireNonEmptyString(transition.reason, "Dispute reason is required"),
      },
    }, actor, now);
  }

  if (action === "correct_false") {
    assertStatus(statement, ["verified", "disputed"], "correct_false");
    const correctingSourceLink = createSourceLink(transition.correctingSourceLink);
    const nextText = requireNonEmptyString(
      transition.correctedText ?? statement.text,
      "Corrected statement text is required",
    );

    return touch({
      ...statement,
      text: nextText,
      status: "false_corrected",
      sourceLinks: addUniqueTrace(statement.sourceLinks, correctingSourceLink),
      correctionHistory: [
        ...statement.correctionHistory,
        {
          priorText: statement.text,
          newText: nextText,
          priorStatus: statement.status,
          newStatus: "false_corrected",
          reason: requireNonEmptyString(transition.reason, "Correction reason is required"),
          correctingSourceLink,
          actor,
          timestamp: now,
          publicNote: transition.publicNote ?? null,
          internalNote: transition.internalNote ?? null,
        },
      ],
    }, actor, now);
  }

  if (action === "do_not_publish") {
    return touch({
      ...statement,
      publication: {
        doNotPublish: true,
        reason: requireNonEmptyString(transition.reason, "Do-not-publish reason is required"),
        updatedAt: now,
        updatedBy: actor,
      },
    }, actor, now);
  }

  throw new Error(`Unsupported verification transition: ${action}`);
}

export function evaluatePublicationGate(statement) {
  const failures = [];

  if (!statement.sourceLinks?.length) {
    failures.push("missing_source_links");
  }

  if (!statement.evidenceLimits || normalizeWhitespace(statement.evidenceLimits).length === 0) {
    failures.push("missing_evidence_limits");
  }

  if (statement.kind === "ai_analysis" && statement.status === "verified") {
    failures.push("ai_analysis_as_fact");
  }

  if (statement.publication?.doNotPublish) {
    failures.push("do_not_publish");
  }

  if (statement.sensitivityFlags?.includes("do_not_publish")) {
    failures.push("do_not_publish_sensitive_flag");
  }

  return {
    publishable: failures.length === 0,
    failures,
  };
}

function assertStatus(statement, allowed, action) {
  if (!allowed.includes(statement.status)) {
    throw new Error(`Cannot ${action} statement from status ${statement.status}`);
  }
}

function assertFactClaim(statement, message) {
  if (statement.kind !== "fact_claim") {
    throw new Error(message);
  }
}

function assertHasSourceTrace(statement) {
  if (!statement.sourceLinks?.length) {
    throw new Error("Verified statements require at least one source link");
  }
  for (const link of statement.sourceLinks) {
    if (!link.traceHash) {
      throw new Error(`Source link ${link.sourceId} is missing traceHash`);
    }
  }
}

function addUniqueTrace(sourceLinks, nextLink) {
  if (sourceLinks.some((link) => link.traceHash === nextLink.traceHash)) {
    return sourceLinks;
  }
  return [...sourceLinks, nextLink];
}

function touch(statement, actor, now) {
  return {
    ...statement,
    audit: {
      ...statement.audit,
      updatedAt: now,
      updatedBy: actor,
    },
  };
}

function normalizeWhitespace(value) {
  return requireNonEmptyString(value, "Trace quote is required").trim().replace(/\s+/g, " ");
}

function requireNonEmptyString(value, message) {
  if (typeof value !== "string" || value.trim().length === 0) {
    throw new Error(message);
  }
  return value;
}

function validateOneOf(value, allowed, label) {
  if (!allowed.includes(value)) {
    throw new Error(`Unsupported ${label}: ${value}`);
  }
  return value;
}
