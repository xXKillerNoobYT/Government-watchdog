# Stage 5.14 — Documentation Maintenance & Project-State Continuity Contract

**Issue:** GOV-580 (plan + sequence) · **Stage:** 5.14 · **Goal:** `43937de7-4ba2-4348-a830-1a32f0908915` (Documentation maintenance and project state continuity)
**Owner:** CTO (impl chain) / CEO (staging) · **Status:** planning contract (versioned, Alpine-only)
**Substrate covered (Stage 4/5):** `src/source-registry.js`, `src/statement-verification.js`,
`src/digest-assembler.js`, `src/refresh-runner.js`, `src/briefing.js`, `src/backgap-regression.js`
**Governing boundary contract:** `Docs/stage4-automation-ai-boundary.md` (GOV-471) — Stage 5.14
EXTENDS that contract; it does not replace it. The buildable-envelope / no-public-output rule
(GOV-436, GOV-420) remains in force.

---

## 1. Purpose

Define a **deterministic, reviewer-internal** mechanism that keeps the Government Watchdog
verification substrate's documentation and project state **current, drift-free, and handoff-ready**,
so a fresh agent can resume Stage 5 work without tribal knowledge.

Two distinct concerns:

1. **Documentation maintenance** — *currency* of the written record. Which docs/state must stay
   current for the reviewer-internal verification substrate (run-logs, workflow files, stage ledger,
   per-module continuity docs), how doc/state drift is detected, and the maintenance cadence + owner.
2. **Project-state continuity** — *handoff-readiness* of the current Stage 5 artifacts
   (source-registry, statement-verification, digest-assembler, refresh-runner, back-gap analyzer):
   each shipped module must have a documented purpose, public surface, inputs/outputs, invariants,
   and test entry point so resume requires no out-of-band knowledge.

This is a **planning/contract slice only** — no script/doc implementation occurs in GOV-580.
Downstream child issues (impl, VSR, security) derive their acceptance criteria from this document.

**Governing principle (inherited from GOV-471):** Deterministic automation owns collection,
identity, validation, and **this drift detection**. The continuity check reads the repository file
set and module export surface and produces a reviewer-internal report. It **never** mutates docs or
code, **never** auto-publishes, and **never** uses AI to decide whether a doc is stale or a module is
undocumented. AI output, if surfaced at all, is confined to optional human-readable summary prose
under an `ai_analysis` label over a report the deterministic engine already produced — it can never
be primary evidence.

---

## 2. Scope and non-scope

| In scope (Alpine-only) | Out of scope |
|---|---|
| A documentation-maintenance **registry** (manifest of required docs/state + owner + cadence) | Any Wyoming/US expansion |
| Deterministic **doc-drift / continuity detector** over the manifest vs the real repo | Auto-publication of any doc/report (email/public web — Isaac-gated, GOV-420) |
| Per-module **continuity docs** (purpose, public surface, I/O, invariants, test entry) | Mutating source/statement records or verification state |
| Reviewer-internal report (object + deterministic text render) | AI-decided staleness / undocumented-module classification |
| Reconcile doc drift introduced by the 5.10 / 5.12 / 5.13 slices | Editorial/newsletter docs (deferred x.08 lane — GOV-572 HOLD) |
| Handoff-readiness check so a fresh agent can resume | Filing/triaging findings as new issues automatically |
| Deterministic, idempotent, read-only analysis | Any unapproved external automated network call |

---

## 3. Inputs

The continuity check is a pure function of explicit inputs — no hidden clock, no network.

1. **Documentation-maintenance manifest** — an explicit, version-controlled descriptor (committed in
   the repo, e.g. `Docs/doc-maintenance-manifest.json` or an exported constant) listing every
   artifact that must stay current. Each manifest entry: `{ id, path, kind, owner, cadence,
   requiredFor, mustReferenceModules? }`. Supplied as data; the detector does not invent expectations.
   - `kind ∈ { module_continuity_doc, stage_contract, run_log, workflow_file, stage_ledger_ref,
     readme }`.
   - `cadence ∈ { per_slice, weekly, on_stage_close, on_demand }`.
