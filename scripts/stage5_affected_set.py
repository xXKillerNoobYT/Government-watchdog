"""Affected-set resolver + selective invalidation + statement/evidence<->diff
binding (GOV-1688, Stage 5 R1/Slice 3, home slots 5.05 + 5.07, requirement A3).

The deterministic, **no-model-in-the-loop** consumer of Slice 2's structured diff
(:mod:`stage5_source_diff`, migration 0034). Slice 2 answered *what changed, where,
and is it material?*; this slice answers the next question the reprocessing chain
cannot skip: *given that anchored change, WHICH canonical records does it
invalidate?* Nothing downstream (six-lens rerun, Slice 4) can run until the diff's
affected set is resolved and only those records are marked for reprocessing.

Two artifacts, both computed in code and stored in ONE ledger
(``source_change_affected_records``, migration 0035):

* the **affected set** — for each diff segment, the canonical records whose civic
  locator matches the segment's ``(anchor_type, anchor_ref)``. The affected
  classes are exactly the A3 list :data:`RECORD_CLASSES`; a record at an anchor no
  segment touched is **not** in the set;
* the **selective invalidation marker + the D-1 binding** — one ledger row per
  affected record. The row's EXISTENCE is the invalidation marker; because it
  carries both ``change_id`` and ``segment_id`` it is simultaneously the binding a
  single join walks: ``source -> source_versions -> source_version_changes ->
  source_version_diff_segments -> this ledger -> the affected statement/evidence``.

Determinism law (Directive 7 / slot .09). Every fact this module derives is
computed in code, never by a model:

* anchor->record matching is a fixed registry of SQL locators
  (:data:`RESOLVER_RULES`), one per ``(anchor_type, record_class)`` that carries a
  deterministic civic-locator column today;
* the affected set is the union of those matches over the change's segments, in a
  stable ``(segment_ordinal, record_class, record_id)`` order;
* the ``affected_id`` is content-addressed from
  ``(change_id, segment_id, record_class, record_id)`` — no randomness, no
  wall-clock in the hashed part, so the same diff + same canonical state reproduce
  a byte-identical affected set (AC-5).

Fail-closed house style:

* an unknown ``anchor_type`` reaching the resolver is **refused**
  (:class:`stage5_source_diff.UnknownAnchorType`) — never silently skipped;
* a segment whose anchor localizes **no** concrete record is **flagged**, never
  dropped: it gets one ``unresolved`` sentinel row (``resolution =
  'unresolved_flagged'``) so the segment is provably present in the ledger. An
  unresolvable/ambiguous anchor widens the set with a flag — it never shrinks it;
* invalidation is idempotent: re-running ``resolve+invalidate`` on the same change
  inserts no new row and re-marks nothing (``UNIQUE`` + ``INSERT OR IGNORE``);
* Slice 3 NEVER overwrites canonical content and NEVER runs a rerun — every
  statements/evidence/normalization/review row it points at is left byte-identical.
  Overwriting is monotonicity (Slice 6); rerunning is Slice 4.

Scope. This slice stops at *affected set resolved (anchored) + only-affected
records selectively invalidated (idempotent) + statement/evidence<->diff binding
recorded.* The six-lens rerun (Slice 4), the completion-state machine (Slice 5),
the monotonicity axis (Slice 6), and any render/audit surface (Slice 7) are
explicitly other tickets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import stage5_source_diff as sd  # noqa: E402  (reused: closed anchor set + error type)

SCOPE = "alpine"  # fixed; broader = planned/gated
ACCESS = "reviewer_internal"  # never "public" — this is a write-side reviewer artifact

# The CLOSED A3 affected-class vocabulary (mirrors the DB CHECK on
# source_change_affected_records.record_class), plus the fail-closed 'unresolved'
# sentinel. A frozenset so any future value is a conscious, reviewed change.
RC_NORMALIZATION = "normalization"
RC_STATEMENT = "statement"
RC_EVIDENCE_LINK = "evidence_link"
RC_TAG = "tag"
RC_SUMMARY = "source_grounded_summary"
RC_LENS_OUTPUT = "lens_output"
RC_REVIEW = "review"
RC_UNRESOLVED = "unresolved"
RECORD_CLASSES: frozenset[str] = frozenset(
    {
        RC_NORMALIZATION,
        RC_STATEMENT,
        RC_EVIDENCE_LINK,
        RC_TAG,
        RC_SUMMARY,
        RC_LENS_OUTPUT,
        RC_REVIEW,
        RC_UNRESOLVED,
    }
)

RESOLUTION_RESOLVED = "resolved"
RESOLUTION_FLAGGED = "unresolved_flagged"


class AffectedSetError(Exception):
    """An affected-set/invalidation request that fail-closed refuses."""


class AffectedSetAuditError(AssertionError):
    """A resolved affected set / ledger violates a Slice-3 invariant (contract guard)."""


# ---------------------------------------------------------------------------
# Deterministic primitives (pure — no model)
# ---------------------------------------------------------------------------


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _affected_id(change_id: str, segment_id: str, record_class: str, record_id: str) -> str:
    """Content-addressed affected-record id — deterministic and byte-stable (AC-5).
    The wall-clock ``marked_utc`` is deliberately NOT part of it."""
    digest = hashlib.sha256(
        f"{change_id}\n{segment_id}\n{record_class}\n{record_id}".encode("utf-8")
    ).hexdigest()
    return f"srcaff:{digest[:24]}"


def _as_int(value: str) -> int | None:
    """Coerce an anchor_ref to int for an integer-keyed anchor (meeting id, page).
    Returns None on a malformed value — the caller then leaves the anchor
    unresolved-and-flagged rather than guessing (fail-closed)."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The anchor->record locator registry (deterministic SQL, no model in the loop)
