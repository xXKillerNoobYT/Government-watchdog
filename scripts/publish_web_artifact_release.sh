#!/usr/bin/env bash
#
# Publish one create-once web artifact, or prove that an existing immutable
# release is the exact same artifact and exit as a non-mutating success.
#
# Callers must already have run the artifact deny-list gate. This script adds
# the release boundary: source/tag/manifest/digest binding, atomic tag creation,
# immutable-release verification, and no overwrite fallback.

set -euo pipefail

artifact_dir="${ARTIFACT_DIR:-dist}"
repository="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
ref_type="${GITHUB_REF_TYPE:?GITHUB_REF_TYPE is required}"
ref_name="${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"
default_branch="${GITHUB_DEFAULT_BRANCH:?GITHUB_DEFAULT_BRANCH is required}"
target_commit="${GITHUB_SHA:?GITHUB_SHA is required}"
python_bin="${PYTHON_BIN:-python3}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
artifact_verifier="${script_dir}/verify_web_artifact.py"

if [[ ! "${target_commit}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "GITHUB_SHA must be a lowercase full 40-character commit SHA" >&2
  exit 1
fi

head_commit="$(git rev-parse HEAD)"
if [[ "${head_commit}" != "${target_commit}" ]]; then
  echo "checked-out HEAD does not equal GITHUB_SHA; refusing publication" >&2
  exit 1
fi
if ! git diff --quiet --ignore-submodules -- \
  || ! git diff --cached --quiet --ignore-submodules --; then
  echo "tracked checkout content differs from GITHUB_SHA; refusing publication" >&2
  exit 1
fi
untracked_sources="$(git ls-files --others --exclude-standard -- scripts)"
if [[ -n "${untracked_sources}" ]]; then
  echo "untracked source exists under scripts/; refusing publication" >&2
  echo "${untracked_sources}" >&2
  exit 1
fi

short_sha="${target_commit:0:12}"
tag="web-artifact-${short_sha}"
expected_asset_name="gw-web-artifact-${short_sha}.tar.gz"

if [[ "${ref_type}" != "branch" || "${ref_name}" != "${default_branch}" ]]; then
  echo "publication requires a manual run from default branch ${default_branch}" >&2
  exit 1
fi

shopt -s nullglob
artifact_files=("${artifact_dir}"/gw-web-artifact-*.tar.gz)
shopt -u nullglob
if [[ "${#artifact_files[@]}" -ne 1 ]]; then
  echo "expected exactly one web artifact in ${artifact_dir}; found ${#artifact_files[@]}" >&2
  exit 1
fi
artifact_path="${artifact_files[0]}"
asset_name="$(basename "${artifact_path}")"
if [[ "${asset_name}" != "${expected_asset_name}" ]]; then
  echo "artifact name must be ${expected_asset_name}; got ${asset_name}" >&2
  exit 1
fi

# Freeze the candidate in a private, read-only path before any network probe.
# The isolated publisher never executes artifact content.
candidate_dir="$(
  mktemp -d "${TMPDIR:-/tmp}/gw-web-artifact-candidate.XXXXXX"
)"
trap 'rm -rf -- "${candidate_dir}"' EXIT
candidate_path="${candidate_dir}/${asset_name}"
cp "${artifact_path}" "${candidate_path}"
chmod 0444 "${candidate_path}"
artifact_path="${candidate_path}"

manifest_metadata="$(
  "${python_bin}" "${artifact_verifier}" \
    --expected-commit "${target_commit}" \
    "${artifact_path}"
)"
IFS=$'\t' read -r manifest_commit manifest_content_sha schema_version \
  <<<"${manifest_metadata}"