2. **Repository file set** — the actual files present under the tooling repo (paths the caller
   enumerates and passes in; the detector does not walk the filesystem itself, to stay pure/testable).
3. **Module export surface** — for each `src/*.js` module that must be documented, the set of
   exported symbol names (passed in by the caller from a static read), used to detect
   `undocumented_export` and `documented_nonexistent_export` drift against the module's continuity doc.
4. **Prior continuity snapshot** *(optional)* — a previously persisted report, used only to mark
   findings as `new` vs `carried` for triage ordering. Absent ⇒ all findings are `carried=false`;
   never treated as an error.
5. **`now` / window bounds** — passed in by the caller (determinism; mirrors `refresh-runner`'s
   injected-clock pattern). The detector must not call `Date.now()` directly.

---

## 4. Findings model (output)

The detector returns a structured, JSON-serializable report — never mutates inputs:

```
{
  generatedAt,                 // echoes injected `now`
  manifestVersion,
  findings: [ Finding ],
  counts: { total, bySeverity: { high, medium, low }, byType: {...} },
  handoffReady: boolean,       // true iff zero high-severity findings
  summary                      // deterministic one-line text
}
```

Each `Finding`:

```
{
  type,        // enumerated below
  severity,    // "high" | "medium" | "low"
  subjectId,   // manifest entry id / module name / doc path
  detail,      // deterministic human-readable string, no AI
  carried,     // present in prior snapshot (for triage ordering)
  evidence     // { path?, expectedModules?, missingExports?, extraExports?, ... }
}
```

### 4.1 Documentation-maintenance finding types

| `type` | Meaning | Severity |
|---|---|---|
| `missing_required_doc` | A manifest-required artifact has no file at its `path` | high |
| `missing_module_continuity_doc` | A shipped `src/*.js` module has no continuity doc entry | high |
| `undocumented_export` | A module exports a symbol absent from its continuity doc's public-surface list | medium |
| `documented_nonexistent_export` | A continuity doc lists an export the module no longer provides (drift) | medium |
| `stale_reference` | A doc references a module/path/issue that no longer exists | medium |
| `unreferenced_required_module` | A manifest entry requires a module reference that the doc omits | medium |
| `cadence_unowned` | A manifest entry has no `owner` or no `cadence` | low |
| `orphan_doc` | A `Docs/` file not covered by any manifest entry (possible undocumented drift) | low |

### 4.2 Project-state-continuity finding types

| `type` | Meaning | Severity |
|---|---|---|
| `handoff_gap` | A Stage 5 artifact lacks one of {purpose, public surface, I/O, invariants, test entry} in its continuity doc | high |
| `missing_test_entry` | A module's continuity doc names no runnable `node --test` entry point | medium |
| `ledger_state_unknown` | Stage-ledger reference in the manifest could not be resolved to a goal status | low |

> **Note on overlap with `backgap-regression`:** the 5.13 analyzer audits *verification records*
> (sources/statements). The 5.14 detector audits *documentation and module state*. They share the
> read-only / deterministic / fail-closed posture and the `{ findings, counts, severity }` report
> shape, but operate on disjoint inputs and must not be merged.

---

## 5. Behavioral guarantees (acceptance-relevant invariants)

1. **Read-only.** The detector must not mutate any doc, manifest, or source file. Same inputs in ⇒
   identical report out (deep-equal); verified by running twice on a frozen fixture.
2. **Deterministic & idempotent.** No `Date.now()`, no `Math.random()`, stable sort on
   `(severity desc, type, subjectId)`. Re-running on an unchanged repo snapshot yields an identical
   report.
3. **Fail-closed on ambiguity.** Unknown/absent inputs degrade to explicit `*_unknown` finding types
   — never silently dropped, never reported as a confirmed high-severity gap.
4. **No network.** The detector performs no external calls; the caller supplies the file set and
   export surface.
