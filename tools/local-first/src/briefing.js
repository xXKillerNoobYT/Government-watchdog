import { evaluatePublicationGate } from "./statement-verification.js";
import { assembleDigest, DIGEST_TITLE } from "./digest-assembler.js";

// Stage 5.08 / GOV-568 (contract GOV-564) — correction-aware, hot-topic-surfacing
// briefing assembler. This is a DETERMINISTIC layer that sits ABOVE the Stage 4
// publication gate + digest assembler. It adds the Stage 5 capabilities required by
// `Docs/stage5-08-newsletter-briefing-editorial-contract.md`:
//
//   F4  hot-topic detection  — flag changed/new/missing Alpine sources by SHA-256
//                              comparison; produce a reviewer triage list (NOT a
//                              digest entry — reviewer action is required first).
//   §5  correction-awareness — `false_corrected` statements are excluded from the
//                              publishable pool and surfaced only as correction
//                              notices; `disputed` statements never appear in the
//                              main body, only under a "Disputed" label.
//   F3  Wayback availability — recorded as `unchecked`; NO external call is made
//                              here (external calls are a CEO/CTO-gated hard stop).
//
// Design rule: we do NOT modify `evaluatePublicationGate` — that gate is the Stage 4
// contract and is covered by 19 frozen tests. Instead we partition statements ABOVE
// the gate, so the Stage 4 body stays byte-identical for the inputs it already
// accepted, and the Stage 5 status-based exclusions are layered on top.
//
// Everything here is pure + deterministic: same inputs -> byte-identical output,
// independent of input array order (every section sorts by a content key).

export const BRIEFING_TITLE = "Reviewer-Internal Weekly Briefing";

// ── F4: hot-topic detection ─────────────────────────────────────────────────
//
// Compares each `current` source record's SHA-256 against the prior run's stored
// hash for the same canonical URL. Detection is hash/recency only — AI never
// decides which sources are "hot". Output is a triage list requiring reviewer
// action before any changed source enters the digest.
//
//   currentSources : source records (from the registry) for this pass.
//   priorHashes    : { [canonicalUrl]: sha256Value } from the last weekly pass.
export function detectHotTopics(currentSources, priorHashes = {}) {
  if (!Array.isArray(currentSources)) {
    throw new Error("detectHotTopics requires an array of source records");
  }

  const triage = [];
  for (const src of currentSources) {
    // Only `current` lifecycle sources are candidates; replaced/rejected are inert.
    if (src?.lifecycleStatus !== "current") continue;

    const canonicalUrl = src.canonicalUrl;
    const currentHash = src.contentHash?.value ?? null;
    const priorHash = priorHashes[canonicalUrl] ?? null;

    let change = null;
    if (currentHash === null) change = "missing"; // current record with no readable hash
    else if (priorHash === null) change = "new";
    else if (priorHash !== currentHash) change = "changed";
    // priorHash === currentHash -> unchanged -> not surfaced.

    if (change === null) continue;

    triage.push({
      sourceId: src.id,
      canonicalUrl,
      change,
      priorHash,
      currentHash,
      // Reviewer must explicitly include/hold before a changed source enters the
      // digest (contract §3.2 / §4 editorial triage row). Detection never auto-includes.
      requiresReviewerAction: true,
    });
  }

  triage.sort(compareTriage);
  return triage;
}

// ── §5: correction notices ──────────────────────────────────────────────────
//
// A `false_corrected` statement whose now-wrong prior text appeared in a prior
// digest carries a publication obligation: surface a correction notice (not the
// stale claim). A notice MUST cite a correcting source link — a notice without one
// is a defect (issue threshold, contract §9), not silently emitted.
//
//   statements       : all current statement records.
//   priorDigestTexts : normalized statement texts that appeared in a prior digest.
export function buildCorrectionNotices(statements, priorDigestTexts = []) {
  if (!Array.isArray(statements)) {
    throw new Error("buildCorrectionNotices requires an array of statements");
  }

  const priorSet = new Set(priorDigestTexts.map(normalizeInline));
  const notices = [];
  const defects = [];

  for (const statement of statements) {
    if (statement?.status !== "false_corrected") continue;

    const history = statement.correctionHistory ?? [];
    if (history.length === 0) continue;

    // The transition to `false_corrected` is the most recent correction entry.
    const entry = history[history.length - 1];
    const priorText = nonEmptyString(entry.priorText) ? entry.priorText : statement.text;

    // Obligation is triggered ONLY by prior publication of the now-wrong claim.
    if (!priorSet.has(normalizeInline(priorText))) continue;

    const link = entry.correctingSourceLink ?? null;
    if (!link || !nonEmptyString(link.traceHash)) {
      defects.push({
        statementId: idOf(statement),
        reason: "correction_notice_missing_correcting_source_link",
      });
      continue; // never emit an uncited correction
    }

    notices.push({
      statementId: idOf(statement),
      priorText,
      newText: nonEmptyString(entry.newText) ? entry.newText : statement.text,
      reason: nonEmptyString(entry.reason) ? entry.reason : null,
      correctingSourceLink: { sourceId: link.sourceId, traceHash: link.traceHash },
    });
  }

  notices.sort(byStatementId);
  defects.sort(byStatementId);
  return { notices, defects };
}

