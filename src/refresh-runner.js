import { readFile, writeFile, mkdir, appendFile } from "node:fs/promises";
import { dirname, resolve, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  buildSourceCapture,
  applyReplacementDetection,
  canonicalizeUrl,
} from "./source-registry.js";
import {
  createStatement,
  applyVerificationTransition,
  evaluatePublicationGate,
} from "./statement-verification.js";
import { assembleDigest, DIGEST_TITLE } from "./digest-assembler.js";

// Stage 4.F2 / GOV-479 — Weekly refresh runner + run-log writer.
//
// Wires the deterministic steps D1–D9 (GOV-471 boundary contract §3/§6) into ONE
// idempotent re-validation pass over the already-registered Alpine source set,
// then rebuilds the reviewer-internal digest via the F1 assembler (D10).
//
// This is a RE-VALIDATION pass, never open-ended discovery (Alpine-only; a
// non-Alpine source is a hard-stop scope leak, never silently processed).
//
// Health signal (contract §6): a run over UNCHANGED sources must produce no new
// records and a byte-identical digest. The lever that guarantees this is "keep,
// don't re-capture": an unchanged source keeps its prior record verbatim (a fresh
// capture would stamp new timestamps and break idempotency); only a changed hash
// mints a new `current` record via `applyReplacementDetection`.

export const ALPINE_HOSTS = Object.freeze(["townofalpine.example.gov"]);
export const DEFAULT_LOG_PATH = "Logs/stage4-newsletter-refresh.log";

// ── Run-log formatting ──────────────────────────────────────────────────────
// Contract §8 format: `[YYYY-MM-DD HH:MM:SS] [LEVEL] msg`. UTC components keep the
// line deterministic regardless of the runner host's timezone.

export function formatTimestamp(date) {
  const iso = date.toISOString(); // 2026-06-23T01:02:03.456Z
  return `${iso.slice(0, 10)} ${iso.slice(11, 19)}`;
}

export function formatLogLine(date, level, msg) {
  return `[${formatTimestamp(date)}] [${level}] ${msg}`;
}

// ── Scope enforcement (Alpine-only, hard stop) ──────────────────────────────

function hostOf(url) {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return null;
  }
}

function isAlpineHost(host, alpineHosts) {
  if (!host) return false;
  return alpineHosts.some((allowed) => host === allowed || host.endsWith(`.${allowed}`));
}

// ── Per-source re-validation (D1–D5) ────────────────────────────────────────
//
// runWeeklyRefresh is a PURE function: same inputs + injected `now` -> identical
// output. The CLI wrapper (main) owns all file I/O and log persistence.
//
//   priorRegistry : source records from the last run (stored state).
//   statements    : existing statement records whose sourceLinks carry the
//                   `sourceContentHash` they were bound to.
//   sourceDefs    : this week's capture batch (fixture-shaped inputs). Each carries
//                   its own capture `id`; unchanged captures are discarded (the
//                   prior record is kept), changed captures become new `current`.

