# Stage 5.13 — Back-gap / Regression Analysis Contract

**Issue:** GOV-573 (plan + sequence) · **Stage:** 5.13 · **Goal:** `e454596f` (Back-gap/regression analysis)
**Owner:** CTO (impl chain) / CEO (staging) · **Status:** planning contract (versioned, Alpine-only)
**Substrate inherited from Stage 4/5:** `src/source-registry.js`, `src/statement-verification.js`,
`src/digest-assembler.js`, `src/refresh-runner.js`, `src/briefing.js`
**Governing boundary contract:** `Docs/stage4-automation-ai-boundary.md` (GOV-471) — Stage 5.13
EXTENDS that contract; it does not replace it. The buildable-envelope / no-public-output rule
(GOV-436, GOV-420) remains in force.

---

## 1. Purpose

Define a **deterministic, reviewer-internal** analyzer that audits the verification substrate for
two distinct classes of defect:

1. **Back-gaps** — *completeness* failures in the historical record at a single point in time:
   things that **should** exist or be traceable but are not (uncaptured sources, statements with no
   live source trace, time-period coverage holes, registry/statement orphans).
2. **Regressions** — *monotonicity* failures across two points in time: verification quality that
   **was good and got worse** (`verified` → `unverified`, `publishable` → withheld, a digest item
   that was included and is now dropped, a captured source that is now `missing_after_capture`, a
   correction that failed to propagate).

This is a **planning/contract slice only** — no analyzer implementation occurs in GOV-573.
Downstream child issues (impl, VSR, security) derive their acceptance criteria from this document.

**Governing principle (inherited from GOV-471):** Deterministic automation owns collection,
identity, validation, exact-source linking, archive checks, gate enforcement, digest assembly, and
**this analysis**. The analyzer reads existing records and prior run state; it produces a
reviewer-internal report. It **never** mutates verification state, **never** auto-publishes, and
**never** uses AI to decide whether something is a gap or a regression. AI output, if surfaced at
all, is confined to optional human-readable summary prose under an `ai_analysis` label over a report
the deterministic engine already produced — it can never be primary evidence.

---

## 2. Scope and non-scope

| In scope (Alpine-only) | Out of scope |
|---|---|
| Back-gap detection over Alpine source registry + statement set | Any Wyoming/US expansion |
| Regression detection by diffing a prior snapshot vs current state | Auto-publication of the report (email/public web — Isaac-gated, GOV-420) |
| Reviewer-internal report (object + deterministic text render) | Mutating statement status or source lifecycle (read-only analyzer) |
| Wayback archive-availability signal as a back-gap input (GATED) | Any unapproved external automated network call |
| Deterministic, idempotent, snapshot-driven analysis | AI-decided gap/regression classification |
| Severity classification + stable ordering for review triage | Filing/triaging the findings as new issues automatically |

---

## 3. Inputs

The analyzer is a pure function of explicit inputs — no hidden clock, no network unless the gated
Wayback signal is explicitly enabled and authorized.

1. **Current source registry** — records with `lifecycleStatus ∈ {current, replaced,
   missing_after_capture}` (from `src/source-registry.js`).
2. **Current statement set** — records with `status ∈ {unverified, verified, disputed,
   false_corrected}` and their `sourceLinks` (from `src/statement-verification.js`).
3. **Prior snapshot** — a previously persisted view of (sources, statements, digest inclusion) used
   as the regression baseline. The prior weekly run-log written by `src/refresh-runner.js`
   (`renderRunLog`) and the prior assembled digest (`src/digest-assembler.js`) are the canonical
   baselines. If no prior snapshot exists, the run is **back-gap-only** and regression findings are
   reported as `baseline_absent` (not as false regressions).
4. **Expected-coverage descriptor** — an explicit, Alpine-only list of what the record is expected to
   contain for the analyzed window (e.g. known meeting dates / agenda anchors). Supplied by the
   caller; the analyzer does not invent expectations. Missing descriptor ⇒ coverage-hole back-gaps
   are reported as `coverage_unknown`, never as confirmed gaps.
5. **`now` / window bounds** — passed in by the caller (determinism; mirrors `refresh-runner`'s
   injected-clock pattern). The analyzer must not call `Date.now()` directly.
6. **Options** — `{ wayback: { enabled: false, authorized: false } }` default-closed. The Wayback
   archive-availability check is a gated external call; it runs **only** when both `enabled` and
   `authorized` are true (CEO/CTO authorization, per GOV-471 §gated-external-call). When disabled,
   archive-related back-gaps are reported as `archive_unchecked`, never as confirmed.

---

## 4. Findings model (output)

The analyzer returns a structured, JSON-serializable report — never mutates inputs:

```
{
  generatedAt,                 // echoes injected `now`
  baseline: "present" | "absent",
  backGaps:   [ Finding ],     // completeness failures (point-in-time)
  regressions:[ Finding ],     // monotonicity failures (vs prior snapshot)
  counts: { backGaps, regressions, bySeverity: {high, medium, low} },
  summary                      // deterministic one-line text
}
```

Each `Finding`:

```
{
  type,        // enumerated below
  axis,        // "back_gap" | "regression"
  severity,    // "high" | "medium" | "low"
  subjectId,   // sourceId / statementId / digestItemId / canonical URL
  detail,      // deterministic human-readable string, no AI
  evidence     // { priorStatus?, currentStatus?, canonical?, sourceLinkIds?, ... }
}
```

