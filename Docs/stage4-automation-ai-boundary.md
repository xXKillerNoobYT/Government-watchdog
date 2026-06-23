# Stage 4.09 — Automation-vs-AI Boundary for Historical Newsletter Generation

**Issue:** GOV-471 · **Stage:** 4.09 · **Owner:** AutomationOpsEngineer
**Status:** boundary contract (versioned technical contract, Alpine-only)
**Substrate this contract governs:** `src/source-registry.js`, `src/statement-verification.js`
**Verification command:** `npm test` (Node test runner — 9 tests, all passing as of this contract)

---

## 1. Purpose

Lock the deterministic/AI boundary for the Stage 4 historical newsletter backbone so Paperclip
work does not drift into unsupported AI gathering, inference, or publication. This contract maps
the six [AI Gateway processing lanes](../../instructions/AI_GATEWAY_PROCESSING_WORKFLOW.md) onto the
concrete code already present in this workspace, and states exactly where deterministic automation
ends and AI assistance begins.

**Governing principle:** *Deterministic automation owns collection, identity, validation,
exact-source linking, archive checks, gate enforcement, and digest assembly. AI may only
summarize evidence that deterministic automation has already collected and source-bound, and only
under a visible label with a citation trail. AI output is never primary evidence and can never
earn a `verified` status.*

This is a reviewer-internal Stage 4 contract. Public publication, email send, and account-facing
delivery remain Isaac-gated (GOV-420) and are out of scope here.

---

## 2. Boundary at a glance

| Lane (AI Gateway workflow) | Owner | Concrete substrate |
|---|---|---|
| 1. Deterministic ingest (fetch, archive, hash/version, extract, store, log) | **Deterministic** | `buildSourceCapture`, `hashFileSha256`, `canonicalizeUrl`, `applyReplacementDetection`, `importSources` |
| 2. AI-assisted extraction (propose statements/summaries w/ confidence + anchors) | **AI, gated** | `createStatement({kind:"ai_analysis"})` + `createSourceLink` trace anchors |
| 3. Verification layer (compare to primary source, assign label, flag uncertainty) | **Deterministic gate + human** | `applyVerificationTransition`, `assertFactClaim`, `assertHasSourceTrace` |
| 4. Risk layer (privacy/legal/publication/moderation no-go) | **Deterministic gate + reviewer** | `evaluatePublicationGate`, `sensitivityFlags`, `publication.doNotPublish` |
| 5. Human/reviewer gate (approve/correct/dispute/hold/reject) | **Human** | `verify`/`dispute`/`correct_false`/`do_not_publish` transitions |
| 6. Frontend/API publication (approved states only, visible labels) | **Deterministic, gated** | `evaluatePublicationGate().publishable === true` — Isaac-gated for public surface |

---

## 3. AC1 — Steps that MUST be deterministic and testable

Each step below must be performed by a script with a covering test. None may be delegated to AI.

