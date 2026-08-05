"""Stage 5.03 source-VERSION inventory (GOV-1684) — reviewer-internal, Alpine.

The 5.03 inventory read for the crawled-source *version history* written by
:mod:`source_version_store` (migration 0033). Where the GOV-484 registry inventory
(:mod:`stage5_source_inventory`) projects one row per *source* — a single latest
snapshot — this read enumerates **every preserved version of each source URL**, so
the inventory reflects versioning rather than a single latest snapshot (AC-6). It
is the enumeration half of Slice 1: two versions preserved + typed lineage + this
read.

Shape — grouped by source URL, deterministic order:

    {
      "scope": "alpine",
      "access": "reviewer_internal",          # never public — this exposes hashes
      "sources": [
        {
          "sourceUrl": "...",
          "sourceId": "...",                   # registry link, or None
          "archiveAvailability": {...},        # archive-status-near-scan (§2, reused)
          "versionCount": 2,
          "versions": [                        # oldest-first (ordinal ASC)
            {"versionId", "versionOrdinal", "retrievalTime", "contentHash",
             "lineageType", "supersedesVersionId", "snapshotPreserved", "provenance"},
            ...
          ],
        }, ...
      ],
      "inventoryDigest": "<sha256 over the sources list>",
    }

Boundary rules:

* **reviewer-internal only.** Unlike the web-safe crossing (``file_read_api``),
  this read intentionally exposes each version's ``content_hash`` — that hash *is*
  the version's identity, and a reviewer needs it to confirm what was preserved. It
  is therefore fixed at ``access: reviewer_internal`` / ``scope: alpine`` and must
  never be routed to a public/published path. (This is why the single-envelope-hash
  guard of the GOV-484 registry inventory does NOT apply here.)
* **the raw snapshot path never leaves.** ``snapshot_path`` is a local filesystem
  locator; it is read to CONFIRM preservation (containment-checked, then
  ``exists()``) and surfaced only as the boolean ``snapshotPreserved`` — never the
  path string.
* **read-site path containment (AC-5).** Every stored ``snapshot_path`` is joined
  under the repo root with :func:`raw_preservation._contained`, which **raises**
  (never clamps) on an absolute or ``..``-escaping value — because
  ``Path(root) / value`` silently discards ``root`` when ``value`` is absolute
  (GOV-1693). A poisoned path fails the whole read loudly rather than reading
  outside the repo.
* **transport backstop.** The assembled body is swept by
  :func:`read_api.assert_no_raw_paths`, so any filesystem path / vault marker that
  slipped a provenance field fails loudly at the boundary (content hashes are hex,
  not paths, and pass).

Pure function of the DB + repo tree: same inputs -> byte-identical inventory. No
mutation, no AI, no network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import raw_preservation  # noqa: E402  (reused: contained-path guard)
import read_api  # noqa: E402  (reused read-only: transport sweep)
import source_version_store as svs  # noqa: E402  (reused read-only: list helper vocab)
import stage5_source_inventory as reg  # noqa: E402  (reused read-only: archive envelope)

REPO_ROOT = SCRIPTS.parent

SCOPE = "alpine"  # fixed; broader = planned
ACCESS = "reviewer_internal"  # never "public" — this read exposes content hashes


class SourceVersionInventoryError(AssertionError):
    """An emitted version-inventory record violates a Slice-1 invariant."""


def _archive_signals(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{source_id: {scan_date, archive_status, archive_url}}`` from the registry.

    The archive-status-near-scan inputs (§2), read categorical/locator-URL only. A
    version whose ``source_id`` is NULL or absent from the registry gets the
    fail-closed ``not_checked`` default from :func:`stage5_source_inventory.archive_availability`.
    """
    signals: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT source_id, scan_date, archive_status, archive_url FROM sources"
    ):
        record = dict(row)
        signals[record["source_id"]] = {
            "scan_date": record.get("scan_date"),
            "archive_status": record.get("archive_status"),
            "archive_url": record.get("archive_url"),
        }
    return signals


def _snapshot_preserved(snapshot_path: Any, repo_root: Path) -> bool:
    """Containment-check a stored snapshot path (AC-5), return whether it exists.

    Raises :class:`raw_preservation.RawPathEscape` on an absolute/escaping path —
    fail-closed at the read site. Returns ``False`` when no path is stored.
    """
    if not snapshot_path or not isinstance(snapshot_path, str):
        return False
    contained = raw_preservation._contained(repo_root, snapshot_path)
    return contained.exists()