tarball_sha="$(shasum -a 256 "${artifact_path}" | awk '{print $1}')"
if [[ ! "${tarball_sha}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "could not compute the uploaded tarball SHA-256" >&2
  exit 1
fi
expected_asset_digest="sha256:${tarball_sha}"

make_release_notes() {
  local source_commit="$1"
  local release_tag="$2"
  local release_asset="$3"
  local release_tarball_sha="$4"
  local release_content_sha="$5"
  local release_schema_version="$6"
  printf '%s\n\n%s\n%s\n%s\n%s\n%s\n%s\n' \
    "Pinned backend web artifact (GOV-1523). Deny-list gated." \
    "Backend source commit: ${source_commit}" \
    "Canonical tag: ${release_tag}" \
    "Artifact asset: ${release_asset}" \
    "Tarball SHA-256: ${release_tarball_sha}" \
    "Manifest content SHA-256: ${release_content_sha}" \
    "Manifest schema version: ${release_schema_version}"
}

release_notes="$(
  make_release_notes \
    "${target_commit}" \
    "${tag}" \
    "${asset_name}" \
    "${tarball_sha}" \
    "${manifest_content_sha}" \
    "${schema_version}"
)"

remote_tag_state="absent"
remote_tag_commit=""
tag_ref_endpoint="repos/${repository}/git/ref/tags/${tag}"

probe_remote_tag() {
  local probe status object_type object_sha
  set +e
  probe="$(
    gh api "${tag_ref_endpoint}" \
      --jq '[.object.type, .object.sha] | @tsv' 2>&1
  )"
  status=$?
  set -e
  if [[ "${status}" -eq 0 ]]; then
    IFS=$'\t' read -r object_type object_sha <<<"${probe}"
    if [[ "${object_type}" != "commit" ]]; then
      echo "canonical tag ${tag} must be a lightweight commit ref" >&2
      exit 1
    fi
    if [[ ! "${object_sha}" =~ ^[0-9a-f]{40}$ ]]; then
      echo "remote tag ${tag} did not resolve to a full commit SHA" >&2
      exit 1
    fi
    remote_tag_state="present"
    remote_tag_commit="${object_sha}"
    return
  fi
  if grep -Eq 'HTTP/(1[.]1|2([.]0)?) 404|HTTP 404' <<<"${probe}"; then
    remote_tag_state="absent"
    remote_tag_commit=""
    return
  fi
  echo "could not determine remote tag state for ${tag}" >&2
  echo "${probe}" >&2
  exit 1
}

published_release_endpoint="repos/${repository}/releases/tags/${tag}"
release_state="absent"

probe_release() {
  local probe status
  set +e
  probe="$(gh api --include "${published_release_endpoint}" 2>&1)"
  status=$?
  set -e
  if [[ "${status}" -eq 0 ]]; then
    release_state="present"
    return
  fi
  if grep -Eq 'HTTP/(1[.]1|2([.]0)?) 404|HTTP 404' <<<"${probe}"; then
    release_state="absent"
    return
  fi
  echo "could not determine release state for ${tag}" >&2
  echo "${probe}" >&2
  exit 1
}

probe_matching_drafts() {
  local count status
  set +e
  count="$(
    gh api --paginate --slurp \
      "repos/${repository}/releases?per_page=100" \
      --jq "[.[][] | select(.draft == true and .tag_name == \"${tag}\")] | length" \
      2>&1
  )"
  status=$?
  set -e
  if [[ "${status}" -ne 0 || ! "${count}" =~ ^[0-9]+$ ]]; then
    echo "could not determine whether a matching draft release exists" >&2
    return 1
  fi
  if [[ "${count}" != "0" ]]; then
    echo "matching draft release exists for ${tag}; owner review/cleanup is required" >&2
    return 1
  fi
}

