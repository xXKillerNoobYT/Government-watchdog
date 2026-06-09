"""Statement + evidence_link model with exact-source pointer (GOV-82, Slice 2 C).

Contract 1.07 §2 (exact-source pointer) + §1.4 (statement relationship semantics
/ the `evidence_link.relation` enum). Consumes GOV-80 gap analysis §4 and
decisions D-3 (relational-FK spine + evidence_links for cross-cutting),
D-4 (forward-only correction via a nullable self-reference), and D-5 (record rows
carry the 6-value RECORD verificationStatus enum directly).

This module turns the migration-0007 tables into a *guarded* write path. The two
contract invariants a single-row SQL CHECK cannot express live here:

* **No orphan claims (1.07 §2.3 / issue acceptance).** A statement is valid only
  if it has a `statement_from_segment` edge (a resolving ``segment_id``) **or** at
  least one ``evidence_link`` carrying a complete, valid ``pointer``. A statement
  with neither is an orphan and :func:`insert_statement` rejects it.
* **Complete, valid pointer (1.07 §2.1/§2.2).** Every ``evidence_link`` must carry
  the required pointer fields, the locator field matching its ``locator_kind``,
  and a ``to_source_id`` that resolves to a registry ``sources`` row.

ENUM REUSE (1.07 §5 / gap analysis D-5): the record-authoritative 6-value
``verificationStatus`` enum, the publication allowlist, and ``compute_ui_status``
are owned by :mod:`publication` and are **imported here, never re-declared**.
Re-typing them would defeat publication.py's import-time drift guards. The only
new vocabularies introduced by 1.07 (the §1.4 ``relation`` enum, the §2 locator
kinds, the §4 ``layer`` enum) are defined once below.

SCOPE (Slice 3 B, GOV-89): the 0007 scope lock is lifted — ``produced_by`` now
permits ``ai`` for the Lane-2 AI extraction path (migration 0009 widened both the
DB CHECK and the app-layer set in lockstep, per CTO D-1). ``ai`` is gated by the
fail-closed defaults (``machine_extracted_unreviewed`` / ``not_publishable`` /
``unreviewed``) + the mandatory source anchor + the run ledger, NOT by exclusion.
The AI writer lives in :mod:`ai_extraction` and reuses :func:`insert_statement`
unchanged. No network here, Alpine-only, local/vault-only.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

# Reuse the SSOT enums — do NOT re-type them (1.07 §5; gap analysis §7/D-5).
import publication as pub

# --- vocabularies introduced by 1.07 (defined once, here) ------------------

# §1.4 evidence_link.relation — distinct from the edge *type*; carries both
# provenance ("references"/"substantiates") and analysis ("supports"/
# "contradicts"/"corrects") semantics.
ALLOWED_EVIDENCE_RELATIONS = frozenset(
    {"references", "supports", "contradicts", "corrects", "substantiates"}
)

# §2.2 locator_kind — selects which locator field on the pointer is authoritative.
ALLOWED_LOCATOR_KINDS = frozenset({"timestamp", "page", "section", "paragraph"})

# The locator field(s) that MUST be present (non-null) for each locator_kind.
LOCATOR_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp_seconds", "timestamp_human"),
    "page": ("page",),
    "section": ("section",),
    "paragraph": ("paragraph",),
}

# §4 append-only layer enum (shared by statements + evidence_links).
ALLOWED_LAYERS = frozenset(
    {"known_then", "presented_then", "ai_thought_then", "corrected_later", "actual_later"}
)

# §2.2 archive_status pair domain.
ALLOWED_ARCHIVE_STATUSES = frozenset({"available", "unavailable", "not_checked"})

ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})

# Slice 3 B (GOV-89) widens the app-layer set to the full SSOT producer set so
# the Lane-2 AI path can land. CTO D-1 ruling (GOV-88 §5): both layers move
# together — this set and the migration-0009 ``statements.produced_by`` CHECK are
# widened in lockstep. The equality (not subset) assertion is the parity guard
# that the app layer cannot drift from the SSOT enum in either direction.
ALLOWED_STATEMENT_PRODUCED_BY = frozenset(pub.ALLOWED_PRODUCED_BY)  # {automation, ai, human}
assert ALLOWED_STATEMENT_PRODUCED_BY == pub.ALLOWED_PRODUCED_BY, (
    "statement produced_by drifted from the SSOT publication.ALLOWED_PRODUCED_BY"
)

# Pointer keys that must be present and non-null on every evidence_link,
# independent of locator_kind (1.07 §2.2 "Required").
_POINTER_REQUIRED = (
    "to_source_id",
    "relation",
    "original_url",
    "archive_status",
    "scan_date",
    "captured_at_utc",
    "locator_kind",
    "verification_status",
    "confidence",
)


class PointerError(ValueError):
    """An evidence_link's pointer is incomplete or invalid (1.07 §2.2/§2.3)."""


