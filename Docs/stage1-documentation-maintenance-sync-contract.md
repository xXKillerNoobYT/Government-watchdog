# Stage 1.14 — Alpine Documentation-Maintenance & Paperclip/Obsidian Sync Contract

- **Stage:** Stage 1.14 (Alpine planning/spec contract).
- **Goal:** `421c0c6c-7379-43b5-8c22-012954a267cd` (Stage 1.14, active).
- **Issue:** GOV-64.
- **Owner:** AutomationOpsEngineer (`b9611d2e`). Cannot self-review.
- **Reviewer lanes:** CTO (`24fddc65`) technical sign-off; VerificationSafetyReviewer (`3f95c8ce`) source-of-truth/boundary correctness.
- **Project:** Government Watchdog Backend (`0a1832c4`).
- **Scope:** Alpine only. **Planning and specification only.** This document defines the documentation-maintenance & Paperclip/Obsidian sync contract. It does **NOT** authorize building sync automation, running jobs, moving/rewriting vault data, publishing, or any expansion beyond Alpine. Stage 1 implementation stays locked.
- **Builds on:** Stage 0.14 base contract (GOV-25, done, goal `11ec55fe`); Stage 1.12 traceability/audit-trail contract; Stage 1.13 back-gap & regression-analysis contract (GOV-61); the daily local-data cleanup precedent (GOV-15 / GOV-53) and the vault-rename cleanup (GOV-54); `AUTOMATION_OPS_WORKFLOWS.md` "Documentation maintenance and Paperclip/Obsidian sync review" workflow; `COMPANY.md`, `WORKFLOW_GOVERNANCE.md`.

This is a **contract**, not a runbook. It states the durable rules a future implementation issue must obey. Where it names a command, log path, or script, that is the *required shape* of a future build, not an instruction to build it now.

---

## 0. Definitions

| Term | Meaning in this contract |
|---|---|
| **Operative record** | The Paperclip object (goal / issue / comment / approval / blocker) that *is* the decision. Changing it changes reality for coordination. |
| **Committed contract** | A versioned file under the backend repo `Docs/` that records a contract artifact under git history. |
| **Reference note** | An Obsidian vault note (plan, lesson, research, raw/source data). Durable thinking, never the operative record. |
| **Drift** | Two surfaces that should agree but disagree (e.g., a repo doc says Stage 1 is locked, a vault note says it is active). |
| **Sync** | Bringing surfaces back into a defined, non-conflicting relationship — **not** making them bidirectionally equal. |
| **Raw/private material** | Unreviewed source caches, identity/address/voter-registry data, transcripts, scratch exports — local/vault-only by default. |

---

## 1. Doc inventory & ownership

The documents that make up the Government Watchdog knowledge surface, where each lives, and who owns updates. "Operative location" is the surface that, if it and another surface disagree, **wins**.

| Doc / artifact | Lives in | Operative location | Owner of updates |
|---|---|---|---|
| Stage gates, owners, blockers, status, approvals | Paperclip goals/issues | **Paperclip** | CEO (staging) / CTO (technical) |
| Stage 0/1 planning contracts (0.13, 0.14, 1.12, 1.13, **this 1.14**, 1.15) | Repo `Docs/*.md` + the issue that produced them | **Repo `Docs/`** for the contract text; **Paperclip issue** for its acceptance/disposition | Authoring specialist (e.g. AutomationOpsEngineer for sync; BackendCrawlerEngineer for crawl) |
| Specs (`Docs/phase1-spec.md`, `Docs/phase2-pilot-spec.md`) | Repo `Docs/` | **Repo `Docs/`** | CTO / authoring engineer |
| Alpine domain reference (`Docs/Alpine-Government-Mechanics.md`) | Repo `Docs/` | **Repo `Docs/`** | BackendCrawlerEngineer / SourceArchivist |
| Watchdog briefs (`Docs/Briefs/`) | Repo `Docs/Briefs/` | **Repo `Docs/`** (reviewed) | Authoring engineer; review lane before publish-ready |
| Master plan, lessons, research, raw/source data | Obsidian vault (`01_projects/Government-Watchdog v1 Plans/`) | **Vault** (reference only) | Isaac (designer/owner) |
| Premium Success-Criteria Framework | Vault (`.../Docs/2026-06-06-Premium-Success-Criteria-Framework.md`) | **Vault** (canonical template); pasted copies in goals/issues are derivatives | CEO + CTO |
| Run logs, backups, snapshots | Local / vault `Paperclip-Backups/` + repo `Logs/` (sanitized) | **Local/vault-only** (replay evidence) | AutomationOpsEngineer |
| Secret-path note (`Docs/stage0-github-paperclip-secret-path.md`) | Repo `Docs/` (path only, no secrets) | **Repo `Docs/`** | SecurityPrivacyAgent |