verify_live_controls() {
  local settings_token immutability_enabled immutability_status
  local tag_ruleset_id tag_ruleset_updated_at
  local tag_ruleset_json tag_ruleset_status

  if [[ "${GW_RELEASE_IMMUTABILITY_CONFIRMED:-}" != "true" ]]; then
    echo "repository release immutability is not owner-confirmed; refusing publication" >&2
    return 1
  fi

  settings_token="${GW_RELEASE_SETTINGS_TOKEN:-}"
  if [[ -z "${settings_token}" ]]; then
    echo "GW_RELEASE_SETTINGS_TOKEN is required for fail-closed settings checks" >&2
    return 1
  fi

  set +e
  immutability_enabled="$(
    GH_TOKEN="${settings_token}" gh api \
      --header "Accept: application/vnd.github+json" \
      --header "X-GitHub-Api-Version: 2026-03-10" \
      "repos/${repository}/immutable-releases" \
      --jq '.enabled' 2>&1
  )"
  immutability_status=$?
  set -e
  if [[
    "${immutability_status}" -ne 0
    || "${immutability_enabled}" != "true"
  ]]; then
    echo "GitHub does not authoritatively report release immutability enabled" >&2
    return 1
  fi

  tag_ruleset_id="${GW_RELEASE_TAG_RULESET_ID:-}"
  if [[ ! "${tag_ruleset_id}" =~ ^[0-9]+$ ]]; then
    echo "GW_RELEASE_TAG_RULESET_ID must identify the approved tag ruleset" >&2
    return 1
  fi
  if [[ "${GW_RELEASE_TAG_RULESET_NO_BYPASS_CONFIRMED:-}" != "true" ]]; then
    echo "tag ruleset no-bypass policy is not owner-confirmed" >&2
    return 1
  fi
  tag_ruleset_updated_at="${GW_RELEASE_TAG_RULESET_UPDATED_AT:-}"
  if [[ -z "${tag_ruleset_updated_at}" ]]; then
    echo "GW_RELEASE_TAG_RULESET_UPDATED_AT must pin the owner-inspected ruleset" >&2
    return 1
  fi
  set +e
  tag_ruleset_json="$(
    GH_TOKEN="${settings_token}" gh api \
      --header "Accept: application/vnd.github+json" \
      --header "X-GitHub-Api-Version: 2026-03-10" \
      "repos/${repository}/rulesets/${tag_ruleset_id}?includes_parents=true" 2>&1
  )"
  tag_ruleset_status=$?
  set -e
  if [[ "${tag_ruleset_status}" -ne 0 ]]; then
    echo "could not verify the approved web-artifact tag ruleset" >&2
    return 1
  fi
  if ! "${python_bin}" -c '
import json
import sys

ruleset = json.load(sys.stdin)
expected_updated_at = sys.argv[1]
expected_pattern = "refs/tags/web-artifact-*"
conditions = ruleset.get("conditions", {}).get("ref_name", {})
rule_types = {
    rule.get("type")
    for rule in ruleset.get("rules", [])
    if isinstance(rule, dict)
}
valid = (
    ruleset.get("target") == "tag"
    and ruleset.get("enforcement") == "active"
    and ruleset.get("updated_at") == expected_updated_at
    and conditions.get("include") == [expected_pattern]
    and conditions.get("exclude") == []
    and (
        "bypass_actors" not in ruleset
        or ruleset.get("bypass_actors") == []
    )
    and {"update", "deletion"}.issubset(rule_types)
)
if not valid:
    raise SystemExit(1)
' "${tag_ruleset_updated_at}" <<<"${tag_ruleset_json}"; then
    echo "web-artifact tag ruleset is not active, exact, current, and non-bypassable" >&2
    return 1
  fi
}