### 4.1 Back-gap finding types

| `type` | Meaning | Severity |
|---|---|---|
| `untraced_statement` | A `verified`/publishable statement with no live `current` source trace | high |
| `orphan_source` | A `current` source referenced by zero statements (possible missed claim) | low |
| `dangling_trace` | A statement `sourceLink` whose `sourceId` is absent from the registry | high |
| `coverage_hole` | An expected-coverage window entry with no source record | medium |
| `coverage_unknown` | Coverage requested but no expected-coverage descriptor supplied | low |
| `archive_unchecked` | A source whose archive availability was not checked (Wayback disabled) | low |
| `archive_missing` | Wayback enabled+authorized and the archive snapshot is unavailable | medium |

### 4.2 Regression finding types

| `type` | Meaning | Severity |
|---|---|---|
| `verification_regressed` | Statement was `verified` in prior snapshot, now `unverified`/`disputed` | high |
| `publish_regressed` | Statement was publishable in prior snapshot, now withheld | high |
| `capture_lost` | Source was `current` in prior snapshot, now `missing_after_capture` | high |
| `digest_item_dropped` | Item was included in prior digest, now absent without a recorded correction | medium |
| `correction_not_propagated` | A `false_corrected`/`disputed` transition not reflected in current digest | high |
| `baseline_absent` | No prior snapshot — regression axis could not be evaluated | low |

> **Note on overlap with `refresh-runner`:** the refresh runner already *re-opens* stale-bound
> statements (verified→unverified) as a side effect of the weekly run. The 5.13 analyzer is
> **read-only and explanatory**: it detects and *reports* these transitions (and the ones the runner
> does not act on — dropped digest items, un-propagated corrections, orphan sources) for reviewer
> triage. It must not duplicate or trigger the runner's mutations.

---

## 5. Behavioral guarantees (acceptance-relevant invariants)

1. **Read-only.** The analyzer must not mutate any source or statement record. Same inputs in ⇒
   identical report out (deep-equal); verified by running twice on a frozen fixture.
2. **Deterministic & idempotent.** No `Date.now()`, no `Math.random()`, stable sort on
   `(axis, severity desc, type, subjectId)`. Re-running on an unchanged snapshot yields zero new
   findings beyond the prior report.
3. **Fail-closed on ambiguity.** Unknown/absent inputs degrade to the explicit `*_unknown` /
   `*_unchecked` / `baseline_absent` finding types — never silently dropped, never reported as a
   confirmed high-severity gap.
4. **Gated external calls.** Wayback availability runs only when `enabled && authorized`; default
   closed. No other network calls.
5. **No public output.** The report is a reviewer-internal artifact. No email, no public web, no
   editorial surface (that is the deferred x.08 lane — GOV-572 HOLD). Buildable-envelope only.
6. **AI boundary.** No AI in the detection path. Optional summary prose only over an
   already-produced deterministic report, labeled `ai_analysis`, never primary evidence.

---

## 6. Implementation shape (for the impl child)

- New module `src/backgap-regression.js`, ESM `export` style consistent with existing substrate.
- Pure exported function, e.g. `analyzeBackGapRegression({ sources, statements, priorSnapshot,
  expectedCoverage, now }, options)` returning the §4 report.
- A deterministic text renderer `renderAnalysisReport(report)` mirroring
  `refresh-runner.renderRunLog` / `digest-assembler` render style.
- Reuse existing helpers (canonicalization, status predicates, publishable check) rather than
  re-deriving them; import from the existing substrate modules.
- Test file `test/backgap-regression.test.js` (node:test), covering every §4 finding type, the
  read-only/idempotent invariants, fail-closed degradation, and the Wayback-gated branch
  (mocked, not a live call).

---

## 7. Stage gate & sequencing

Mirrors the Stage 5.12 traceability slice (plan → impl → VSR → sec):

| Step | Issue | Owner | Blocked by | Initial status |
|---|---|---|---|---|
| Plan + sequence | **GOV-573** (this) | CTO | — | this slice |
| Impl | 5.13-impl | BackendCrawlerEngineer | — | **todo (chain head)** |
| VSR | 5.13-vsr | VerificationSafetyReviewer | impl | blocked |
| Security | 5.13-sec | SecurityPrivacyAgent | vsr | blocked |

Only the impl child is unblocked. VSR and Security carry real `blockedByIssueIds`. No scope beyond
Alpine; no public/email/editorial output. The deferred Stage 5.08/5.09/5.11 chains are NOT advanced
(GOV-572 HOLD — Isaac owner decision).

---

## 8. Verification evidence required at each step

- **Impl:** `node --test test/backgap-regression.test.js` green; new module + tests committed; commit
  SHA + file paths in the issue comment; full-suite `node --test` still green.
- **VSR:** independent confirmation of the §5 invariants (read-only, deterministic/idempotent,
  fail-closed, gated-external, no-public-output, AI-boundary) with the exact commands run and their
  output; explicit pass/fail per invariant.
- **Security:** privacy/publication-gate review — confirm the report contains no
  publishable-as-fact unreviewed content, no PII beyond what the substrate already holds, and that
  the buildable-envelope (no public output) holds; pass/fail with evidence.