def _project_version(row: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    """One reviewer-internal version entry. The raw ``snapshot_path`` never leaves."""
    provenance = row.get("provenance")
    try:
        provenance_parsed: Any = json.loads(provenance) if provenance else None
    except (TypeError, ValueError):
        provenance_parsed = provenance  # a verbatim token — surfaced as-is
    return {
        "versionId": row["version_id"],
        "versionOrdinal": row["version_ordinal"],
        "retrievalTime": row["retrieval_time"],
        "contentHash": row["content_hash"],
        "lineageType": row["lineage_type"],
        "supersedesVersionId": row["supersedes_version_id"],
        "snapshotPreserved": _snapshot_preserved(row.get("snapshot_path"), repo_root),
        "provenance": provenance_parsed,
    }


def version_inventory(
    conn: sqlite3.Connection, repo_root: Path = REPO_ROOT
) -> list[dict[str, Any]]:
    """One entry per source URL with ALL its preserved versions, deterministic order.

    Grouped by ``source_url`` and ordered ``(source_url, version_ordinal)`` so the
    same DB yields a byte-identical list. A changed URL therefore carries both (or
    all) of its versions — never just the latest (AC-6) — with the archive-status-
    near-scan envelope attached from the linked registry row.
    """
    archive = _archive_signals(conn)
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT version_id, source_id, source_url, retrieval_time, content_hash, "
            "provenance, snapshot_path, version_ordinal, supersedes_version_id, "
            "lineage_type, created_utc FROM source_versions "
            "ORDER BY source_url ASC, version_ordinal ASC"
        )
    ]
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        url = row["source_url"]
        if url not in grouped:
            order.append(url)
            source_id = row["source_id"]
            sig = archive.get(source_id, {}) if source_id else {}
            grouped[url] = {
                "sourceUrl": url,
                "sourceId": source_id,
                "archiveAvailability": reg.archive_availability(
                    sig.get("scan_date"), sig.get("archive_status"), sig.get("archive_url")
                ),
                "versions": [],
            }
        grouped[url]["versions"].append(_project_version(row, repo_root))
    entries: list[dict[str, Any]] = []
    for url in order:
        entry = grouped[url]
        entry["versionCount"] = len(entry["versions"])
        entries.append(entry)
    return entries


def _envelope_digest(sources: list[dict[str, Any]]) -> str:
    """One sha256 over the canonical sources list — the sole envelope-level hash."""
    payload = json.dumps(sources, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_inventory(
    conn: sqlite3.Connection, repo_root: Path = REPO_ROOT
) -> dict[str, Any]:
    """Assemble ``{scope, access, sources[], inventoryDigest}`` and transport-sweep it.

    Every preserved version is emitted (never hidden). The whole body is swept by
    :func:`read_api.assert_no_raw_paths` (I1 backstop): a filesystem path / vault
    marker that slipped a provenance field fails loudly — content hashes are hex and
    pass, the raw ``snapshot_path`` is not in the body at all.
    """
    sources = version_inventory(conn, repo_root=repo_root)
    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public"
        "sources": sources,
        "inventoryDigest": _envelope_digest(sources),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# Contract guard (load-bearing, non-tautological)
# ---------------------------------------------------------------------------


def assert_lineage_types_valid(body: dict[str, Any]) -> bool:
    """RED if any version's emitted ``lineageType`` is outside the closed set.

    A real cross-check on the EMITTED body: the first version of each URL must carry
    ``lineageType`` ``None`` (it supersedes nothing) and every later version must
    carry a member of :data:`source_version_store.LINEAGE_TYPES`. A build that emits
    an out-of-vocab edge type goes RED.
    """
    for entry in body.get("sources", []):
        for version in entry.get("versions", []):
            lineage = version.get("lineageType")
            ordinal = version.get("versionOrdinal")
            if ordinal == 1:
                if lineage is not None:
                    raise SourceVersionInventoryError(
                        f"{entry.get('sourceUrl')!r} v1 carries lineageType {lineage!r} "
                        "but the first version supersedes nothing"
                    )
            elif lineage not in svs.LINEAGE_TYPES:
                raise SourceVersionInventoryError(
                    f"{entry.get('sourceUrl')!r} v{ordinal} lineageType {lineage!r} "
                    f"outside the closed set {sorted(svs.LINEAGE_TYPES)}"
                )
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.03 reviewer-internal Alpine source-VERSION inventory (GOV-1684)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check", action="store_true", help="run the lineage-type contract guard"
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        body = build_inventory(conn)
        if args.check:
            assert_lineage_types_valid(body)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