| # | Deterministic step | Code symbol | Covering test | Reproducibility anchor |
|---|---|---|---|---|
| D1 | **Collection / capture** of official Alpine records to a local path with capture metadata | `buildSourceCapture` (`source-registry.js`) | `missing local capture is preserved as missing_after_capture` | Capture method, actor, MIME, size recorded |
| D2 | **Source identity** — canonical URL normalization | `canonicalizeUrl` | exercised by replacement tests | lowercased host, stripped hash, no trailing slash |
| D3 | **Content hashing** — SHA-256 of captured bytes | `hashFileSha256` | `hashFileSha256 returns stable SHA-256 content hashes` | byte-identical input → identical hash |
| D4 | **Change/replacement detection** — same canonical URL, changed hash → `replaced` lifecycle | `applyReplacementDetection` | `same canonical URL with changed hash creates replacement records`; `TOA import fixtures prove changed-hash replacement and missing preservation` | deterministic `replaced` ↔ replacement linkage |
| D5 | **Archive/lifecycle state** — `current` / `replaced` / `missing_after_capture` / `rejected` | `LIFECYCLE_STATUSES` | replacement + missing-capture tests | closed enum, no AI-assigned states |
| D6 | **Exact-source linking** — statement → source quote + page/timestamp/location with a stable trace hash | `createSourceLink`, `computeSourceTraceHash` | `source links include deterministic trace hashes for quote and location` | normalized-whitespace quote → stable trace |
| D7 | **Verification gate enforcement** — only `fact_claim` with a source trace can become `verified` | `applyVerificationTransition` (`verify`), `assertFactClaim`, `assertHasSourceTrace` | `verification transitions cover unverified to verified...`; `AI analysis cannot be verified as fact` | AI analysis structurally barred from `verified` |
| D8 | **Publication gate** — fail on missing source links, missing evidence limits, AI-as-fact, do-not-publish | `evaluatePublicationGate` | `publication gate fails missing source links, evidence limits, AI-as-fact, and do-not-publish flags` | gate is pure function of statement fields |
| D9 | **Correction bookkeeping** — prior/new text + status + correcting source recorded in history | `applyVerificationTransition` (`correct_false`) | `correction to false_corrected records prior and new statement history` | append-only correction history |
| D10 | **Repeatable digest assembly** — select only `publishable === true` statements into the weekly digest in a stable, reproducible order | *follow-up assembler (see §7 F1); MUST reuse `evaluatePublicationGate`* | to be added with the assembler | same inputs → byte-identical digest body |

**Rule:** A digest the assembler emits must be reconstructable from the source registry + statement
records alone. If a digest line cannot be traced to a `publishable` statement with at least one
source link and a trace hash, it is a defect, not a stylistic choice.

---

## 4. AC2 — Where AI summarization is allowed (labeling + citation requirements)

AI is permitted in exactly one place: **summarizing evidence already collected and source-bound by
deterministic steps D1–D9** (AI Gateway lane 2). AI is *not* permitted to gather sources, decide
verification status, assign risk, set publication state, or author the digest's factual claims.

AI output must satisfy ALL of the following or it is rejected at the gate:

1. **Labeled kind.** Stored only as `createStatement({ kind: "ai_analysis" })`. It can never be
   `fact_claim`, and `assertFactClaim` guarantees it can never transition to `verified`.
2. **Citation trail.** Every AI item must carry ≥1 `sourceLink` (`sourceId` + `quote` +
   page/timestamp/location) pointing to an already-registered source. No source link → blocked by
   `missing_source_links` in `evaluatePublicationGate`.
3. **Evidence limits.** `evidenceLimits` must state what the cited source does and does not prove.
   Empty → blocked by `missing_evidence_limits`.
4. **Visible label on display.** Per the Backend/Frontend Evidence Workflow, AI analysis must render
   visually separate from source-backed facts, with the AI label and source/audit links visible.
   Visual polish must never imply verification.
5. **No new claims.** AI may compress/paraphrase already-cited evidence. It may not introduce a fact,
   motive, intent, legal conclusion, allegation, or entity not present in the cited source.
6. **Source-grounded prompt.** The gateway prompt must require source-grounded output, uncertainty
   labels, and no unsupported allegations (AI Gateway workflow rule).

**Summary:** AI writes the *prose around* verified facts under an `ai_analysis` label; deterministic
automation owns *which facts exist, that they are true, and that they are publishable.*

---

## 5. AC3 — Failure behavior when a source/evidence link is missing, ambiguous, corrected, or disputed

Failure behavior is **fail-closed**: the affected statement is withheld from the digest until repaired.
"Withheld" means `evaluatePublicationGate().publishable === false` or status is not `verified`.

