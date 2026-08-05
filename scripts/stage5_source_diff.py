"""Late-change detector + structured before/after source diff (GOV-1685, Stage 5
R1/Slice 2, home slot 5.16).

The deterministic, **no-model-in-the-loop** consumer of Slice 1's preserved
version pair (:mod:`source_version_store`, migration 0033). It answers the one
question the rest of the chain reprocesses against: *given two preserved versions
of the SAME crawled civic source URL, what changed, where, and is it material?*
Nothing downstream (affected-set resolver → six-lens rerun) can run until the
change between two preserved versions is DETECTED and STRUCTURED (migration 0034).

Two artifacts, both computed in code:

* a **detected change** — one header row per version pair
  (``source_version_changes``) binding the pair, the stable ``change_hash`` of the
  whole structured diff (the reproducibility anchor), and the deterministic
  lateness verdict;
* a **structured diff** — one or more anchored SEGMENT rows
  (``source_version_diff_segments``), each anchored to a civic locator in the
  CLOSED set :data:`ANCHOR_TYPES` with its ``{before, after, materiality_reason}``.
  A raw text blob is never stored.

Determinism law (Directive 7 / slot .09). Every fact this module derives is
computed in code, never by a model:

* the change **trigger** is a ``content_hash`` comparison of the two version rows
  — identical hashes are **no detection** (consistent with Slice 1's
  no-op-on-unchanged), a differing hash is a detected change;
* the structured diff is a set/dict comparison of the two versions' anchored
  content;
* ``materiality_reason`` is derived by an ordered predicate over the changed
  field-set (:func:`derive_materiality`); the closed material-field vocabulary is
  :data:`MATERIAL_FIELDS`;
* the **lateness** verdict (A2's "viewed/notified after retrieval" +
  meeting-proximity trigger) is derived by :func:`derive_lateness`;
* the change/segment ids and ``change_hash`` are content-addressed (no randomness,
  no wall-clock in the hashed part), so the same version pair reproduces a
  byte-identical artifact.

Fail-closed house style:

* an unknown ``anchor_type`` is **rejected** (:class:`UnknownAnchorType`), never
  stored — matched by the DB-level CHECK as a backstop;
* a change whose structured content the caller cannot localize (or that differs
  in bytes but not in the supplied structured content — a lossy extraction) is
  **flagged**, never silently dropped: it becomes one ``attachment``-anchored
  segment with materiality :data:`UNDIFFABLE`;
* a stored ``snapshot_path`` read while building the audit record is
  containment-checked (:func:`raw_preservation._contained`, which **raises** on an
  absolute/``..``-escaping value) and surfaced only as the boolean
  ``snapshotPreserved`` — never the path string;
* re-diffing an already-detected pair is a **no-op** (the ``change_hash`` must
  match); a mismatch means non-determinism and is refused.

Concurrency. The read-decide-write ("is this pair already detected?") runs under
an explicit ``BEGIN IMMEDIATE`` before the first read, per ``CLAUDE.md``; the
``UNIQUE (old_version_id, new_version_id)`` constraint is the DB-level backstop.

Scope. This slice stops at *change detected + structured diff artifact anchored +
materiality reason recorded + present in the audit record.* The affected-set
resolver / selective invalidation (Slice 3), the six-lens rerun (Slice 4), the
completion-state machine (Slice 5), and any render/audit-visibility SURFACE
(Slice 7) are explicitly other tickets. This module makes the facts **present in
the audit record**; surfacing them is Slice 7.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import raw_preservation  # noqa: E402  (reused: contained-path guard)
import read_api  # noqa: E402  (reused read-only: transport sweep)
import source_version_store as svs  # noqa: E402  (reused read-only: version rows/vocab)

REPO_ROOT = SCRIPTS.parent

SCOPE = "alpine"  # fixed; broader = planned/gated
ACCESS = "reviewer_internal"  # never "public" — the audit record exposes content hashes

# The CLOSED civic-locator vocabulary a diff segment may anchor to. A frozenset so
# any future value is a conscious, reviewed change — never drift. Mirrors the DB
# CHECK on source_version_diff_segments.anchor_type exactly.
ANCHOR_PAGE = "page"
ANCHOR_SECTION = "section"
ANCHOR_AGENDA_ITEM = "agenda_item"
ANCHOR_MEETING = "meeting"
ANCHOR_ATTACHMENT = "attachment"
ANCHOR_TYPES: frozenset[str] = frozenset(
    {ANCHOR_PAGE, ANCHOR_SECTION, ANCHOR_AGENDA_ITEM, ANCHOR_MEETING, ANCHOR_ATTACHMENT}
)

# The CLOSED set of civic-significant fields. A change to any of these at an anchor
# is MATERIAL; a change touching none is still flagged (fail-closed) but tagged
# non-material. Kept deliberately small and reviewed — widening it silently
# redefines "material" across the whole change-detection surface.
MATERIAL_FIELDS: frozenset[str] = frozenset(
    {
        "date",
        "time",
        "datetime",
        "location",
        "title",
        "subject",
        "vote",
        "decision",
        "outcome",
        "status",
        "hearing",
        "deadline",
        "amount",
        "ordinance",
        "resolution",
    }
)

# Materiality reason tokens (a closed vocabulary; the segment stores one).
ANCHOR_ADDED = "anchor_added"
ANCHOR_REMOVED = "anchor_removed"
MATERIAL_FIELD_CHANGE = "material_field_change"  # suffixed with :<sorted,fields>
NONMATERIAL_CHANGE = "nonmaterial_change_flagged"
UNDIFFABLE = "undiffable_change_flagged"  # bytes differ, structure could not localize it

# Lateness basis tokens (a closed vocabulary; the header stores one or NULL).
LATE_AFTER_NOTIFIED = "changed_after_prior_notified"
LATE_MEETING_PROXIMITY = "within_meeting_proximity_window"

# How close to a meeting a change must land to count as a late red-flag. Named so
# the fires/does-not-fire boundary is one reviewed number, not a magic literal.
MEETING_PROXIMITY_WINDOW = timedelta(hours=48)


class SourceDiffError(Exception):
    """A late-change/diff request that fail-closed refuses."""


class UnknownAnchorType(SourceDiffError):
    """An anchor_type outside the closed :data:`ANCHOR_TYPES` set."""


class SourceDiffAuditError(AssertionError):
    """An emitted audit record violates a Slice-2 invariant (contract guard)."""


# ---------------------------------------------------------------------------
# Deterministic primitives (pure — no DB, no network, no model)
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _canonical(obj: Any) -> str:
    """Canonical JSON — sorted keys, no whitespace. The one serialization used for
    every content hash so the same value always hashes identically."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _change_id(old_version_id: str, new_version_id: str) -> str:
    """Deterministic, content-addressed change id from the version pair."""
    digest = hashlib.sha256(
        f"{old_version_id}\n{new_version_id}".encode("utf-8")
    ).hexdigest()
    return f"srcchg:{digest[:24]}"


