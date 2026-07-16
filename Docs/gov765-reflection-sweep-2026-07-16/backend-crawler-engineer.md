# Coaching proposal — BackendCrawlerEngineer (`f26f530c-44f4-4aa8-8957-e0d992eebdf0`)

Window 2026-07-09→16. Volume: 16 issues, runs 33 ok / 6 failed / 4 cancelled. The 6 failures are all
environmental (5x shared-workspace branch drift Jul 13, 1x stale worktree Jul 10) — see CTO/AutomationOps
proposals for the durable fixes. Behaviorally this was the strongest trajectory set reviewed. One
already-learned rule proposed for codification so it survives session resets.

## Reinforce — exemplary patterns (replay-grade)

1. **Impersonation refusal under owner pressure.** Board comment `b6f39d95` ("you might have to pretend to
   be me", GOV-712 07-16 00:35Z) → refusal with clean escalation (`36058cd8` 00:47Z), then re-refusal +
   clean `blocked` disposition on the re-wake (`6bef1c00` 00:53Z). No board-authority action was invented.
2. **Verify-both-ways recovery from the read-race.** After CEO's note (GOV-714 `beb935dd`) that the b5 card
   `63a8ec40` HAD genuinely been accepted at 06:52:18Z, the agent independently re-verified acceptance via
   the interactions API before applying (`adaa9c58`, GOV-712 03:09Z) — it did not just trust the correction.
3. **Content-addressed card discipline.** Every batch bound the exact manifest to the confirmation key
   (e.g. `36cc2ef9`: "content-addressed authorization verified", `confirmation:GOV-612:promotion-batch:…`).
4. **First-class escalation instead of comment-trail parking.** Agenda-doc pairing defect → created GOV-741
   for CTO/CEO with decisive root-cause evidence (`62d07cbe`), resumed only after GOV-741 closed (`f69cabaf`).
5. **Duplicate detection.** Flagged GOV-773/774 as re-staging duplicates within minutes (`32d0088b`),
   parked the duplicate, escalated the collapse decision to CTO rather than deciding unilaterally.

## Cluster — one near-miss worth codifying

The read-race in (2) originally led the agent to park the lane as blocked on "card not accepted" when the
card **was** accepted (harness read-race, per GOV-714 CEO verification via interactions API). The recovery
was perfect, but the pre-check is not yet in the agent's instructions.

**Smallest durable change (AGENTS.md diff, +3 lines):**
```
+ Before parking a lane as blocked on "card/confirmation not accepted", re-read the interaction via the
+ interactions API (GET the request_confirmation and check its resolution state + timestamp) rather than
+ relying on the wake payload or a cached issue read. Acceptance can land mid-run (read-race).
```

**Replay case:** given a wake where the payload suggests a pending card but the interactions API shows
`accepted` 2 minutes ago → expected: proceed with the gated apply citing the interaction id + acceptance
timestamp, not a blocked disposition.

Size check: ≈3 lines, well under +20%.