class OrphanClaimError(ValueError):
    """A statement has neither a segment edge nor a complete source pointer (§2.3)."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def validate_pointer(pointer: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> None:
    """Validate a single evidence_link pointer (1.07 §2.2/§2.3). Raises on failure.

    Checks, in order: required keys present; enum domains
    (``relation``/``locator_kind``/``archive_status``/``verification_status``/
    ``confidence``); the locator field matching ``locator_kind`` is present
    (§2.3 — ``locator_kind: timestamp`` with null ``timestamp_seconds`` is
    rejected); and, when a ``conn`` is given, that ``to_source_id`` resolves to a
    registry ``sources`` row carrying its classification (§2.2 "must resolve to a
    source_record").
    """
    for key in _POINTER_REQUIRED:
        if _is_missing(pointer.get(key)):
            raise PointerError(f"pointer missing required field {key!r}: {pointer!r}")

    relation = pointer["relation"]
    if relation not in ALLOWED_EVIDENCE_RELATIONS:
        raise PointerError(
            f"relation {relation!r} not in {sorted(ALLOWED_EVIDENCE_RELATIONS)}"
        )

    locator_kind = pointer["locator_kind"]
    if locator_kind not in ALLOWED_LOCATOR_KINDS:
        raise PointerError(
            f"locator_kind {locator_kind!r} not in {sorted(ALLOWED_LOCATOR_KINDS)}"
        )
    for field in LOCATOR_REQUIRED_FIELDS[locator_kind]:
        if _is_missing(pointer.get(field)):
            raise PointerError(
                f"locator_kind={locator_kind!r} requires non-null {field!r}: {pointer!r}"
            )

    if pointer["archive_status"] not in ALLOWED_ARCHIVE_STATUSES:
        raise PointerError(f"archive_status {pointer['archive_status']!r} invalid")

    # Reuse the SSOT enum — never a local copy (D-5).
    if pointer["verification_status"] not in pub.ALLOWED_VERIFICATION_STATUSES:
        raise PointerError(
            f"verification_status {pointer['verification_status']!r} not a 6-value record status"
        )
    if pointer["confidence"] not in ALLOWED_CONFIDENCE:
        raise PointerError(f"confidence {pointer['confidence']!r} invalid")

    layer = pointer.get("layer", "known_then")
    if layer not in ALLOWED_LAYERS:
        raise PointerError(f"layer {layer!r} not in {sorted(ALLOWED_LAYERS)}")

    if conn is not None:
        row = conn.execute(
            "SELECT source_id, source_type, source_class FROM sources WHERE source_id = ?",
            (pointer["to_source_id"],),
        ).fetchone()
        if row is None:
            raise PointerError(
                f"to_source_id {pointer['to_source_id']!r} does not resolve to a registry source"
            )
        # §2.2: the source-derived classification must exist on the resolved row.
        if _is_missing(row["source_type"]) or _is_missing(row["source_class"]):
            raise PointerError(
                f"resolved source {pointer['to_source_id']!r} lacks source_type/source_class"
            )


def is_orphan(
    statement: dict[str, Any],
    evidence_links: list[dict[str, Any]] | None,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """True if the statement has neither a resolving segment edge nor a valid pointer.

    The issue's "no orphan claims" disjunction: a ``statement_from_segment`` edge
    (a ``segment_id`` that, when ``conn`` is given, resolves to a
    ``transcript_segments`` row) OR at least one ``evidence_link`` whose pointer
    passes :func:`validate_pointer`. A dangling ``segment_id`` (set but not
    resolving) does not satisfy the rule.
    """
    has_segment = not _is_missing(statement.get("segment_id"))
    if has_segment and conn is not None:
        has_segment = (
            conn.execute(
                "SELECT 1 FROM transcript_segments WHERE segment_id = ?",
                (statement["segment_id"],),
            ).fetchone()
            is not None
        )

    has_valid_pointer = False
    for link in evidence_links or []:
        try:
            validate_pointer(link, conn=conn)
            has_valid_pointer = True
            break
        except PointerError:
            continue

    return not (has_segment or has_valid_pointer)


def insert_statement(
    conn: sqlite3.Connection,
    statement: dict[str, Any],
    evidence_links: list[dict[str, Any]] | None = None,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Insert one statement + its evidence_links under the contract invariants.

    Rejects (raising before any write) an orphan claim
    (:class:`OrphanClaimError`) or an evidence_link with an invalid pointer
    (:class:`PointerError`). On success, writes the statement (default
    ``publication_state = not_publishable``, default ``verification_status =
    machine_extracted_unreviewed``) and every evidence_link in one transaction,
    and stamps the statement's ``ui_status`` via the SSOT
    :func:`publication.compute_ui_status`. Never auto-flips ``publication_state``.

    Returns the inserted statement dict (with derived ``ui_status``).
    """
    evidence_links = list(evidence_links or [])

    statement_id = statement.get("statement_id")
    if _is_missing(statement_id):
        raise ValueError("statement requires a non-empty statement_id")
    if _is_missing(statement.get("statement_text")):
        raise ValueError("statement requires non-empty statement_text")

    produced_by = statement.get("produced_by", "automation")
    if produced_by not in ALLOWED_STATEMENT_PRODUCED_BY:
        raise ValueError(
            f"produced_by {produced_by!r} not in {sorted(ALLOWED_STATEMENT_PRODUCED_BY)}"
        )

    verification_status = statement.get("verification_status", "machine_extracted_unreviewed")
    if verification_status not in pub.ALLOWED_VERIFICATION_STATUSES:
        raise ValueError(
            f"verification_status {verification_status!r} not a 6-value record status"
        )

    layer = statement.get("layer", "known_then")
    if layer not in ALLOWED_LAYERS:
        raise ValueError(f"layer {layer!r} not in {sorted(ALLOWED_LAYERS)}")

    confidence = statement.get("confidence", "medium")
    if confidence not in ALLOWED_CONFIDENCE:
        raise ValueError(f"confidence {confidence!r} invalid")

    # Validate every provided pointer up front (fail before any write).
    for link in evidence_links:
        validate_pointer(link, conn=conn)

    # No orphan claims: segment edge OR a complete pointer (both already
    # validated above, so any present link is valid here).
    if is_orphan(statement, evidence_links, conn=conn):
        raise OrphanClaimError(
            f"statement {statement_id!r} has no statement_from_segment edge and no "
            f"complete evidence_link pointer — orphan claim rejected (1.07 §2.3)"
        )

    # Fail-closed publication gate: never auto-publishable.
    publication_state = statement.get("publication_state", pub.DEFAULT_PUBLICATION_STATE)
    if publication_state not in pub.ALLOWED_PUBLICATION_STATES:
        raise ValueError(f"publication_state {publication_state!r} invalid")

    source_changed = 1 if statement.get("source_changed") else 0
    source_present = not _is_missing(statement.get("segment_id")) or any(
        not _is_missing(link.get("to_source_id")) for link in evidence_links
    )
    archive_present = any(link.get("archive_status") == "available" for link in evidence_links)

    ui_status = pub.compute_ui_status(
        {
            "verificationStatus": verification_status,
            "correctionStatus": statement.get("correction_status", "none"),
            "sourceChanged": bool(source_changed),
            "sourcePresent": source_present,
            "archivePresent": archive_present,
            "rawPreserved": bool(statement.get("raw_preserved")),
        }
    )

    # Per-record gateway-run provenance (0009): an AI-produced row names the run
    # that produced it; every evidence_link inherits the same run id unless it
    # carries its own. NULL for automation/human rows.
    run_id = statement.get("ai_extraction_run_id")

    now = _now_utc_iso()
    conn.execute(
        "INSERT INTO statements ("
        "statement_id, segment_id, agenda_item_id, speaker_attribution_id, "
        "statement_text, is_verbatim, layer, produced_by, verification_status, "
        "correction_status, review_state, publication_state, source_changed, "
        "ui_status, confidence, updates_statement_id, ai_extraction_run_id, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            statement_id,
            statement.get("segment_id"),
            statement.get("agenda_item_id"),
            statement.get("speaker_attribution_id"),
            statement["statement_text"],
            1 if statement.get("is_verbatim", 1) else 0,
            layer,
            produced_by,
            verification_status,
            statement.get("correction_status", "none"),
            statement.get("review_state", "unreviewed"),
            publication_state,
            source_changed,
            ui_status,
            confidence,
            statement.get("updates_statement_id"),
            run_id,
            now,
        ),
    )

    for index, link in enumerate(evidence_links):
        link_id = link.get("evidence_link_id") or f"{statement_id}:ev-{index:04d}"
        conn.execute(
            "INSERT INTO evidence_links ("
            "evidence_link_id, from_node_id, from_node_type, to_source_id, relation, "
            "layer, locator_kind, timestamp_seconds, timestamp_human, page, section, "
            "paragraph, original_url, final_url, archive_url, archive_status, scan_date, "
            "captured_at_utc, agenda_item_id, is_verbatim, verification_status, "
            "correction_status, confidence, transcript_path, deep_link, "
            "ai_extraction_run_id, created_utc"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                link_id,
                link.get("from_node_id", statement_id),
                link.get("from_node_type", "statement"),
                link["to_source_id"],
                link["relation"],
                link.get("layer", "known_then"),
                link["locator_kind"],
                link.get("timestamp_seconds"),
                link.get("timestamp_human"),
                link.get("page"),
                link.get("section"),
                link.get("paragraph"),
                link.get("original_url"),
                link.get("final_url"),
                link.get("archive_url"),
                link.get("archive_status", "not_checked"),
                link.get("scan_date"),
                link.get("captured_at_utc"),
                link.get("agenda_item_id"),
                1 if link.get("is_verbatim", 1) else 0,
                link.get("verification_status", "machine_extracted_unreviewed"),
                link.get("correction_status", "none"),
                link.get("confidence", "medium"),
                link.get("transcript_path"),
                link.get("deep_link"),
                link.get("ai_extraction_run_id", run_id),
                now,
            ),
        )

    if commit:
        conn.commit()

    result = dict(statement)
    result["ui_status"] = ui_status
    result["publication_state"] = publication_state
    return result