export async function runWeeklyRefresh(input, options = {}) {
  const now = options.now ?? new Date();
  const actor = options.actor ?? "stage4-refresh-runner";
  const alpineHosts = input.alpineHosts ?? ALPINE_HOSTS;
  const title = input.title ?? DIGEST_TITLE;

  const priorRegistry = input.priorRegistry ?? [];
  const sourceDefs = input.sourceDefs ?? [];
  const inputStatements = input.statements ?? [];

  const log = [];
  const issueCandidates = [];
  const emit = (level, msg) => log.push({ level, msg });

  const counts = {
    sourcesChecked: 0,
    unchanged: 0,
    replaced: 0,
    registered: 0,
    missing: 0,
    scopeRejected: 0,
    statementsReopened: 0,
    statementsExcluded: 0,
  };

  // Carry the prior registry forward; mutate via applyReplacementDetection only.
  let registry = [...priorRegistry];
  // canonical URLs whose live capture came back missing this run (archive check,
  // §6) — dependent statements must be re-opened even when a prior record is kept.
  const missingCanonicals = new Set();

  // canonicalUrl -> prior `current` record (the stored baseline we compare against).
  const priorCurrentByCanonical = new Map();
  for (const record of priorRegistry) {
    if (record.lifecycleStatus === "current") {
      priorCurrentByCanonical.set(record.canonicalUrl, record);
    }
  }

  for (const def of sourceDefs) {
    const host = hostOf(def.sourceUrl);

    // Scope leak = hard stop. Reject, log loudly, raise an issue candidate, and
    // never re-capture or register an out-of-scope source.
    if (!isAlpineHost(host, alpineHosts)) {
      counts.scopeRejected += 1;
      emit("ERROR", `SCOPE_LEAK rejected non-Alpine source ${def.id} host=${host ?? "(unparseable)"}`);
      issueCandidates.push({
        type: "scope_leak",
        detail: `Non-Alpine source ${def.id} (host ${host ?? "unparseable"}) appeared in the weekly pass.`,
        sourceId: def.id,
      });
      continue;
    }

    counts.sourcesChecked += 1;
    const fresh = await buildSourceCapture(def, { now, actor });
    const canonical = fresh.canonicalUrl;
    const prior = priorCurrentByCanonical.get(canonical);

    // Missing-after-capture (D1): file gone at re-capture. Never silently skipped.
    if (fresh.lifecycleStatus === "missing_after_capture" || !fresh.contentHash) {
      counts.missing += 1;
      missingCanonicals.add(canonical);
      emit("WARN", `MISSING source ${def.id} canonical=${canonical} (no readable local capture)`);
      // Record the missing capture so provenance is preserved, but only if there is
      // no prior current to fall back on (preserve the prior current otherwise).
      if (!prior) {
        registry = applyReplacementDetection(registry, fresh, { now });
      }
      continue;
    }

    if (!prior) {
      // First sighting of this source URL -> initial registration (still Alpine-only).
      counts.registered += 1;
      emit("INFO", `REGISTERED new current source ${fresh.id} canonical=${canonical}`);
      registry = applyReplacementDetection(registry, fresh, { now });
      continue;
    }

    const priorHash = prior.contentHash?.value ?? null;
    if (priorHash === fresh.contentHash.value) {
      // Unchanged -> KEEP the prior record verbatim (idempotency lever). Discard
      // the fresh capture; recording an identical capture would add a new record.
      counts.unchanged += 1;
      emit("INFO", `UNCHANGED source ${canonical} (sha256 stable, prior record kept)`);
      continue;
    }

    // Changed hash (D3/D4) -> mark prior `replaced`, register fresh `current`.
    counts.replaced += 1;
    emit("INFO", `REPLACED source ${canonical}: prior ${prior.id} -> current ${fresh.id} (sha256 changed)`);
    registry = applyReplacementDetection(registry, fresh, { now });
  }

  // current record per canonicalUrl after this pass.
  const currentByCanonical = new Map();
  for (const record of registry) {
    if (record.lifecycleStatus === "current") {
      currentByCanonical.set(record.canonicalUrl, record);
    }
  }
  // sourceId -> canonicalUrl (so a statement's link can be resolved to its URL).
  const canonicalBySourceId = new Map();
  for (const record of registry) {
    canonicalBySourceId.set(record.id, record.canonicalUrl);
  }

  // ── Re-open stale-bound statements (contract §6) ──────────────────────────
  // A statement whose bound source content hash no longer matches the `current`
  // capture can no longer stand as a verified fact. Re-open it: verified ->
  // unverified (per §5, "withheld" = publishable===false OR status !== verified).
  const refreshedStatements = inputStatements.map((statement) => {
    const staleReasons = [];
    for (const link of statement.sourceLinks ?? []) {
      const canonical = canonicalBySourceId.get(link.sourceId);
      if (!canonical) {
        staleReasons.push(`unresolved_source:${link.sourceId}`);
        continue;
      }
      if (missingCanonicals.has(canonical)) {
        // Live capture vanished this run -> cannot re-confirm the binding.
        staleReasons.push(`source_missing:${link.sourceId}`);
        continue;
      }
      const current = currentByCanonical.get(canonical);
      if (!current || !current.contentHash) {
        staleReasons.push(`source_missing:${link.sourceId}`);
        continue;
      }
      if ((link.sourceContentHash ?? null) !== current.contentHash.value) {
        staleReasons.push(`source_replaced:${link.sourceId}`);
      }
    }

    if (staleReasons.length === 0) return statement;

    // A still-verified statement backed by a now-missing source that was publishable
    // is the highest-severity threshold condition.
    const wasPublishable = statement.status === "verified"
      && evaluatePublicationGate(statement).publishable === true;
    if (wasPublishable && staleReasons.some((r) => r.startsWith("source_missing"))) {
      issueCandidates.push({
        type: "missing_after_capture_published",
        detail: `Published statement ${statement.id} lost its source capture (${staleReasons.join(", ")}).`,
        statementId: statement.id,
      });
    }

    if (statement.status !== "verified") {
      // Not currently a verified fact -> nothing to re-open, but record the drift.
      emit("INFO", `STALE non-verified statement ${statement.id} (${staleReasons.join(", ")}); no re-open needed`);
      return statement;
    }

    counts.statementsReopened += 1;
    emit("WARN", `REOPENED statement ${statement.id}: verified->unverified (${staleReasons.join(", ")})`);
    return {
      ...statement,
      status: "unverified",
      verification: { ...statement.verification, verifiedAt: null, verifiedBy: null },
      audit: {
        ...statement.audit,
        updatedAt: now.toISOString(),
        updatedBy: actor,
        notes: [
          ...(statement.audit?.notes ?? []),
          `Re-opened by weekly refresh: ${staleReasons.join(", ")}.`,
        ],
      },
    };
  });

  // ── Digest rebuild (D10 / F1) ─────────────────────────────────────────────
  // Eligible = verified facts only (§5). assembleDigest applies the publication
  // gate + trace-back and logs its own exclusions; re-opened statements never
  // reach it (logged above), so nothing is silently dropped.
  const eligible = refreshedStatements.filter((s) => s.status === "verified");
  const digest = assembleDigest(eligible, { title });

  for (const ex of digest.excluded) {
    counts.statementsExcluded += 1;
    emit("WARN", `EXCLUDED statement ${ex.id} from digest: ${ex.failures.join(",")}`);
    if (ex.failures.includes("missing_trace_hash")) {
      issueCandidates.push({
        type: "digest_traceback_failure",
        detail: `Publishable statement ${ex.id} could not be traced back to a source (missing_trace_hash).`,
        statementId: ex.id,
      });
    }
  }

  const digestLineCount = digest.body.split("\n").filter((l) => l.length > 0).length;
  emit(
    "INFO",
    `SUMMARY checked=${counts.sourcesChecked} unchanged=${counts.unchanged} replaced=${counts.replaced} `
    + `registered=${counts.registered} missing=${counts.missing} scopeRejected=${counts.scopeRejected} `
    + `reopened=${counts.statementsReopened} excluded=${counts.statementsExcluded} digestLines=${digestLineCount} `
    + `digestPublishable=${digest.included.length}`,
  );

  return {
    now,
    registry,
    statements: refreshedStatements,
    digest,
    digestLineCount,
    counts,
    log,
    issueCandidates,
    ok: issueCandidates.every((i) => i.type !== "scope_leak"),
  };
}

