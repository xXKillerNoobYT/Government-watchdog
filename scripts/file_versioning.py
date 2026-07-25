"""Supplied-file versioning + red-flag-on-supersede workflow (GOV-1578 / B5).

Parent: GOV-1566 "New files". B2 (``file_records.py`` + migration 0028) owns the
``supplied_files`` record and the versioning *anchors* (``version_group_id`` and
``supersedes_id``). **B5** (this module + migration 0029) owns the supersede
*workflow* the plan (§5) calls a "red-flag event":

    "Replacing/superseding a file is a red-flag event: keep both versions,
     compute before/after, mark affected records, and require re-review of
     affected work."

Three guarantees, each backed by a test:

  1. **Both versions retained.** :func:`supersede_file` NEVER updates or deletes
     the prior row. It inserts a new ``supplied_files`` row (via B2's
     ``insert_file_record`` with ``supersedes_id`` set) and re-asserts the prior
     row is byte-identical afterwards. Preservation is the invariant.

  2. **New + old share the group; supersedes_id set.** Inherited straight from
     B2's ``insert_file_record`` -- the new version's ``version_group_id`` equals
     the prior's and its ``supersedes_id`` points at the prior ``file_id``.

  3. **Before/after diff + affected records flagged.** :func:`compute_before_after`
     produces a deterministic field-level diff (content-integrity + provenance
     only -- NOT AI output). Every downstream record that registered a dependency
     on the *superseded* version (:func:`register_dependency`) is flipped to
     ``needs_re_review`` -- fail-closed: affected work is not trusted until a human
     re-reviews it (:func:`resolve_re_review`). Each supersede also writes one
     immutable :data:`SupersedeEvent` for the audit trail.

Downstream lanes stay decoupled: a lane records "record R was built from file
version F" with a generic ``(record_kind, record_ref)`` pair. B5 needs no
knowledge of the lane's own schema, so B5 depends only on B2 (not B4 linkage).

Usage:
    import file_records as fr
    import file_versioning as fv

    v1 = fr.insert_file_record(conn, ...)                 # original supplied file
    fv.register_dependency(conn, file_id=v1.file_id,      # a downstream record
                           record_kind="agenda_anchor", record_ref="ai-77")

    result = fv.supersede_file(                            # a corrected file arrives
        conn, v1.file_id, area="alpine", source_type="agenda_packet",
        original_filename="2026-06-23-packet-v2.pdf", sha256=<new 64 hex>,
        mime="application/pdf", byte_size=52000, supplied_by="isaac",
        captured_at="2026-06-24T00:00:00.000+00:00", superseded_by="isaac")

    assert result.prior.file_id == v1.file_id             # prior preserved
    assert result.new.version_group_id == v1.version_group_id
    assert result.new.supersedes_id == v1.file_id
    assert result.diff["content_changed"] is True         # sha256 differs
    assert [d.record_ref for d in result.flagged] == ["ai-77"]  # affected flagged
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import file_records as fr

# --- re-review flag lifecycle ----------------------------------------------

#: The only flag a freshly-registered dependency may hold (nothing to re-review).
INITIAL_REVIEW_FLAG = "current"

#: Every legal review_flag (must mirror the CHECK in 0029_supplied_file_versioning).
REVIEW_FLAGS = ("current", "needs_re_review")

#: The record fields compared in a before/after diff. Content-integrity +
#: provenance ONLY -- deliberately excludes identity/lifecycle columns
#: (file_id, version_group_id, supersedes_id, review_state, created_at), which
#: are expected to differ between two versions and are not "changes to the file".
DIFF_FIELDS = (
    "sha256",
    "byte_size",
    "original_filename",
    "mime",
    "area",
    "source_type",
    "origin_url",
    "supplied_by",
    "captured_at",
)

_DEPENDENCY_COLUMNS = (
    "dependency_id",
    "file_id",
    "version_group_id",
    "record_kind",
    "record_ref",
    "review_flag",
    "flagged_by_file_id",
    "created_at",
    "flagged_at",
    "resolved_at",
)

_EVENT_COLUMNS = (
    "event_id",
    "version_group_id",
    "superseded_file_id",
    "new_file_id",
    "diff_json",
    "affected_count",
    "superseded_by",
    "created_at",
)


# --- errors ----------------------------------------------------------------

class VersioningError(Exception):
    """Base error for the B5 versioning/red-flag workflow."""


class DependencyNotFound(VersioningError):
    """No supplied_file_dependencies row for the requested dependency_id."""


class IllegalFlagTransition(VersioningError):
    """A review_flag change the fail-closed lifecycle forbids."""


# --- records ---------------------------------------------------------------

@dataclass(frozen=True)
class DependencyRecord:
    """A downstream record's dependency on one supplied-file VERSION, plus its
    re-review flag. record_kind/record_ref are an opaque pointer; B5 stores no
    interpretation of the downstream record."""

    dependency_id: str
    file_id: str
    version_group_id: str
    record_kind: str
    record_ref: str
    review_flag: str
    flagged_by_file_id: str | None
    created_at: str
    flagged_at: str | None
    resolved_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DependencyRecord":
        return cls(**{col: row[col] for col in _DEPENDENCY_COLUMNS})


@dataclass(frozen=True)
class SupersedeEvent:
    """One immutable audit row per supersede. diff_json is a deterministic
    before/after comparison (fact, not AI)."""

    event_id: str
    version_group_id: str
    superseded_file_id: str
    new_file_id: str
    diff_json: str
    affected_count: int
    superseded_by: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SupersedeEvent":
        return cls(**{col: row[col] for col in _EVENT_COLUMNS})

    @property
    def diff(self) -> dict:
        """The parsed before/after diff (as :func:`compute_before_after` produced)."""
        return json.loads(self.diff_json)


@dataclass(frozen=True)
class SupersedeResult:
    """Everything a supersede produced: the new + preserved-prior records, the
    before/after diff, the dependencies flipped to needs_re_review, and the audit
    event."""

    new: fr.FileRecord
    prior: fr.FileRecord
    diff: dict
    flagged: list[DependencyRecord]
    event: SupersedeEvent


# --- helpers ---------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _cursor(conn: sqlite3.Connection) -> sqlite3.Cursor:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur


# --- diff ------------------------------------------------------------------

def compute_before_after(prior: fr.FileRecord, new: fr.FileRecord) -> dict:
    """Deterministic field-level before/after diff between two file versions.

    Compares only :data:`DIFF_FIELDS` (content-integrity + provenance). Returns::

        {
          "changed":   {field: {"before": <old>, "after": <new>}, ...},
          "unchanged": [field, ...],
          "content_changed": <bool>,   # sha256 differs -> the bytes changed
        }

    Pure function, no DB, no AI -- this is fact computed from two records.
    """
    changed: dict[str, dict] = {}
    unchanged: list[str] = []
    for field in DIFF_FIELDS:
        before = getattr(prior, field)
        after = getattr(new, field)
        if before != after:
            changed[field] = {"before": before, "after": after}
        else:
            unchanged.append(field)
    return {
        "changed": changed,
        "unchanged": unchanged,
        "content_changed": prior.sha256 != new.sha256,
    }


# --- dependency registry ---------------------------------------------------

def register_dependency(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    record_kind: str,
    record_ref: str,
    dependency_id: str | None = None,
    created_at: str | None = None,
) -> DependencyRecord:
    """Record that a downstream record was built from a specific file VERSION.

    Idempotent per ``(file_id, record_kind, record_ref)``: a repeat call returns
    the existing row unchanged rather than raising. The referenced
    ``supplied_files`` row must exist (fail-closed) -- a dependency cannot point at
    a file that was never recorded.

    Raises :class:`file_records.FileRecordNotFound` if ``file_id`` names no row,
    and :class:`file_records.MissingProvenance` if kind/ref are blank.
    """
    fr._require_text("record_kind", record_kind)
    fr._require_text("record_ref", record_ref)

    target = fr.get_file_record(conn, file_id)
    if target is None:
        raise fr.FileRecordNotFound(
            f"file_id {file_id!r} names no supplied_files row to depend on"
        )

    existing = _get_dependency_by_ref(conn, file_id, record_kind, record_ref)
    if existing is not None:
        return existing

    dependency_id = dependency_id or f"sfdep-{secrets.token_hex(12)}"
    created_at = created_at or _now_utc_iso()
    conn.execute(
        "INSERT INTO supplied_file_dependencies ("
        " dependency_id, file_id, version_group_id, record_kind, record_ref,"
        " review_flag, flagged_by_file_id, created_at, flagged_at, resolved_at)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL)",
        (
            dependency_id, file_id, target.version_group_id, record_kind,
            record_ref, INITIAL_REVIEW_FLAG, created_at,
        ),
    )
    conn.commit()
    created = get_dependency(conn, dependency_id)
    assert created is not None  # just inserted
    return created


def get_dependency(conn: sqlite3.Connection, dependency_id: str) -> DependencyRecord | None:
    cur = _cursor(conn)
    row = cur.execute(
        f"SELECT {', '.join(_DEPENDENCY_COLUMNS)} FROM supplied_file_dependencies"
        " WHERE dependency_id = ?",
        (dependency_id,),
    ).fetchone()
    return DependencyRecord.from_row(row) if row is not None else None


def _get_dependency_by_ref(
    conn: sqlite3.Connection, file_id: str, record_kind: str, record_ref: str
) -> DependencyRecord | None:
    cur = _cursor(conn)
    row = cur.execute(
        f"SELECT {', '.join(_DEPENDENCY_COLUMNS)} FROM supplied_file_dependencies"
        " WHERE file_id = ? AND record_kind = ? AND record_ref = ?",
        (file_id, record_kind, record_ref),
    ).fetchone()
    return DependencyRecord.from_row(row) if row is not None else None


def list_dependencies(conn: sqlite3.Connection, file_id: str) -> list[DependencyRecord]:
    """All dependency rows registered against one specific file VERSION."""
    cur = _cursor(conn)
    rows = cur.execute(
        f"SELECT {', '.join(_DEPENDENCY_COLUMNS)} FROM supplied_file_dependencies"
        " WHERE file_id = ? ORDER BY created_at, dependency_id",
        (file_id,),
    ).fetchall()
    return [DependencyRecord.from_row(r) for r in rows]


def list_needs_re_review(
    conn: sqlite3.Connection, version_group_id: str | None = None
) -> list[DependencyRecord]:
    """Every dependency currently flagged ``needs_re_review`` (the open red-flags),
    optionally scoped to one version group."""
    cur = _cursor(conn)
    if version_group_id is None:
        rows = cur.execute(
            f"SELECT {', '.join(_DEPENDENCY_COLUMNS)} FROM supplied_file_dependencies"
            " WHERE review_flag = 'needs_re_review' ORDER BY flagged_at, dependency_id"
        ).fetchall()
    else:
        rows = cur.execute(
            f"SELECT {', '.join(_DEPENDENCY_COLUMNS)} FROM supplied_file_dependencies"
            " WHERE review_flag = 'needs_re_review' AND version_group_id = ?"
            " ORDER BY flagged_at, dependency_id",
            (version_group_id,),
        ).fetchall()
    return [DependencyRecord.from_row(r) for r in rows]


def resolve_re_review(
    conn: sqlite3.Connection,
    dependency_id: str,
    *,
    resolved_at: str | None = None,
) -> DependencyRecord:
    """Clear a red-flag after a human has re-reviewed the affected work.

    Fail-closed: only a ``needs_re_review`` row may be resolved. Resolving a
    ``current`` row raises :class:`IllegalFlagTransition` (there is nothing to
    clear), so a caller can never silently mark unreviewed work as reviewed.
    """
    dep = get_dependency(conn, dependency_id)
    if dep is None:
        raise DependencyNotFound(f"no dependency row for dependency_id {dependency_id!r}")
    if dep.review_flag != "needs_re_review":
        raise IllegalFlagTransition(
            f"dependency {dependency_id!r} is {dep.review_flag!r}, not needs_re_review; "
            "nothing to resolve"
        )
    conn.execute(
        "UPDATE supplied_file_dependencies"
        " SET review_flag = 'current', resolved_at = ? WHERE dependency_id = ?",
        (resolved_at or _now_utc_iso(), dependency_id),
    )
    conn.commit()
    updated = get_dependency(conn, dependency_id)
    assert updated is not None
    return updated


# --- supersede -------------------------------------------------------------

def supersede_file(
    conn: sqlite3.Connection,
    prior_file_id: str,
    *,
    area: str,
    source_type: str,
    original_filename: str,
    sha256: str,
    mime: str,
    byte_size: int,
    supplied_by: str,
    captured_at: str,
    superseded_by: str,
    origin_url: str | None = None,
    new_file_id: str | None = None,
    created_at: str | None = None,
) -> SupersedeResult:
    """Supersede ``prior_file_id`` with a new version; the red-flag event.

    Steps (fail-closed, prior never mutated):

      1. Load the prior record; :class:`file_records.FileRecordNotFound` if absent.
      2. Insert the new version via B2's ``insert_file_record`` with
         ``supersedes_id = prior_file_id`` -- it inherits the prior's
         ``version_group_id`` and starts, like every record, at ``review_state
         = 'pending'`` (a superseding file is itself unreviewed).
      3. Re-load the prior and assert it is byte-identical -- proof both versions
         are retained (the supersede added a row, it did not overwrite one).
      4. Compute the deterministic before/after diff.
      5. Flip every ``current`` dependency on the *prior* version to
         ``needs_re_review`` (red-flag the affected downstream work).
      6. Append one immutable audit :class:`SupersedeEvent`.

    ``superseded_by`` (who performed the supersede) is mandatory provenance for
    the audit row.
    """
    fr._require_text("superseded_by", superseded_by)

    prior = fr.get_file_record(conn, prior_file_id)
    if prior is None:
        raise fr.FileRecordNotFound(
            f"prior_file_id {prior_file_id!r} names no supplied_files row to supersede"
        )
    prior_snapshot = prior  # frozen dataclass -- capture for the preservation check

    # 2. New version. insert_file_record enforces provenance + inherits the group.
    new = fr.insert_file_record(
        conn,
        area=area,
        source_type=source_type,
        original_filename=original_filename,
        sha256=sha256,
        mime=mime,
        byte_size=byte_size,
        supplied_by=supplied_by,
        captured_at=captured_at,
        origin_url=origin_url,
        supersedes_id=prior_file_id,
        file_id=new_file_id,
        created_at=created_at,
    )

    # 3. Preservation invariant: the prior row still exists, byte-identical.
    prior_after = fr.get_file_record(conn, prior_file_id)
    if prior_after != prior_snapshot:
        raise VersioningError(
            "supersede must not mutate or delete the prior version; "
            f"{prior_file_id!r} changed"
        )

    # 4. Before/after diff (deterministic, not AI).
    diff = compute_before_after(prior_snapshot, new)

    # 5. Red-flag every downstream record built from the superseded version.
    stamp = _now_utc_iso()
    conn.execute(
        "UPDATE supplied_file_dependencies"
        " SET review_flag = 'needs_re_review', flagged_by_file_id = ?, flagged_at = ?"
        " WHERE file_id = ? AND review_flag = 'current'",
        (new.file_id, stamp, prior_file_id),
    )
    flagged = [
        d for d in list_dependencies(conn, prior_file_id)
        if d.review_flag == "needs_re_review" and d.flagged_by_file_id == new.file_id
    ]

    # 6. Immutable audit event.
    event_id = f"sfevent-{secrets.token_hex(12)}"
    diff_json = json.dumps(diff, sort_keys=True)
    conn.execute(
        "INSERT INTO supplied_file_supersede_events ("
        " event_id, version_group_id, superseded_file_id, new_file_id, diff_json,"
        " affected_count, superseded_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id, new.version_group_id, prior_file_id, new.file_id, diff_json,
            len(flagged), superseded_by, stamp,
        ),
    )
    conn.commit()
    event = get_supersede_event(conn, event_id)
    assert event is not None
    return SupersedeResult(new=new, prior=prior_snapshot, diff=diff, flagged=flagged, event=event)


# --- audit read ------------------------------------------------------------

def get_supersede_event(conn: sqlite3.Connection, event_id: str) -> SupersedeEvent | None:
    cur = _cursor(conn)
    row = cur.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM supplied_file_supersede_events"
        " WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return SupersedeEvent.from_row(row) if row is not None else None


def list_supersede_events(
    conn: sqlite3.Connection, version_group_id: str
) -> list[SupersedeEvent]:
    """Every supersede event in a version group, oldest-first (the audit trail)."""
    cur = _cursor(conn)
    rows = cur.execute(
        f"SELECT {', '.join(_EVENT_COLUMNS)} FROM supplied_file_supersede_events"
        " WHERE version_group_id = ? ORDER BY created_at, event_id",
        (version_group_id,),
    ).fetchall()
    return [SupersedeEvent.from_row(r) for r in rows]
