# Government Watchdog Tooling

Local-first tooling workspace for the Government Watchdog verification layer.

This workspace only models source provenance and local capture verification. It
does not publish a website, create accounts, contact officials, make legal
conclusions, or produce campaign messaging.

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

Run focused verification:

```sh
npm test
```