// ── Partition: route each statement to exactly one bucket ───────────────────
//
// Order matters and encodes the safety precedence:
//   1. gate failure (incl. do_not_publish / missing evidence) -> hard exclude
//   2. false_corrected -> corrected bucket (out of publishable pool, §5.3)
//   3. disputed        -> disputed bucket (labeled only, §5.4)
//   4. ai_analysis     -> labeled AI-prose bucket (§6 AI-label visibility)
//   5. otherwise       -> body-eligible (fed to the Stage 4 digest assembler)
export function partitionBriefingStatements(statements) {
  if (!Array.isArray(statements)) {
    throw new Error("partitionBriefingStatements requires an array of statements");
  }

  const bodyEligible = [];
  const disputed = [];
  const corrected = [];
  const aiProse = [];
  const excluded = [];
  const log = [];

  for (const statement of statements) {
    const id = idOf(statement);
    const gate = evaluatePublicationGate(statement ?? {});

    if (!gate.publishable) {
      // do_not_publish + missing evidence/source are hard exclusions regardless of
      // verification status (contract §6) — they never reach a display bucket.
      excluded.push({ id, status: statement?.status ?? null, failures: gate.failures });
      log.push({ level: "EXCLUDE", statementId: id, reason: gate.failures.join(",") });
      continue;
    }

    if (statement.status === "false_corrected") {
      corrected.push(statement);
      log.push({ level: "CORRECTED", statementId: id, reason: "false_corrected_excluded_from_publishable_pool" });
      continue;
    }

    if (statement.status === "disputed") {
      disputed.push(statement);
      log.push({ level: "DISPUTED", statementId: id, reason: "disputed_excluded_from_main_body" });
      continue;
    }

    if (statement.kind === "ai_analysis") {
      aiProse.push(statement);
      log.push({ level: "AI", statementId: id, reason: "ai_analysis_routed_to_labeled_section" });
      continue;
    }

    bodyEligible.push(statement);
  }

  return { bodyEligible, disputed, corrected, aiProse, excluded, log };
}

