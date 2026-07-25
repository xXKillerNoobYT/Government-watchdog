"""Supplied-file record + provenance model (GOV-1575 / B2).

Parent: GOV-1566 "New files" — supplied-source-file intake, save & reuse. B1
(``scripts/raw_object_store.py``) saves the raw *bytes*; **B2** (this module) owns
the canonical, queryable **record** describing each supplied file, with full,
mandatory provenance. The two are complementary: B1 is bytes-in/bytes-out keyed
by SHA-256; B2 is the ledger row that says WHAT the file is, WHERE it came from,
WHO supplied it, WHEN, and WHERE it sits in the review lifecycle.

Hard gates carried from GOV-1566 (enforced here, not just documented):

  * **Provenance is mandatory.** ``insert_file_record`` refuses to write a row
    unless every provenance field is present and non-blank. The DB enforces
    NOT NULL; this layer additionally rejects empty/whitespace strings and
    malformed SHA-256, which NOT NULL alone would let through.
  * **Fail-closed / private-by-default.** A new record is ALWAYS ``pending``.
    ``review_state`` is not even a parameter of :func:`insert_file_record`, so a
    caller can never mint a ``web_safe`` (displayable) record. State only advances
    through :func:`set_review_state`, which enforces a legal-transition map — this
    is the teeth behind review-before-AI and review-before-display.
  * **No AI output stored as fact.** A :class:`FileRecord` carries only human/
    operator-supplied provenance and content-derived integrity values
    (``sha256``/``byte_size``). There is deliberately NO column for a model
    summary, model classification, extracted claim, or any interpretation.
    :data:`PROVENANCE_COLUMNS` is the frozen, AI-free column set the tests pin.

Versioning (B5): every record belongs to a ``version_group_id`` (a brand-new
file starts its own group = its ``file_id``); a new revision sets
``supersedes_id`` to the row it replaces and inherits that row's group. B5 owns
the supersede/red-flag *workflow*; B2 provides the structural anchors + the
inherit-the-group behaviour so B5 can build on a consistent model.

Usage:
    import file_records as fr
    rec = fr.insert_file_record(
        conn, area="alpine", source_type="agenda_packet",
        original_filename="2026-06-23-packet.pdf", sha256=<64 hex>,
        mime="application/pdf", byte_size=51234, supplied_by="isaac",
        captured_at="2026-06-23T00:00:00.000+00:00", origin_url=None)
    assert rec.review_state == "pending"          # fail-closed default
    rec2 = fr.set_review_state(conn, rec.file_id, "reviewing")
    rec3 = fr.set_review_state(conn, rec2.file_id, "web_safe")  # reviewer-only path
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# --- review lifecycle ------------------------------------------------------

#: The only state a freshly-inserted record may hold (fail-closed default).
INITIAL_REVIEW_STATE = "pending"

#: Every legal review_state (must mirror the CHECK in 0028_supplied_file_records.sql).
REVIEW_STATES = ("pending", "reviewing", "web_safe", "held", "rejected")

#: Legal review_state transitions. Anything not listed is rejected (fail-closed),
#: so a file can only become 'web_safe' (displayable) via an explicit reviewer
#: step out of 'reviewing' — never straight from 'pending'.
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"reviewing", "held", "rejected"}),
    "reviewing": frozenset({"web_safe", "held", "rejected", "pending"}),
    "web_safe": frozenset({"held", "rejected", "reviewing"}),
    "held": frozenset({"reviewing", "rejected", "pending"}),
    "rejected": frozenset({"reviewing"}),  # reopen only; never straight to web_safe
}

#: The exact, AI-free column set of the record, in schema order. Pinned by a test
#: so no AI-interpretation column can be added without a deliberate change here.
PROVENANCE_COLUMNS = (
    "file_id",
    "area",
    "source_type",
    "original_filename",
    "supplied_by",
    "captured_at",
    "origin_url",
    "sha256",
    "mime",
    "byte_size",
    "review_state",
    "version_group_id",
    "supersedes_id",
    "created_at",
)

#: Provenance fields that must be present and non-blank on insert (fail-closed).
#: origin_url is intentionally excluded (optional locator). byte_size/sha256 are
#: validated separately (numeric range / hex format).
_MANDATORY_TEXT_FIELDS = (
    "area",
    "source_type",
    "original_filename",
    "supplied_by",
    "captured_at",
    "mime",
)


# --- errors ----------------------------------------------------------------

class FileRecordError(Exception):
    """Base error for the supplied-file record model."""


class MissingProvenance(FileRecordError):
    """A mandatory provenance field was absent, blank, or malformed."""


class FileRecordNotFound(FileRecordError):
    """No supplied_files row for the requested file_id."""


class IllegalReviewTransition(FileRecordError):
    """A review_state change that the legal-transition map forbids."""


# --- record ----------------------------------------------------------------

@dataclass(frozen=True)
class FileRecord:
    """One supplied-file record. Every field is provenance or content-integrity;
    NONE is an AI interpretation."""

    file_id: str
    area: str
    source_type: str
    original_filename: str
    supplied_by: str
    captured_at: str
    origin_url: str | None
    sha256: str
    mime: str
    byte_size: int
    review_state: str
    version_group_id: str
    supersedes_id: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileRecord":
        return cls(**{col: row[col] for col in PROVENANCE_COLUMNS})


# --- helpers ---------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _require_text(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissingProvenance(
            f"provenance field {name!r} is mandatory and must be a non-blank string"
        )
    return value


# --- write -----------------------------------------------------------------

def insert_file_record(
    conn: sqlite3.Connection,
    *,
    area: str,
    source_type: str,
    original_filename: str,
    sha256: str,
    mime: str,
    byte_size: int,
    supplied_by: str,
    captured_at: str,
    origin_url: str | None = None,
    version_group_id: str | None = None,
    supersedes_id: str | None = None,
    file_id: str | None = None,
    created_at: str | None = None,
) -> FileRecord:
    """Insert one supplied-file record with mandatory provenance; fail-closed.

    review_state is intentionally NOT a parameter — a new record is always
    ``pending``. Advance it only via :func:`set_review_state`.

    Versioning: if ``supersedes_id`` is given, the superseded record must exist;
    the new record inherits that record's ``version_group_id`` unless an explicit
    (matching) ``version_group_id`` is passed. A brand-new file (no supersedes)
    starts its own group = its ``file_id``.

    Raises :class:`MissingProvenance` if any mandatory field is absent/blank or
    ``sha256``/``byte_size`` is malformed, and :class:`FileRecordNotFound` if
    ``supersedes_id`` names a row that does not exist.
    """
    _local = locals()
    for name in _MANDATORY_TEXT_FIELDS:
        _require_text(name, _local[name])

    if not isinstance(sha256, str) or not _is_sha256(sha256):
        raise MissingProvenance(
            "sha256 is mandatory and must be a 64-char lowercase hex digest "
            "(the B1 content address)"
        )
    if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size < 0:
        raise MissingProvenance("byte_size is mandatory and must be a non-negative int")

    file_id = file_id or f"file-{secrets.token_hex(12)}"
    created_at = created_at or _now_utc_iso()

    if supersedes_id is not None:
        prior = get_file_record(conn, supersedes_id)
        if prior is None:
            raise FileRecordNotFound(
                f"supersedes_id {supersedes_id!r} names no existing supplied_files row"
            )
        if version_group_id is None:
            version_group_id = prior.version_group_id
        elif version_group_id != prior.version_group_id:
            raise FileRecordError(
                "version_group_id must match the superseded record's group "
                f"({prior.version_group_id!r})"
            )

    # A brand-new file with no explicit group starts its own group.
    version_group_id = version_group_id or file_id

    conn.execute(
        "INSERT INTO supplied_files ("
        " file_id, area, source_type, original_filename, supplied_by, captured_at,"
        " origin_url, sha256, mime, byte_size, review_state, version_group_id,"
        " supersedes_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            file_id, area, source_type, original_filename, supplied_by, captured_at,
            origin_url, sha256, mime, byte_size, INITIAL_REVIEW_STATE,
            version_group_id, supersedes_id, created_at,
        ),
    )
    conn.commit()
    record = get_file_record(conn, file_id)
    assert record is not None  # just inserted
    return record


def set_review_state(
    conn: sqlite3.Connection,
    file_id: str,
    new_state: str,
) -> FileRecord:
    """Advance a record's review_state through the legal-transition map.

    Fail-closed: an unknown ``new_state`` or a transition not permitted from the
    current state raises :class:`IllegalReviewTransition` and writes nothing. This
    is the only path a file reaches ``web_safe`` (displayable).
    """
    if new_state not in REVIEW_STATES:
        raise IllegalReviewTransition(f"{new_state!r} is not a valid review_state")
    current = get_file_record(conn, file_id)
    if current is None:
        raise FileRecordNotFound(f"no supplied_files row for file_id {file_id!r}")
    if new_state == current.review_state:
        return current  # idempotent no-op
    allowed = _LEGAL_TRANSITIONS.get(current.review_state, frozenset())
    if new_state not in allowed:
        raise IllegalReviewTransition(
            f"illegal review_state transition {current.review_state!r} -> {new_state!r}"
        )
    conn.execute(
        "UPDATE supplied_files SET review_state = ? WHERE file_id = ?",
        (new_state, file_id),
    )
    conn.commit()
    updated = get_file_record(conn, file_id)
    assert updated is not None
    return updated


# --- read ------------------------------------------------------------------

def get_file_record(conn: sqlite3.Connection, file_id: str) -> FileRecord | None:
    """Fetch one record by id, or None. Uses a Row factory locally so callers
    need not have configured one."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    row = cur.execute(
        f"SELECT {', '.join(PROVENANCE_COLUMNS)} FROM supplied_files WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    return FileRecord.from_row(row) if row is not None else None


def list_versions(conn: sqlite3.Connection, version_group_id: str) -> list[FileRecord]:
    """All records in a version group, oldest-first (by created_at then file_id).

    B5 builds its supersede chain / red-flag view on top of this."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(
        f"SELECT {', '.join(PROVENANCE_COLUMNS)} FROM supplied_files"
        " WHERE version_group_id = ? ORDER BY created_at, file_id",
        (version_group_id,),
    ).fetchall()
    return [FileRecord.from_row(r) for r in rows]
