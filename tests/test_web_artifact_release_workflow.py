"""Regression tests for create-once, immutable web-artifact publication."""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_web_artifact as artifact_contract  # noqa: E402

PUBLISH_SCRIPT = ROOT / "scripts" / "publish_web_artifact_release.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "web-artifact-release.yml"
RELEASE_LOCK = ROOT / "requirements-release-macos-arm64-py312.lock"
SHA = "a" * 40
OTHER_SHA = "b" * 40
SHORT_SHA = SHA[:12]
SCHEMA_VERSION = 1
DEFAULT_CONTENT = b"[]\n"


def _trusted_service_files() -> dict[str, bytes]:
    files = {
        "service/run.py": artifact_contract.RUN_PY.encode(),
        "service/schema.sql": artifact_contract.schema_sql_bytes(),
    }
    for relative_path in artifact_contract.compute_service_closure(
        ROOT / "scripts"
    ):
        files[f"service/{relative_path.as_posix()}"] = (
            ROOT / "scripts" / relative_path
        ).read_bytes()
    return files


TRUSTED_SERVICE_FILES = _trusted_service_files()


def _content_digest(files: dict[str, bytes]) -> str:
    hasher = hashlib.sha256()
    for relative_path in sorted(files):
        hasher.update(relative_path.encode())
        hasher.update(b"\0")
        hasher.update(files[relative_path])
        hasher.update(b"\0")
    return hasher.hexdigest()