verify_release() (
  local endpoint="$1"
  local expected_immutable="$2"
  local expected_draft="$3"
  local immutable draft release_tag asset_total matching_count
  local asset_id remote_digest body
  local download_dir remote_artifact remote_metadata
  local remote_manifest_commit remote_manifest_content_sha remote_schema_version
  local remote_tarball_sha expected_remote_body

  if [[ "${remote_tag_state}" != "present" ]]; then
    echo "release ${tag} exists without a resolvable remote tag" >&2
    return 1
  fi
  if [[ "${remote_tag_commit}" != "${target_commit}" ]]; then
    echo "remote tag ${tag} points to a different commit" >&2
    return 1
  fi

  release_tag="$(gh api "${endpoint}" --jq '.tag_name')"
  immutable="$(gh api "${endpoint}" --jq '.immutable')"
  draft="$(gh api "${endpoint}" --jq '.draft')"
  if [[ "${release_tag}" != "${tag}" ]]; then
    echo "release endpoint does not bind canonical tag ${tag}" >&2
    return 1
  fi
  if [[ "${immutable}" != "${expected_immutable}" ]]; then
    echo "release ${tag} has unexpected immutable state ${immutable}" >&2
    return 1
  fi
  if [[ "${draft}" != "${expected_draft}" ]]; then
    echo "release ${tag} has unexpected draft state ${draft}" >&2
    return 1
  fi

  asset_total="$(gh api "${endpoint}" --jq '.assets | length')"
  matching_count="$(
    gh api "${endpoint}" \
      --jq "[.assets[] | select(.name == \"${asset_name}\")] | length"
  )"
  if [[ "${asset_total}" != "1" || "${matching_count}" != "1" ]]; then
    echo "release ${tag} must contain exactly the one expected asset" >&2
    return 1
  fi

  asset_id="$(
    gh api "${endpoint}" \
      --jq ".assets[] | select(.name == \"${asset_name}\") | .id"
  )"
  if [[ ! "${asset_id}" =~ ^[0-9]+$ ]]; then
    echo "release asset ${asset_name} has no valid API id" >&2
    return 1
  fi
  remote_digest="$(
    gh api "${endpoint}" \
      --jq ".assets[] | select(.name == \"${asset_name}\") | .digest"
  )"
  if [[ "${remote_digest}" != "${expected_asset_digest}" ]]; then
    echo "release asset ${asset_name} differs from the local candidate" >&2
    return 1
  fi

  download_dir="$(
    mktemp -d "${TMPDIR:-/tmp}/gw-web-artifact-release-verify.XXXXXX"
  )"
  trap 'rm -rf -- "${download_dir}"' EXIT
  remote_artifact="${download_dir}/${asset_name}"
  if ! gh api \
    --header "Accept: application/octet-stream" \
    "repos/${repository}/releases/assets/${asset_id}" \
    >"${remote_artifact}"; then
    echo "could not download release asset ${asset_name} for verification" >&2
    return 1
  fi
  if [[ ! -f "${remote_artifact}" ]]; then
    echo "downloaded release asset ${asset_name} is missing" >&2
    return 1
  fi
  remote_tarball_sha="$(shasum -a 256 "${remote_artifact}" | awk '{print $1}')"
  if [[ "${remote_digest}" != "sha256:${remote_tarball_sha}" ]]; then
    echo "release asset ${asset_name} bytes do not match GitHub's digest" >&2
    return 1
  fi
  remote_metadata="$(
    "${python_bin}" "${artifact_verifier}" \
      --expected-commit "${target_commit}" \
      "${remote_artifact}"
  )"
  IFS=$'\t' read -r \
    remote_manifest_commit \
    remote_manifest_content_sha \
    remote_schema_version \
    <<<"${remote_metadata}"

  if [[
    "${remote_tarball_sha}" != "${tarball_sha}"
    || "${remote_manifest_content_sha}" != "${manifest_content_sha}"
    || "${remote_schema_version}" != "${schema_version}"
  ]]; then
    echo "release content differs from the local candidate" >&2
    return 1
  fi

  body="$(gh api "${endpoint}" --jq '.body // ""')"
  expected_remote_body="$(
    make_release_notes \
      "${remote_manifest_commit}" \
      "${tag}" \
      "${asset_name}" \
      "${remote_tarball_sha}" \
      "${remote_manifest_content_sha}" \
      "${remote_schema_version}"
  )"
  if [[ "${body}" != "${expected_remote_body}" ]]; then
    echo "release ${tag} notes do not exactly match the verified asset" >&2
    return 1
  fi
)

