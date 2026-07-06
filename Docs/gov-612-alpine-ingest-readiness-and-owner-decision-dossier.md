# GOV-612 — Alpine Ingest Readiness & Owner-Decision Dossier

Status: **owner-gated, awaiting Isaac A/B/C** (interaction `confirmation:GOV-612:ingest:v2`, pending).
Owner decision only — CEO does **not** self-authorize ingest (legal / privacy / publication gate).
Author: CEO heartbeat 2026-07-06. Goal: `5e8b8006` (HEAD mission). Non-gated planning artifact.

---

## 1. Verified current state (evidence)

| Layer | State | Evidence |
|---|---|---|
| Agenda-board UI + redesign | DONE, merged, VSR=PASS | GOV-599 + GOV-605/606/607; PR #96 `655afba`, PR #28 `f305cdd` |
| Real-data projection layer | DONE, verified | GOV-610 evidence gate closed |
| Crawl→ingest pipeline | Built, **fixture-tested only** | Never run vs real Alpine sources |
| Registry (reviewed Alpine rows) | **EMPTY (0 rows)** | Board renders honest disclosed-empty `cardCount:0` |

**The gap is DATA, not code.** The only step left to make "see real Alpine progress" true is running
ingest against real sources — an owner-gated action. The disclosed-empty board is a *correctness
property*: the projection surfaces only reviewer-approved rows (AI-gateway rule: "AI output is never
primary evidence"), and none exist yet.

## 2. What "authorize ingest" concretely dispatches (turnkey chain for Option A)

Per `AI_GATEWAY_PROCESSING_WORKFLOW` (6 lanes) + `RISK_ASSESSMENT_WORKFLOW` + `GATED_BETA_ACCESS_WORKFLOW`.
On Isaac = A (or C, scoped), CEO stages this chain — **each a child issue with owner / stage /
acceptance / evidence**, blocker-linked in sequence. Not created now (respects the gate).

1. **Source-set definition** — enumerate the specific Alpine public government sources (council
   agendas/minutes, budget docs, meeting records) with URL, `captured_at` plan, robots/ToS posture,
   archive/hash strategy. Owner: CTO/BackendCrawlerEngineer. Acceptance: vetted source list + legal-posture note.
2. **Lane 1 — Deterministic ingest** — fetch, archive, hash/version, extract text/transcript, store
   metadata, log run. Acceptance: raw store + run log; no AI yet. Owner: BackendCrawlerEngineer.
3. **Lane 2 — AI-assisted extraction** — propose statements/events/summaries with confidence +
   source anchors (`source_id` + exact citation). Acceptance: every item traceable or labelled Unverified.
4. **Lane 3 — Verification layer** — compare AI output to primary source, assign verification label,
   flag uncertainty. Owner: VerificationSafetyReviewer.
5. **Lane 4 — Risk layer** — screen the 6 risk categories (evidence, AI-overclaim, privacy,
   defamation/legal, moderation, publication). No-go on unsupported allegations / private PII / legal
   conclusions / AI-only claims. Owner: SecurityPrivacyAgent + VerificationSafetyReviewer.
6. **Lane 5 — Human/reviewer gate** — approve / correct / dispute / hold / reject before any beta
   presentation. Only approved rows land in the registry. Owner: VerificationSafetyReviewer.
7. **Lane 6 — Publication layer** — board surfaces only approved states with visible labels +
   source/audit links, behind the gated-beta account model (no anonymous access). Owner: Frontend + CTO.

**Failure rule:** failed gateway processing blocks downstream presentation until repaired or
owner/reviewer-waived. Logs must record source set, model/tool version, output artifact, errors,
reviewer state, retry status.

## 3. Owner-decision risk posture (what Isaac is weighing)

- **Legal/source:** each Alpine source needs ToS/robots review + archive/citation before publish.
- **Privacy:** civic records can carry personal identifiers → PII screen (lane 4) is mandatory pre-publish.
- **AI-overclaim:** AI summaries must be labelled and source-anchored; never presented as fact.
- **Publication readiness:** beta is account-gated; nothing public until reviewer gate passes.
- **Reversibility:** Option C (narrow pilot) yields a reviewable real sample before any full run — the
  lowest-risk path to validate the end-to-end chain against a single source / date window.

## 4. Decision → next action map

| Isaac picks | CEO next action |
|---|---|
| **A — Authorize** | Stage the §2 chain (child issues, blocker-linked, Stage-0/AI-gateway gated), assign CTO as chain head. |
| **B — Hold disclosed-empty** (default/no-reply) | Terminal-hold GREEN. Board stays honest-empty. Frontier parks; goals incomplete-not-failed. No churn. |
| **C — Narrow pilot** | Stage §2 chain scoped to one source / date window; reviewer-gate the sample; report back before any full run. |

## 5. Guardrails (do-not)

- CEO does not run/authorize ingest without Isaac's selection.
- Do not pre-create the §2 child issues before A/C (avoids duplicate/churn — see GOV-617 duplicate incident).
- Do not flip goals `5e8b8006` / `fe3fc35a` / `2d8c4151` to achieved; frontier is parked, not done.
- Do not publish any ingested content outside the gated-beta account model.
