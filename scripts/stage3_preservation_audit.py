"""Stage 3.04 raw-preservation read-time auditor (GOV-367).

A **separate additive** module that *proves* the four raw-preservation invariants
(RP-1..RP-4) plus the RP-0 raw-before-parse ordering over the EXISTING Alpine
corpus, and emits the reviewer-internal ``PreservationStatusRow`` overlay
(contract §3, ``Docs/stage3-04-raw-preservation-contract.md``). It implements the
contract's §5 exactly:

* **extend-not-fork** — it *reads/calls* :mod:`raw_preservation` (the Stage 1.04 /
  2.04 engine) for every hash/drift decision; it does NOT re-implement hashing, the
  absolute drift rule, or the raw-before-parse gate. RP-1/RP-2 verdicts come from
  :func:`raw_preservation.verify_reproducibility` (documents) and
  :func:`raw_preservation.reconcile_transcript_text` (transcripts); the source
  preservation verdict comes from :func:`raw_preservation.validate_sources` (run
  read-only, ``apply=False``); the envelope digest is
  :func:`raw_preservation.preservation_manifest`; the RP-0 ordering check calls
  :func:`raw_preservation.assert_raw_preserved` per derived row.
* **0-diff SSOT** — it imports :data:`publication.WEB_UNSAFE_FIELDS` /
  :func:`publication.to_web_safe` and :data:`read_api.RAW_PATH_MARKERS` /
  :func:`read_api.assert_no_raw_paths` *by reference*. It declares no copy of any
  of them. ``publication.py`` and ``read_api.py`` stay byte-for-byte unchanged.

No-leak posture (contract §3/§4): the per-unit overlay row carries the hash
*verdict* (``hash_ok: bool``) only — never a ``raw_local_path``, a ``sha256``
value, a ``.sha256`` filename, or a vault marker. The single
``preservation_manifest.aggregate_sha256`` MAY appear once at the envelope level as
an opaque reviewer-internal audit fingerprint. The assembled overlay is swept by
:func:`read_api.assert_no_raw_paths` before return. A public projection of any
preservation-bearing record is limited — by the existing ``to_web_safe`` allowlist,
not a new field — to ``scan_date`` / ``last_validated_utc`` / ``archive_status`` /
``ui_status``.

Fail-closed (contract §4.5 / GOV-262): a missing/mismatch raw is a preservation
``defect`` (``hash_ok=False``); the auditor never re-fetches, never overwrites a
recorded ``sha256`` (it runs ``validate_sources`` with ``apply=False`` and issues
no UPDATE/INSERT), and never downgrades a defect to a ``completeness_gap``.

Read-only over the existing corpus: no new crawl, no new source, no schema change.

Usage:
    python scripts/stage3_preservation_audit.py [--db PATH] [--check-ordering]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import publication as pub  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

SCOPE = "alpine"
ACCESS = "reviewer_internal"

# Preservation-state vocabulary (contract §3). Derived from the raw_preservation
# verdicts — never a new enum the engine doesn't already produce.
STATE_PRESERVED = "preserved"
STATE_DEFECT = "defect"
STATE_EXCEPTION = "exception_documented"

# Recorded archive state when a unit's source leg has not been checked (RP-4: an
# absence is an explicit recorded state, never a silent gap). Mirrors the
# `sources.archive_status` column default — no new vocabulary is introduced.
_ARCHIVE_NOT_CHECKED = "not_checked"

# The exact key set of a PreservationStatusRow (contract §3). Named so a no-leak
# test can assert the projected row is a SUBSET of this — and so a future field add
# is a conscious, reviewed change, not an accidental column leak.
PRESERVATION_ROW_FIELDS = frozenset({
    "unit_ref",            # {object_type, id|source_id} — slug/id only, never a path
    "retained",            # RP-1
    "hash_ok",             # RP-2 (verdict only — the sha256 itself never appears)
    "as_of",               # RP-3 {first_captured, last_validated}
    "archive",             # RP-4 {present, status}
    "preservation_state",  # preserved | defect | exception_documented
})


class RawBeforeParseViolation(AssertionError):
    """Raised when a derived ``raw_text`` row has no hash-verifiable raw predecessor."""


def _row(
    unit_ref: dict[str, Any],
    *,
    retained: bool,
    hash_ok: bool,
    first_captured: str | None,
    last_validated: str | None,
    archive_present: bool,
    archive_status: str | None,
    preservation_state: str,
) -> dict[str, Any]:
    """Assemble one PreservationStatusRow (the §3 shape, hash-verdict only)."""
    return {
        "unit_ref": unit_ref,
        "retained": retained,
        "hash_ok": hash_ok,
        "as_of": {"first_captured": first_captured, "last_validated": last_validated},
        "archive": {"present": archive_present, "status": archive_status},
        "preservation_state": preservation_state,
    }


def _source_meta(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """RP-3/RP-4 source leg, keyed by ``source_id`` (read-only).

    The Wayback/archive reference (RP-4) and the latest-validation timestamp (RP-3
    as-of #2) live on ``sources``; a document/transcript inherits them from its
    parent source so each unit row reports the validation + archive state that
    actually governs it.
    """
    meta: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        "SELECT source_id, scan_date, last_validated_utc, archive_url, archive_status "
        "FROM sources"
    ):
        meta[r["source_id"]] = {
            "scan_date": r["scan_date"],
            "last_validated_utc": r["last_validated_utc"],
            "archive_url": r["archive_url"],
            "archive_status": r["archive_status"],
        }
    return meta


def _archive_for(meta: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not meta:
        return False, _ARCHIVE_NOT_CHECKED
    return bool(meta.get("archive_url")), meta.get("archive_status") or _ARCHIVE_NOT_CHECKED


def _document_rows(
    conn: sqlite3.Connection, repo_root: Path, source_meta: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[int]]:
    """RP-1/RP-2 per document, via the engine's reproducibility leg (extend-not-fork).

    Returns the rows and the set of failed document ids (for the source children
    rule, mirroring ``preservation_replay``'s ``bad_document_ids``).
    """
    summary = rp.verify_reproducibility(conn, repo_root=repo_root, object_types=("document",))
    missing = {e["id"] for e in summary["missing"]}     # file absent on disk
    mismatch = {e["id"] for e in summary["mismatch"]}    # file present, bytes drifted
    bad = missing | mismatch
    rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT id, source_id, sha256, local_path, fetch_time_utc FROM documents ORDER BY id"
    ):
        did = r["id"]
        if not (r["sha256"] and r["local_path"]):
            retained, hash_ok, state = False, False, STATE_DEFECT  # no raw locator at all
        elif did in missing:
            retained, hash_ok, state = False, False, STATE_DEFECT
        elif did in mismatch:
            retained, hash_ok, state = True, False, STATE_DEFECT
        else:
            retained, hash_ok, state = True, True, STATE_PRESERVED
        present, status = _archive_for(source_meta.get(r["source_id"]))
        last_validated = (source_meta.get(r["source_id"]) or {}).get("last_validated_utc")
        rows.append(_row(
            {"object_type": "document", "id": did},
            retained=retained, hash_ok=hash_ok,
            first_captured=r["fetch_time_utc"], last_validated=last_validated,
            archive_present=present, archive_status=status, preservation_state=state,
        ))
    return rows, bad


def _transcript_rows(
    conn: sqlite3.Connection, repo_root: Path, source_meta: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """RP-1/RP-2 per transcript, via the engine's text-reconcile leg (extend-not-fork)."""
    summary = rp.reconcile_transcript_text(conn, repo_root=repo_root)
    mismatch = {e["id"] for e in summary["mismatch"]}        # text drifted
    missing_text = {e["id"] for e in summary["missing_text"]}  # hash recorded, text gone
    rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT id, source_id, sha256, full_text, fetch_time_utc FROM transcripts ORDER BY id"
    ):
        tid = r["id"]
        if not r["sha256"]:
            retained, hash_ok, state = bool(r["full_text"]), False, STATE_DEFECT
        elif tid in missing_text:
            retained, hash_ok, state = False, False, STATE_DEFECT
        elif tid in mismatch:
            retained, hash_ok, state = True, False, STATE_DEFECT
        else:
            retained, hash_ok, state = True, True, STATE_PRESERVED
        present, status = _archive_for(source_meta.get(r["source_id"]))
        last_validated = (source_meta.get(r["source_id"]) or {}).get("last_validated_utc")
        rows.append(_row(
            {"object_type": "transcript", "id": tid},
            retained=retained, hash_ok=hash_ok,
            first_captured=r["fetch_time_utc"], last_validated=last_validated,
            archive_present=present, archive_status=status, preservation_state=state,
        ))
    return rows


def _source_rows(
    conn: sqlite3.Connection,
    repo_root: Path,
    source_meta: dict[str, dict[str, Any]],
    bad_document_ids: set[int],
) -> list[dict[str, Any]]:
    """RP-1..RP-4 per source, via the engine's preservation-validity pass (read-only).

    ``validate_sources`` is invoked with ``apply=False`` and ``gap_exceptions=()`` so
    NOTHING is mutated (no status upgrade, no gap row, no commit) — the auditor only
    *reads* the verdict. A seed_only source with no preserved raw and no documented
    exception classifies ``invalid`` => ``defect`` (fail-closed).
    """
    verdict = rp.validate_sources(
        conn, repo_root,
        bad_document_ids=bad_document_ids, apply=False, gap_exceptions=(), run_id=None,
    )
    preserved = set(verdict["preserved"]) | set(verdict["upgraded"])
    documented = set(verdict["exception_documented"])
    rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT source_id, raw_local_path FROM sources ORDER BY source_id"
    ):
        sid = r["source_id"]
        if sid in preserved:
            retained, hash_ok, state = True, True, STATE_PRESERVED
        elif sid in documented:
            retained, hash_ok, state = bool(r["raw_local_path"]), False, STATE_EXCEPTION
        else:
            retained, hash_ok, state = bool(r["raw_local_path"]), False, STATE_DEFECT
        meta = source_meta.get(sid) or {}
        present, status = _archive_for(meta)
        rows.append(_row(
            {"object_type": "source", "source_id": sid},
            retained=retained, hash_ok=hash_ok,
            first_captured=meta.get("scan_date"), last_validated=meta.get("last_validated_utc"),
            archive_present=present, archive_status=status, preservation_state=state,
        ))
    return rows