# ---------------------------------------------------------------------------
#
# Each rule maps ONE (anchor_type, record_class) pair to the record ids whose
# civic locator matches a segment anchor. A rule is a pure function of the DB
# state; it returns a sorted list of record ids (may be empty). ``source_id`` is
# the change's source (resolved once per change) for the source-scoped anchors
# (page/section/attachment); a rule that needs it and does not have it returns
# nothing, so the segment falls through to the fail-closed flag.

ResolverFn = Callable[[sqlite3.Connection, str, str | None], list[str]]


def _sorted_col(rows: list[sqlite3.Row]) -> list[str]:
    return sorted(str(r[0]) for r in rows)


# -- agenda_item anchor (anchor_ref = agenda_item_id) ------------------------


def _agenda_item_statements(conn, ref, source_id):
    return _sorted_col(
        conn.execute(
            "SELECT statement_id FROM statements WHERE agenda_item_id = ?", (ref,)
        ).fetchall()
    )


def _agenda_item_evidence(conn, ref, source_id):
    return _sorted_col(
        conn.execute(
            "SELECT evidence_link_id FROM evidence_links WHERE agenda_item_id = ?", (ref,)
        ).fetchall()
    )


# -- meeting anchor (anchor_ref = meeting_id) --------------------------------


def _meeting_statements(conn, ref, source_id):
    mid = _as_int(ref)
    if mid is None:
        return []
    return _sorted_col(
        conn.execute(
            "SELECT s.statement_id FROM statements s "
            "JOIN agenda_items a ON s.agenda_item_id = a.agenda_item_id "
            "WHERE a.meeting_id = ?",
            (mid,),
        ).fetchall()
    )


def _meeting_evidence(conn, ref, source_id):
    mid = _as_int(ref)
    if mid is None:
        return []
    return _sorted_col(
        conn.execute(
            "SELECT e.evidence_link_id FROM evidence_links e "
            "JOIN agenda_items a ON e.agenda_item_id = a.agenda_item_id "
            "WHERE a.meeting_id = ?",
            (mid,),
        ).fetchall()
    )


def _meeting_normalization(conn, ref, source_id):
    mid = _as_int(ref)
    if mid is None:
        return []
    return _sorted_col(
        conn.execute(
            "SELECT alias_id FROM node_label_aliases WHERE first_seen_meeting_id = ?",
            (mid,),
        ).fetchall()
    )


# -- page anchor (anchor_ref = page number; source-scoped) -------------------


def _page_evidence(conn, ref, source_id):
    page = _as_int(ref)
    if page is None or source_id is None:
        return []  # a page change cannot be localized without its source (fail-closed)
    return _sorted_col(
        conn.execute(
            "SELECT evidence_link_id FROM evidence_links "
            "WHERE locator_kind = 'page' AND page = ? AND to_source_id = ?",
            (page, source_id),
        ).fetchall()
    )


# -- section anchor (anchor_ref = section; source-scoped) --------------------


