"""Supplied-file linkage + deterministic gap-detection update (GOV-1577 / B4).

Parent: GOV-1566 "New files". B1 (``raw_object_store``) stores the raw bytes, B2
(``file_records`` / migration 0028) records the canonical ``supplied_files``
ledger row, and **B4** (this module / migration 0029) attaches that recorded file
to the subject it is a source for — an **area**, a **meeting**, or an **agenda
item** — and then updates the deterministic completeness-gap layer (GOV-125,
``completeness`` / migration 0015) so that a supplied *primary source* closes a
known ``no_primary_source`` gap.

Two responsibilities, kept separate:

  1. **Linkage** (:func:`link_file`, :func:`unlink_file`, :func:`links_for_subject`,
     :func:`links_for_file`) — the operator attaches / detaches a supplied file to
     a subject, optionally flagging it the **primary source** (source-of-record)
     for that subject. ``is_primary_source`` is an OPERATOR classification, never a
     model label (GOV-1566 §9: no AI output as fact).

  2. **Gap-detection update** (:func:`has_primary_source`,
     :func:`refresh_no_primary_source_gap`) — a *deterministic, source-grounded*
     recomputation. It reads the real linked-file rows (no AI, no fabrication) and
     flips an existing ``no_primary_source`` gap's ``resolved_status`` between
     ``open`` and ``resolved`` to reflect whether the subject now demonstrably has
     a primary source.

Hard rules carried from GOV-125 / GOV-1566 (enforced here, not just documented):

  * **Deterministic + source-grounded.** The same linked rows always yield the same
    verdict and the same gap transition. Nothing here calls a model, fetches the
    network, or invents a value.
  * **Never fabricates, never gates publication.** The gap update only moves a gap
    between ``open`` and ``resolved``; it never touches ``publication_state`` /
    ``ui_status`` and never creates a gap out of thin air. Gap *creation* stays
    with the structuring detector (``structure_real_corpus`` via ``completeness``);
    B4 only *resolves / reopens* what that detector surfaced.
  * **Respects human dispositions.** Only gaps we own — ``produced_by =
    'deterministic'`` in ``{open, resolved}`` — are moved. A reviewer's
    ``acknowledged`` or ``wontfix`` is left exactly as the reviewer set it.
  * **Reversible.** Because :func:`refresh_no_primary_source_gap` recomputes from
    scratch, if the linked primary source is later rejected (B2 review) or
    unlinked/superseded (B5), a re-run flips a previously ``resolved`` gap back to
    ``open`` — the gap layer never lies about a source that went away.

A file's ``review_state`` (B2) and this completeness axis are DISTINCT: a *pending*
primary source still means the subject HAS a primary source (the preserved bytes
exist), so it closes the completeness gap. Only a **rejected** (repudiated) file is
excluded — a rejected source is not a source. Whether a file is *web-safe for
display* is a separate gate owned by B6, not by this completeness signal.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import completeness  # noqa: E402  (gap SSOT: no_primary_source + record/read helpers)
import file_records  # noqa: E402  (supplied_files record model — subject-of the link)

# --- controlled linkage vocabulary (SSOT — mirrors the 0029 CHECK) ----------

#: The three subjects a supplied file may be linked to. Mirrors the
#: ``subject_node_type`` CHECK in ``0029_supplied_file_links.sql`` exactly; the
#: parity test pins the two together (same guard concept completeness.py uses).
LINK_SUBJECT_TYPES = frozenset({"area", "meeting", "agenda_item"})

#: The completeness gap this module resolves/reopens on linkage (from the GOV-125
#: SSOT). Named once so the coupling to ``completeness`` is explicit.
PRIMARY_SOURCE_GAP_TYPE = "no_primary_source"

#: review_states (B2) that do NOT count as a present primary source. A rejected
#: file is repudiated — it is not a source. Everything else (pending/reviewing/
#: web_safe/held) means the primary bytes exist, which is what a completeness gap
#: measures. Kept as a frozenset so a future policy change is a one-line edit.
NON_COUNTING_REVIEW_STATES = frozenset({"rejected"})


class FileLinkageError(ValueError):
    """A linkage write violated the vocabulary or contract (fail-closed)."""


class UnknownSubjectType(FileLinkageError):
    """subject_node_type was not one of :data:`LINK_SUBJECT_TYPES`."""


# --- record -----------------------------------------------------------------

@dataclass(frozen=True)
class FileLink:
    """One supplied-file→subject attachment. Every field is provenance or an
    operator classification; NONE is an AI interpretation."""

    link_id: str
    file_id: str
    subject_node_type: str
    subject_node_id: str
    is_primary_source: bool
    linked_by: str
    linked_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileLink":
        return cls(
            link_id=row["link_id"],
            file_id=row["file_id"],
            subject_node_type=row["subject_node_type"],
            subject_node_id=row["subject_node_id"],
            is_primary_source=bool(row["is_primary_source"]),
            linked_by=row["linked_by"],
            linked_at=row["linked_at"],
        )


@dataclass(frozen=True)
class GapRefreshResult:
    """Outcome of a :func:`refresh_no_primary_source_gap` call — the deterministic
    evidence artifact a test / audit inspects."""

    subject_node_type: str
    subject_node_id: str
    has_primary_source: bool
    gap_id: str | None          # the no_primary_source gap for the subject, if one exists
    previous_status: str | None
    new_status: str | None
    changed: bool


# --- helpers ----------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _require_text(name: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FileLinkageError(f"{name!r} is mandatory and must be a non-blank string")
    return value


def _require_subject_type(subject_node_type: str) -> str:
    if subject_node_type not in LINK_SUBJECT_TYPES:
        raise UnknownSubjectType(
            f"subject_node_type {subject_node_type!r} not in "
            f"{sorted(LINK_SUBJECT_TYPES)}"
        )
    return subject_node_type


def make_link_id(subject_node_type: str, subject_node_id: str, file_id: str) -> str:
    """Deterministic link id — the UNIQUE (file, subject) key, slugged.

    Anchors idempotency: re-linking the same file to the same subject yields the
    same id, so :func:`link_file` upserts in place instead of duplicating."""
    return f"link:{subject_node_type}:{subject_node_id}:{file_id}"


def _rows(conn: sqlite3.Connection, sql: str, params: Iterable) -> list[sqlite3.Row]:
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return cur.execute(sql, tuple(params)).fetchall()


# --- write: linkage ---------------------------------------------------------

def link_file(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    subject_node_type: str,
    subject_node_id: str,
    linked_by: str,
    is_primary_source: bool = False,
    linked_at: str | None = None,
    commit: bool = True,
) -> FileLink:
    """Attach a recorded supplied file to an area / meeting / agenda item.

    Fail-closed validation before any write:

      * ``subject_node_type`` must be in :data:`LINK_SUBJECT_TYPES`
        (:class:`UnknownSubjectType`).
      * ``file_id`` must name an existing ``supplied_files`` row — you cannot link
        a file that was never recorded (:class:`FileLinkageError`).
      * ``subject_node_id`` and ``linked_by`` must be present and non-blank.

    ``is_primary_source`` is an OPERATOR classification (source-of-record?), never
    a model output. Re-linking the same (file, subject) upserts in place (updates
    ``is_primary_source`` / ``linked_by`` / ``linked_at``), so the call is
    idempotent on the derived :func:`make_link_id`.

    NOTE: this writes ONLY the link. It does not touch the gap layer — call
    :func:`refresh_no_primary_source_gap` afterwards so the caller controls when
    the (possibly expensive over many links) recomputation happens and against
    which gap key.
    """
    _require_subject_type(subject_node_type)
    _require_text("subject_node_id", subject_node_id)
    _require_text("linked_by", linked_by)
    if not isinstance(is_primary_source, bool):
        raise FileLinkageError("is_primary_source must be a bool")

    if file_records.get_file_record(conn, file_id) is None:
        raise FileLinkageError(
            f"file_id {file_id!r} names no existing supplied_files row; "
            "record the file (B2) before linking it"
        )

    link_id = make_link_id(subject_node_type, subject_node_id, file_id)
    linked_at = linked_at or _now_utc_iso()
    # Upsert on the (file, subject) unique key: a re-link is an operator correcting
    # the attachment (e.g. flipping is_primary_source), not a duplicate row.
    conn.execute(
        "INSERT INTO supplied_file_links ("
        " link_id, file_id, subject_node_type, subject_node_id,"
        " is_primary_source, linked_by, linked_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(file_id, subject_node_type, subject_node_id) DO UPDATE SET"
        "   is_primary_source = excluded.is_primary_source,"
        "   linked_by = excluded.linked_by,"
        "   linked_at = excluded.linked_at",
        (
            link_id, file_id, subject_node_type, subject_node_id,
            1 if is_primary_source else 0, linked_by, linked_at,
        ),
    )
    if commit:
        conn.commit()
    link = _get_link(conn, link_id)
    assert link is not None  # just upserted
    return link


def unlink_file(
    conn: sqlite3.Connection,
    *,
    file_id: str,
    subject_node_type: str,
    subject_node_id: str,
    commit: bool = True,
) -> bool:
    """Remove a supplied-file→subject link. Returns True if a row was deleted.

    Provided for B5 (supersede/red-flag) and operator correction. Like
    :func:`link_file`, this does NOT touch the gap layer — the caller re-runs
    :func:`refresh_no_primary_source_gap`, which (being a full recompute) will
    reopen a gap that lost its last primary source.
    """
    _require_subject_type(subject_node_type)
    cur = conn.execute(
        "DELETE FROM supplied_file_links"
        " WHERE file_id = ? AND subject_node_type = ? AND subject_node_id = ?",
        (file_id, subject_node_type, subject_node_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def _get_link(conn: sqlite3.Connection, link_id: str) -> FileLink | None:
    rows = _rows(
        conn,
        "SELECT link_id, file_id, subject_node_type, subject_node_id,"
        " is_primary_source, linked_by, linked_at FROM supplied_file_links"
        " WHERE link_id = ?",
        (link_id,),
    )
    return FileLink.from_row(rows[0]) if rows else None


# --- read: linkage ----------------------------------------------------------

def links_for_subject(
    conn: sqlite3.Connection,
    subject_node_type: str,
    subject_node_id: str,
) -> list[FileLink]:
    """Every file linked to a subject, oldest-first (linked_at then link_id)."""
    _require_subject_type(subject_node_type)
    rows = _rows(
        conn,
        "SELECT link_id, file_id, subject_node_type, subject_node_id,"
        " is_primary_source, linked_by, linked_at FROM supplied_file_links"
        " WHERE subject_node_type = ? AND subject_node_id = ?"
        " ORDER BY linked_at, link_id",
        (subject_node_type, subject_node_id),
    )
    return [FileLink.from_row(r) for r in rows]


def links_for_file(conn: sqlite3.Connection, file_id: str) -> list[FileLink]:
    """Every subject a file is linked to, oldest-first."""
    rows = _rows(
        conn,
        "SELECT link_id, file_id, subject_node_type, subject_node_id,"
        " is_primary_source, linked_by, linked_at FROM supplied_file_links"
        " WHERE file_id = ? ORDER BY linked_at, link_id",
        (file_id,),
    )
    return [FileLink.from_row(r) for r in rows]


# --- deterministic gap-detection update -------------------------------------

def has_primary_source(
    conn: sqlite3.Connection,
    subject_node_type: str,
    subject_node_id: str,
    *,
    non_counting_states: frozenset[str] = NON_COUNTING_REVIEW_STATES,
) -> bool:
    """Does the subject have at least one linked primary source that counts?

    Deterministic + source-grounded: joins ``supplied_file_links`` to
    ``supplied_files`` and asks whether any link with ``is_primary_source = 1``
    points at a file whose ``review_state`` is NOT in ``non_counting_states``
    (default: excludes only ``rejected`` — a repudiated file is not a source).
    Reads real rows; no AI, no fabrication.
    """
    _require_subject_type(subject_node_type)
    placeholders = ", ".join("?" for _ in non_counting_states)
    exclude_clause = (
        f" AND f.review_state NOT IN ({placeholders})" if non_counting_states else ""
    )
    row = conn.execute(
        "SELECT 1 FROM supplied_file_links l"
        " JOIN supplied_files f ON f.file_id = l.file_id"
        " WHERE l.subject_node_type = ? AND l.subject_node_id = ?"
        "   AND l.is_primary_source = 1"
        f"{exclude_clause}"
        " LIMIT 1",
        (subject_node_type, subject_node_id, *sorted(non_counting_states)),
    ).fetchone()
    return row is not None


def _owned_no_primary_source_gap(
    conn: sqlite3.Connection,
    subject_node_type: str,
    subject_node_id: str,
) -> dict | None:
    """The no_primary_source gap for the subject that B4 is allowed to move —
    ``produced_by = 'deterministic'`` and ``resolved_status`` in {open, resolved}.

    A human ``acknowledged`` / ``wontfix`` gap, or an ai/human-produced gap, is
    deliberately NOT returned, so :func:`refresh_no_primary_source_gap` leaves it
    exactly as set. Returns None when there is no such gap (B4 never creates one)."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    row = cur.execute(
        "SELECT gap_id, resolved_status, produced_by FROM completeness_gaps"
        " WHERE gap_type = ? AND subject_node_type = ? AND subject_node_id = ?",
        (PRIMARY_SOURCE_GAP_TYPE, subject_node_type, subject_node_id),
    ).fetchone()
    if row is None:
        return None
    if row["produced_by"] != "deterministic":
        return None
    if row["resolved_status"] not in ("open", "resolved"):
        return None
    return dict(row)