**Ownership rule:** every doc has exactly one accountable owner role. A doc with no named owner is itself drift and must be flagged in the next maintenance pass (§4).

---

## 2. Source-of-truth hierarchy (the app-boundary rule — may not be weakened)

This is the central constraint, taken verbatim in intent from `COMPANY.md`. No section of this contract, and no future automation built from it, may weaken it.

1. **Paperclip goals/issues/comments/approvals are the operative source of truth** for coordination, state, decisions, gates, owners, and verification. When a contract changes, **the Paperclip goal/issue is updated as the operative record** — not just a vault file, and not just a repo commit.
2. **Backend repo `Docs/` holds the committed contract artifacts.** The repo is where contract *text* is versioned; the Paperclip issue is where its *acceptance and disposition* live.
3. **The Obsidian vault is Isaac's reference second-brain** (plans, lessons, research, raw/source data). A vault link can **support** a Paperclip record but must **never replace** it. Vault file paths must not be used to make systems "coordinate" outside Paperclip.
4. **Raw/unreviewed/private material stays local/vault-only.** It is never synced into public-ish docs or UI.

**Explicit statement (required by acceptance criteria):** *A vault note supports, it never replaces, a Paperclip record. If a vault note and a Paperclip issue disagree about a decision, gate, owner, or status, the Paperclip issue is correct and the vault note is stale until reconciled.*

**Tie-break order when surfaces disagree:**

```
Paperclip (operative)  >  repo Docs/ (committed contract text)  >  Obsidian vault (reference)  >  local logs/backups (replay evidence)
```

A higher surface is never "fixed" to match a lower one. Reconciliation always flows the conflicting lower surface up to match the higher, or escalates if the higher surface is the one that is wrong (owner decision).

---

## 3. Sync workflow (how a change propagates without drift)

A change is any edit to a contract, gate, owner, status, field name, or decision. The contract defines **one-way** vs **reconciled** flows so nothing silently diverges.

### 3.1 Canonical change path (a contract changes)

```
1. Decision is taken            → recorded in the Paperclip issue/goal (operative).      [ALWAYS FIRST]
2. Contract text is edited      → committed to repo Docs/ on a branch + PR.              [one-way: Paperclip decision → repo]
3. Acceptance/disposition       → posted back to the Paperclip issue (path + line count). [closes the loop]
4. Supporting reference         → optional vault note updated to point AT the Paperclip
                                  record + repo path.                                     [one-way: down to vault, link only]
```

### 3.2 One-way vs reconciled

| Edge | Direction | Type | Rule |
|---|---|---|---|
| Paperclip decision → repo `Docs/` | down | one-way | Repo never originates a gate/owner/status change; it records contract text after the decision exists in Paperclip. |
| Repo `Docs/` → Paperclip disposition | up | one-way | The commit/PR path and `wc -l` are posted to the issue as acceptance evidence. |
| Paperclip / repo → vault note | down | one-way | Vault notes get a *link* to the operative record; they never push state up. |
| Vault → Paperclip | up | **blocked** | A vault edit is never authoritative. It may *prompt* a Paperclip change (someone reads it and files/updates an issue), but the vault edit itself changes nothing operative. |
| Local logs/backups → anything | — | replay-only | Evidence for reconstruction; never a coordination input. |

