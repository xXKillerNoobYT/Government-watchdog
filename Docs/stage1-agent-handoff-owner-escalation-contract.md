# Stage 1.15 — Alpine Agent Handoff & Owner Escalation Contract

Stage: 1.15 (final Stage 1 planning contract). Alpine-first. Planning/spec only — defines how work is handed off between agents and when/how it escalates to the owner (Isaac). No implementation, publication, official contact, or scope beyond Alpine is authorized here.

Owner: CEO. Reviewer lanes: CTO (technical/governance sign-off), VerificationSafetyReviewer (escalation-trigger + safety-boundary correctness).

Builds on the Stage 0.15 base (GOV-26) and the operating reality observed across the Stage 1.01–1.14 chain. Where Stage 0.15 defined the foundation rules, this contract makes them implementation-ready for Alpine and encodes the failure modes actually seen in execution.

## 0. Definitions

- **Handoff** — a transfer of the next action from one actor to another (agent→agent, agent→reviewer, agent→owner) such that exactly one actor owns the next step and that actor will actually be woken to take it.
- **Wake** — the event that causes a wake-on-demand agent to run: assignment, an `@`-mention comment, a resolved blocker, completed children, an approval resolution, a pending interaction targeted at the agent, or a harness liveness-recovery issue.
- **Owner** — Isaac (designer/owner). The owner is escalated to only for the decisions enumerated in §6; everything else is an agent-to-agent handoff.
- **Reviewer lane** — a named agent who signs off on a deliverable via a child review issue + comment + status, not via a board `request_confirmation`.
- **Liveness invariant** — a harness-enforced rule that every non-terminal issue must have a live actor owning its next action (see §5).

## 1. The wake model (why handoffs must be explicit)

Only the **CEO** has a timed heartbeat (every ~2h). **All other GOV agents are wake-on-demand.** A wake-on-demand agent does nothing until a wake event fires. This single fact drives every rule below: a handoff is only real if it produces a wake for the receiving actor.

Consequences encoded as contract rules:

1. **A comment is not a handoff.** Posting "please review" on an issue assigned to someone else does not wake them. To hand off to a wake-on-demand agent you must do one of: assign/create an issue to them, `@`-mention them with a structured `[@Name](agent://<id>)` link, resolve a blocker they are waiting on, or target a pending interaction at them.
2. **`in_review` pointed at a wake-on-demand reviewer with no wake is a stall**, not a healthy waiting state. It will be caught by the liveness invariant (§5) and is a defect.
3. **The CEO is the unstick-of-last-resort.** Because the CEO is the only timed agent, the CEO heartbeat is responsible for detecting and breaking stalls (waking a stuck reviewer, reconciling goal state, creating the next sequential issue when the board is idle).

## 2. Handoff patterns (the sanctioned shapes)

### 2.1 CEO → specialist (delegate work)
Create an issue with `assigneeAgentId` set, `goalId`/`projectId` linked, planning-only scope, acceptance criteria, reviewer lanes, and the §6 escalation triggers. Assignment wakes the specialist. Use first-class `blockedByIssueIds` for real dependencies, never text-only "blocked by" notes. One executable issue at a time — do not flood the goal map.

### 2.2 Specialist → reviewer (sign-off)
When a deliverable is ready, the owner of the work creates a **child review issue** assigned to the reviewer (creation wakes them). The reviewer records APPROVE / changes-requested via comment + status. This is the GOV pattern (e.g. GOV-65 CTO, GOV-66 VerificationSafetyReviewer). An author may **not** self-review their own deliverable.

### 2.3 Reviewer → author (changes requested)
Reviewer sets the review issue to a terminal state with a changes-requested comment and re-routes to the author by assignment/mention so the author is woken to revise.

### 2.4 Agent → CEO (cannot proceed)
If a specialist hits a missing plan, missing blocker, scope ambiguity, or a §6 trigger, it does **not** improvise. It comments the needed repair and routes to CEO (assignment or mention), setting the issue `blocked` with the named unblock owner/action if it cannot continue.

### 2.5 CEO → owner (escalation)
See §6. The CEO is the only actor that escalates to Isaac, and only for the enumerated decisions. Rule #1 still holds: never ask a human to do what an agent could do.

## 3. The "next action owner" rule

At all times, every non-terminal issue must have **exactly one** clearly identifiable actor who owns the next action **and** a live wake path to that actor. Before any agent exits a heartbeat it must leave the issue in one of:

- `done` — work complete, artifact verified, no follow-up on this issue.
- `in_review` — a real reviewer/approver/interaction/monitor path exists **and that actor will be woken**.
- `blocked` — first-class `blockedByIssueIds` resolve it, or a named owner has a concrete unblock action.
- `in_progress` — only with a live continuation (active run, queued work, or a monitor that will wake the assignee).
- delegated — a child issue owns the next step, linked by `parentId`/blocker.

A successful deliverable left `in_progress` with no live path is invalid — convert it to `done` or hand it off.

## 4. Reviewer-lane assignment matrix (Alpine)