def _section_evidence(conn, ref, source_id):
    if source_id is None:
        return []  # cannot localize a section change without its source (fail-closed)
    return _sorted_col(
        conn.execute(
            "SELECT evidence_link_id FROM evidence_links "
            "WHERE locator_kind = 'section' AND section = ? AND to_source_id = ?",
            (ref, source_id),
        ).fetchall()
    )


# -- attachment anchor (anchor_ref = source url or id; whole-source) ----------


def _attachment_source_id(conn, ref, source_id):
    """The source this attachment anchor targets: the change's own source when
    known, else a registry row whose url matches the anchor_ref."""
    if source_id is not None:
        return source_id
    row = conn.execute("SELECT source_id FROM sources WHERE url = ?", (ref,)).fetchone()
    return row[0] if row is not None else None


def _attachment_evidence(conn, ref, source_id):
    target = _attachment_source_id(conn, ref, source_id)
    if target is None:
        return []
    return _sorted_col(
        conn.execute(
            "SELECT evidence_link_id FROM evidence_links WHERE to_source_id = ?", (target,)
        ).fetchall()
    )


def _attachment_statements(conn, ref, source_id):
    target = _attachment_source_id(conn, ref, source_id)
    if target is None:
        return []
    return _sorted_col(
        conn.execute(
            "SELECT s.statement_id FROM statements s "
            "JOIN agenda_items a ON s.agenda_item_id = a.agenda_item_id "
            "WHERE a.agenda_doc_source_id = ?",
            (target,),
        ).fetchall()
    )


def _attachment_normalization(conn, ref, source_id):
    target = _attachment_source_id(conn, ref, source_id)
    if target is None:
        return []
    return _sorted_col(
        conn.execute(
            "SELECT alias_id FROM node_label_aliases WHERE source_ref_source_id = ?",
            (target,),
        ).fetchall()
    )


# The registry: {anchor_type: [(record_class, resolver_fn), ...]}. The tuple order
# is fixed so the derived set is deterministic. tag / source_grounded_summary /
# lens_output carry no landed civic-locator table today, so they have no rule and
# a later slice adds one WITHOUT a schema change (the class is already allowed).
RESOLVER_RULES: dict[str, list[tuple[str, ResolverFn]]] = {
    sd.ANCHOR_AGENDA_ITEM: [
        (RC_STATEMENT, _agenda_item_statements),
        (RC_EVIDENCE_LINK, _agenda_item_evidence),
    ],
    sd.ANCHOR_MEETING: [
        (RC_STATEMENT, _meeting_statements),
        (RC_EVIDENCE_LINK, _meeting_evidence),
        (RC_NORMALIZATION, _meeting_normalization),
    ],
    sd.ANCHOR_PAGE: [
        (RC_EVIDENCE_LINK, _page_evidence),
    ],
    sd.ANCHOR_SECTION: [
        (RC_EVIDENCE_LINK, _section_evidence),
    ],
    sd.ANCHOR_ATTACHMENT: [
        (RC_STATEMENT, _attachment_statements),
        (RC_EVIDENCE_LINK, _attachment_evidence),
        (RC_NORMALIZATION, _attachment_normalization),
    ],
}


def _change_source_id(conn: sqlite3.Connection, change_id: str) -> str | None:
    """The registry source_id both versions of the change belong to (may be NULL:
    a version can be preserved before its registry seed). Read from the new
    version row; the pair shares one source_url, hence one source_id."""
    row = conn.execute(
        "SELECT sv.source_id FROM source_version_changes c "
        "JOIN source_versions sv ON sv.version_id = c.new_version_id "
        "WHERE c.change_id = ?",
        (change_id,),
    ).fetchone()
    if row is None:
        raise AffectedSetError(f"no detected change {change_id!r}")
    return row[0]


