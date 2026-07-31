# P0 reviewer cookie gate evidence — 2026-07-24

## Scope

This change addresses
[Government-watchdog issue #122](https://github.com/xXKillerNoobYT/Government-watchdog/issues/122)
from backend base `0597802db7df12eec604ec6b4bab42b449398683`.
It changes only the packaged loopback service's authorization path for
`GET /api/reviewer-internal`; it does not activate a feature flag, publish an
artifact, deploy a service, change a reviewer allowlist, or expose a new port.

## Authorization contract

The reviewer endpoint accepts exactly one credential family per request:

- an approved, live account session in a case-sensitive `Bearer` header; or
- one `gw_beta_session` cookie whose session is live, whose normalized email is
  still allowlisted, and whose latest `beta_gate_enabled` owner flag is on.

All other states return the same constant `403 {"error":"access_denied"}` body.
The civic-data file is read only after authorization succeeds.

Requests fail closed when they contain:

- both an Authorization header and a beta cookie, even when either credential
  is malformed;
- more than one Authorization header field;
- repeated `gw_beta_session` values in one Cookie header;
- more than one Cookie header field;
- an unknown, expired, revoked, non-allowlisted, or flag-disabled beta session;
  or
- an unknown, expired, revoked, or non-approved account session.

The packaged service keeps its existing loopback-only bind guard. The handler
does not log request headers, cookies, bearer tokens, principals, or denial
details.

## Packaging boundary

`scripts/web_access/` is an authorization-only package. The artifact exporter
adds it to the service import closure, while the public and reviewer data lanes
remain unchanged. The generated runtime continues to contain the only HTTP
router and forwards Cookie/Authorization metadata to the composed gate.

## Verification

Verification used an isolated Python 3.12 environment installed from
`requirements.txt`.

- Focused security and artifact suite:
  `40 passed in 4.36s`
- Complete backend suite:
  `1779 passed in 48.40s`
- `git diff --check`: passed

The focused suite includes an actual loopback HTTP server and covers valid
bearer access, valid beta-cookie access, same-request mixed credentials,
malformed Authorization values, duplicate Authorization headers, duplicate
cookie names, duplicate Cookie headers, expiry, revocation, allowlist state,
feature-flag state, constant denials, staged artifact packaging, and existing
deployment wiring.

## Release state

This is review evidence for a draft pull request. It is not deployment
approval. Artifact publication remains separately blocked on immutable-release
work tracked by
[Government-watchdog issue #123](https://github.com/xXKillerNoobYT/Government-watchdog/issues/123).
