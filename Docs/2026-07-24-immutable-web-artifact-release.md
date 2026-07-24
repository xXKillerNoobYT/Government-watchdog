# Immutable web-artifact publication — 2026-07-24

## Purpose

This release boundary addresses
[Government-watchdog issue #123](https://github.com/xXKillerNoobYT/Government-watchdog/issues/123).
It prevents the backend artifact workflow from force-moving a tag, suppressing
a failed tag operation, or overwriting an existing release asset.

This change does not publish an artifact. It prepares the reviewed workflow and
records the separate owner setting required before the next publication.

## Current observation

As checked through the GitHub API on 2026-07-24:

- repository release immutability is disabled;
- the existing `web-artifact-0597802db7df` release reports
  `immutable: false`; and
- its asset therefore remains historical mutable evidence, not an approved
  expansion/deployment input.

GitHub release immutability applies only to releases published after the
setting is enabled. GitHub documents that publishing an immutable release locks
its associated tag and assets and creates release attestation evidence:
<https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases>.

## Owner activation gate

The workflow is manual-only. A tag push cannot start it, and a default run
builds and verifies without publishing. Before an owner runs it from the
protected default branch with `attach_release: true`:

1. A repository administrator enables **Settings → Releases → Enable release
   immutability**.
2. Create an active **tag ruleset** whose only include pattern is
   `refs/tags/web-artifact-*`, whose exclude list and bypass-actor list are
   empty, and which contains both **Restrict updates** and **Restrict
   deletions**. Record its numeric ID for the protected environment.
3. Create the Actions environment **`web-artifact-release`**:
   - require one or more owner-selected reviewers;
   - prevent self-review and administrator bypass; and
   - restrict deployments to the protected default branch only.
   Required-reviewer protection must be proven available for this private
   personal-account repository before activation. If the repository's plan does
   not expose that protection, do not activate publication: move the release
   boundary to an eligible organization/plan or add a separately reviewed,
   machine-verifiable owner gate. Manual dispatch alone is not equivalent to an
   independent approval.
4. Add the environment secret `GW_RELEASE_SETTINGS_TOKEN`, using a fine-grained
   GitHub App installation/user token or PAT limited to this repository and
   **Administration: read**. It performs fail-closed settings/ruleset reads
   only. Do not grant Administration: write.
5. Only after the environment and repository controls are active, add these
   environment variables:
   - `GW_RELEASE_IMMUTABILITY_CONFIRMED=true`;
   - `GW_RELEASE_TAG_RULESET_ID=<numeric ruleset id>`; and
   - `GW_RELEASE_TAG_RULESET_NO_BYPASS_CONFIRMED=true`; and
   - `GW_RELEASE_TAG_RULESET_UPDATED_AT=<server updated_at value observed
     during the no-bypass inspection>`.

Immediately before any new tag or release mutation, the publication script:

- requires the acknowledgement variable to be exactly `true`;
- calls
  `GET /repos/xXKillerNoobYT/Government-watchdog/immutable-releases` with API
  version `2026-03-10` and requires `enabled: true`; and
- reads the configured ruleset ID and requires the exact active
  update/deletion contract above; and
- rejects any bypass actor returned by the API. Because an Administration-read
  token may receive a redacted ruleset without `bypass_actors`, the separate
  no-bypass environment variable records the owner's inspection and the pinned
  server `updated_at` value makes every later ruleset edit fail closed.

A missing secret, denied API call, 404, disabled setting, stale ruleset ID,
changed `updated_at`, or ruleset drift all fail without creating a tag or
release. The same live checks run again immediately before the draft is
published. After publication, the job separately requires the release API's
`immutable` field to be `true`.

The variables are owner acknowledgements, not substitutes for the protected
environment, GitHub settings, or authoritative API preflight. Do not set them
in advance.

## Runner and credential boundary

The build and publish jobs intentionally run in different trust zones:

- the persistent self-hosted Mac checks out with `contents: read`, does not
  retain checkout credentials, proves the checkout is clean, runs the artifact
  and release-policy tests, builds from the local registry, and receives no
  settings or publication secret;
- that runner installs only the reviewed CPython 3.12/macOS ARM64 release lock
  with exact wheel hashes—no mutable `>=` resolution, source distributions, or
  runtime `pip` upgrade—and re-proves the source tree is clean after setup and
  tests but before opening the registry;
- only when `attach_release: true`, the build uploads the already-gated
  tarball as a private Actions artifact with one-day retention and no additional
  compression; and
- an isolated GitHub-hosted publisher downloads that candidate, checks out the
  exact workflow SHA, and receives `contents: write` plus the protected
  environment's read-only settings token only after environment approval.

The publisher freezes the candidate in a private read-only temporary path. It
never imports or executes code from the tarball; its verifier treats every
archive byte as untrusted input. The verifier independently regenerates the
expected service entrypoint, import closure, and seedless schema from the exact
checked-out backend source, then requires every packaged executable byte to
match. The local registry projection remains data, not executable authority.

## Create-once contract

The workflow serializes all publication runs under one concurrency group. The
publication script then proves:

- `HEAD` equals the full 40-character `GITHUB_SHA`;
- the only accepted tag is `web-artifact-<first-12-of-full-sha>`;
- the only accepted asset is
  `gw-web-artifact-<first-12-of-full-sha>.tar.gz`;
- the checked-out source tree has no tracked changes or untracked source under
  `scripts/`;
- the tarball contains exactly one `manifest.json`, only canonical unique
  regular-file members, and at least one content file;
- JSON is strict browser-compatible JSON: escaped strings are scanned after
  decoding and non-finite `NaN`/infinity constants are rejected;
- `manifest.backend_commit` equals the full source SHA;
- `manifest.artifact_sha256` equals a fresh recomputation over the archived
  deterministic `(path, bytes)` content tree (excluding `manifest.json`);
- every packaged service-code and generated-schema byte equals the exact
  checked-out source;
- the manifest content digest and uploaded tarball-byte SHA-256 are both valid
  and recorded separately; and
- an existing or newly-created remote tag resolves to that same source SHA.

For the manual default-branch dispatch, the private repository's tag state is
read through the authenticated Git refs API. The canonical tag must be a
lightweight commit ref. A missing tag is created with GitHub's create-ref API.
That API fails rather than moving an existing ref. The release is created once
through an explicit draft lifecycle:

1. create the draft release through the API;
2. upload the canonical asset exactly once, with no clobber path;
3. download the draft asset again and verify its exact bytes, manifest, schema,
   data-lane counts, safety contract, and evidence body;
4. re-probe the protected tag;
5. publish that exact release ID;
6. re-download and verify the now-immutable release; and
7. require both GitHub release and asset attestation verification, with bounded
   retries.

The non-bypassable tag ruleset prevents the tag from moving during the
draft/upload/verify/publish interval.

The build pins `generated_at_utc` to the source commit's committed timestamp
instead of the wall clock. This makes equal source and registry inputs
byte-reproducible. The registry is still a deliberately mutable external input.
Because the current canonical tag binds only the backend source commit, a
changed registry snapshot at the same commit is not a valid retry: publication
fails without mutation and requires a new backend commit/tag identity.

Release notes mirror:

- full backend source commit;
- canonical tag;
- exact asset name;
- uploaded tarball SHA-256;
- manifest deterministic-content SHA-256; and
- manifest schema version.

Release-note text is mutable GitHub metadata. Exact comparison makes an
unexpected edit a fail-closed drift alarm, but the immutable asset digest and
GitHub release attestation are the provenance authority.

## Retry and mismatch behavior

An existing release is a successful no-op only when both the local candidate
and the remote release are verified as the exact same artifact:

- GitHub reports the release as immutable;
- the remote tag resolves to the expected full commit;
- the release contains exactly one asset with the expected name;
- a fresh download's bytes equal GitHub's asset digest;
- the downloaded manifest source commit equals the expected full commit;
- the downloaded content-tree digest recomputes exactly; and
- the local and downloaded tarball-byte digests, content-tree digests, and
  schema versions match exactly;
- the complete normalized release body exactly equals the evidence derived
  from those downloaded bytes.

Any mismatch, mutable release, extra/missing asset, API/network ambiguity, tag
drift, or publication error fails the workflow without an overwrite attempt.
A partial run that created the exact tag but no release has no durable
candidate digest, so it cannot resume: the next run fails and requires a new
backend commit/tag. A partial run that created a draft is never auto-edited or
auto-deleted: the next run fails and requires an owner to inspect and explicitly
clean up that draft, then roll forward with a new backend commit/tag.

## Verification

`tests/test_web_artifact_release_workflow.py` covers:

- forbidden force/clobber/failure-suppression paths;
- global publication serialization;
- canonical source/tag/asset binding;
- embedded manifest recomputation plus duplicate, unsafe-path, and non-regular
  member rejection;
- decoded escape-sequence leak detection and strict non-finite JSON rejection;
- exact executable-service and generated-schema binding to the checked-out
  source;
- clean tracked source and no untracked packaged source;
- create-ref before release creation;
- exact immutable retry as a non-mutating success;
- changed-registry retry as a non-mutating failure;
- tag, digest, mutable-release, filename, and manifest mismatches;
- owner-confirmation, live-setting, settings-token, and tag-ruleset failures;
- ruleset-version drift and the second immediate pre-publication control check;
- exact release-note comparison, including conflicting duplicate evidence;
- API failure;
- release-create, upload, draft-publication, and post-publication failure
  propagation; and
- post-publication immutability and release-attestation failure propagation.

## Roll forward

Published artifact releases are never edited in place. A new approved backend
commit produces a new canonical tag and asset; the website adopts it through a
separate reviewed full-SHA `BACKEND_REF` change. The old release remains
auditable.

GitHub treats the tag name of a deleted immutable release as tombstoned and
non-reusable. Recovery therefore rolls forward with a new approved backend
commit and canonical tag; it never attempts to recreate, force-move, or reuse
the deleted tag name.
