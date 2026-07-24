#!/usr/bin/env python3
"""Verify the source and deterministic content digest inside a web artifact.

The release workflow calls this at both sides of publication:

* on the local candidate before any GitHub mutation; and
* on a downloaded immutable asset before accepting a retry as a no-op.

Output is a single tab-separated record for the shell boundary:
``backend_commit, artifact_sha256, schema_version``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import export_web_artifact as artifact_contract  # noqa: E402

MANIFEST_NAME = "manifest.json"
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_CONTENT_BYTES = 256 * 1024 * 1024
MAX_MEMBER_COUNT = 1024
MANIFEST_KEYS = {
    "artifact_sha256",
    "backend_commit",
    "gate_functions",
    "generated_at_utc",
    "row_counts",
    "schema_version",
}


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _strict_json_loads(value):
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


def _content_digest(files: dict[str, bytes]) -> str:
    """Mirror the artifact contract's deterministic ``(path, bytes)`` digest."""

    hasher = hashlib.sha256()
    for relative_path in sorted(files):
        if relative_path == MANIFEST_NAME:
            continue
        hasher.update(relative_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(files[relative_path])
        hasher.update(b"\0")
    return hasher.hexdigest()


def _canonical_tarball(files: dict[str, bytes]) -> bytes:
    """Serialize exactly as the trusted builder, including an empty gzip name."""

    raw = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for relative_path in sorted(files):
                data = files[relative_path]
                info = tarfile.TarInfo(name=relative_path)
                info.size = len(data)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _expected_service_files() -> dict[str, bytes]:
    """Rebuild the executable subset from the trusted checked-out source."""

    expected = {
        "service/run.py": artifact_contract.RUN_PY.encode("utf-8"),
        "service/schema.sql": artifact_contract.schema_sql_bytes(
            artifact_contract.SCRIPTS_DIR
        ),
    }
    for relative_path in artifact_contract.compute_service_closure(
        artifact_contract.SCRIPTS_DIR
    ):
        expected[f"service/{relative_path.as_posix()}"] = (
            artifact_contract.SCRIPTS_DIR / relative_path
        ).read_bytes()
    return expected


def inspect_artifact(path: Path, *, expected_commit: str | None = None) -> dict:
    """Return verified manifest metadata or raise ``ValueError``."""

    try:
        archive_size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"archive cannot be stat-ed: {exc}") from exc
    if archive_size > MAX_ARCHIVE_BYTES:
        raise ValueError(
            f"archive exceeds the {MAX_ARCHIVE_BYTES}-byte compressed limit"
        )

    files: dict[str, bytes] = {}
    total_content_bytes = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            member_count = 0
            for member in archive:
                member_count += 1
                if member_count > MAX_MEMBER_COUNT:
                    raise ValueError(
                        f"archive exceeds the {MAX_MEMBER_COUNT}-member limit"
                    )
                if not member.isfile():
                    raise ValueError(
                        f"archive member must be a regular file: {member.name!r}"
                    )
                normalized = PurePosixPath(member.name)
                if (
                    normalized.is_absolute()
                    or ".." in normalized.parts
                    or normalized.as_posix() != member.name
                ):
                    raise ValueError(
                        f"archive member has a non-canonical path: {member.name!r}"
                    )
                if member.name in files:
                    raise ValueError(
                        f"archive contains duplicate member: {member.name!r}"
                    )
                if member.size > MAX_MEMBER_BYTES:
                    raise ValueError(
                        f"archive member exceeds the {MAX_MEMBER_BYTES}-byte limit: "
                        f"{member.name!r}"
                    )
                total_content_bytes += member.size
                if total_content_bytes > MAX_TOTAL_CONTENT_BYTES:
                    raise ValueError(
                        "archive exceeds the total uncompressed-content limit"
                    )
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError(f"archive member is unreadable: {member.name!r}")
                files[member.name] = stream.read()
            if member_count == 0:
                raise ValueError("archive is empty")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(f"archive cannot be read: {exc}") from exc

    if MANIFEST_NAME not in files:
        raise ValueError("archive must contain exactly one manifest.json")
    if len(files) == 1:
        raise ValueError("archive contains no content files")

    original_bytes = path.read_bytes()
    if _canonical_tarball(files) != original_bytes:
        raise ValueError(
            "archive bytes are not the canonical release serialization "
            "(metadata, gzip header, or trailing bytes differ)"
        )

    try:
        manifest = _strict_json_loads(files[MANIFEST_NAME])
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"manifest.json is invalid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must contain a JSON object")
    if set(manifest) != MANIFEST_KEYS:
        raise ValueError("manifest.json must contain exactly the required fields")

    commit = manifest.get("backend_commit")
    content_digest = manifest.get("artifact_sha256")
    schema_version = manifest.get("schema_version")
    generated_at = manifest.get("generated_at_utc")
    gate_functions = manifest.get("gate_functions")
    row_counts = manifest.get("row_counts")
    if not isinstance(commit, str) or FULL_SHA_RE.fullmatch(commit) is None:
        raise ValueError("manifest.backend_commit is not a lowercase full SHA")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("manifest.backend_commit does not equal the expected commit")
    if (
        not isinstance(content_digest, str)
        or SHA256_RE.fullmatch(content_digest) is None
    ):
        raise ValueError("manifest.artifact_sha256 is not a lowercase SHA-256")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != artifact_contract.SCHEMA_VERSION
    ):
        raise ValueError(
            "manifest.schema_version does not equal the supported contract version"
        )
    if not isinstance(generated_at, str):
        raise ValueError("manifest.generated_at_utc is not an ISO-8601 string")
    try:
        generated_timestamp = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("manifest.generated_at_utc is not valid ISO-8601") from exc
    if generated_timestamp.tzinfo is None:
        raise ValueError("manifest.generated_at_utc must include a UTC offset")
    if gate_functions != artifact_contract.GATE_FUNCTIONS:
        raise ValueError("manifest.gate_functions does not match the contract")
    if (
        not isinstance(row_counts, dict)
        or set(row_counts) != {"published", "reviewer_internal"}
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in row_counts.values()
        )
    ):
        raise ValueError("manifest.row_counts is not the required nonnegative map")

    try:
        published = _strict_json_loads(files[artifact_contract.PUBLISHED_NAME])
        reviewer_internal = _strict_json_loads(
            files[artifact_contract.REVIEWER_INTERNAL_NAME]
        )
    except (KeyError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"artifact data lanes are invalid: {exc}") from exc
    if not isinstance(published, list) or not isinstance(reviewer_internal, list):
        raise ValueError("artifact data lanes must both be JSON arrays")
    if row_counts != {
        "published": len(published),
        "reviewer_internal": len(reviewer_internal),
    }:
        raise ValueError("manifest.row_counts does not match the data lanes")

    computed_digest = _content_digest(files)
    if computed_digest != content_digest:
        raise ValueError(
            "manifest.artifact_sha256 does not match the archived content tree"
        )

    for relative_path, expected_bytes in _expected_service_files().items():
        if files.get(relative_path) != expected_bytes:
            raise ValueError(
                "artifact service file does not match checked-out source: "
                f"{relative_path}"
            )

    try:
        with tempfile.TemporaryDirectory() as raw_tmp:
            staged = artifact_contract.extract_to(files, Path(raw_tmp))
            violations = artifact_contract.deny_list_violations(staged)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"artifact contract scan failed: {exc}") from exc
    if violations:
        raise ValueError(
            "artifact contract scan failed:\n  " + "\n  ".join(violations)
        )

    return {
        "backend_commit": commit,
        "artifact_sha256": content_digest,
        "schema_version": schema_version,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--expected-commit")
    args = parser.parse_args()
    try:
        metadata = inspect_artifact(
            args.artifact,
            expected_commit=args.expected_commit,
        )
    except ValueError as exc:
        parser.exit(1, f"invalid web artifact: {exc}\n")
    print(
        metadata["backend_commit"],
        metadata["artifact_sha256"],
        metadata["schema_version"],
        sep="\t",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