Owner of a contract cannot review it. Standard lanes by authoring role:

| Authoring role | Primary sign-off | Secondary / consult |
| --- | --- | --- |
| BackendCrawlerEngineer / CTO | VerificationSafetyReviewer | BackendCrawlerEngineer (feasibility) |
| FrontendTimelineEngineer | CTO | VerificationSafetyReviewer, UXProductDesigner |
| SourceArchivist / TranscriptEvidenceEngineer | VerificationSafetyReviewer | CTO |
| NewsletterEditor | VerificationSafetyReviewer | CTO |
| AutomationOpsEngineer | CTO | VerificationSafetyReviewer |
| SecurityPrivacyAgent | CTO | VerificationSafetyReviewer |
| VerificationSafetyReviewer (QA) | CTO | SecurityPrivacyAgent |
| CEO | CTO | VerificationSafetyReviewer |

SecurityPrivacyAgent is always consulted when a public/private data boundary is touched. Any deliverable touching public-facing language/labels also routes through UXProductDesigner or VerificationSafetyReviewer.

## 5. Liveness invariants (avoid the GOV-49 class of incident)

The harness enforces `in_review_without_action_path`: an issue in `in_review` with an agent assignee but no participant, interaction, approval, user owner, wake, active run, or recovery issue trips a liveness incident and the harness auto-creates a recovery issue (observed: GOV-46 → GOV-49). Recovery works, but a tripped invariant is a defect, not a workflow.

Contract rules to prevent it:

1. Do not set `in_review` pointing at a wake-on-demand agent unless you also created the wake (a child review issue, or a targeted interaction).
2. Prefer: stay `in_progress` until the artifact + the reviewer child issue exist, then mark `done`; the review child issue carries the next action and wakes the reviewer.
3. The CEO heartbeat scans `in_review` issues and, where the reviewer's last heartbeat predates the pending interaction/handoff, wakes them with a structured mention.

## 6. Owner-escalation triggers (CEO → Isaac only)

Escalate to the owner — and only the owner — for these decisions. Define them as rules + escalation points in specs; never bake a unilateral judgment in:

1. Public launch / publishing any content publicly.
2. Official or subscriber contact, or any contact automation.
3. Accusations, legal conclusions, defamation/privacy judgments about a **named individual**.
4. Campaign messaging or political framing.
5. Budget / donations / paid services.
6. AI-label policy changes (what counts as labeled/gated).
7. Scope expansion beyond Alpine (Star Valley/Lincoln, Wyoming, US).
8. Contradictory product direction or a change that invalidates a prior owner decision.

Everything else is an agent-to-agent handoff. The CEO escalates with a decision-ready summary (what is being asked, options, recommendation, risks), not an open-ended question.

## 7. Backend ↔ frontend handoff

Handoff metadata must stay consistent with the upstream contracts: backend emits produced-by (`automation|ai|human`), `verificationStatus` (6-value), review state, and the source trail; the frontend renders gated public/private states and never surfaces unreviewed/private data. Field names align with the 1.06 frontend, 1.07 transcript/evidence, 1.11 publication-gate, and 1.12 traceability contracts. No orphan claims cross the boundary.

## 8. Similar-product research (handoff & escalation patterns)

- **Incident on-call / PagerDuty escalation policies** — explicit escalation tiers with timeouts and a named owner per tier. Lesson GOV uses: every handoff names exactly one next owner; escalate up a fixed chain, never sideways into ambiguity. Avoid: paging a human for what an agent can resolve.
- **GitHub PR review (CODEOWNERS + required reviewers)** — authorship and review are separated; merge is gated on a non-author approval. Lesson: the §2.2 author-cannot-self-review rule. Avoid: self-merge of one's own contract.
- **Editorial workflow (draft → editor → publish)** — promotion gates between states with a distinct approver. Lesson: the state machine in §3. Avoid: auto-publish on author signal.
- **Saga / workflow orchestration (durable task handoff)** — each step must durably own the next step or compensate; no "fire and forget." Lesson: the next-action-owner + liveness rules. Avoid: a comment-as-handoff that no one is woken to act on.

Fit for Alpine: these are operational patterns (not legislative-data models like GovTrack/Open States), so they map directly onto GOV's agent coordination layer rather than its civic-data layer.

## 9. Premium success-criteria template (completed)

## GOV Premium Success Criteria

Stage: 1.15 — Agent handoff & owner escalation (final Stage 1 planning contract)
Scope: Alpine-first; planning/spec only; defines handoff + escalation rules, builds nothing
Project/repo: Government Watchdog Backend / `xXKillerNoobYT/Government-watchdog`
Owner role: CEO (authored); CTO + VerificationSafetyReviewer sign-off
Reviewer path: child review issues assigned to CTO and VerificationSafetyReviewer
Blockers / unlock rule: predecessor Stage 1.14 (GOV-64) done; no blocker needed

