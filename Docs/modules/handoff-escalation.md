# Module continuity — `src/handoff-escalation.js`

> Reviewer-internal Stage 5 handoff doc. Tracked by `Docs/doc-maintenance-manifest.json`
> (`modcont-handoff-escalation`). Keep the **public surface** list byte-aligned with the
> module's named exports — the 5.14 `doc-continuity` detector checks this.

## Purpose
Deterministic, reviewer-internal agent-handoff & owner-escalation evaluator
(Stage 5.15, GOV-585; contract GOV-584 `Docs/stage5-15-agent-handoff-escalation-contract.md`).
Given a declarative role-transition model + a proposed handoff request, it decides whether the
transition is legal and whether its evidence bundle is complete and trust-separated; given a
proposed action it decides whether an Isaac owner gate fires (fail-closed); and given a stage-state
snapshot it decides whether the Stage-5 → Stage-6 exit gate is `ready`. Disjoint from the 5.13
back-gap analyzer (verification records, Python substrate) and the 5.14 doc-continuity detector
(documentation/module state) — this module audits *role transitions, evidence custody, and owner
gates* (contract §4 note). Reads the committed model `Docs/handoff-escalation-model.json`.

## Public surface
- `HANDOFF_VERDICT_TITLE` / `MODEL_VERSION` — render title + default model version constants.
- `HANDOFF_VIOLATION_TYPES` — frozen `{ type: severity }` map enumerating every §4.1 violation type.
- `ROLE_SET` / `TRIGGER_SET` / `EVIDENCE_BUCKETS` — frozen §3 enumerations.
- `OWNER_GATE_CONDITIONS` / `OWNER_GATES` — the five frozen Isaac owner gates (§3.4).
- `evaluateHandoff({ model, request })` — §4 handoff block (legal transition? bundle complete + trust-separated?).
- `evaluateEscalation({ triggerSet, action })` — §4 escalation block; fail-closed on ambiguous/unknown → `required`.
- `evaluateStageExit({ snapshot })` — §4 stage-exit block (deferred 5.08/5.09/5.11 excluded); `null` when no snapshot.
- `evaluateHandoffSlice({ model, request, triggerSet, action, snapshot, now })` — the full §4 verdict (composes the three).
- `renderHandoffVerdict(verdict)` — deterministic text render (mirrors `refresh-runner.renderRunLog`).

## Inputs / outputs
- **In:** `model:{ version, transitions[], ownerGates[] }`, `request:{ from, to, trigger, evidenceBundle:{ facts[], summaries[], aiAssumptions[], laterVerification[] }, reviewStatus }`, `action:{ conditionKind?, descriptorFlags }`, optional `snapshot:{ subgoals[], handoffLedger[] }`, injected `now`.
- **Out:** `{ generatedAt, modelVersion, handoff:{ decision, violations[], preservedReviewStatus, evidenceContract }, escalation:{ decision, gatesTriggered[], reason }, stageExit?:{ decision, blockingSubgoals[], openHandoffBlockers[], reason }, summary }`. `handoff.decision === "allow"` iff zero violations.
- **Side effects:** the exported functions have **none** (pure). The CLI `main()` reads the model JSON (+ an optional `--fixture` file), runs the slice, prints the verdict, and exits non-zero when the handoff is `deny` or escalation is `required`. It writes nothing, makes no network call, and contacts no owner.

## Invariants
- **Read-only / idempotent:** same inputs → deep-equal verdict; inputs never mutated.
- **Deterministic:** no `Date.now()` / `Math.random()` in the decision path; stable sort `(severity desc, type, subjectId)`.
- **Fail-closed:** illegal transition → `deny`; ambiguous/unknown escalation → `required`; missing stage state → absent `stageExit` block (never a silent `allow`/`not_required`/`ready`).
- **Trust separation is hard:** `facts`/`summaries`/`aiAssumptions`/`laterVerification` kept disjoint; no AI assumption is promotable to a fact; known-then context never rewritten (GOV-36).
- **No network, no owner contact, no public output, no AI in the decision path** — reviewer-internal artifact only (GOV-471/436/420).

## Test entry
```
node --test test/handoff-escalation.test.js
```
