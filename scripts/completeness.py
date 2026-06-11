"""First-class completeness-gap SSOT + guarded write path (GOV-125, Stage 1).

Plan §3 (GOV-125 plan rev c50ce2ad, author CTO). Builder: TranscriptEvidenceEngineer.

A *completeness gap* is a surfaced, queryable statement that some expected
evidence is absent or incomplete for a subject (a meeting, an agenda item, a
transcript, a document, a folder/date). It is the company's "do not paper over the
backfill" instrument: per GOV-124/133 ground-truth only 34/124 Alpine meeting
folders have a primary source, so ~90 meetings MUST carry a surfaced gap rather
than silently appearing complete.

Design rules (non-negotiable, plan §3):
- **Always surfaced, never silently dropped** — gaps are written to a first-class
  table (`completeness_gaps`, migration 0015) and served read-only.
- **Never gate or flip publication** — a gap is metadata about absence; it does
  NOT touch `publication_state`/`ui_status`. (Contrast: a *blocking* severity is a
  reviewer signal, not an auto-publish lever.)
- **Never fabricate** — a gap records what is MISSING. It never invents a
  timestamp, a speaker, or source text to fill the hole.
- **Deterministic** — same inputs → same `gap_id` (derived) → idempotent re-runs.

SSOT PARITY: this module is the single source of truth for the gap vocabulary.
:data:`GAP_TYPES` mirrors the `gap_type` CHECK literal in
``Database/migrations/0015_completeness_gaps.sql`` exactly; a parity test asserts
the two cannot drift (the same guard concept_map.py uses for its edge registry).

Scope: NO network, NO AI, Alpine-only, local/vault-only.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

# GOV-105 structured-PII guard, reused at the gap-`detail` write boundary (CTO
# ruling B3 + SecurityPrivacy reconciliation). NOTE the guard's documented scope
# limit: it catches only ENUMERABLE STRUCTURED PII (email/phone/SSN/address/
# voter-id) and is NAME-BLIND by design. The control for personal NAMES in a gap
# `detail` is the vault-only/reviewer-internal boundary (gap detail is never
# projected to a public surface) PLUS callers anchoring `detail` on stable node/
# doc ids rather than raw human-readable titles — NOT this guard. So this wiring
# is necessary defense-in-depth, not sufficient on its own.
from concept_map import assert_no_pii  # noqa: E402

# --- the controlled gap vocabulary (SSOT — mirrors the 0015 CHECK) ----------

# Each value is a deterministic, non-AI detectable absence. Adding one here means
# adding it to the 0015 CHECK in lockstep (the parity test enforces this).
GAP_TYPES = frozenset({
    "missing_transcript",     # a meeting has no transcript source at all
    "missing_timestamps",     # a transcript exists but carries no MM:SS locators
    "partial_agenda",         # an agenda is present but incomplete / items unparsed
    "unresolved_thread",      # an agenda thread is left open across meetings
    "no_primary_source",      # a meeting folder has only derived (.md) material
    "pdf_text_unextracted",   # a PDF source exists but its text is not extracted
    "untimed_segment",        # RESERVED — see RESERVED_GAP_TYPES (moot under Option B)
    "speaker_unattributable",  # a statement's speaker cannot be safely attributed
})

# CTO ruling B2: under Option B, untimed content creates NO transcript_segments
# (the bridge records a `missing_timestamps` gap on the transcript instead), so
# there is never an untimed SEGMENT to flag — `untimed_segment` is moot. It is
# kept in GAP_TYPES (and the 0015 CHECK) for schema/SSOT parity but is RESERVED:
# the deterministic structuring run must never emit it. `missing_timestamps` is
# the operative untimed label.
RESERVED_GAP_TYPES = frozenset({"untimed_segment"})
EMITTABLE_GAP_TYPES = GAP_TYPES - RESERVED_GAP_TYPES

SEVERITIES = frozenset({"info", "warn", "blocking"})
RESOLVED_STATUSES = frozenset({"open", "acknowledged", "resolved", "wontfix"})
PRODUCED_BY = frozenset({"deterministic", "ai", "human"})

# Default severity per gap type. Absence of a whole primary source is the headline
# backfill gap (warn — reviewer should see it, but it does not block the pipeline);
# an unparsed/partial structure is info; nothing here is `blocking` by default
# because a gap must never silently halt the deterministic run.
_DEFAULT_SEVERITY: dict[str, str] = {
    "missing_transcript": "warn",
    "missing_timestamps": "warn",
    "partial_agenda": "info",
    "unresolved_thread": "info",
    "no_primary_source": "warn",
    "pdf_text_unextracted": "info",
    "untimed_segment": "info",
    "speaker_unattributable": "info",
}


class GapError(ValueError):
    """A completeness-gap write violated the SSOT vocabulary or contract."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_severity(gap_type: str) -> str:
    """The reviewer-facing severity for a gap type (fail-closed to 'warn')."""
    return _DEFAULT_SEVERITY.get(gap_type, "warn")