// ── Top-level: assemble the reviewer-internal briefing ──────────────────────
export function assembleBriefing(input = {}, options = {}) {
  const statements = input.statements ?? [];
  const currentSources = input.currentSources ?? [];
  const priorSourceHashes = input.priorSourceHashes ?? {};
  const priorDigestTexts = input.priorDigestTexts ?? [];
  const title = options.title ?? BRIEFING_TITLE;

  const part = partitionBriefingStatements(statements);

  // Reuse the Stage 4 F1 assembler for the publishable facts body — same gate,
  // same trace-back, same deterministic sort. Only status-clean fact_claims reach it.
  const digest = assembleDigest(part.bodyEligible, { title: DIGEST_TITLE });

  const hotTopics = detectHotTopics(currentSources, priorSourceHashes);
  const corrections = buildCorrectionNotices(statements, priorDigestTexts);

  // F3 Wayback: external archive check is a CEO/CTO-gated hard stop. We never make
  // a network call here. Until F3 is built + authorized, the field is `unchecked`.
  const waybackStatus = "unchecked";

  const disputedView = part.disputed
    .map((s) => ({
      statementId: idOf(s),
      text: s.text,
      disputeReason: s.verification?.disputeReason ?? null,
    }))
    .sort(byStatementId);

  const aiProseView = part.aiProse
    .map((s) => ({ statementId: idOf(s), text: s.text, evidenceLimits: s.evidenceLimits }))
    .sort(byStatementId);

  // Merge exclusions: gate failures from the partition + trace-back defects the
  // digest catches on otherwise-publishable statements. Nothing is silently dropped.
  const excluded = [...part.excluded, ...digest.excluded].sort((a, b) =>
    a.id < b.id ? -1 : a.id > b.id ? 1 : 0,
  );

  const log = [...part.log, ...digest.log];

  // Issue candidates (contract §9 thresholds) — surfaced for the runner/reviewer.
  const issueCandidates = [];
  for (const d of corrections.defects) {
    issueCandidates.push({
      type: "correction_notice_missing_source",
      detail: `Correction for ${d.statementId} has no correcting source link; notice suppressed.`,
      statementId: d.statementId,
    });
  }
  for (const ex of digest.excluded) {
    if (ex.failures.includes("missing_trace_hash")) {
      issueCandidates.push({
        type: "digest_traceback_failure",
        detail: `Publishable statement ${ex.id} could not be traced back to a source (missing_trace_hash).`,
        statementId: ex.id,
      });
    }
  }

  const result = {
    title,
    hotTopics,
    corrections: corrections.notices,
    correctionDefects: corrections.defects,
    disputed: disputedView,
    publishable: digest.included,
    aiProse: aiProseView,
    excluded,
    waybackStatus,
    digestBody: digest.body,
    log,
    issueCandidates,
  };

  result.body = renderBriefing(result);
  return result;
}

// ── Rendering ───────────────────────────────────────────────────────────────

function renderBriefing(result) {
  const lines = [`# ${result.title}`, ""];

  lines.push(`## Hot-topic triage (reviewer action required): ${result.hotTopics.length}`);
  for (const t of result.hotTopics) {
    lines.push(`- ${t.change}: ${t.sourceId} ${t.canonicalUrl} prior=${t.priorHash ?? "(none)"} current=${t.currentHash ?? "(none)"}`);
  }
  lines.push("");

  lines.push(`## Corrections: ${result.corrections.length}`);
  for (const c of result.corrections) {
    lines.push(`- ${c.statementId}: prior="${normalizeInline(c.priorText)}" -> corrected="${normalizeInline(c.newText)}"`);
    lines.push(`  reason: ${normalizeInline(c.reason ?? "")}`);
    lines.push(`  correcting-source: ${c.correctingSourceLink.sourceId} trace=${c.correctingSourceLink.traceHash}`);
  }
  lines.push("");

  lines.push(`## Disputed — NOT settled fact: ${result.disputed.length}`);
  for (const d of result.disputed) {
    lines.push(`- [DISPUTED] ${d.statementId}: ${normalizeInline(d.text)}`);
    lines.push(`  dispute-reason: ${normalizeInline(d.disputeReason ?? "")}`);
  }
  lines.push("");

  lines.push(`## Publishable facts: ${result.publishable.length}`);
  // The Stage 4 digest body already carries its own `# title` + count header; embed it verbatim.
  lines.push(result.digestBody.trimEnd());
  lines.push("");

  lines.push(`## AI-assisted prose [AI ANALYSIS — NOT VERIFIED]: ${result.aiProse.length}`);
  for (const a of result.aiProse) {
    lines.push(`- [AI] ${a.statementId}: ${normalizeInline(a.text)}`);
  }
  lines.push("");

  lines.push(`## Wayback archive status: ${result.waybackStatus} (F3 gated — no external call made)`);

  return lines.join("\n") + "\n";
}

// ── helpers (kept local; mirror digest-assembler conventions) ───────────────

function compareTriage(a, b) {
  if (a.canonicalUrl !== b.canonicalUrl) return a.canonicalUrl < b.canonicalUrl ? -1 : 1;
  if (a.sourceId !== b.sourceId) return a.sourceId < b.sourceId ? -1 : 1;
  return 0;
}

function byStatementId(a, b) {
  if (a.statementId !== b.statementId) return a.statementId < b.statementId ? -1 : 1;
  return 0;
}

function idOf(statement) {
  return nonEmptyString(statement?.id) ? statement.id : "(unknown)";
}

function normalizeInline(value) {
  if (!nonEmptyString(value)) return "";
  return value.trim().replace(/\s+/g, " ");
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}