### 3.3 Conflict resolution

- **Repo vs Paperclip conflict on an operative rule (gate/owner/status):** Paperclip wins; open or update an issue to correct the repo doc; never edit the issue to match a stale doc. (Matches `AUTOMATION_OPS_WORKFLOWS.md` issue-creation threshold "repo docs and Paperclip goals conflict on operative requirements".)
- **Vault vs Paperclip:** Paperclip wins; vault is reference. Flag the stale vault path if it affects an active goal/issue/workflow (§4).
- **Repo vs vault, Paperclip silent:** the contract surface (repo) wins for *contract text*; escalate to the owner if the disagreement is about a decision Paperclip should have recorded but didn't.
- **Field-name conflict across docs:** resolved by §7 (single field dictionary).

---

## 4. Drift detection & cadence

Drift is detected, not assumed-absent. This ties directly to the Stage 1.13 back-gap & regression-analysis contract: a stale/contradictory doc is a back-gap in the knowledge surface.

### 4.1 What is flagged

| Drift class | Example | Action |
|---|---|---|
| **Stale vault path** | A goal references a vault path that moved (cf. GOV-54 vault → "v1 Plans" rename) | Flag if it affects an active goal/issue/workflow/artifact lookup; patch the *reference*, not the operative record. |
| **Repo ↔ Paperclip contradiction** | Repo doc says "Stage 1 locked", a goal flipped to active without a gate | Open repair issue; Paperclip is authoritative *for state*, but verify the state change was a real owner/CEO decision. |
| **Orphan doc** | A `Docs/*.md` with no owner and no linked issue | Assign owner or mark superseded (§5). |
| **Field-name skew** | Backend doc says `doc_date`, frontend doc says `documentDate` | Reconcile via §7 dictionary. |
| **Privacy leak risk** | Raw/private material appears in a publish-ready doc or UI surface | Stop; §6; escalate to SecurityPrivacyAgent. |

### 4.2 Cadence (mirrors `AUTOMATION_OPS_WORKFLOWS.md` review cadence)

- After **each Stage contract closeout**.
- **Weekly** during active Stage 0/Stage 1 automation work.
- **Before** any Stage 1 implementation relies on snapshots, vault paths, or backup artifacts.
- On **path-drift events** (a rename/move like GOV-54).

### 4.3 Detection shape (for a future build — not built here)

A future drift-check job, when authorized, must: read live Paperclip state first; compare against repo `Docs/` contract text and referenced vault paths; classify each finding into the table above; write to `Logs/documentation-maintenance.log`; and create a Paperclip issue only at the §4.1 thresholds. It must be `--dry-run` by default. **Building it is out of scope for Stage 1.14.**

---

## 5. Versioning & changelog (tied to 1.12 traceability/audit trail)

Doc revisions are traceable, and supersession is explicit — consistent with the Stage 1.12 traceability/audit-trail contract.

- **Repo `Docs/` text** is versioned by git: commit SHA + PR is the revision record. Every contract change references the issue identifier in the commit body (e.g. `GOV-64: ...`).
- **Operative revisions** (gate/owner/status) are versioned by the Paperclip issue/comment timeline; that timeline is the audit trail of record.
- **Supersession:** when a contract is replaced, the superseded doc gets a header line `> SUPERSEDED by <doc path / issue> on <date>` and the new doc links back to it. A superseded contract is never silently deleted — supersession is recorded, matching 1.12.
- **Changelog location:** the Paperclip issue that authorized the change is the changelog entry. Repo `Docs/` may carry a short `## Revision history` block, but the issue timeline is operative.
- **Cross-link:** a contract revision must name (a) the prior revision, (b) the authorizing issue, (c) the verification evidence. No orphan revisions (parallels the premium framework "no orphan claims").

---

## 6. Privacy / data boundary

This restates and binds the `WORKFLOW_GOVERNANCE.md` data-publication boundary into the sync contract.

