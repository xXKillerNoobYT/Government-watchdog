# Coaching proposal — VerificationSafetyReviewer (`3f95c8ce-c929-4c30-a327-9871bcbc5643`)

Window 2026-07-09→16. Volume: 7 issues, runs 7 ok / 3 failed / 1 cancelled. Failures environmental
(workspace drift Jul 13, stale worktree Jul 10, one `process_lost` with auto-retry). Audit quality itself
(GOV-651, GOV-688, GOV-701, GOV-713, GOV-761 all `done`) shows no evidence-backed behavioral pattern to
correct. No instruction change proposed.

## Cluster — duplicated audit lane currently parked blocked

**Evidence:** GOV-770 ("Wave-2 VSR audit — … card-gate integrity, WRITE-ONCE…") and GOV-774 ("VSR audit —
Wave-2 scaled promotion lane: card gating, WRITE-ONCE, anchor discipline…") are scope-identical audits, one
per duplicated exec lane (GOV-769 vs the cancelled GOV-773). CTO confirmed the duplication (`20fa4afd` on
GOV-769: "GOV-773/GOV-774 … are scope-identical re-staging duplicates"). GOV-774 is now blocked by the
*cancelled* GOV-773, which the harness escalated as GOV-776 (comments `e0498208`, `33d0a826` on GOV-774).

**Proposed action (board/CTO, not a VSR instruction change):** when GOV-776 resolves, fold GOV-774 into
GOV-770 so exactly one Wave-2 VSR audit survives, blocked only by the surviving exec lane GOV-769.

## Optional micro-rule (offered, low confidence — only if the board wants it)

VSR could not have prevented the duplication (both audits were created for it), but a cheap self-defense:
```
+ On wake for an audit issue, check for a sibling audit with near-identical scope before starting; if one
+ exists, flag the pair to CTO/CEO for collapse instead of proceeding on both.
```
**Replay case:** given two audit issues with ~identical titles created minutes apart → expected: one
comment flagging the pair with both ids, work proceeds on the older lane only.

Evidence volume for this rule is a single occurrence, so it is offered as optional rather than recommended
(rules need repeated patterns; dropping it costs nothing since CTO collapse handling already worked).