def audit_raw_before_parse(
    conn: sqlite3.Connection, repo_root: Path = REPO_ROOT
) -> list[dict[str, Any]]:
    """RP-0 read-time: every derived ``raw_text`` row has a hash-verifiable predecessor.

    Calls the existing :func:`raw_preservation.assert_raw_preserved` gate over every
    document whose ``raw_text`` is populated (a derived row). A row whose raw
    predecessor is missing / unhashed / drifted is an ordering violation. Returns the
    violations as ``[{object_type, id}]`` (id only — the gate's message can name a
    raw path, so it is NOT carried into the result that the overlay reports).
    """
    violations: list[dict[str, Any]] = []
    for r in conn.execute("SELECT id FROM documents WHERE raw_text IS NOT NULL ORDER BY id"):
        try:
            rp.assert_raw_preserved(conn, "document", r["id"], repo_root)
        except rp.RawPreservationError:
            violations.append({"object_type": "document", "id": r["id"]})
    return violations


def assert_raw_before_parse_holds(
    conn: sqlite3.Connection, repo_root: Path = REPO_ROOT
) -> bool:
    """RED if any derived row lacks a hash-verifiable raw predecessor (RP-0 guard)."""
    violations = audit_raw_before_parse(conn, repo_root=repo_root)
    if violations:
        raise RawBeforeParseViolation(
            f"raw-before-parse violated for {len(violations)} derived row(s): "
            f"{[v['id'] for v in violations]}"
        )
    return True


