# Government Watchdog Tooling

Local-first tooling workspace for the Government Watchdog verification layer.

This workspace only models source provenance, local capture verification,
statement verification, correction history, and publication gates. It does not
publish a website, create accounts, contact officials, make legal conclusions,
or produce campaign messaging.

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