def _segment_id(change_id: str, anchor_type: str, anchor_ref: str) -> str:
    """Deterministic, content-addressed segment id (unique within a change)."""
    digest = hashlib.sha256(
        f"{change_id}\n{anchor_type}\n{anchor_ref}".encode("utf-8")
    ).hexdigest()
    return f"srcseg:{digest[:24]}"


def diff_change_hash(segments: list[dict[str, Any]]) -> str:
    """sha256 over the canonical ordered structured diff — the reproducibility
    anchor (AC-3). Depends only on the segments' content and order, so the same
    version pair yields a byte-identical hash. The wall-clock ``detected_utc`` is
    deliberately NOT part of it."""
    payload = [
        {
            "anchor_type": s["anchor_type"],
            "anchor_ref": s["anchor_ref"],
            "before": s["before"],
            "after": s["after"],
            "materiality_reason": s["materiality_reason"],
        }
        for s in segments
    ]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _parse_ts(value: str, *, label: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp; a naive value is assumed UTC. Fail-closed on
    an unparseable value rather than guessing."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SourceDiffError(f"{label} is not a parseable ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def derive_materiality(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """The ordered materiality predicate over a changed anchor (pure, in code).

    * anchor **added** (no before) -> ``anchor_added``;
    * anchor **removed** (no after) -> ``anchor_removed``;
    * a field in :data:`MATERIAL_FIELDS` changed -> ``material_field_change:<f1,f2>``
      (the changed material fields, sorted, so the token is deterministic);
    * only non-material fields changed -> ``nonmaterial_change_flagged`` (still
      flagged: fail-closed, never dropped).

    Never returns "no change" — the caller only invokes this on an anchor that
    actually differs.
    """
    if before is None:
        return ANCHOR_ADDED
    if after is None:
        return ANCHOR_REMOVED
    changed = {
        key
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    }
    material = sorted(changed & MATERIAL_FIELDS)
    if material:
        return f"{MATERIAL_FIELD_CHANGE}:{','.join(material)}"
    return NONMATERIAL_CHANGE


def derive_lateness(
    *,
    new_retrieval_time: str,
    meeting_time: str | None = None,
    notified_after: str | None = None,
) -> dict[str, Any]:
    """The deterministic lateness verdict for a detected change (pure, in code).

    A2's late-change red flag, two ordered rules — the first that fires wins:

    1. the source changed **after users were notified of the prior version**
       (``notified_after`` given and the new retrieval is later) ->
       :data:`LATE_AFTER_NOTIFIED`;
    2. the new version was retrieved **within :data:`MEETING_PROXIMITY_WINDOW` of
       (or after) the meeting** -> :data:`LATE_MEETING_PROXIMITY`.

    Neither fires (or neither input given) -> not late. Returns
    ``{"late": bool, "basis": str | None}``.
    """
    retrieved = _parse_ts(new_retrieval_time, label="new_retrieval_time")
    if notified_after is not None:
        notified = _parse_ts(notified_after, label="notified_after")
        if retrieved > notified:
            return {"late": True, "basis": LATE_AFTER_NOTIFIED}
    if meeting_time is not None:
        meeting = _parse_ts(meeting_time, label="meeting_time")
        if retrieved >= meeting - MEETING_PROXIMITY_WINDOW:
            return {"late": True, "basis": LATE_MEETING_PROXIMITY}
    return {"late": False, "basis": None}


def _normalize_content(content: Any, *, label: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Normalize supplied structured version content into ``{(anchor_type,
    anchor_ref): fields}``, validating the anchor vocabulary (fail-closed).

    Accepts a list of ``{anchor_type, anchor_ref, fields}`` units — the
    deterministic, upstream-extracted structured view of one version. ``None`` is
    an empty view (used for the fail-closed fallback). An unknown ``anchor_type``
    is refused (:class:`UnknownAnchorType`); a duplicate ``(anchor_type,
    anchor_ref)`` within one view is ambiguous and refused.
    """
    if content is None:
        return {}
    if not isinstance(content, (list, tuple)):
        raise SourceDiffError(f"{label} must be a list of anchored content units")
    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    for unit in content:
        if not isinstance(unit, dict):
            raise SourceDiffError(f"{label} unit must be a dict, got {type(unit).__name__}")
        anchor_type = unit.get("anchor_type")
        anchor_ref = unit.get("anchor_ref")
        if anchor_type not in ANCHOR_TYPES:
            raise UnknownAnchorType(
                f"anchor_type {anchor_type!r} is not in the closed set "
                f"{sorted(ANCHOR_TYPES)} — refusing"
            )
        if not anchor_ref or not isinstance(anchor_ref, str):
            raise SourceDiffError(f"{label} unit needs a non-empty string anchor_ref")
        fields = unit.get("fields", {})
        if not isinstance(fields, dict):
            raise SourceDiffError(f"{label} unit 'fields' must be a dict")
        key = (anchor_type, anchor_ref)
        if key in normalized:
            raise SourceDiffError(f"{label} has a duplicate anchor {key!r} — ambiguous")
        normalized[key] = fields
    return normalized


def compute_segments(old_content: Any, new_content: Any) -> list[dict[str, Any]]:
    """The structured, anchored diff over two versions' content (pure, in code).

    One segment per anchor that DIFFERS, in deterministic ``(anchor_type,
    anchor_ref)`` order with 1-based ordinals. Each segment carries
    ``{anchor_type, anchor_ref, before, after, materiality_reason, segment_ordinal}``.
    An anchor present-and-equal in both versions produces no segment. Validates the
    anchor vocabulary (fail-closed) via :func:`_normalize_content`.
    """
    old_map = _normalize_content(old_content, label="old_content")
    new_map = _normalize_content(new_content, label="new_content")
    segments: list[dict[str, Any]] = []
    for key in sorted(set(old_map) | set(new_map)):
        anchor_type, anchor_ref = key
        before = old_map.get(key)
        after = new_map.get(key)
        if before == after:
            continue  # present in both, unchanged
        segments.append(
            {
                "anchor_type": anchor_type,
                "anchor_ref": anchor_ref,
                "before": before,
                "after": after,
                "materiality_reason": derive_materiality(before, after),
            }
        )
    for ordinal, segment in enumerate(segments, start=1):
        segment["segment_ordinal"] = ordinal
    return segments


def _undiffable_segment(source_url: str, old_hash: str, new_hash: str) -> dict[str, Any]:
    """The fail-closed fallback: bytes differ but the change could not be localized
    into structured anchors. Flagged as one ``attachment``-anchored segment, never
    dropped."""
    return {
        "anchor_type": ANCHOR_ATTACHMENT,
        "anchor_ref": source_url,
        "before": {"contentHash": old_hash},
        "after": {"contentHash": new_hash},
        "materiality_reason": UNDIFFABLE,
        "segment_ordinal": 1,
    }


# ---------------------------------------------------------------------------
# DB-facing: detect + store, load the audit record
# ---------------------------------------------------------------------------


def _load_version(conn: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT version_id, source_id, source_url, retrieval_time, content_hash, "
        "provenance, snapshot_path, version_ordinal FROM source_versions "
        "WHERE version_id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        raise SourceDiffError(f"source_versions row {version_id!r} not found")
    return dict(row)


def _latest_two(conn: sqlite3.Connection, source_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT version_id, source_url, retrieval_time, content_hash, "
            "version_ordinal FROM source_versions WHERE source_url = ? "
            "ORDER BY version_ordinal DESC LIMIT 2",
            (source_url,),
        )
    ]
    if len(rows) < 2:
        raise SourceDiffError(
            f"{source_url!r} has fewer than two preserved versions — nothing to diff"
        )
    newer, older = rows[0], rows[1]  # DESC: [0] is the newer
    return older, newer


def detect_and_store(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    old_version_id: str | None = None,
    new_version_id: str | None = None,
    old_content: Any = None,
    new_content: Any = None,
    meeting_time: str | None = None,
    notified_after: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Detect the change between a preserved version pair and store the structured
    diff artifact. Deterministic and idempotent.

    * **No detection** — the two versions share a ``content_hash`` (identical
      content): returns ``{"detected": False, ...}``; nothing is stored (AC-1,
      consistent with Slice 1's no-op-on-unchanged).
    * **Detected / first** — differing ``content_hash``: computes the structured
      anchored diff, derives the lateness verdict, and stores exactly one change
      header + its segments, bound to both version rows by id (AC-1/AC-2/AC-4).
    * **Detected / repeat** — the pair was already detected: a **no-op**, and the
      recomputed ``change_hash`` must match the stored one (else non-determinism is
      refused). No duplicate row/artifact (AC-3).

    When ``old_version_id``/``new_version_id`` are omitted the two latest versions
    of ``source_url`` are used. If structured content cannot localize a differing
    pair, the fail-closed ``attachment`` fallback segment is stored (never dropped).
    """
    if old_version_id is not None and new_version_id is not None:
        older = _load_version(conn, old_version_id)
        newer = _load_version(conn, new_version_id)
        if older["source_url"] != source_url or newer["source_url"] != source_url:
            raise SourceDiffError("both versions must belong to source_url")
        if older["version_id"] == newer["version_id"]:
            raise SourceDiffError("a version cannot diff against itself")
        if older["version_ordinal"] >= newer["version_ordinal"]:
            raise SourceDiffError(
                "old_version_id must be the prior (lower-ordinal) version; "
                "the before/after orientation is not ambiguous"
            )
    elif old_version_id is None and new_version_id is None:
        older, newer = _latest_two(conn, source_url)
    else:
        raise SourceDiffError("pass both version ids or neither")

    if older["content_hash"] == newer["content_hash"]:
        return {
            "detected": False,
            "reason": "identical_content_hash",
            "source_url": source_url,
            "old_version_id": older["version_id"],
            "new_version_id": newer["version_id"],
        }

    segments = compute_segments(old_content, new_content)
    if not segments:
        # Bytes differ but the supplied structured content did not localize it —
        # flag, never drop (fail-closed).
        segments = [
            _undiffable_segment(source_url, older["content_hash"], newer["content_hash"])
        ]

    lateness = derive_lateness(
        new_retrieval_time=newer["retrieval_time"],
        meeting_time=meeting_time,
        notified_after=notified_after,
    )
    change_id = _change_id(older["version_id"], newer["version_id"])
    change_hash = diff_change_hash(segments)
    detected_utc = now or _now_utc_iso()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT change_id, change_hash FROM source_version_changes WHERE change_id = ?",
            (change_id,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            if existing["change_hash"] != change_hash:
                raise SourceDiffError(
                    f"change {change_id} already stored with a DIFFERENT diff hash — "
                    "the diff is not reproducible; refusing to overwrite history"
                )
            return {
                "detected": True,
                "action": "noop",
                "change_id": change_id,
                "change_hash": change_hash,
                "late_change": bool(lateness["late"]),
                "lateness_basis": lateness["basis"],
                "segments": segments,
            }

        conn.execute(
            "INSERT INTO source_version_changes (change_id, source_url, "
            "old_version_id, new_version_id, change_hash, late_change, "
            "lateness_basis, detected_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                change_id,
                source_url,
                older["version_id"],
                newer["version_id"],
                change_hash,
                1 if lateness["late"] else 0,
                lateness["basis"],
                detected_utc,
            ),
        )
        for segment in segments:
            conn.execute(
                "INSERT INTO source_version_diff_segments (segment_id, change_id, "
                "anchor_type, anchor_ref, before_detail, after_detail, "
                "materiality_reason, segment_ordinal) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _segment_id(change_id, segment["anchor_type"], segment["anchor_ref"]),
                    change_id,
                    segment["anchor_type"],
                    segment["anchor_ref"],
                    _canonical(segment["before"]),
                    _canonical(segment["after"]),
                    segment["materiality_reason"],
                    segment["segment_ordinal"],
                ),
            )
        conn.commit()
        return {
            "detected": True,
            "action": "created",
            "change_id": change_id,
            "change_hash": change_hash,
            "late_change": bool(lateness["late"]),
            "lateness_basis": lateness["basis"],
            "segments": segments,
        }
    except Exception:
        conn.rollback()
        raise


def _version_citation(conn: sqlite3.Connection, version_id: str, repo_root: Path) -> dict[str, Any]:
    """A reviewer-internal source citation for one version. The raw ``snapshot_path``
    is read only to CONFIRM preservation (containment-checked, AC-6) and surfaced
    as the boolean ``snapshotPreserved`` — never the path string."""
    row = _load_version(conn, version_id)
    snapshot_path = row.get("snapshot_path")
    if snapshot_path and isinstance(snapshot_path, str):
        # Raises RawPathEscape on an absolute/`..`-escaping value — fail-closed at
        # the read site (Path(root)/value silently discards root when absolute).
        contained = raw_preservation._contained(repo_root, snapshot_path)
        snapshot_preserved = contained.exists()
    else:
        snapshot_preserved = False
    return {
        "versionId": row["version_id"],
        "versionOrdinal": row["version_ordinal"],
        "retrievalTime": row["retrieval_time"],
        "contentHash": row["content_hash"],
        "snapshotPreserved": snapshot_preserved,
    }


def build_audit_record(
    conn: sqlite3.Connection, change_id: str, repo_root: Path = REPO_ROOT
) -> dict[str, Any]:
    """Assemble the reviewer-internal audit record for a detected change (AC-5).

    Makes present-in-the-audit-record — *rendering is Slice 7* — every fact D-3
    requires of the backend half: the materiality reason(s), the before/after
    change detail, and the source citations for BOTH versions, plus the lateness
    verdict and the change/reprocessing binding. Fixed at ``reviewer_internal`` /
    ``alpine`` (it exposes content hashes) and swept by
    :func:`read_api.assert_no_raw_paths` as a transport backstop.
    """
    change = conn.execute(
        "SELECT change_id, source_url, old_version_id, new_version_id, change_hash, "
        "late_change, lateness_basis, detected_utc FROM source_version_changes "
        "WHERE change_id = ?",
        (change_id,),
    ).fetchone()
    if change is None:
        raise SourceDiffError(f"no detected change {change_id!r}")
    change = dict(change)

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT anchor_type, anchor_ref, before_detail, after_detail, "
            "materiality_reason, segment_ordinal FROM source_version_diff_segments "
            "WHERE change_id = ? ORDER BY segment_ordinal ASC",
            (change_id,),
        )
    ]
    segments: list[dict[str, Any]] = []
    materiality_reasons: list[str] = []
    for row in rows:
        materiality_reasons.append(row["materiality_reason"])
        segments.append(
            {
                "anchorType": row["anchor_type"],
                "anchorRef": row["anchor_ref"],
                "before": json.loads(row["before_detail"]),
                "after": json.loads(row["after_detail"]),
                "materialityReason": row["materiality_reason"],
            }
        )

    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public"
        "change": {
            "changeId": change["change_id"],
            "sourceUrl": change["source_url"],
            "oldVersionId": change["old_version_id"],
            "newVersionId": change["new_version_id"],
            "changeHash": change["change_hash"],
            "detectedAt": change["detected_utc"],
        },
        "lateness": {
            "lateChange": bool(change["late_change"]),
            "latenessBasis": change["lateness_basis"],
        },
        "materialityReasons": materiality_reasons,
        "segments": segments,
        "citations": {
            "old": _version_citation(conn, change["old_version_id"], repo_root),
            "new": _version_citation(conn, change["new_version_id"], repo_root),
        },
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# Contract guards (load-bearing, non-tautological — cross-check the EMITTED body)
# ---------------------------------------------------------------------------