def audit_preservation(
    conn: sqlite3.Connection, repo_root: Path = REPO_ROOT
) -> dict[str, Any]:
    """Build the reviewer-internal preservation overlay (contract §3), unswept.

    One ``PreservationStatusRow`` per document, transcript, and source, plus the
    envelope-level audit fingerprint (the manifest ``aggregate_sha256``) and the
    RP-0 ordering verdict. Pure function of the corpus — no mutation, no re-fetch.
    Callers that serve the body over a transport should use
    :func:`build_preservation_overlay`, which adds the ``assert_no_raw_paths`` sweep.
    """
    source_meta = _source_meta(conn)
    doc_rows, bad_docs = _document_rows(conn, repo_root, source_meta)
    tr_rows = _transcript_rows(conn, repo_root, source_meta)
    src_rows = _source_rows(conn, repo_root, source_meta, bad_docs)
    rows = doc_rows + tr_rows + src_rows

    manifest = rp.preservation_manifest(conn)
    defect_count = sum(1 for r in rows if r["preservation_state"] == STATE_DEFECT)
    ordering_ok = not audit_raw_before_parse(conn, repo_root=repo_root)
    return {
        "scope": SCOPE,
        "access": ACCESS,
        # one opaque reviewer-internal fingerprint at the envelope level (§3) — the
        # only place a sha256 may surface; per-unit rows carry hash_ok only.
        "manifest_digest": manifest["aggregate_sha256"],
        "unit_count": len(rows),
        "defect_count": defect_count,
        "raw_before_parse_ok": ordering_ok,
        "preservation_status": rows,
    }


def build_preservation_overlay(
    conn: sqlite3.Connection, repo_root: Path = REPO_ROOT
) -> dict[str, Any]:
    """Assemble the preservation overlay and transport-sweep it (§3 hard rule).

    Routes the assembled body through the existing
    :func:`read_api.assert_no_raw_paths` backstop — the SAME guard every read
    surface uses — so a path / ``.sha256`` / vault-marker leak fails LOUDLY at the
    boundary, not silently downstream. Returns the (unchanged) body on success.
    """
    return read_api.assert_no_raw_paths(audit_preservation(conn, repo_root=repo_root))


def public_preservation_view(record: dict[str, Any]) -> dict[str, Any]:
    """Web-safe projection of a preservation-bearing record (NO new web-safe field).

    Delegates to the existing :func:`publication.to_web_safe` allowlist — the SSOT,
    consumed read-only. For a source record that means only the already-allowlisted,
    already-3.03-cleared ``scan_date`` / ``last_validated_utc`` / ``archive_status`` /
    ``ui_status`` survive; every raw locator / hash / preservation-status column is
    dropped fail-closed (contract §3 last paragraph / §4.2).
    """
    return pub.to_web_safe(record)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3.04 reviewer-internal raw-preservation auditor (GOV-367)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check-ordering", action="store_true",
        help="assert the RP-0 raw-before-parse ordering held over the corpus",
    )
    args = parser.parse_args(argv)

    db.apply_migrations(args.db)
    with db.open_db(args.db) as conn:
        overlay = build_preservation_overlay(conn)
        if args.check_ordering:
            assert_raw_before_parse_holds(conn)
    print(json.dumps(overlay, indent=2, sort_keys=True))
    return 1 if overlay["defect_count"] else 0


if __name__ == "__main__":
    raise SystemExit(_main())