- Raw/unreviewed/private identity, address, or voter-registry material **stays local/vault-only** and is **never synced** into repo `Docs/`, public-ish docs, briefs, feeds, or UI.
- Only **processed, reviewed, selected, website-ready** data may cross toward a public surface, and only after the relevant gate.
- Run logs, raw source caches, local databases, transcripts/raw media, scratch exports, and unreviewed research bundles are local/vault-only unless Isaac explicitly approves a sanitized fixture/sample.
- The secret-path note (`Docs/stage0-github-paperclip-secret-path.md`) documents *where* secrets live, never the secrets themselves; the same rule applies to any synced doc.
- **Sync never weakens privacy:** if a sync step would move raw/private material up the hierarchy (vault → repo → public), the step is blocked and routed to SecurityPrivacyAgent. This is a hard stop, not a judgment call for automation.

---

## 7. Backend ↔ frontend doc handoff (keeps 1.06–1.13 consistent)

Contract field names must stay aligned across docs so the Stage 1.06–1.13 contracts do not drift apart.

- **Single field dictionary:** field names (e.g. `doc_date`, `doc_type`, `meeting_date`, `source_url`, `scan_date`, `verification_status`, `uiStatus`) are defined once and referenced, not re-coined per doc. A future build may extract this dictionary; this contract names the rule.
- **Handoff rule:** when a backend doc introduces or renames a field that a frontend doc consumes, the change must (a) update the dictionary, (b) update both docs in the same change set, (c) be recorded on the Paperclip issue. A one-sided rename is drift (§4.1 field skew).
- **Predecessor alignment:** the Stage 1.05/1.06 contracts and the `uiStatus` validator/enums live on the unmerged `gov-17-newsletter-briefing-contract` branch (not yet `main`); any field-name reconciliation must check that branch, not assume `main` holds the canonical enum set, until it is merged.
- **Access-state behavior** (per `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`) must use the same labels in backend and frontend docs so a reviewer can trace one field end-to-end.

---

## 8. Similar-product research (docs-as-code / single-source-of-truth patterns)

Per the premium framework, comparable patterns reviewed before settling this contract's model. The question: how do mature teams keep one operative source of truth while letting reference material exist alongside it?

| Pattern | What it does | Pro | Con | Fit for GOV |
|---|---|---|---|---|
| **Docs-as-code (docs in-repo, PR-reviewed)** — e.g. GitLab Handbook, Write the Docs practice. Source: https://www.writethedocs.org/guide/docs-as-code/ | Treats docs like code: versioned, reviewed, single repo home. | Strong versioning, review, and history; no "which copy is current?". | Repo is poor at *coordination state* (who owns what now, what's blocked). | **Adopt for contract text** (repo `Docs/`). Do **not** make the repo the coordination plane — that's Paperclip. |
| **Single-source-of-truth + projection (SSOT)** — one authoritative store, everything else is a read-only projection. Source: https://en.wikipedia.org/wiki/Single_source_of_truth | One record originates a fact; other surfaces display copies that never write back. | Eliminates contradictory authorities; clear tie-break. | Requires discipline to keep projections read-only; tempting to "just edit the copy". | **Core of this contract:** Paperclip is SSOT for state; repo/vault are downstream, one-way. §3.2 enforces read-only-down. |
| **Knowledge-base vs system-of-record split** — Notion/Obsidian for thinking, Jira/Linear for operative tickets. Source: https://obsidian.md/ (PKM positioning) | Reference brain separate from the ticketing system of record. | Lets durable thinking live richly without polluting operative state. | Risk of the wiki silently becoming the "real" plan if links go stale. | **Exactly our vault↔Paperclip split.** §2 + §4.1 stale-path detection guard the failure mode. |
| **Changelog / ADR with supersession** — Architecture Decision Records, "superseded by" links. Source: https://adr.github.io/ | Decisions are append-only; new ones supersede, never silently overwrite, old ones. | Full decision history; supersession is explicit and auditable. | Overhead per decision; needs a home that won't be rewritten. | **Adopt for §5** supersession + the 1.12 audit trail; Paperclip issue timeline is the ADR log. |