def make_gap_id(subject_node_type: str, subject_node_id: str, gap_type: str) -> str:
    """Deterministic gap id — the UNIQUE (subject, type, gap_type) key, slugged.

    Idempotency is anchored on this: re-detecting the same absence yields the same
    id, so the INSERT OR IGNORE in :func:`record_gap` writes nothing the 2nd time.
    """
    return f"{gap_type}:{subject_node_type}:{subject_node_id}"


def record_gap(
    conn: sqlite3.Connection,
    *,
    subject_node_id: str,
    subject_node_type: str,
    gap_type: str,
    detail: str | None = None,
    source_id: str | None = None,
    detected_run_id: int | None = None,
    severity: str | None = None,
    produced_by: str = "deterministic",
    resolved_status: str = "open",
    commit: bool = True,
) -> str:
    """Record one completeness gap (idempotent). Returns the gap_id.

    Validates the full vocabulary up front (raising :class:`GapError` before any
    write) so an out-of-vocab gap can never reach the table even though the DB
    CHECK would also catch it — fail-closed, with a clearer error. The write is an
    ``INSERT OR IGNORE`` on the derived id, so a re-run is a no-op.

    This function NEVER touches publication state and NEVER fabricates data.
    """
    if gap_type not in GAP_TYPES:
        raise GapError(f"gap_type {gap_type!r} not in SSOT {sorted(GAP_TYPES)}")
    if gap_type in RESERVED_GAP_TYPES:
        raise GapError(
            f"gap_type {gap_type!r} is RESERVED (not emittable under Option B); "
            f"use one of {sorted(EMITTABLE_GAP_TYPES)}"
        )
    # B3 defense-in-depth: reject STRUCTURED PII in the detail at the write
    # boundary (name-blind by design — names are held by the vault-only boundary
    # + callers anchoring detail on ids, not this guard). Raises PiiGuardError.
    if detail is not None:
        assert_no_pii(detail, "completeness_gap.detail")
    sev = severity if severity is not None else default_severity(gap_type)
    if sev not in SEVERITIES:
        raise GapError(f"severity {sev!r} not in {sorted(SEVERITIES)}")
    if produced_by not in PRODUCED_BY:
        raise GapError(f"produced_by {produced_by!r} not in {sorted(PRODUCED_BY)}")
    if resolved_status not in RESOLVED_STATUSES:
        raise GapError(f"resolved_status {resolved_status!r} not in {sorted(RESOLVED_STATUSES)}")
    if not subject_node_id or not subject_node_type:
        raise GapError("gap requires non-empty subject_node_id and subject_node_type")

    gap_id = make_gap_id(subject_node_type, subject_node_id, gap_type)
    conn.execute(
        "INSERT OR IGNORE INTO completeness_gaps ("
        "gap_id, subject_node_id, subject_node_type, gap_type, severity, detail, "
        "source_id, detected_run_id, detected_utc, resolved_status, produced_by"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            gap_id,
            subject_node_id,
            subject_node_type,
            gap_type,
            sev,
            detail,
            source_id,
            detected_run_id,
            _now_utc_iso(),
            resolved_status,
            produced_by,
        ),
    )
    if commit:
        conn.commit()
    return gap_id


def gaps_for(
    conn: sqlite3.Connection,
    *,
    subject_node_id: str | None = None,
    subject_node_type: str | None = None,
    gap_type: str | None = None,
    only_open: bool = False,
) -> list[dict[str, Any]]:
    """Read gaps (always serveable — gaps are never hidden). Optional filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if subject_node_id is not None:
        clauses.append("subject_node_id = ?")
        params.append(subject_node_id)
    if subject_node_type is not None:
        clauses.append("subject_node_type = ?")
        params.append(subject_node_type)
    if gap_type is not None:
        clauses.append("gap_type = ?")
        params.append(gap_type)
    if only_open:
        clauses.append("resolved_status = 'open'")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT gap_id, subject_node_id, subject_node_type, gap_type, severity, "
        "detail, source_id, detected_run_id, detected_utc, resolved_status, produced_by "
        f"FROM completeness_gaps{where} ORDER BY gap_type, subject_node_id",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def gap_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """A surfaced summary of every gap (plan §5.5 completeness-gap report).

    Returns total + per-type + per-severity counts. This is the evidence artifact:
    the ~90 no-primary-source meetings appear here, not hidden.
    """
    by_type = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT gap_type, COUNT(*) FROM completeness_gaps GROUP BY gap_type"
        )
    }
    by_severity = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT severity, COUNT(*) FROM completeness_gaps GROUP BY severity"
        )
    }
    total = conn.execute("SELECT COUNT(*) FROM completeness_gaps").fetchone()[0]
    return {
        "total": total,
        "by_type": dict(sorted(by_type.items())),
        "by_severity": dict(sorted(by_severity.items())),
    }