def assert_anchor_types_valid(record: dict[str, Any]) -> bool:
    """RED if any emitted segment anchors to a type outside :data:`ANCHOR_TYPES`."""
    for segment in record.get("segments", []):
        anchor_type = segment.get("anchorType")
        if anchor_type not in ANCHOR_TYPES:
            raise SourceDiffAuditError(
                f"segment anchorType {anchor_type!r} outside the closed set "
                f"{sorted(ANCHOR_TYPES)}"
            )
    return True


def assert_audit_record_complete(record: dict[str, Any]) -> bool:
    """RED unless D-3's backend facts are all present (AC-5).

    A real cross-check on the EMITTED body: at least one segment; every segment
    carries a non-null ``before``/``after``/``materialityReason``; a materiality
    reason is recorded; and BOTH version citations are present with a version id
    and content hash. A build that dropped any of these goes RED.
    """
    segments = record.get("segments", [])
    if not segments:
        raise SourceDiffAuditError("audit record carries no diff segment")
    if not record.get("materialityReasons"):
        raise SourceDiffAuditError("audit record carries no materiality reason")
    for segment in segments:
        if segment.get("before") is None and segment.get("after") is None:
            raise SourceDiffAuditError("a segment has neither before nor after detail")
        if not segment.get("materialityReason"):
            raise SourceDiffAuditError("a segment has no materiality reason")
    citations = record.get("citations", {})
    for side in ("old", "new"):
        cite = citations.get(side)
        if not cite or not cite.get("versionId") or not cite.get("contentHash"):
            raise SourceDiffAuditError(f"missing {side}-version source citation")
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.16 late-change / structured-diff audit record (GOV-1685)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--change-id", required=True, help="a detected change_id to audit")
    parser.add_argument(
        "--check", action="store_true", help="run the anchor + completeness guards"
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        body = build_audit_record(conn, args.change_id)
        if args.check:
            assert_anchor_types_valid(body)
            assert_audit_record_complete(body)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