def refresh_no_primary_source_gap(
    conn: sqlite3.Connection,
    subject_node_type: str,
    subject_node_id: str,
    *,
    commit: bool = True,
) -> GapRefreshResult:
    """Recompute the ``no_primary_source`` gap for a subject from its linked files.

    Deterministic rule (source-grounded, no AI):

      * Compute ``has_primary_source`` from the real linked rows.
      * If an *owned* ``no_primary_source`` gap exists for the subject (produced by
        the deterministic detector, currently ``open`` or ``resolved``):
          - present primary source + gap ``open``      → flip to ``resolved``
          - no primary source     + gap ``resolved``   → flip back to ``open``
          - otherwise                                  → no change (idempotent)
      * If no owned gap exists, nothing is written (B4 does not create gaps).

    Never touches publication state, never fabricates, never clobbers a human
    ``acknowledged`` / ``wontfix``. Because it recomputes from scratch it is fully
    reversible: reject or unlink the primary source and a re-run reopens the gap.

    Returns a :class:`GapRefreshResult` describing the (possibly no-op) transition.
    """
    _require_subject_type(subject_node_type)
    present = has_primary_source(conn, subject_node_type, subject_node_id)
    gap = _owned_no_primary_source_gap(conn, subject_node_type, subject_node_id)

    if gap is None:
        return GapRefreshResult(
            subject_node_type=subject_node_type,
            subject_node_id=subject_node_id,
            has_primary_source=present,
            gap_id=None,
            previous_status=None,
            new_status=None,
            changed=False,
        )

    previous = gap["resolved_status"]
    target = "resolved" if present else "open"
    if target == previous:
        return GapRefreshResult(
            subject_node_type=subject_node_type,
            subject_node_id=subject_node_id,
            has_primary_source=present,
            gap_id=gap["gap_id"],
            previous_status=previous,
            new_status=previous,
            changed=False,
        )

    conn.execute(
        "UPDATE completeness_gaps SET resolved_status = ? WHERE gap_id = ?",
        (target, gap["gap_id"]),
    )
    if commit:
        conn.commit()
    return GapRefreshResult(
        subject_node_type=subject_node_type,
        subject_node_id=subject_node_id,
        has_primary_source=present,
        gap_id=gap["gap_id"],
        previous_status=previous,
        new_status=target,
        changed=True,
    )