// Render structured log entries to the `[ts] [LEVEL] msg` line format (§8).
export function renderRunLog(result, options = {}) {
  const now = result.now;
  const prefix = options.dryRun ? "[DRY-RUN] " : "";
  return result.log.map((e) => formatLogLine(now, e.level, `${prefix}${e.msg}`)).join("\n") + "\n";
}

// ── CLI wrapper ─────────────────────────────────────────────────────────────
// `--dry-run` (DEFAULT) computes + logs but persists nothing. `--apply` persists
// the updated state and digest. CTO reviews a dry-run before the first `--apply`
// (contract §8 pass-up gate). The run log is written in BOTH modes (it is the
// gitignored, local/vault-only run evidence).

function parseArgs(argv) {
  const args = { apply: false, state: null, out: null, log: DEFAULT_LOG_PATH };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--apply") args.apply = true;
    else if (a === "--dry-run") args.apply = false;
    else if (a === "--state") args.state = argv[++i];
    else if (a === "--out") args.out = argv[++i];
    else if (a === "--log") args.log = argv[++i];
  }
  return args;
}

// Build a runnable, reproducible sample state from the repo fixtures when no
// `--state` file is supplied: import the Alpine sources (real hashes), bind +
// verify the seed statements, then re-validate (a healthy run is idempotent).
async function bootstrapFromFixtures(fixturesDir, now) {
  const sourcesPath = join(fixturesDir, "sources.json");
  const sourceDefs = JSON.parse(await readFile(sourcesPath, "utf8")).map((def) => ({
    ...def,
    toaLocalPath: isAbsolute(def.toaLocalPath)
      ? def.toaLocalPath
      : resolve(fixturesDir, def.toaLocalPath),
  }));

  let registry = [];
  for (const def of sourceDefs) {
    const capture = await buildSourceCapture(def, { now });
    registry = applyReplacementDetection(registry, capture, { now });
  }
  const hashByCanonical = new Map(
    registry.filter((r) => r.lifecycleStatus === "current" && r.contentHash)
      .map((r) => [r.canonicalUrl, { id: r.id, hash: r.contentHash.value }]),
  );

  const seedDefs = JSON.parse(await readFile(join(fixturesDir, "statements.seed.json"), "utf8"));
  const statements = seedDefs.map((def) => {
    const links = def.sourceLinks.map((link) => {
      const canonical = canonicalizeUrl(link.sourceUrl);
      const src = hashByCanonical.get(canonical);
      return { ...link, sourceId: src?.id ?? link.sourceId, sourceContentHash: src?.hash ?? null };
    });
    let statement = createStatement({ ...def, sourceLinks: links }, { now });
    statement = applyVerificationTransition(statement, { action: "verify" }, { now });
    return statement;
  });

  return { registry, statements, sourceDefs };
}