5. **No public output.** The report is a reviewer-internal artifact. No email, no public web, no
   editorial surface (deferred x.08 lane — GOV-572 HOLD). Buildable-envelope only (GOV-436).
6. **AI boundary.** No AI in the detection path. Optional summary prose only over an
   already-produced deterministic report, labeled `ai_analysis`, never primary evidence.

---

## 6. Implementation shape (for the impl child)

The impl child has **two deliverables**:

**(A) The deterministic continuity detector**
- New module `src/doc-continuity.js`, ESM `export` style consistent with existing substrate.
- Pure exported function, e.g. `analyzeDocContinuity({ manifest, fileSet, moduleExports,
  priorSnapshot, now })` returning the §4 report.
- A deterministic text renderer `renderContinuityReport(report)` mirroring
  `refresh-runner.renderRunLog` / `digest-assembler` render style.
- Reuse existing helpers where applicable; do not re-derive canonicalization/status predicates.
- Test file `test/doc-continuity.test.js` (node:test) covering every §4 finding type, the
  read-only/idempotent invariants, fail-closed degradation, and `handoffReady` computation.

**(B) The documentation the detector checks for**
- A committed manifest (`Docs/doc-maintenance-manifest.json`, or an exported constant the test
  imports) enumerating every required artifact with `owner` + `cadence`.
- Per-module continuity docs for each shipped Stage 5 module (one `Docs/modules/<module>.md` per
  `src/*.js`, or a single consolidated `Docs/stage5-module-continuity.md`) covering purpose, public
  surface, I/O, invariants, and the `node --test` entry — sufficient that the detector run over the
  current repo returns `handoffReady: true` (zero high-severity findings).
- **Reconcile 5.10 / 5.12 / 5.13 doc drift:** ensure the back-gap analyzer (`backgap-regression.js`),
  traceability (5.12), and any 5.10 artifacts are represented in the manifest and have continuity
  docs, so the detector reports zero `missing_module_continuity_doc` for already-shipped code.

**Definition of impl done:** `node --test` full suite green (including the new test), and a recorded
run of the detector over the real repo file set returning `handoffReady: true` with its report text
pasted into the impl issue comment.

---

## 7. Stage gate & sequencing

Mirrors the Stage 5.12/5.13 slices (plan → impl → VSR → security).

| Step | Issue | Owner | Blocked by | Initial status |
|---|---|---|---|---|
| Plan + sequence | **GOV-580** (this) | CTO | — | this slice |
| Impl | 5.14-impl | AutomationOpsEngineer | — | **todo (chain head)** |
| VSR | 5.14-vsr | VerificationSafetyReviewer | impl | blocked |
| Security | 5.14-sec | SecurityPrivacyAgent | vsr | blocked |

Only the impl child is unblocked. VSR and Security carry the blocker in their description
(`status:blocked` + blocker GOV-id), because `blockedByIssueIds` does not persist in this build.
No scope beyond Alpine; no public/email/editorial output. The deferred Stage 5.08/5.09/5.11 chains
are NOT advanced (Isaac owner-gate). 5.15 (Agent handoff & owner escalation) follows 5.14 and is the
only remaining non-deferred subgoal.

---

## 8. Verification evidence required at each step

- **Impl:** `node --test` full suite green; new `src/doc-continuity.js` + `test/doc-continuity.test.js`
  + manifest + module continuity docs committed; commit SHA + file paths + the detector's
  `handoffReady: true` report text in the issue comment.
- **VSR:** independent confirmation of the §5 invariants (read-only, deterministic/idempotent,
  fail-closed, no-network, no-public-output, AI-boundary) with the exact commands run and their
  output; explicit pass/fail per invariant.
- **Security:** privacy/publication-gate review — confirm the report and continuity docs contain no
  publishable-as-fact unreviewed content, no PII beyond what the substrate already holds, and that
  the buildable-envelope (no public output) holds; pass/fail with evidence.

**Closeout:** at impl merge, flip goal `43937de7-4ba2-4348-a830-1a32f0908915` → `achieved` with a
CTO closeout comment naming the merge commit and the dependents transitioned.
