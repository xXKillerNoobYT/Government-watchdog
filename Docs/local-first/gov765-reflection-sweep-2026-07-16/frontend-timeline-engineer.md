# Coaching proposal — FrontendTimelineEngineer (`a73c847f-72cf-411c-a77b-3753f8a2225f`)

Window 2026-07-09→16. Volume: 4 issues (GOV-707/758/763/764, all `done`), runs 7 ok / 5 failed. Agent
currently shows `error` status. **No instruction change proposed** — every failure is adapter-level, and the
delivered work was high quality.

## Cluster — session-limit failure burst (not agent behavior)

**Evidence:** all 5 failures are Jul 16 `acpx_turn_failed: "You've hit your session limit · resets 2:50am
(America/Denver)"` — identical to CTO (x4) and Reflection Coach (x3) the same day. The runs never reached
agent reasoning; there is nothing to coach. The GOV-755→GOV-764 lane completed after the limit reset
(prod deploy `4693f9a`, CEO prod verification on GOV-706 comment `5a822b39`).

**Proposed action (ops, next-step owner: board/CEO):**
- Reset this agent's `error` status so wake-on-demand resumes cleanly.
- Consider harness-level backoff/stagger for claude_local wakes near the shared session-limit window
  instead of repeated immediate retries (12 burned runs across 3 agents on Jul 16).

## Reinforce

- Evidence discipline on the responsive-fix lane: GOV-763 shipped the CSS fix with local/beta screenshot
  validation at 3 viewports; GOV-764 deployed only after Isaac approval and closed with prod evidence.
  This matches the company rule that responsive claims come from real-browser evidence, not headless-CLI
  window-size screenshots.

## Replay case (positive)

Given a prod-affecting CSS fix → expected: local screenshots at 390/768/1440, owner-gated deploy, prod
re-verification before `done` — exactly the GOV-763/764 shape.