async function main(argv) {
  const args = parseArgs(argv);
  const now = new Date();
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

  let priorRegistry;
  let statements;
  let sourceDefs;
  if (args.state) {
    const state = JSON.parse(await readFile(args.state, "utf8"));
    priorRegistry = state.registry ?? [];
    statements = state.statements ?? [];
    sourceDefs = state.sourceDefs ?? priorRegistry
      .filter((r) => r.lifecycleStatus === "current")
      .map((r) => ({
        id: r.id,
        sourceUrl: r.sourceUrl,
        sourceClass: r.sourceClass,
        title: r.title,
        toaLocalPath: r.toaLocalPath,
        capture: r.capture,
      }));
  } else {
    const boot = await bootstrapFromFixtures(resolve(repoRoot, "fixtures/refresh"), now);
    priorRegistry = boot.registry;
    statements = boot.statements;
    sourceDefs = boot.sourceDefs;
  }

  const result = await runWeeklyRefresh({ priorRegistry, statements, sourceDefs }, { now });

  // Run log is always written (both modes) — it is the gitignored run evidence.
  const logPath = isAbsolute(args.log) ? args.log : resolve(repoRoot, args.log);
  await mkdir(dirname(logPath), { recursive: true });
  await appendFile(logPath, renderRunLog(result, { dryRun: !args.apply }));

  const mode = args.apply ? "APPLY" : "DRY-RUN";
  process.stdout.write(`\n=== Stage 4.F2 weekly refresh (${mode}) ===\n`);
  process.stdout.write(renderRunLog(result, { dryRun: !args.apply }));
  process.stdout.write(`\n--- reviewer-internal digest (${result.digest.included.length} publishable) ---\n`);
  process.stdout.write(result.digest.body);
  if (result.issueCandidates.length > 0) {
    process.stdout.write(`\n--- issue candidates (${result.issueCandidates.length}) ---\n`);
    for (const ic of result.issueCandidates) {
      process.stdout.write(`  ${ic.type}: ${ic.detail}\n`);
    }
  }

  if (args.apply) {
    if (args.state) {
      await writeFile(
        args.state,
        JSON.stringify({ registry: result.registry, statements: result.statements }, null, 2) + "\n",
      );
    }
    if (args.out) {
      const outPath = isAbsolute(args.out) ? args.out : resolve(repoRoot, args.out);
      await mkdir(dirname(outPath), { recursive: true });
      await writeFile(outPath, result.digest.body);
    }
    process.stdout.write(`\n[APPLIED] state + digest persisted.\n`);
  } else {
    process.stdout.write(`\n[DRY-RUN] no state persisted; re-run with --apply after CTO review.\n`);
  }

  // Hard stop: a scope leak fails the run (non-zero exit) so a scheduler notices.
  return result.ok ? 0 : 2;
}

const invokedDirectly = process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  main(process.argv.slice(2))
    .then((code) => process.exit(code))
    .catch((error) => {
      process.stderr.write(`refresh-runner failed: ${error?.stack ?? error}\n`);
      process.exit(1);
    });
}