def _load_segments(conn: sqlite3.Connection, change_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT segment_id, anchor_type, anchor_ref, segment_ordinal "
        "FROM source_version_diff_segments WHERE change_id = ? "
        "ORDER BY segment_ordinal ASC",
        (change_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Resolve (pure read) → the affected set
# ---------------------------------------------------------------------------


def resolve_affected(conn: sqlite3.Connection, change_id: str) -> list[dict[str, Any]]:
    """Resolve the canonical records a detected change invalidates (pure read).

    A deterministic function of the stored diff + the current canonical state. For
    each diff segment, every registered locator for its ``anchor_type`` contributes
    the records whose civic locator matches the segment's ``anchor_ref``. Reviews
    are resolved transitively off the segment's affected statements (an independent
    review is affected iff the statement it reviewed is). A segment that localizes
    **no** concrete record is flagged with one ``unresolved`` sentinel row
    (fail-closed) — never dropped.

    Returns the affected rows in a stable ``(segment_ordinal, record_class,
    record_id)`` order. Raises :class:`stage5_source_diff.UnknownAnchorType` if a
    stored segment carries an anchor_type outside the closed set (defense in depth
    behind the DB CHECK).
    """
    if conn.execute(
        "SELECT 1 FROM source_version_changes WHERE change_id = ?", (change_id,)
    ).fetchone() is None:
        raise AffectedSetError(f"no detected change {change_id!r}")

    source_id = _change_source_id(conn, change_id)
    segments = _load_segments(conn, change_id)
    affected: list[dict[str, Any]] = []

    for seg in segments:
        anchor_type = seg["anchor_type"]
        anchor_ref = seg["anchor_ref"]
        segment_id = seg["segment_id"]
        if anchor_type not in sd.ANCHOR_TYPES:
            raise sd.UnknownAnchorType(
                f"segment {segment_id} anchor_type {anchor_type!r} outside the "
                f"closed set {sorted(sd.ANCHOR_TYPES)} — refusing"
            )

        seg_rows: list[dict[str, Any]] = []
        statement_ids: list[str] = []
        for record_class, resolver in RESOLVER_RULES.get(anchor_type, []):
            for record_id in resolver(conn, anchor_ref, source_id):
                seg_rows.append(
                    {
                        "segment_id": segment_id,
                        "anchor_type": anchor_type,
                        "anchor_ref": anchor_ref,
                        "segment_ordinal": seg["segment_ordinal"],
                        "record_class": record_class,
                        "record_id": record_id,
                        "resolution": RESOLUTION_RESOLVED,
                    }
                )
                if record_class == RC_STATEMENT:
                    statement_ids.append(record_id)

        # Reviews: transitive off the segment's affected statements.
        for decision_id in _reviews_for_statements(conn, statement_ids):
            seg_rows.append(
                {
                    "segment_id": segment_id,
                    "anchor_type": anchor_type,
                    "anchor_ref": anchor_ref,
                    "segment_ordinal": seg["segment_ordinal"],
                    "record_class": RC_REVIEW,
                    "record_id": decision_id,
                    "resolution": RESOLUTION_RESOLVED,
                }
            )

        if not seg_rows:
            # Fail-closed: the anchor localized nothing — flag it, never drop it.
            seg_rows.append(
                {
                    "segment_id": segment_id,
                    "anchor_type": anchor_type,
                    "anchor_ref": anchor_ref,
                    "segment_ordinal": seg["segment_ordinal"],
                    "record_class": RC_UNRESOLVED,
                    "record_id": f"{anchor_type}:{anchor_ref}",
                    "resolution": RESOLUTION_FLAGGED,
                }
            )

        seg_rows.sort(key=lambda r: (r["record_class"], r["record_id"]))
        affected.extend(seg_rows)

    return affected


def _reviews_for_statements(conn: sqlite3.Connection, statement_ids: list[str]) -> list[str]:
    if not statement_ids:
        return []
    placeholders = ",".join("?" for _ in sorted(set(statement_ids)))
    rows = conn.execute(
        f"SELECT decision_id FROM reviewer_decisions WHERE statement_id IN ({placeholders})",
        tuple(sorted(set(statement_ids))),
    ).fetchall()
    return _sorted_col(rows)


# ---------------------------------------------------------------------------
# Invalidate (write the ledger) — idempotent, marker-only, never destructive
# ---------------------------------------------------------------------------


def invalidate(
    conn: sqlite3.Connection, change_id: str, now: str | None = None
) -> dict[str, Any]:
    """Selectively invalidate exactly the records the change affects. Idempotent.

    Writes one marker row per affected record into
    ``source_change_affected_records`` (the row's existence IS the invalidation
    marker and the D-1 binding). Canonical content is NEVER touched — an unaffected
    record stays byte-identical, and so does an affected one (Slice 3 marks; it
    does not overwrite or rerun). Re-running on the same change inserts nothing new
    (``UNIQUE`` + ``INSERT OR IGNORE``), so ``created`` is 0 on the second call.

    Returns ``{"change_id", "created", "skipped", "records"}``.
    """
    affected = resolve_affected(conn, change_id)
    marked_utc = now or _now_utc_iso()
    before = conn.total_changes
    for row in affected:
        conn.execute(
            "INSERT OR IGNORE INTO source_change_affected_records "
            "(affected_id, change_id, segment_id, anchor_type, anchor_ref, "
            "record_class, record_id, resolution, marked_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _affected_id(change_id, row["segment_id"], row["record_class"], row["record_id"]),
                change_id,
                row["segment_id"],
                row["anchor_type"],
                row["anchor_ref"],
                row["record_class"],
                row["record_id"],
                row["resolution"],
                marked_utc,
            ),
        )
    conn.commit()
    created = conn.total_changes - before
    return {
        "change_id": change_id,
        "created": created,
        "skipped": len(affected) - created,
        "records": affected,
    }


# ---------------------------------------------------------------------------
# Binding trace (D-1) — the single join, no dangling hop
# ---------------------------------------------------------------------------


def affected_trace(conn: sqlite3.Connection, change_id: str) -> list[dict[str, Any]]:
    """The D-1 binding as a single join: for every affected record, the full chain
    ``source -> source_versions -> source_version_changes ->
    source_version_diff_segments -> ledger -> record``. Every hop resolves in one
    SQL statement; a dangling hop would drop the row (an INNER JOIN), which
    :func:`assert_binding_no_dangling_hop` turns into a RED."""
    rows = conn.execute(
        "SELECT sv.source_url AS source_url, sv.source_id AS source_id, "
        "c.change_id AS change_id, c.old_version_id AS old_version_id, "
        "c.new_version_id AS new_version_id, seg.segment_id AS segment_id, "
        "seg.anchor_type AS anchor_type, seg.anchor_ref AS anchor_ref, "
        "a.record_class AS record_class, a.record_id AS record_id, "
        "a.resolution AS resolution "
        "FROM source_change_affected_records a "
        "JOIN source_version_diff_segments seg ON seg.segment_id = a.segment_id "
        "JOIN source_version_changes c ON c.change_id = a.change_id "
        "JOIN source_versions sv ON sv.version_id = c.new_version_id "
        "WHERE a.change_id = ? "
        "ORDER BY seg.segment_ordinal ASC, a.record_class ASC, a.record_id ASC",
        (change_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Contract guards (load-bearing, non-tautological — cross-check EMITTED state)
# ---------------------------------------------------------------------------


def assert_every_segment_covered(conn: sqlite3.Connection, change_id: str) -> bool:
    """RED unless EVERY diff segment of the change has at least one ledger row
    (AC-4, fail-closed). A segment silently dropped from the affected set — the
    exact failure the 'unresolved' sentinel exists to prevent — goes RED here."""
    segments = {s["segment_id"] for s in _load_segments(conn, change_id)}
    covered = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT segment_id FROM source_change_affected_records "
            "WHERE change_id = ?",
            (change_id,),
        ).fetchall()
    }
    missing = segments - covered
    if missing:
        raise AffectedSetAuditError(
            f"{len(missing)} diff segment(s) have no affected-set row (silently "
            f"dropped): {sorted(missing)}"
        )
    return True


def assert_binding_no_dangling_hop(conn: sqlite3.Connection, change_id: str) -> bool:
    """RED if the D-1 binding drops any ledger row on a broken hop (AC-3). The
    ledger row count for the change must equal the fully-joined trace row count;
    an unequal count means a segment/change/version hop failed to resolve."""
    ledger = conn.execute(
        "SELECT COUNT(*) FROM source_change_affected_records WHERE change_id = ?",
        (change_id,),
    ).fetchone()[0]
    joined = len(affected_trace(conn, change_id))
    if ledger != joined:
        raise AffectedSetAuditError(
            f"binding has a dangling hop: {ledger} ledger rows but the "
            f"source->version->change->segment->record join yields {joined}"
        )
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5 Slice 3 affected-set resolver + selective invalidation (GOV-1688)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--change-id", required=True, help="a detected change_id (from Slice 2)")
    parser.add_argument(
        "--apply", action="store_true", help="write the invalidation ledger (default: dry-run resolve)"
    )
    parser.add_argument(
        "--check", action="store_true", help="run the coverage + binding guards after apply"
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.apply:
            result = invalidate(conn, args.change_id)
            if args.check:
                assert_every_segment_covered(conn, args.change_id)
                assert_binding_no_dangling_hop(conn, args.change_id)
        else:
            result = {
                "change_id": args.change_id,
                "dry_run": True,
                "records": resolve_affected(conn, args.change_id),
            }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