verify_release_attestation() {
  local attempt
  for attempt in 1 2 3 4 5; do
    if gh release verify "${tag}" --repo "${repository}" >/dev/null 2>&1 \
      && gh release verify-asset \
        "${tag}" "${artifact_path}" --repo "${repository}" >/dev/null 2>&1; then
      return 0
    fi
    if [[ "${attempt}" != "5" ]]; then
      sleep 5
    fi
  done
  echo "GitHub release attestation did not verify after bounded retries" >&2
  return 1
}

probe_remote_tag
probe_release

if [[ "${release_state}" == "present" ]]; then
  if verify_release "${published_release_endpoint}" true false; then
    verify_release_attestation
    echo "verified identical immutable release ${tag}; retry is a no-op"
    exit 0
  fi
  echo "existing release ${tag} does not match; refusing mutation" >&2
  exit 1
fi

probe_matching_drafts

if [[ "${remote_tag_state}" == "present" ]]; then
  if [[ "${remote_tag_commit}" != "${target_commit}" ]]; then
    echo "remote tag ${tag} already points to a different commit" >&2
  else
    echo "orphan tag ${tag} has no release-bound candidate digest; roll forward with a new commit" >&2
  fi
  exit 1
fi

verify_live_controls

# Create-ref is atomic: GitHub returns an error instead of moving an existing
# tag if another actor wins the race after the absence probe.
gh api --method POST "repos/${repository}/git/refs" \
  -f "ref=refs/tags/${tag}" \
  -f "sha=${target_commit}" >/dev/null
probe_remote_tag
if [[
  "${remote_tag_state}" != "present"
  || "${remote_tag_commit}" != "${target_commit}"
]]; then
  echo "new remote tag ${tag} could not be verified" >&2
  exit 1
fi

# Create a draft, upload exactly once, verify the downloaded draft bytes and
# contract, re-check the protected tag, then publish that exact release ID.
# A partial failure leaves a visible draft that the next run refuses to mutate.
draft_response="$(
  gh api --method POST "repos/${repository}/releases" \
    -f "tag_name=${tag}" \
    -f "target_commitish=${target_commit}" \
    -f "name=Web artifact ${tag}" \
    -f "body=${release_notes}" \
    -F "draft=true" \
    -F "prerelease=false" \
    -f "make_latest=false"
)"
draft_release_id="$(
  "${python_bin}" -c \
    'import json, sys; print(json.load(sys.stdin).get("id", ""))' \
    <<<"${draft_response}"
)"
if [[ ! "${draft_release_id}" =~ ^[0-9]+$ ]]; then
  echo "GitHub did not return a valid draft release id" >&2
  exit 1
fi
draft_release_endpoint="repos/${repository}/releases/${draft_release_id}"

gh api --method POST \
  --header "Content-Type: application/octet-stream" \
  "https://uploads.github.com/repos/${repository}/releases/${draft_release_id}/assets?name=${asset_name}" \
  --input "${artifact_path}" >/dev/null

if ! verify_release "${draft_release_endpoint}" false true; then
  echo "draft release ${draft_release_id} failed exact artifact verification" >&2
  exit 1
fi

probe_remote_tag
if [[
  "${remote_tag_state}" != "present"
  || "${remote_tag_commit}" != "${target_commit}"
]]; then
  echo "protected tag ${tag} changed before draft publication" >&2
  exit 1
fi

# Close the long draft/upload window against owner-setting or ruleset drift.
verify_live_controls

gh api --method PATCH "${draft_release_endpoint}" -F "draft=false" >/dev/null

probe_remote_tag
probe_release
if [[ "${release_state}" != "present" ]] \
  || ! verify_release "${published_release_endpoint}" true false; then
  echo "new release ${tag} failed immutable post-publication verification" >&2
  exit 1
fi
verify_release_attestation

echo "published and verified immutable release ${tag}"
