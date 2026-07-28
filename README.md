# Government Watchdog Tooling

Local-first tooling workspace for the Government Watchdog verification layer.

This workspace only models source provenance, local capture verification,
statement verification, correction history, and publication gates. It does not
publish a website, create accounts, contact officials, make legal conclusions,
or produce campaign messaging.

## Repository identity — read before pushing

Three repositories in this account have near-identical names. They are
different things, and one of them differs only by a single letter's case.
**Identify a repo by its remote and root commit, never by its name.**

| Repo | What it is |
| --- | --- |
| `~/GitHub/Government-Watchdog` (this one) | Local-first tooling workspace. Root commit `6bfee26`, 2026-06-05. |
| `github.com/xXKillerNoobYT/Government-watchdog` | The **backend**. Root commit `6d5c341`, 2026-05-03. Unrelated history to this repo. |
| `Government-watchdog-website` | The website. Cloned twice: `~/Code/...` (human) and `~/GitHub/...` (agents). |

This repo and the backend share **no common ancestor** — `git merge-base`
returns nothing and 461 files differ. They are not clones of each other.

### Push rules

`origin` points at the backend repo purely as a backup target. Because the
histories are unrelated:

- **Never push this repo's `main` to `origin/main`.** It will fail, and forcing
  it would destroy 138 commits of real backend work.
- **Never `git pull origin main` here.** Git will refuse unrelated histories;
  overriding that would wreck this working tree.
- Push feature branches under their own names only.

As of 2026-07-25 this repo's branches are backed up upstream as
`GOV-585-handoff-escalation`, `GOV-581-doc-continuity`,
`stage4-automation-ai-boundary`, and `local-orphan-main-20260726`. Those are a
preserved snapshot, not mergeable work — porting their content into the backend
has to be done deliberately.

## Source Registry Slice

The source registry records:

- source metadata: canonical URL, title, jurisdiction, issuing body, dates
- source class: official record, agenda packet, minutes, ordinance, etc.
- TOA local path: `~/Documents/TOA/TownOfAlpine/...`
- capture metadata: capture method, actor, MIME type, size, source path
- content hash: SHA-256 for replacement detection
- lifecycle status: current, replaced, missing after capture, rejected
- replacement tracking: same-URL changed-hash linkage
- audit fields: created/updated timestamps and actors

## Statement Verification Slice

The statement verification contract records:

- statement status: unverified, verified, disputed, false/corrected
- statement kind: fact claim or AI analysis
- source links: source id, quote, page/timestamp/location, source content hash
- deterministic trace hash for each statement-to-source quote/location
- evidence limits explaining what the source does and does not prove
- correction history: prior/new text and status, reason, correcting source,
  actor, timestamp, public note, and internal note
- publication gates for missing sources, missing evidence limits,
  AI-analysis-as-fact, and do-not-publish overrides or sensitive flags

Run focused verification:

```sh
npm test
```