| Condition | Deterministic detection | Behavior | Owner action |
|---|---|---|---|
| **Missing source link** | `evaluatePublicationGate` → `missing_source_links`; `assertHasSourceTrace` throws on `verify` | Statement is non-publishable and cannot be verified. Excluded from digest. | Backend/Transcript engineer adds source link, or statement is dropped. |
| **Missing evidence limits** | `evaluatePublicationGate` → `missing_evidence_limits` | Non-publishable. Excluded from digest. | Author supplies `evidenceLimits`. |
| **AI analysis presented as fact** | `evaluatePublicationGate` → `ai_analysis_as_fact`; `assertFactClaim` blocks `verify` | Non-publishable; transition rejected. | Relabel as `ai_analysis`, keep visible AI label, or supply a `fact_claim` with its own source. |
| **Ambiguous / duplicate source** | same canonical URL, changed hash → `applyReplacementDetection` marks prior `replaced`; trace hash distinguishes distinct quotes | Digest must cite the `current` (non-`replaced`) record; `replaced` records are excluded from new claims. | Reviewer confirms which capture is authoritative; re-link statement to `current` source. |
| **Missing-after-capture source** | `buildSourceCapture` sets `lifecycleStatus: "missing_after_capture"`, `contentHash: null` | Source cannot back a verified claim (no hash). Any statement relying on it is non-publishable. | Re-capture the record; do not publish from a source that cannot be hash-verified. |
| **Corrected (false/corrected)** | `applyVerificationTransition('correct_false')` sets status `false_corrected`, appends `correctionHistory` | Original claim is not republished as-is. Correction history is preserved and must be surfaced; only the corrected text (with its correcting source) may flow forward. | Reviewer authors correction with `reason` + `correctingSourceLink`; digest shows correction, not stale claim. |
| **Disputed** | `applyVerificationTransition('dispute')` sets status `disputed` with `disputeReason` | Disputed statements are not treated as verified facts. They may appear only with a visible "Disputed" label and the dispute reason, never as a settled claim. | Reviewer resolves to `verified` (re-affirm), `false_corrected`, or holds as disputed. |
| **Do-not-publish / sensitive flag** | `evaluatePublicationGate` → `do_not_publish` / `do_not_publish_sensitive_flag` | Hard exclusion regardless of verification status. | Owner/reviewer decision required to lift; default is exclude. |

**Cross-cutting rule:** A failed gateway item blocks downstream presentation until repaired or
explicitly waived by owner/reviewer (AI Gateway workflow). Silent drops are not acceptable — every
excluded statement must be visible in the run log (see §8) with its failure reason(s).

---

## 6. AC4 — Crawl / check cadence for historical weekly newsletter refreshes

The "historical" newsletter is a weekly refresh over already-known Alpine sources. It is a
**re-validation pass**, not open-ended discovery. Cadence assumptions:

| Aspect | Assumption |
|---|---|
| **Refresh interval** | Weekly. One deterministic re-validation pass per registered Alpine source set. |
| **Scope** | Alpine-only. No scope expansion without CEO unlock (hard stop). The pass re-checks sources already in the registry; new-source discovery is a separate, explicitly-authorized task. |
| **Per-source check** | Re-capture → `hashFileSha256` → compare to stored `contentHash`. Unchanged hash = no-op (idempotent). Changed hash = `applyReplacementDetection` marks prior `replaced`, registers new `current`, and **re-opens** any statement whose `sourceContentHash`/trace no longer matches the `current` capture. |
| **Idempotency** | A weekly run over unchanged sources must produce no new records and no digest changes — byte-identical output. This is the primary "is the automation healthy?" signal. |
| **Archive check** | Each refresh confirms the local capture still exists and is readable; `missing_after_capture` is flagged, never silently skipped. (External Wayback/archive confirmation is a Stage 5 follow-up — see §7.) |
| **Digest rebuild** | After re-validation, re-assemble the digest from `publishable === true` statements only. Statements that fell out of `publishable` (e.g., source replaced, now disputed) are dropped from the new digest with a logged reason. |
| **Run logging** | Every weekly run writes a summary log: sources checked, unchanged, replaced, missing, statements re-opened, statements excluded (with reasons), digest line count. Log path under §8. |
| **Failure / issue threshold** | Create a Paperclip issue on: 3+ consecutive run failures; any `missing_after_capture` on a source backing a published claim; any scope leak (non-Alpine source); any digest line that fails trace-back. (Matches Run-log-review workflow thresholds.) |
| **Review cadence** | A human/reviewer reviews the weekly run log before any reviewer-internal digest is treated as current. No auto-promotion. |

**Cadence is deterministic; AI does not run on a schedule.** AI summarization is invoked only on
the already-collected, already-verified evidence set, on demand, after the deterministic refresh
completes — never as the crawl mechanism.