**Lessons taken:** (1) repo = contract text, Paperclip = state — never collapse the two (docs-as-code + SSOT); (2) projections are one-way and read-only-down (SSOT); (3) reference brain must carry links *to* the operative record or it rots (KB split); (4) supersede, never silently delete (ADR). **Avoided:** making the vault a coordination plane; bidirectional sync; deleting superseded contracts.

---

## 9. Premium success-criteria template (completed)

Pasted from `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md` and completed for this contract.

## GOV Premium Success Criteria

Stage: Stage 1.14 — Alpine documentation maintenance & Paperclip/Obsidian sync (planning/spec contract).
Scope: Alpine only. Spec-only. Defines the contract; does not build automation, move vault data, publish, or expand scope.
Project/repo: Government Watchdog Backend (`0a1832c4`), repo `Docs/stage1-documentation-maintenance-sync-contract.md`.
Owner role: AutomationOpsEngineer (`b9611d2e`).
Reviewer path: CTO (`24fddc65`) technical sign-off; VerificationSafetyReviewer (`3f95c8ce`) source-of-truth/boundary correctness.
Blockers / unlock rule: Predecessor Stage 1.13 (GOV-61) done → no blocker. Stage 1 *implementation* stays locked regardless of this contract; unlocking is a separate CEO/owner gate.

### Success Definition
- Success means: a committed contract document exists that encodes the source-of-truth hierarchy without weakening it, defines one-way vs reconciled sync edges, defines drift detection + cadence, ties versioning to the 1.12 audit trail, binds the privacy boundary, and is signed off by CTO and VerificationSafetyReviewer.
- Evidence proving success: file path + `wc -l`; spec-only note (no code change); reviewer APPROVE on the two sign-off child issues.

### Failure Definition
- Failure looks like: the contract lets a vault note override a Paperclip record; describes bidirectional sync; permits raw/private data to flow into repo/public surfaces; has no owner per doc; or is marked done with no committed artifact (a GOV-49-class liveness incident).
- Stop/escalation trigger: any need to build automation, run a job against vault/repo, move/rewrite vault data, publish, make legal/privacy judgments on a named individual, or expand beyond Alpine → stop and route to CEO.

### Workability
- Real user/operator workflow: AutomationOpsEngineer (or a future maintenance job) runs the §4 drift pass after a contract closeout; reads Paperclip state first; classifies drift; patches the surface that owns the operative rule; posts evidence to Paperclip.
- Inputs: live Paperclip issue/goal state; repo `Docs/` contract text; referenced vault paths; logs/backups.
- Outputs: a drift classification, a patch on the correct surface, a Paperclip closeout comment, and (only at threshold) a repair issue.
- Missing/stale/disputed source behavior: stale vault path → flag + repair reference; repo↔Paperclip conflict → Paperclip wins, open repair issue; privacy leak → hard stop to SecurityPrivacyAgent.
- Resume/retry behavior: maintenance passes are idempotent by design (re-reading state and re-classifying yields the same result); a future job must be `--dry-run` default and safe to re-run.

### Ease of Use
- Resident/Isaac comprehension target: Isaac can read §2 and know, in one sentence, that Paperclip wins and the vault is a supporting brain.
- Labels/statuses/gaps visible: drift classes (§4.1) and the tie-break order (§2) are named explicitly; no hidden authority.
- Required screenshot/prototype/wireframe/review note: N/A (spec contract, no UI); reviewer sign-off comments substitute.

### Comparable Research
- Comparable tools reviewed: Docs-as-code (Write the Docs), Single-source-of-truth (SSOT), KB-vs-system-of-record split (Obsidian/Linear), ADR supersession.
- Lessons GOV should use: repo = contract text, Paperclip = state; one-way read-only-down projections; reference brain must link to the operative record; supersede never delete.
- Patterns GOV should avoid: vault as coordination plane; bidirectional sync; silent deletion of superseded contracts.
- Source links: in §8.