class ImmutableReleaseWorkflowTests(unittest.TestCase):
    @staticmethod
    def _write_artifact(
        path: Path,
        *,
        manifest_commit: str = SHA,
        manifest_content_sha: str | None = None,
        content: bytes = DEFAULT_CONTENT,
        duplicate_member: bool = False,
        unsafe_member: bool = False,
        symbolic_link: bool = False,
        trailing_bytes: bytes = b"",
        service_override: tuple[str, bytes] | None = None,
        manifest_extra: dict | None = None,
    ) -> tuple[str, str]:
        published = json.loads(content)
        if not isinstance(published, list):
            raise ValueError("test published content must be a JSON array")
        content_files = {
            artifact_contract.PUBLISHED_NAME: content,
            artifact_contract.REVIEWER_INTERNAL_NAME: b"[]\n",
            **TRUSTED_SERVICE_FILES,
        }
        if service_override is not None:
            relative_path, payload = service_override
            content_files[relative_path] = payload
        effective_content_sha = (
            manifest_content_sha
            if manifest_content_sha is not None
            else _content_digest(content_files)
        )
        manifest_data = {
            "backend_commit": manifest_commit,
            "artifact_sha256": effective_content_sha,
            "generated_at_utc": "2026-07-21T20:37:52-06:00",
            "schema_version": artifact_contract.SCHEMA_VERSION,
            "gate_functions": artifact_contract.GATE_FUNCTIONS,
            "row_counts": {
                "published": len(published),
                "reviewer_internal": 0,
            },
        }
        if manifest_extra is not None:
            manifest_data.update(manifest_extra)
        manifest = json.dumps(
            manifest_data,
            indent=2,
            sort_keys=True,
        ).encode() + b"\n"
        files = {**content_files, "manifest.json": manifest}
        with path.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                fileobj=raw,
                mode="wb",
                mtime=0,
            ) as compressed:
                archive = tarfile.open(fileobj=compressed, mode="w")
                for name in sorted(files):
                    payload = files[name]
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    archive.addfile(info, io.BytesIO(payload))
                if duplicate_member:
                    name = artifact_contract.PUBLISHED_NAME
                    payload = files[name]
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                if unsafe_member:
                    payload = b"outside\n"
                    info = tarfile.TarInfo("../outside.txt")
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                if symbolic_link:
                    info = tarfile.TarInfo("data/link")
                    info.type = tarfile.SYMTYPE
                    info.linkname = "published.json"
                    archive.addfile(info)
                archive.close()
        if trailing_bytes:
            with path.open("ab") as stream:
                stream.write(trailing_bytes)
        return (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            effective_content_sha,
        )

    def _run(
        self,
        *,
        ref_type: str = "branch",
        remote_tag: str = "absent",
        release: str = "absent",
        manifest_commit: str = SHA,
        manifest_content_sha: str | None = None,
        content: bytes = DEFAULT_CONTENT,
        duplicate_member: bool = False,
        unsafe_member: bool = False,
        symbolic_link: bool = False,
        trailing_bytes: bytes = b"",
        service_override: tuple[str, bytes] | None = None,
        manifest_extra: dict | None = None,
        remote_manifest_content_sha: str | None = None,
        remote_content: bytes | None = None,
        artifact_name: str | None = None,
        event_tag: str | None = None,
        head_sha: str = SHA,
        checkout_state: str = "clean",
        immutability_confirmed: str = "true",
        immutability_setting: str = "true",
        settings_token: str = "settings-token",
        tag_ruleset_id: str = "42",
        tag_ruleset_state: str = "valid",
        tag_ruleset_no_bypass_confirmed: str = "true",
        tag_ruleset_updated_at: str = "2026-07-24T08:00:00Z",
        release_create: str = "success",
        release_upload: str = "success",
        release_publish: str = "success",
        post_release_state: str = "exact",
        attestation_state: str = "success",
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            artifact_dir = tmp / "dist"
            artifact_dir.mkdir()
            name = artifact_name or f"gw-web-artifact-{SHORT_SHA}.tar.gz"
            artifact = artifact_dir / name
            tarball_sha, local_content_sha = self._write_artifact(
                artifact,
                manifest_commit=manifest_commit,
                manifest_content_sha=manifest_content_sha,
                content=content,
                duplicate_member=duplicate_member,
                unsafe_member=unsafe_member,
                symbolic_link=symbolic_link,
                trailing_bytes=trailing_bytes,
                service_override=service_override,
                manifest_extra=manifest_extra,
            )

            remote_artifact = tmp / "remote-artifact.tar.gz"
            if (
                remote_manifest_content_sha is None
                and remote_content is None
                and manifest_commit == SHA
            ):
                shutil.copyfile(artifact, remote_artifact)
                remote_tarball_sha = tarball_sha
                remote_content_sha = local_content_sha
            else:
                remote_tarball_sha, remote_content_sha = self._write_artifact(
                    remote_artifact,
                    manifest_commit=SHA,
                    manifest_content_sha=remote_manifest_content_sha,
                    content=remote_content if remote_content is not None else content,
                )

            def release_body(content_sha: str, asset_sha: str) -> str:
                return "\n".join(
                    [
                        "Pinned backend web artifact (GOV-1523). Deny-list gated.",
                        "",
                        f"Backend source commit: {SHA}",
                        f"Canonical tag: web-artifact-{SHORT_SHA}",
                        f"Artifact asset: {name}",
                        f"Tarball SHA-256: {asset_sha}",
                        f"Manifest content SHA-256: {content_sha}",
                        f"Manifest schema version: {SCHEMA_VERSION}",
                    ]
                )

            local_expected_body = tmp / "local-expected-body.txt"
            local_expected_body.write_text(
                release_body(local_content_sha, tarball_sha),
                encoding="utf-8",
            )
            expected_body = tmp / "expected-body.txt"
            expected_body.write_text(
                release_body(remote_content_sha, remote_tarball_sha),
                encoding="utf-8",
            )

            remote_artifact_pointer = tmp / "remote-artifact-pointer"
            remote_artifact_pointer.write_text(
                str(remote_artifact),
                encoding="utf-8",
            )
            asset_digest = tmp / "asset-digest"
            asset_digest.write_text(
                f"sha256:{remote_tarball_sha}",
                encoding="utf-8",
            )

            call_log = tmp / "calls.log"
            remote_state = tmp / "remote-tag-state"
            remote_state.write_text(remote_tag, encoding="utf-8")
            release_state = tmp / "release-state"
            release_state.write_text(release, encoding="utf-8")
            immutability_checks = tmp / "immutability-checks"
            immutability_checks.write_text("0", encoding="utf-8")

            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            git = fake_bin / "git"
            git.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    {
                      printf 'git'
                      printf ' %q' "$@"
                      printf '\\n'
                    } >> "$CALL_LOG"
                    case "$1" in
                      diff)
                        if [ "$FAKE_CHECKOUT_STATE" = "dirty" ]; then exit 1; fi
                        exit 0
                        ;;
                      ls-files)
                        if [ "$FAKE_CHECKOUT_STATE" = "untracked" ]; then
                          echo "scripts/untracked.py"
                        fi
                        exit 0
                        ;;
                      rev-parse)
                        echo "$FAKE_HEAD_SHA"
                        exit 0
                        ;;
                    esac
                    exit 1
                    """
                ),
                encoding="utf-8",
            )
            git.chmod(0o755)

            gh = fake_bin / "gh"
            gh.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    {
                      printf 'gh'
                      printf ' %q' "$@"
                      printf '\\n'
                    } >> "$CALL_LOG"

                    args="$*"
                    if [ "$1" = "api" ] && [[ "$args" == *"/git/ref/tags/"* ]]; then
                      state="$(cat "$REMOTE_STATE_FILE")"
                      case "$state" in
                        exact)
                          printf 'commit\\t%s\\n' "$FAKE_SHA"
                          exit 0
                          ;;
                        mismatch)
                          printf 'commit\\t%s\\n' "$FAKE_OTHER_SHA"
                          exit 0
                          ;;
                        annotated)
                          printf 'tag\\t%s\\n' "$FAKE_OTHER_SHA"
                          exit 0
                          ;;
                        absent)
                          echo "gh: Not Found (HTTP 404)" >&2
                          exit 1
                          ;;
                        error)
                          echo "remote lookup failed" >&2
                          exit 1
                          ;;
                      esac
                    fi

                    if [ "$1" = "api" ] && [[ "$args" == *"immutable-releases"* ]]; then
                      check_count="$(cat "$IMMUTABILITY_CHECKS_FILE")"
                      echo "$((check_count + 1))" > "$IMMUTABILITY_CHECKS_FILE"
                      case "$FAKE_IMMUTABILITY_SETTING" in
                        true) echo true; exit 0 ;;
                        false) echo false; exit 0 ;;
                        true_then_false)
                          if [ "$check_count" = "0" ]; then echo true; else echo false; fi
                          exit 0
                          ;;
                        error) echo "settings lookup failed" >&2; exit 1 ;;
                      esac
                    fi

                    if [ "$1" = "api" ] && [[ "$args" == *"/rulesets/"* ]]; then
                      case "$FAKE_TAG_RULESET_STATE" in
                        valid)
                          printf '{"target":"tag","enforcement":"active","updated_at":"%s","bypass_actors":[],"conditions":{"ref_name":{"include":["refs/tags/web-artifact-*"],"exclude":[]}},"rules":[{"type":"update"},{"type":"deletion"}]}\\n' "$FAKE_RULESET_UPDATED_AT"
                          exit 0
                          ;;
                        redacted)
                          printf '{"target":"tag","enforcement":"active","updated_at":"%s","conditions":{"ref_name":{"include":["refs/tags/web-artifact-*"],"exclude":[]}},"rules":[{"type":"update"},{"type":"deletion"}]}\\n' "$FAKE_RULESET_UPDATED_AT"
                          exit 0
                          ;;
                        bypass)
                          printf '{"target":"tag","enforcement":"active","updated_at":"%s","bypass_actors":[{"actor_type":"RepositoryRole","actor_id":5}],"conditions":{"ref_name":{"include":["refs/tags/web-artifact-*"],"exclude":[]}},"rules":[{"type":"update"},{"type":"deletion"}]}\\n' "$FAKE_RULESET_UPDATED_AT"
                          exit 0
                          ;;
                        inactive)
                          printf '{"target":"tag","enforcement":"evaluate","updated_at":"%s","bypass_actors":[],"conditions":{"ref_name":{"include":["refs/tags/web-artifact-*"],"exclude":[]}},"rules":[{"type":"update"},{"type":"deletion"}]}\\n' "$FAKE_RULESET_UPDATED_AT"
                          exit 0
                          ;;
                        error)
                          echo "ruleset lookup failed" >&2
                          exit 1
                          ;;
                      esac
                    fi

                    if [ "$1" = "api" ] && [ "${2:-}" = "--include" ]; then
                      state="$(cat "$RELEASE_STATE_FILE")"
                      case "$state" in
                        absent|draft|draft_empty|draft_exact)
                          echo "HTTP/2.0 404 Not Found" >&2
                          exit 1
                          ;;
                        error)
                          echo "network failure" >&2
                          exit 1
                          ;;
                        *)
                          echo "HTTP/2.0 200 OK"
                          echo '{}'
                          exit 0
                          ;;
                      esac
                    fi

                    if [ "$1" = "api" ] && [[ "$args" == *"releases?per_page=100"* ]]; then
                      state="$(cat "$RELEASE_STATE_FILE")"
                      case "$state" in
                        draft|draft_empty|draft_exact) echo 1; exit 0 ;;
                        draft_error) echo "draft lookup failed" >&2; exit 1 ;;
                        *) echo 0; exit 0 ;;
                      esac
                    fi

                    if [ "$1" = "api" ] && [[ "$args" == *"releases/assets/9001"* ]]; then
                      cat "$(cat "$REMOTE_ARTIFACT_POINTER_FILE")"
                      exit 0
                    fi

                    if [ "$1" = "api" ] && [ "${2:-}" = "--method" ] \
                      && [[ "$args" == *"/git/refs"* ]]; then
                      if [ "$(cat "$REMOTE_STATE_FILE")" != "absent" ]; then
                        echo "reference already exists" >&2
                        exit 1
                      fi
                      echo exact > "$REMOTE_STATE_FILE"
                      exit 0
                    fi

                    if [ "$1" = "api" ] && [ "${2:-}" = "--method" ] \
                      && [[ "$args" == *"uploads.github.com"* ]]; then
                      if [ "$FAKE_RELEASE_UPLOAD" = "fail" ]; then
                        echo "release upload failed" >&2
                        exit 1
                      fi
                      input_file=""
                      previous=""
                      for argument in "$@"; do
                        if [ "$previous" = "--input" ]; then
                          input_file="$argument"
                          break
                        fi
                        previous="$argument"
                      done
                      if [ -z "$input_file" ]; then exit 1; fi
                      printf '%s' "$input_file" > "$REMOTE_ARTIFACT_POINTER_FILE"
                      printf 'sha256:%s' "$(shasum -a 256 "$input_file" | awk '{print $1}')" \
                        > "$ASSET_DIGEST_FILE"
                      cp "$LOCAL_EXPECTED_BODY_FILE" "$EXPECTED_BODY_FILE"
                      echo draft_exact > "$RELEASE_STATE_FILE"
                      printf '%s\n' '{"id":9001}'
                      exit 0
                    fi

                    if [ "$1" = "api" ] && [ "${2:-}" = "--method" ] \
                      && [[ "$args" == *"repos/owner/repository/releases"* ]] \
                      && [[ "$args" == *"--method POST"* ]] \
                      && [[ "$args" != *"/releases/100"* ]]; then
                      if [ "$FAKE_RELEASE_CREATE" = "fail" ]; then
                        echo "release create failed" >&2
                        exit 1
                      fi
                      echo draft_empty > "$RELEASE_STATE_FILE"
                      printf '%s\n' '{"id":100}'
                      exit 0
                    fi

                    if [ "$1" = "api" ] && [ "${2:-}" = "--method" ] \
                      && [[ "$args" == *"PATCH"*"releases/100"* ]]; then
                      if [ "$FAKE_RELEASE_PUBLISH" = "fail" ]; then
                        echo "release publish failed" >&2
                        exit 1
                      fi
                      echo "$POST_RELEASE_STATE" > "$RELEASE_STATE_FILE"
                      printf '%s\n' '{"id":100,"draft":false}'
                      exit 0
                    fi

                    if [ "$1" = "api" ]; then
                      state="$(cat "$RELEASE_STATE_FILE")"
                      case "$args" in
                        *".tag_name"*)
                          echo "web-artifact-$FAKE_SHORT_SHA"
                          ;;
                        *".immutable"*)
                          case "$state" in
                            mutable|draft|draft_empty|draft_exact) echo false ;;
                            *) echo true ;;
                          esac
                          ;;
                        *".draft"*)
                          case "$state" in
                            draft|draft_empty|draft_exact) echo true ;;
                            *) echo false ;;
                          esac
                          ;;
                        *".assets | length"*)
                          case "$state" in
                            extra_asset) echo 2 ;;
                            draft_empty) echo 0 ;;
                            *) echo 1 ;;
                          esac
                          ;;
                        *"select(.name"*"length"*)
                          if [ "$state" = "missing_asset" ]; then echo 0; else echo 1; fi
                          ;;
                        *"select(.name"*".id"*)
                          echo 9001
                          ;;
                        *".digest"*)
                          if [ "$state" = "digest_mismatch" ]; then
                            echo "sha256:$FAKE_OTHER_DIGEST"
                          elif [ "$state" = "download_mismatch" ]; then
                            echo "sha256:$FAKE_LOCAL_TARBALL_SHA"
                          else
                            cat "$ASSET_DIGEST_FILE"
                          fi
                          ;;
                        *".body"*)
                          if [ "$state" = "notes_mismatch" ]; then
                            echo "missing release evidence"
                          elif [ "$state" = "notes_conflict" ]; then
                            cat "$EXPECTED_BODY_FILE"
                            echo
                            echo "Tarball SHA-256: $FAKE_OTHER_DIGEST"
                          else
                            cat "$EXPECTED_BODY_FILE"
                          fi
                          ;;
                        *)
                          echo "unexpected gh api query: $args" >&2
                          exit 1
                          ;;
                      esac
                      exit 0
                    fi

                    if [ "$1" = "release" ] \
                      && { [ "$2" = "verify" ] || [ "$2" = "verify-asset" ]; }; then
                      case "$FAKE_ATTESTATION_STATE" in
                        success) exit 0 ;;
                        release_fail)
                          if [ "$2" = "verify" ]; then exit 1; fi
                          exit 0
                          ;;
                        asset_fail)
                          if [ "$2" = "verify-asset" ]; then exit 1; fi
                          exit 0
                          ;;
                      esac
                    fi
                    exit 1
                    """
                ),
                encoding="utf-8",
            )
            gh.chmod(0o755)

            sleep = fake_bin / "sleep"
            sleep.write_text(
                "#!/usr/bin/env bash\nexit 0\n",
                encoding="utf-8",
            )
            sleep.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "ARTIFACT_DIR": str(artifact_dir),
                    "ASSET_DIGEST_FILE": str(asset_digest),
                    "CALL_LOG": str(call_log),
                    "EXPECTED_BODY_FILE": str(expected_body),
                    "FAKE_ATTESTATION_STATE": attestation_state,
                    "FAKE_ASSET_NAME": name,
                    "FAKE_CHECKOUT_STATE": checkout_state,
                    "FAKE_HEAD_SHA": head_sha,
                    "FAKE_IMMUTABILITY_SETTING": immutability_setting,
                    "FAKE_LOCAL_TARBALL_SHA": tarball_sha,
                    "FAKE_OTHER_DIGEST": "d" * 64,
                    "FAKE_OTHER_SHA": OTHER_SHA,
                    "FAKE_RELEASE_CREATE": release_create,
                    "FAKE_RELEASE_PUBLISH": release_publish,
                    "FAKE_RELEASE_UPLOAD": release_upload,
                    "FAKE_SHA": SHA,
                    "FAKE_SHORT_SHA": SHORT_SHA,
                    "FAKE_TAG_RULESET_STATE": tag_ruleset_state,
                    "FAKE_RULESET_UPDATED_AT": "2026-07-24T08:00:00Z",
                    "GITHUB_REF_NAME": event_tag
                    or (
                        f"web-artifact-{SHORT_SHA}"
                        if ref_type == "tag"
                        else "main"
                    ),
                    "GITHUB_REF_TYPE": ref_type,
                    "GITHUB_DEFAULT_BRANCH": "main",
                    "GITHUB_REPOSITORY": "owner/repository",
                    "GITHUB_SHA": SHA,
                    "GW_RELEASE_IMMUTABILITY_CONFIRMED": immutability_confirmed,
                    "GW_RELEASE_SETTINGS_TOKEN": settings_token,
                    "GW_RELEASE_TAG_RULESET_ID": tag_ruleset_id,
                    "GW_RELEASE_TAG_RULESET_NO_BYPASS_CONFIRMED": (
                        tag_ruleset_no_bypass_confirmed
                    ),
                    "GW_RELEASE_TAG_RULESET_UPDATED_AT": tag_ruleset_updated_at,
                    "IMMUTABILITY_CHECKS_FILE": str(immutability_checks),
                    "LOCAL_ARTIFACT_FILE": str(artifact),
                    "LOCAL_EXPECTED_BODY_FILE": str(local_expected_body),
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "PYTHON_BIN": sys.executable,
                    "POST_RELEASE_STATE": post_release_state,
                    "REMOTE_ARTIFACT_POINTER_FILE": str(remote_artifact_pointer),
                    "RELEASE_STATE_FILE": str(release_state),
                    "REMOTE_STATE_FILE": str(remote_state),
                }
            )
            result = subprocess.run(
                ["bash", str(PUBLISH_SCRIPT)],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            calls = (
                call_log.read_text(encoding="utf-8").splitlines()
                if call_log.exists()
                else []
            )
            return result, calls, tarball_sha

    @staticmethod
    def _mutations(calls: list[str]) -> list[str]:
        return [
            call
            for call in calls
            if (
                "git/refs" in call
                or (
                    call.startswith("gh api --method POST ")
                    and (
                        "/releases" in call
                        or "uploads.github.com" in call
                    )
                )
                or call.startswith("gh api --method PATCH ")
                or call.startswith("git push ")
                or call.startswith("git tag ")
            )
        ]

    def test_workflow_has_serialization_owner_gate_and_no_overwrite_hatch(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        script = PUBLISH_SCRIPT.read_text(encoding="utf-8")
        publication_source = workflow + "\n" + script
        self.assertIn("group: web-artifact-release", workflow)
        self.assertIn("queue: max", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("environment:\n      name: web-artifact-release", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("\n  push:\n", workflow)
        self.assertIn("GW_RELEASE_IMMUTABILITY_CONFIRMED", workflow)
        self.assertIn("GW_RELEASE_SETTINGS_TOKEN", workflow)
        self.assertIn("GW_RELEASE_TAG_RULESET_ID", workflow)
        self.assertIn("GW_RELEASE_TAG_RULESET_NO_BYPASS_CONFIRMED", workflow)
        self.assertIn("GW_RELEASE_TAG_RULESET_UPDATED_AT", workflow)
        self.assertIn("PYTHON_BIN: python", workflow)
        self.assertIn("--generated-at", workflow)
        self.assertIn("--require-hashes", workflow)
        self.assertIn("--only-binary=:all:", workflow)
        self.assertIn("requirements-release-macos-arm64-py312.lock", workflow)
        self.assertNotIn("--upgrade pip", workflow)
        self.assertNotIn("-r requirements.txt", workflow)
        self.assertGreaterEqual(
            workflow.count("git status --porcelain=v1 --untracked-files=all"),
            2,
        )
        self.assertIn("X-GitHub-Api-Version: 2026-03-10", script)
        self.assertNotIn("git tag -f", publication_source)
        self.assertNotIn("git push -f", publication_source)
        self.assertNotIn("--clobber", publication_source)
        self.assertNotIn("|| gh release upload", publication_source)
        self.assertNotIn("|| true", script)
        self.assertNotIn("gh release create", script)
        self.assertIn('"draft=true"', script)
        self.assertIn("gh release verify-asset", script)
        self.assertIn("repos/${repository}/git/refs", script)
        self.assertIn("repos/${repository}/git/ref/tags/${tag}", script)
        self.assertNotIn("git ls-remote", script)

    def test_release_dependency_lock_is_exact_and_hashed(self):
        lock = RELEASE_LOCK.read_text(encoding="utf-8")
        requirement_lines = [
            line
            for line in lock.splitlines()
            if line and not line.startswith("#") and not line.startswith(" ")
        ]
        self.assertEqual(len(requirement_lines), 9)
        self.assertTrue(all("==" in line for line in requirement_lines))
        self.assertEqual(lock.count("--hash=sha256:"), 9)
        self.assertNotIn(">=", lock)
        self.assertNotIn("~=", lock)
        self.assertNotIn("-r ", lock)

    def test_clean_dispatch_creates_ref_release_and_verifies_immutability(self):
        result, calls, tarball_sha = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        create_ref = next(i for i, call in enumerate(calls) if "git/refs" in call)
        setting_checks = [
            i for i, call in enumerate(calls) if "immutable-releases" in call
        ]
        ruleset_checks = [
            i for i, call in enumerate(calls) if "/rulesets/" in call
        ]
        draft_create = next(
            i
            for i, call in enumerate(calls)
            if (
                call.startswith("gh api --method POST ")
                and "repos/owner/repository/releases" in call
                and "uploads.github.com" not in call
            )
        )
        upload = next(
            i for i, call in enumerate(calls) if "uploads.github.com" in call
        )
        publish = next(
            i for i, call in enumerate(calls) if call.startswith("gh api --method PATCH ")
        )
        self.assertEqual(len(setting_checks), 2)
        self.assertEqual(len(ruleset_checks), 2)
        self.assertLess(setting_checks[0], create_ref)
        self.assertLess(ruleset_checks[0], create_ref)
        self.assertLess(create_ref, draft_create)
        self.assertLess(draft_create, upload)
        self.assertLess(upload, publish)
        self.assertLess(upload, setting_checks[1])
        self.assertLess(upload, ruleset_checks[1])
        self.assertLess(setting_checks[1], publish)
        self.assertLess(ruleset_checks[1], publish)
        self.assertIn(f"tag_name=web-artifact-{SHORT_SHA}", calls[draft_create])
        self.assertIn(f"target_commitish={SHA}", calls[draft_create])
        self.assertIn(f"name=Web\\ artifact\\ web-artifact-{SHORT_SHA}", calls[draft_create])
        self.assertIn("draft=true", calls[draft_create])
        self.assertIn("prerelease=false", calls[draft_create])
        self.assertIn("make_latest=false", calls[draft_create])
        self.assertIn("body=", calls[draft_create])
        self.assertIn(SHA, calls[draft_create])
        self.assertIn(tarball_sha, calls[draft_create])
        self.assertIn("Manifest", calls[draft_create])
        self.assertIn(f"assets\\?name={f'gw-web-artifact-{SHORT_SHA}.tar.gz'}", calls[upload])
        self.assertTrue(any(call.startswith("gh release verify ") for call in calls))
        self.assertTrue(
            any(call.startswith("gh release verify-asset ") for call in calls)
        )
        self.assertIn("published and verified immutable release", result.stdout)

    def test_exact_existing_immutable_release_is_verified_noop(self):
        result, calls, _ = self._run(remote_tag="exact", release="exact")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retry is a no-op", result.stdout)
        self.assertEqual(self._mutations(calls), [])

    def test_changed_registry_snapshot_is_not_an_identical_noop(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="exact",
            content=b'[{"publication_state":"publishable","snapshot":"new"}]\n',
            remote_content=(
                b'[{"publication_state":"publishable","snapshot":"published"}]\n'
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from the local candidate", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_existing_asset_digest_mismatch_fails_without_mutation(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="digest_mismatch",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from the local candidate", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_existing_mutable_release_is_not_accepted_as_evidence(self):
        result, calls, _ = self._run(remote_tag="exact", release="mutable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected immutable state", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_existing_release_with_extra_asset_fails_without_mutation(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="extra_asset",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly the one expected asset", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_existing_release_with_missing_evidence_fails_without_mutation(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="notes_mismatch",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("notes do not exactly match", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_existing_release_with_conflicting_evidence_fails_without_mutation(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="notes_conflict",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("notes do not exactly match", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_existing_tag_at_other_commit_fails_before_release_create(self):
        result, calls, _ = self._run(remote_tag="mismatch")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different commit", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_orphan_exact_tag_cannot_resume_without_candidate_digest(self):
        result, calls, _ = self._run(remote_tag="exact")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("orphan tag", result.stderr)
        self.assertIn("roll forward with a new commit", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_annotated_canonical_tag_is_rejected(self):
        result, calls, _ = self._run(remote_tag="annotated")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a lightweight commit ref", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_manifest_commit_mismatch_fails_before_remote_calls(self):
        result, calls, _ = self._run(manifest_commit=OTHER_SHA)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest.backend_commit", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_manifest_content_digest_mismatch_fails_before_remote_calls(self):
        result, calls, _ = self._run(manifest_content_sha="d" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match the archived content tree", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_manifest_unknown_field_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            manifest_extra={
                "unexpected": "/Users/IA/private reviewer@example.com",
            }
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exactly the required fields", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_duplicate_archive_member_fails_before_remote_calls(self):
        result, calls, _ = self._run(duplicate_member=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate member", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_unsafe_archive_path_fails_before_remote_calls(self):
        result, calls, _ = self._run(unsafe_member=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-canonical path", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_symbolic_link_member_fails_before_remote_calls(self):
        result, calls, _ = self._run(symbolic_link=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a regular file", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_trailing_asset_bytes_fail_before_remote_calls(self):
        result, calls, _ = self._run(
            trailing_bytes=b"SECRET-TRAILER reviewer@example.com /Users/IA/private",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not the canonical release serialization", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_deny_list_violation_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            content=(
                b'[{"publication_state":"not_publishable",'
                b'"email":"reviewer@example.com","src":"/Users/IA/private"}]\n'
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact contract scan failed", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_json_escaped_path_and_email_fail_before_remote_calls(self):
        result, calls, _ = self._run(
            content=(
                b'[{"publication_state":"publishable",'
                b'"email":"reviewer\\u0040example.com",'
                b'"src":"\\u002fUsers\\u002fIA\\u002fprivate"}]\n'
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact contract scan failed", result.stderr)
        self.assertIn("decoded absolute path", result.stderr)
        self.assertIn("plaintext email", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_nonfinite_json_number_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            content=b'[{"publication_state":"publishable","metric":NaN}]\n',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-finite JSON constant", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_duplicate_json_keys_fail_before_remote_calls(self):
        result, calls, _ = self._run(
            content=(
                b'[{"publication_state":"publishable",'
                b'"probe":"reviewer\\u0040example.com","probe":"safe",'
                b'"src":"\\u002fUsers\\u002fIA","src":"safe"}]\n'
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate JSON key", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_boolean_schema_version_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            manifest_extra={"schema_version": True},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest.schema_version", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_service_code_mismatch_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            service_override=(
                "service/run.py",
                b'raise SystemExit("substituted runner payload")\n',
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "artifact service file does not match checked-out source",
            result.stderr,
        )
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_downloaded_bytes_must_match_github_asset_digest(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="download_mismatch",
            remote_content=b'[{"publication_state":"publishable"}]\n',
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bytes do not match GitHub's digest", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_checked_out_head_mismatch_fails_before_remote_calls(self):
        result, calls, _ = self._run(head_sha=OTHER_SHA)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HEAD does not equal GITHUB_SHA", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_dirty_tracked_checkout_fails_before_remote_calls(self):
        result, calls, _ = self._run(checkout_state="dirty")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked checkout content differs", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_untracked_script_fails_before_remote_calls(self):
        result, calls, _ = self._run(checkout_state="untracked")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("untracked source exists", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_noncanonical_asset_name_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            artifact_name="gw-web-artifact-deadbeefdead.tar.gz",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact name must be", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_tag_trigger_fails_before_remote_calls(self):
        result, calls, _ = self._run(
            ref_type="tag",
            event_tag="web-artifact-manual-name",
            remote_tag="exact",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manual run from default branch", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_non_default_branch_fails_before_remote_calls(self):
        result, calls, _ = self._run(event_tag="feature/release")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manual run from default branch", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertFalse(any(call.startswith("gh ") for call in calls))

    def test_missing_owner_immutability_confirmation_mutates_nothing(self):
        result, calls, _ = self._run(immutability_confirmed="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not owner-confirmed", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_missing_settings_token_mutates_nothing(self):
        result, calls, _ = self._run(settings_token="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GW_RELEASE_SETTINGS_TOKEN is required", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_disabled_live_immutability_setting_mutates_nothing(self):
        result, calls, _ = self._run(immutability_setting="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not authoritatively report", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_immutability_setting_lookup_error_mutates_nothing(self):
        result, calls, _ = self._run(immutability_setting="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not authoritatively report", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_bypassable_tag_ruleset_mutates_nothing(self):
        result, calls, _ = self._run(tag_ruleset_state="bypass")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not active, exact, current, and non-bypassable", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_redacted_empty_bypass_list_requires_and_accepts_owner_attestation(self):
        result, _, _ = self._run(tag_ruleset_state="redacted")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_no_bypass_owner_attestation_mutates_nothing(self):
        result, calls, _ = self._run(tag_ruleset_no_bypass_confirmed="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no-bypass policy is not owner-confirmed", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_missing_tag_ruleset_id_mutates_nothing(self):
        result, calls, _ = self._run(tag_ruleset_id="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GW_RELEASE_TAG_RULESET_ID", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_missing_tag_ruleset_version_pin_mutates_nothing(self):
        result, calls, _ = self._run(tag_ruleset_updated_at="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GW_RELEASE_TAG_RULESET_UPDATED_AT", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_stale_tag_ruleset_version_pin_mutates_nothing(self):
        result, calls, _ = self._run(
            tag_ruleset_updated_at="2026-07-23T08:00:00Z"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not active, exact, current, and non-bypassable", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_live_controls_are_rechecked_before_publish(self):
        result, calls, _ = self._run(immutability_setting="true_then_false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not authoritatively report", result.stderr)
        self.assertEqual(
            len([call for call in calls if "immutable-releases" in call]),
            2,
        )
        self.assertFalse(any(call.startswith("gh api --method PATCH ") for call in calls))

    def test_release_lookup_error_is_not_treated_as_absence(self):
        result, calls, _ = self._run(release="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not determine release state", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_release_create_failure_propagates_without_upload_fallback(self):
        result, calls, _ = self._run(release_create="fail")
        self.assertNotEqual(result.returncode, 0)
        releases = [
            call
            for call in calls
            if (
                call.startswith("gh api --method POST ")
                and "repos/owner/repository/releases" in call
                and "uploads.github.com" not in call
            )
        ]
        self.assertEqual(len(releases), 1)
        self.assertFalse(any("uploads.github.com" in call for call in calls))
        self.assertFalse(any(call.startswith("gh api --method PATCH ") for call in calls))

    def test_matching_partial_draft_requires_owner_cleanup_without_mutation(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="draft",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owner review/cleanup is required", result.stderr)
        self.assertEqual(self._mutations(calls), [])

    def test_upload_failure_leaves_unpublished_draft_and_fails(self):
        result, calls, _ = self._run(release_upload="fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release upload failed", result.stderr)
        self.assertTrue(
            any(
                call.startswith("gh api --method POST ")
                and "repos/owner/repository/releases" in call
                for call in calls
            )
        )
        self.assertFalse(any(call.startswith("gh api --method PATCH ") for call in calls))

    def test_publish_failure_leaves_verified_draft_and_fails(self):
        result, calls, _ = self._run(release_publish="fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release publish failed", result.stderr)
        self.assertEqual(
            len([call for call in calls if call.startswith("gh api --method PATCH ")]),
            1,
        )
        self.assertFalse(
            any(call.startswith("gh release verify ") for call in calls)
        )
        self.assertFalse(
            any(call.startswith("gh release verify-asset ") for call in calls)
        )

    def test_post_publish_mutable_release_fails_verification(self):
        result, calls, _ = self._run(post_release_state="mutable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed immutable post-publication", result.stderr)
        self.assertEqual(
            len([call for call in calls if call.startswith("gh api --method PATCH ")]),
            1,
        )

    def test_existing_release_attestation_failure_is_not_suppressed(self):
        result, calls, _ = self._run(
            remote_tag="exact",
            release="exact",
            attestation_state="release_fail",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attestation did not verify", result.stderr)
        self.assertEqual(self._mutations(calls), [])
        self.assertEqual(
            len([call for call in calls if call.startswith("gh release verify ")]),
            5,
        )

    def test_new_release_asset_attestation_failure_is_not_suppressed(self):
        result, calls, _ = self._run(attestation_state="asset_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("attestation did not verify", result.stderr)
        self.assertEqual(
            len([call for call in calls if call.startswith("gh api --method PATCH ")]),
            1,
        )
        self.assertEqual(
            len(
                [
                    call
                    for call in calls
                    if call.startswith("gh release verify-asset ")
                ]
            ),
            5,
        )


if __name__ == "__main__":
    unittest.main()
