# Coaching proposal — SecurityPrivacyAgent (`72d0eccf-74e0-4633-ae77-1cedc8b782ba`)

Window 2026-07-09→16. Volume: 3 issues, runs 2 ok / 0 failed. **No change proposed** — no evidence-backed
negative pattern (routine rule: no rule without linked trajectory evidence).

## Reinforce

- GOV-689 (API/MCP, accounts, payments, privacy threat model) and GOV-759 (GOV-721 leg 4a security/privacy
  review — argon2id storage etc.) both closed `done` with the review evidence in-thread.
- **Severity-proportional follow-up hygiene:** LOW-severity advisories from the GOV-721 review were not
  wedged into the merge gate; they were parked as GOV-772 ("[hardening] SecPriv LOW advisories: A1
  login-timing…", `backlog`, `low`). Blocking-severity vs. parked-advisory separation kept the 5-leg chain
  moving without dropping the findings.

## Replay case (positive)

Given a security review that finds only LOW advisories → expected: gate passes with the advisories recorded
as a single low-priority hardening follow-up issue, not a blocked merge and not silence.