### Tradeoffs
- Main tradeoffs: strict one-way sync (more discipline, occasional manual reconciliation) vs bidirectional convenience (drift, contradictory authorities); repo-versioned text vs Paperclip-versioned state (two homes, but each authoritative for its own thing).
- Chosen approach and reason: strict one-way + SSOT-in-Paperclip, because the COMPANY.md app-boundary rule is non-negotiable and bidirectional sync is the exact failure mode it forbids.

### Plan Before Implementation
- Concept/data model: surfaces (Paperclip / repo / vault / logs) with a fixed tie-break order; docs with single owners; a field dictionary.
- UI/operator behavior: maintenance-pass workflow (§4); future drift-check job shape (§4.3) — not built here.
- Verification commands or review steps: `wc -l Docs/stage1-documentation-maintenance-sync-contract.md`; `rg` for required-section coverage; reviewer sign-off issues.
- Artifact paths: this file; `Logs/documentation-maintenance.log` and `Logs/paperclip-sync.log` (future, named not built).
- Failure handling: §3.3 conflict resolution; §6 privacy hard stop; §4.1 thresholds for issue creation.

### Source and Auditability
- Required source fields: each contract revision names prior revision, authorizing issue, and verification evidence (§5).
- Local source-data paths: vault `Paperclip-Backups/`; local logs — replay evidence only, never coordination.
- Archive/Wayback/timestamp/page requirements: N/A for this meta-contract; applies to the content docs it governs.
- Verification/correction status handling: supersession recorded per §5, tied to the 1.12 audit trail.

### Timeline and Concept Integrity
- Known-then vs later-outcome handling: superseded contracts keep their original text + a forward "SUPERSEDED by" link; later decisions link forward, never rewrite the old contract (parallels timeline known-then rule).
- Correction handling: a corrected contract is a new revision authorized by an issue; the old one is superseded, not edited away.
- Concept records kept separate: surfaces (operative / committed / reference / replay) are distinct record classes and never merged.
- Required typed relationships: `issue authorizes contract`, `contract supersedes prior contract`, `vault note supports issue`, `commit records contract revision`.

### Acceptance Evidence
- Required artifacts: this committed document.
- Required tests/checks: `wc -l` line count; `rg` section-coverage check; spec-only (no code) confirmation.
- Required issue/PR/screenshot/API/source evidence: CTO + VerificationSafetyReviewer sign-off child issues with APPROVE.

---

## 10. Stage boundary (locked scope)

**Stage 1.14 defines this contract only.** It does **NOT** authorize:

- building sync automation, drift-check jobs, or backup/restore scripts;
- running any job against the vault or repo;
- moving, renaming, or rewriting vault data;
- publishing to any public surface;
- contacting any external service or official;
- legal/privacy judgment on a named individual;
- AI-label policy changes;
- budget decisions;
- any scope beyond Alpine.

Stage 1 *implementation* stays locked. Any of the above is an **owner-escalation trigger** → stop and route to CEO (and Isaac for owner-level decisions). A future implementation issue, separately authorized, would carry the `--dry-run`-by-default, log-path, idempotency, and acceptance-test non-negotiables from `AUTOMATION_OPS_WORKFLOWS.md`.

---

## Verification evidence

- **File:** `Docs/stage1-documentation-maintenance-sync-contract.md`.
- **Spec-only:** no code changed; no script run; no automation built; no vault data moved. Line count recorded in the GOV-64 closeout comment.
- **Coverage:** source-of-truth hierarchy / app-boundary rule (§2, with the explicit "vault supports, never replaces Paperclip" statement and tie-break order), sync workflow one-way vs reconciled (§3), drift detection + cadence (§4), versioning + supersession tied to 1.12 (§5), and the privacy/data boundary (§6) are all covered.
- **Review:** CTO technical sign-off and VerificationSafetyReviewer source-of-truth/boundary sign-off requested via child issues (mirrors GOV-62/63).
