"""Stage 3.04 raw-preservation read-time auditor (GOV-367) — reviewer-internal overlay.

Implements the GOV-363 contract (``Docs/stage3-04-raw-preservation-contract.md``)
§5 verbatim: a read-time auditor that *proves* the four raw-preservation invariants
(RP-1..RP-4) plus the RP-0 raw-before-parse ordering over the EXISTING Alpine corpus,
emits the reviewer-internal ``PreservationStatusRow`` overlay (§3), and routes it
through the existing ``read_api.assert_no_raw_paths`` transport backstop.

Extend-not-fork (contract §0 / §2): every hash / drift / ordering decision is
delegated to :mod:`raw_preservation` — this module re-implements NONE of it.

* RP-1 raw retained / RP-2 content hash — read off
  :func:`raw_preservation.preservation_replay` (``apply=False``, ``strict=False``),
  whose ``documents`` / ``transcripts`` verdict lists already classify every unit as
  preserved / missing / mismatch / missing-text. The auditor only *reads* that run-log
  result; it does not duplicate the pass (contract §2).
* RP-3 version/as-of — the immutable first-capture timestamp + latest-validation
  timestamp, read directly from the unit's own columns.
* RP-4 archive reference — ``sources.archive_url`` presence + ``archive_status``.
* RP-0 raw-before-parse — :func:`raw_before_parse_violations` re-asserts
  :func:`raw_preservation.assert_raw_preserved` for every derived (``raw_text``
  non-null) document; an empty list is the read-time proof the ordering held.

No-leak boundary (contract §3 / §4), all by construction:

* per-unit rows carry only ``hash_ok: bool`` — the 64-hex ``sha256`` NEVER appears;
* the single aggregate ``preservation_manifest.aggregate_sha256`` surfaces ONLY at the
  feed-envelope level, as one opaque audit fingerprint (not a path, not a per-unit hash);
* no ``raw_local_path`` / ``raw_sha256`` / ``raw_preservation_status`` / ``fetch_time_utc``
  crosses a web-safe projection — :func:`web_safe_preservation_projection` runs through
  :func:`publication.to_web_safe`, fail-closed by the SSOT allowlist (the field is absent)
  and named-unsafe in ``publication.WEB_UNSAFE_FIELDS`` (defense-in-depth);
* the assembled reviewer-internal overlay is swept by
  :func:`read_api.assert_no_raw_paths` (GOV-34 transport backstop) — a FS path /
  ``.sha256`` / vault marker fails LOUDLY at the boundary.

Separate ADDITIVE module (the GOV-347 / GOV-364 precedent): ``publication.py`` and
``read_api.py`` stay 0-diff; the allowlist + transport guard are consumed read-only.

Reviewer-internal lane ONLY (``access: reviewer_internal``); never on a public/card lane.
Pure read-time function of the corpus — no crawl, no re-fetch, no schema/migration, and
the recorded ``sha256`` is NEVER overwritten (GOV-262 absolute drift rule).
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
import publication as pub  # noqa: E402  (read-only: SSOT allowlist / unsafe set, no mutation)
import raw_preservation as rp  # noqa: E402  (the preservation engine — described, never forked)
import read_api  # noqa: E402  (read-only: transport guard, no mutation)

JURISDICTION = "alpine"  # envelope scope (fixed; broader = planned — contract §0)

# §3 vocabulary (frozen). preservation_state is a derived verdict, never the raw
# `raw_preservation_status` column value.
PRESERVED = "preserved"
DEFECT = "defect"
EXCEPTION_DOCUMENTED = "exception_documented"
PRESERVATION_STATES: frozenset[str] = frozenset({PRESERVED, DEFECT, EXCEPTION_DOCUMENTED})

# archive status for a unit that is not itself a source (archive ref is a
# source-level fact — RP-4 column of record is sources.archive_url/archive_status).
_ARCHIVE_NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Per-unit PreservationStatusRow builders (§3 shape)
# ---------------------------------------------------------------------------


def _row(object_type: str, ref_key: str, ref_val: Any, *, retained: bool, hash_ok: bool,
         first_captured: Any, last_validated: Any,
         archive_present: bool, archive_status: str, preservation_state: str) -> dict[str, Any]:
    """One reviewer-internal ``PreservationStatusRow`` (contract §3 shape).

    Carries ``hash_ok`` (the verdict) — never the ``sha256`` value, never a path.
    """
    return {
        "unit_ref": {"object_type": object_type, ref_key: ref_val},
        "retained": retained,                         # RP-1
        "hash_ok": hash_ok,                           # RP-2 (verdict only)
        "as_of": {                                    # RP-3
            "first_captured": first_captured,
            "last_validated": last_validated,
        },
        "archive": {"present": archive_present, "status": archive_status},  # RP-4
        "preservation_state": preservation_state,
    }


def _document_rows(conn: sqlite3.Connection, replay: dict[str, Any]) -> list[dict[str, Any]]:
    """Document rows, aligned to the engine's ``verify_reproducibility`` scope.

    ``missing`` ⇒ artifact gone (RP-1 fail). ``mismatch`` ⇒ present but tampered
    (RP-2 fail). Otherwise preserved. The enumeration mirrors the engine's WHERE
    clause exactly so the defect lists line up unit-for-unit.
    """
    missing = {e["id"] for e in replay["documents"]["missing"]}
    mismatch = {e["id"] for e in replay["documents"]["mismatch"]}
    rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT id, fetch_time_utc FROM documents "
        "WHERE sha256 IS NOT NULL AND local_path IS NOT NULL ORDER BY id"
    ):
        did = r["id"]
        is_missing = did in missing
        is_mismatch = did in mismatch
        retained = not is_missing
        hash_ok = not (is_missing or is_mismatch)
        captured = r["fetch_time_utc"]
        rows.append(_row(
            "document", "id", did,
            retained=retained, hash_ok=hash_ok,
            first_captured=captured, last_validated=captured,
            archive_present=False, archive_status=_ARCHIVE_NOT_APPLICABLE,
            preservation_state=PRESERVED if hash_ok else DEFECT,
        ))
    return rows


def _transcript_rows(conn: sqlite3.Connection, replay: dict[str, Any]) -> list[dict[str, Any]]:
    """Transcript rows, aligned to the engine's ``reconcile_transcript_text`` scope.

    ``missing_text`` ⇒ no preserved text (RP-1 fail). ``mismatch`` ⇒ text drifted
    (RP-2 fail). Otherwise preserved.
    """
    missing_text = {e["id"] for e in replay["transcripts"]["missing_text"]}
    mismatch = {e["id"] for e in replay["transcripts"]["mismatch"]}
    rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT id, fetch_time_utc FROM transcripts WHERE sha256 IS NOT NULL ORDER BY id"
    ):
        tid = r["id"]
        is_missing = tid in missing_text
        is_mismatch = tid in mismatch
        retained = not is_missing
        hash_ok = not (is_missing or is_mismatch)
        captured = r["fetch_time_utc"]
        rows.append(_row(
            "transcript", "id", tid,
            retained=retained, hash_ok=hash_ok,
            first_captured=captured, last_validated=captured,
            archive_present=False, archive_status=_ARCHIVE_NOT_APPLICABLE,
            preservation_state=PRESERVED if hash_ok else DEFECT,
        ))
    return rows


def _source_verdict_index(replay: dict[str, Any]) -> dict[str, tuple[str, bool, bool]]:
    """``{source_id: (preservation_state, retained, hash_ok)}`` from the engine verdict.

    Derived from ``validate_sources`` buckets — never a re-hash here:

    * ``preserved`` / ``upgraded`` (raw valid by own bytes or by all children) ⇒
      ``preserved`` (the audit reports the preservation truth, not the status label);
    * ``exception_documented`` ⇒ a deliberate ``no_primary_source`` exception — the
      absence is an explicit recorded state, not a silent gap;
    * ``invalid`` ⇒ ``defect``; ``retained`` reflects whether the bytes are on disk
      (``own_state in {valid, defect}``) vs absent (``missing``/``none``).
    """
    index: dict[str, tuple[str, bool, bool]] = {}
    for sid in replay["sources"]["preserved"]:
        index[sid] = (PRESERVED, True, True)
    for sid in replay["sources"]["upgraded"]:
        index[sid] = (PRESERVED, True, True)
    for sid in replay["sources"]["exception_documented"]:
        index[sid] = (EXCEPTION_DOCUMENTED, False, False)
    for entry in replay["sources"]["invalid"]:
        own = entry.get("own_state")
        retained = own in ("valid", "defect")  # bytes present on disk but bad
        index[entry["source_id"]] = (DEFECT, retained, False)
    return index


def _source_rows(conn: sqlite3.Connection, replay: dict[str, Any]) -> list[dict[str, Any]]:
    """Source rows: as-of + archive read directly from the source's own columns.

    ``scan_date`` (immutable as-of #1) + ``last_validated_utc`` (as-of #2) are
    already public-safe (3.03). ``archive_url`` is read only to compute the boolean
    ``present`` — the URL string itself never enters the row.
    """
    verdict = _source_verdict_index(replay)
    rows: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT source_id, scan_date, last_validated_utc, archive_url, archive_status "
        "FROM sources ORDER BY source_class, source_id"
    ):
        sid = r["source_id"]
        state, retained, hash_ok = verdict.get(sid, (DEFECT, False, False))
        captured = r["scan_date"]
        archive_present = bool(r["archive_url"])
        archive_status = r["archive_status"] or ("present" if archive_present else "not_checked")
        rows.append(_row(
            "source", "source_id", sid,
            retained=retained, hash_ok=hash_ok,
            first_captured=captured,
            last_validated=r["last_validated_utc"] or captured,
            archive_present=archive_present, archive_status=archive_status,
            preservation_state=state,
        ))
    return rows


def _rows_from_replay(conn: sqlite3.Connection, replay: dict[str, Any]) -> list[dict[str, Any]]:
    return (
        _document_rows(conn, replay)
        + _transcript_rows(conn, replay)
        + _source_rows(conn, replay)
    )


def _replay(conn: sqlite3.Connection, repo_root: Path) -> dict[str, Any]:
    """Read the engine's preservation-replay verdict (dry-run, non-raising).

    ``apply=False`` ⇒ no source-status mutation; ``strict=False`` ⇒ a failed corpus
    reports rather than raises (the auditor surfaces defects, never aborts the read).
    The append-only ``crawl_runs`` audit row the engine writes is intended Lane-1
    behavior, not a corpus mutation (contract §2).
    """
    return rp.preservation_replay(conn, repo_root=repo_root, apply=False, strict=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_unit_rows(conn: sqlite3.Connection, repo_root: Path = rp.REPO_ROOT) -> list[dict[str, Any]]:
    """Per-unit ``PreservationStatusRow`` list over the corpus (RP-1..RP-4)."""
    return _rows_from_replay(conn, _replay(conn, repo_root))


def raw_before_parse_violations(
    conn: sqlite3.Connection, repo_root: Path = rp.REPO_ROOT
) -> list[dict[str, Any]]:
    """RP-0: derived (``raw_text`` non-null) rows whose raw predecessor fails the gate.

    Re-asserts :func:`raw_preservation.assert_raw_preserved` per derived document; an
    empty list is the read-time proof that no derived row exists without a
    hash-verifiable raw predecessor. Read-only — it never re-gates ingestion.
    """
    violations: list[dict[str, Any]] = []
    for r in conn.execute("SELECT id FROM documents WHERE raw_text IS NOT NULL ORDER BY id"):
        did = r["id"]
        try:
            rp.assert_raw_preserved(conn, "document", did, repo_root=repo_root)
        except rp.RawPreservationError:
            violations.append({"object_type": "document", "id": did})
    return violations


def build_preservation_overlay(
    conn: sqlite3.Connection, repo_root: Path = rp.REPO_ROOT
) -> dict[str, Any]:
    """Assemble the reviewer-internal preservation overlay and transport-sweep it.

    Envelope shape: ``{scope, access, replay_status, manifest_digest, unit_count,
    raw_before_parse_ok, units[]}``. ``manifest_digest`` is the single opaque
    aggregate fingerprint (envelope-level only — contract §3). The whole body is
    swept by :func:`read_api.assert_no_raw_paths` (GOV-34 backstop), so a path /
    ``.sha256`` / vault marker fails LOUDLY at the boundary.
    """
    replay = _replay(conn, repo_root)
    overlay: dict[str, Any] = {
        "scope": JURISDICTION,
        "access": "reviewer_internal",  # never "public" — contract §4.1
        "replay_status": replay["status"],
        "manifest_digest": replay["manifest"]["aggregate_sha256"],
        "unit_count": replay["manifest"]["unit_count"],
        "raw_before_parse_ok": not raw_before_parse_violations(conn, repo_root),
        "units": _rows_from_replay(conn, replay),
    }
    return read_api.assert_no_raw_paths(overlay)


def web_safe_preservation_projection(record: dict[str, Any]) -> dict[str, Any]:
    """Public/web-safe projection of preservation for a source-like record (§3).

    Surfaces ONLY the already-allowlisted, 3.03-cleared fields — ``scan_date`` /
    ``last_validated_utc`` / ``archive_status`` plus the computed ``ui_status`` (which
    already folds ``rawPreserved`` into ``compute_ui_status`` rules #3/#10). Every raw
    metadata field (``raw_local_path`` / ``raw_sha256`` / ``raw_preservation_status`` /
    ``fetch_time_utc``) is dropped — fail-closed by :func:`publication.to_web_safe`'s
    allowlist (the field is absent) and named-unsafe (defense-in-depth). No new
    web-safe field is introduced by 3.04.
    """
    ui_status = pub.compute_ui_status({
        "verificationStatus": record.get("verification_status"),
        "correctionStatus": record.get("correction_status"),
        "sourceChanged": bool(record.get("source_changed")),
        "sourcePresent": bool(record.get("url") or record.get("original_url")),
        "archivePresent": record.get("archive_status") == "available",
        "rawPreserved": record.get("raw_preservation_status") in rp.PRESERVED_STATES,
    })
    return pub.to_web_safe({**record, "ui_status": ui_status})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3.04 reviewer-internal raw-preservation audit overlay (GOV-367)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        overlay = build_preservation_overlay(conn)
    print(json.dumps(overlay, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