---

## 7. AC5 — Implementation follow-ups (incl. Stage 5 hot-topic / Wayback)

These are noted, not started here. They require CEO/CTO staging and (where marked) remain gated.

| ID | Follow-up | Stage | Notes / gate |
|---|---|---|---|
| **F1** | **Deterministic digest assembler** consuming `publishable` statements in a stable order (step D10). Must reuse `evaluatePublicationGate`; no AI in selection. Needs its own test for byte-identical output and trace-back. | Stage 4 | Reviewer-internal; buildable now under existing Stage 4 GO. |
| **F2** | **Weekly refresh runner + run-log writer** wiring D1–D9 into one idempotent pass (§6). Add `--dry-run`/`--apply`, log format `[YYYY-MM-DD HH:MM:SS] [LEVEL] msg`, idempotency test. | Stage 4 | Per Script-implementation workflow; CTO reviews dry-run before first `--apply`. |
| **F3** | **External archive (Wayback) confirmation** — beyond local-capture existence, confirm an independent archived copy exists/was created for each source. | **Stage 5** | Contacts an external service → AI Gateway/Automation hard stop: needs CEO/CTO authorization before any automated external call. |
| **F4** | **Hot-topic detection** — surfacing newly-changed or high-activity Alpine sources for the live (non-historical) newsletter. Must stay deterministic for *detection* (hash/diff/recency); AI only summarizes after. | **Stage 5** | Separate from historical refresh; do not let hot-topic discovery widen historical-pass scope. |
| **F5** | **Reviewer queue surfacing** of disputed / corrected / do-not-publish statements so humans clear them before each weekly digest. | Stage 4/5 | Frontend reviewer-internal; respects gated-beta access rules. |
| **F6** | **Public/email delivery** of the digest. | — | **Isaac-gated (GOV-420).** Out of scope until owner approves public surface. |

---

## 8. Contract operations (logs / failure / owner / escalation)

| Field | Value |
|---|---|
| **Run trigger** | Weekly deterministic refresh (F2, once built); on-demand re-validation; `npm test` for substrate verification. |
| **Input contract** | Registered Alpine sources (`fixtures/toa-sources.json` shape) + their local captures; existing statement records with source links. |
| **Output contract** | Updated source registry (lifecycle transitions), re-evaluated statement publication states, reviewer-internal digest of `publishable` statements, and a run log. No public/email output (gated). |
| **Log path** | `Logs/stage4-newsletter-refresh.log` (gitignored, local/vault-only — raw run evidence is not committed per the Data publication boundary). Format `[YYYY-MM-DD HH:MM:SS] [LEVEL] msg`. |
| **Failure handling** | Fail-closed (§5). Excluded statements logged with reasons. Gateway failure blocks downstream presentation until repaired/waived. |
| **Issue threshold** | 3+ consecutive run failures; missing-after-capture on a published source; scope leak (non-Alpine); any digest line failing trace-back; any AI item lacking source link/evidence limits. |
| **Review cadence** | Human review of each weekly run log before the digest is treated as current; this contract reviewed at each Stage 4 closeout and before Stage 5 (F3/F4) work starts. |
| **Acceptance / verification** | `npm test` → 9 passing tests prove D2–D9 deterministic guarantees; F1/F2 add their own idempotency + trace-back tests before scheduling. |
| **Owner** | AutomationOpsEngineer (this contract + F1/F2 runner). CTO owns technical sequencing; reviewer/VerificationSafetyReviewer owns human gate; CEO owns Stage 5 unlock; Isaac owns public publication (F6). |
| **Escalation / hard stops** | External calls (F3 Wayback), scope beyond Alpine, public/email delivery (F6), or any `--apply` default → escalate to CEO/CTO before proceeding. |

---

## 9. Verification evidence (this contract)

```
$ npm test
# tests 9
# pass 9
# fail 0
```

The 9 passing tests (listed per-step in §3) are the current proof that the deterministic substrate
this boundary depends on behaves as specified. This contract adds no code; it documents and locks
the boundary around code that already passes.