### Success Definition
- Success means: any GOV agent, reading this contract, can determine for any issue who owns the next action, how to hand it off so the receiver is actually woken, which reviewer signs off, and whether a decision must escalate to Isaac — without improvising.
- Evidence proving success: this committed document; CTO + VerificationSafetyReviewer APPROVE on the sign-off child issues; the rules match observed execution (GOV-46/49 liveness, GOV-65/66 sign-off pattern).

### Failure Definition
- Failure looks like: a handoff rule that lets an issue sit `in_review` with no wake (stall), an author self-approving, an agent escalating routine work to a human, or an escalation trigger missing one of the §6 owner decisions.
- Stop/escalation trigger: any §6 decision; any contradiction with COMPANY.md governance.

### Workability
- Real user/operator workflow: CEO delegates → specialist delivers → reviewer signs off → CEO advances or escalates.
- Inputs: an issue with goal/project/owner/acceptance criteria.
- Outputs: a terminal disposition with a live next-action owner at every step.
- Missing/stale/disputed source behavior: out of scope for this contract (covered by 1.10/1.11/1.13); handoff-wise, ambiguity routes to CEO (§2.4).
- Resume/retry behavior: blocked issues auto-resume on `issue_blockers_resolved`; stalled reviews are unstuck by the CEO heartbeat (§5.3).

### Ease of Use
- Resident/Isaac comprehension target: Isaac can see, from §6 alone, exactly which decisions will ever reach him and in what form (decision-ready summary).
- Labels/statuses/gaps visible: the §3 status machine maps 1:1 to Paperclip statuses.
- Required screenshot/prototype/wireframe/review note: N/A (process contract, not UI); the §4 matrix is the reference table.

### Comparable Research
- Comparable tools reviewed: PagerDuty escalation, GitHub CODEOWNERS review, editorial draft→publish, saga orchestration (§8).
- Lessons GOV should use: one next-owner per step; author≠reviewer; promotion gates; durable handoff.
- Patterns GOV should avoid: sideways/ambiguous escalation, self-merge, auto-publish, comment-as-handoff.
- Source links: https://support.pagerduty.com/docs/escalation-policies , https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners

### Tradeoffs
- Main tradeoffs: explicit-wake overhead (mention/child-issue cost budget) vs. silent stalls; CEO-as-unstick centralizes load vs. distributes resilience.
- Chosen approach and reason: explicit handoffs + CEO unstick — correctness and liveness beat budget micro-savings; the wake model leaves no other safe option.

### Plan Before Implementation
- Concept/data model: handoff = (issue, next-owner, wake-path, status); escalation = (trigger, decision-ready summary, owner).
- UI/operator behavior: Paperclip statuses + reviewer child issues; no new UI.
- Verification commands or review steps: CTO + VerificationSafetyReviewer sign-off issues; cross-check rules against GOV-26 and observed incidents.
- Artifact paths: `Docs/stage1-agent-handoff-owner-escalation-contract.md`.
- Failure handling: liveness invariant (§5) + CEO heartbeat scan.

### Source and Auditability
- Required source fields: each rule traces to its origin (COMPANY.md, CEO_STAGING_WORKFLOW, GOV-26, observed incidents GOV-46/49/65/66).
- Local source-data paths: N/A (process contract).
- Archive/Wayback/timestamp/page requirements: comparable-product source links recorded in §8.
- Verification/correction status handling: handoff metadata preserves `verificationStatus` across boundaries (§7).

### Timeline and Concept Integrity
- Known-then vs later-outcome handling: handoffs never rewrite a prior reviewer decision; changes-requested creates a forward revision, not a history edit.
- Correction handling: a superseding owner decision creates a fresh escalation, preserving the prior one.
- Concept records kept separate: issues, reviews, approvals, and interactions stay distinct records.
- Required typed relationships: `parentId` (delegation), `blockedByIssueIds` (dependency), review-of (sign-off child → deliverable).

### Acceptance Evidence
- Required artifacts: this committed document.
- Required tests/checks: spec-only, no code; rules validated against observed execution.
- Required issue/PR/screenshot/API/source evidence: GOV-67 + PR; CTO and VerificationSafetyReviewer APPROVE child issues.

## 10. Stage boundary (locked scope)

This contract defines handoff and escalation **rules** only. It does NOT authorize: building any coordination tooling, changing agent runtime/heartbeat config, publication, official contact, legal/privacy judgments on named individuals, AI-label policy changes, budget, or scope beyond Alpine. It closes the Stage 1 planning chain; **Stage 2 is not unlocked by this issue** — that is a separate CEO→Isaac owner-gate decision evaluated against the Stage 1 exit criteria.

## Verification evidence

- Artifact: `Docs/stage1-agent-handoff-owner-escalation-contract.md` (this file).
- Spec-only; no code changes; no validator required.
- Coverage confirmed: wake model, handoff patterns, next-action-owner rule, reviewer matrix, liveness invariants, the eight owner-escalation triggers, backend↔frontend metadata handoff, comparable research, and the completed premium template.
- Sign-off: pending CTO (`24fddc65`) and VerificationSafetyReviewer (`3f95c8ce`) child review issues.
