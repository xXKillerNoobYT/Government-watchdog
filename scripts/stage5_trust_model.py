"""Stage 5.07 transcript/evidence/statement trust model (GOV-531) — reviewer-internal, Alpine.

Authors the **Stage 5.07 trust model** per the CTO contract recorded on GOV-530
(``Docs/stage5-07-trust-model-contract.md``): a deterministic, idempotent,
reviewer-internal *model layer* that formalizes four trust mechanics over the
already-merged statement/evidence/transcript substrate — no AI, no network, no new
claims. Each model is a presentation-precursor projection over the Stage-3/4/5 read
surface, NOT a new source of truth:

* **§0 recordSeparation** (:func:`build_record_separation`) — the five-way record
  separation made queryable. Every meaningful claim stays in exactly one of
  ``fact | summary | action_outcome | ai_assumption | verification_correction``,
  derived from — never collapsing — the existing ``statements.ALLOWED_LAYERS`` enum
  (``known_then / presented_then / actual_later / ai_thought_then / corrected_later``).
  The mapping is total over that SSOT enum (import-time parity guard), so an AI
  assumption can never be silently re-bucketed as a verified fact. Source trail +
  review status ride alongside every record.
* **§1 corrections** (:func:`build_corrections`) — Model 1, the forward-only correction
  state model. One typed correction edge per *corrected record*, pointing the corrected
  ``known_then`` record at its superseding record via the forward-only
  ``updates_statement_id`` spine (migration 0007 §D-4) — never rewriting then-known
  context. Adds ``correctionEffectiveFrom`` (the correction date) + ``correctionStatus
  ∈ {replaced, superseded, corrected, amended}`` and the
  :func:`effective_view_at` time-travel helper (a record's effective view at time T
  reflects corrections with ``effectiveFrom ≤ T``; the historical record stays
  preserved + addressable). Fail-closed: an unresolved superseding ref →
  ``correction_unresolved`` gap, never a fabricated ref.
* **§2 hotTopicReasons** (:func:`build_hot_topic_reasons`) — Model 2, the hot-topic
  reason model. WHO/WHAT marked a topic (``markedBy``) + WHY (a reason grounded in a
  record/source ref), distinct from the 5.05 salience *score* — this adds the
  *reason/provenance*. Multiple deterministic markers per topic; a record whose
  topic/agenda anchor does not resolve carries the ``topic_anchor_missing`` gap
  (resolving the 5.05 latent agenda_thread anchor).
* **§3 sourceChangeArchive** (:func:`build_source_change_archive`) — Model 3, the
  source-change + archive verification model. Reuses the 5.03 inventory verbatim and
  formalizes the lifecycle ↔ archive-availability binding so changed/disappeared/
  replaced sources are first-class + representable. http(s)-only URLs (``file://``
  already stripped upstream; re-guarded here).
* **§4 assumptionVerifications** (:func:`build_assumption_verifications`) — Model 4, the
  future-fact verification model. A past AI assumption (``layer == ai_thought_then``)
  can later be marked ``verificationOutcome ∈ {supported, contradicted,
  partially_supported, corrected, unresolved}`` with ``verificationOrigin`` (who/what),
  ``verificationMethod`` (how), a verifying source/evidence ref, and a verification
  date. Forward-only like corrections (the verification is a *later* record attached
  via ``updates_statement_id``; the original assumption is never mutated). Fail-closed:
  an un-reverified assumption reads ``unresolved`` — never silently upgraded to fact.

A single envelope hash — :func:`_trust_digest` over the five model envelopes — is the
only hash exposed (no per-source raw-content hash). The ``--check`` CLI is the CI gate
(exit 1 on any defect).

Boundary rules (contract §5 / premium I1–I8), restated as trust-model invariants:

* it **never** mutates / re-derives ``read_api.py`` / ``publication.py`` /
  ``statements.py`` / ``stage4_*`` / ``stage5_*`` — it consumes them read-only (I4/I7),
  so the merged read surface is the *only* vocabulary;
* every emitted artifact is transport-swept by :func:`read_api.assert_no_raw_paths`, so
  a FS path / ``.sha256`` / vault marker / ``file://`` that slipped a column fails
  LOUDLY at the boundary (I1); ``localSourcePath`` is never emitted (I2); exactly one
  envelope ``trustDigest`` is exposed — no per-source raw hash (I3);
* it runs entirely at ``access: reviewer_internal`` / ``scope: alpine`` and is absent
  from any public / ``published_records`` path (I6); public launch stays Isaac-gated
  (GOV-420), untouched here.

Pure function of the registry: same DB -> byte-identical trust envelope (idempotent
re-projection). No mutation, no AI, no network.
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
import read_api  # noqa: E402  (consumed read-only: serve + transport sweep + url helper)
import statements as st  # noqa: E402  (the SSOT layer enum — imported, never re-typed)
import stage3_card_feed as card_feed  # noqa: E402  (status + card-handle + date, by reference)
import stage5_source_inventory as inv  # noqa: E402  (the merged 5.03 contract — consumed)

SCOPE = "alpine"
ACCESS = "reviewer_internal"  # never "public" — I6


class TrustModelError(AssertionError):
    """Raised when an emitted trust-model artifact violates a GOV-531 contract invariant."""


# ---------------------------------------------------------------------------
# §0 — five-way record separation (mapped onto the SSOT layer enum, total)
# ---------------------------------------------------------------------------

# The five conceptual record classes (contract §0 / issue "five-way separation").
RECORD_CLASS_FACT = "fact"
RECORD_CLASS_SUMMARY = "summary"
RECORD_CLASS_ACTION_OUTCOME = "action_outcome"
RECORD_CLASS_AI_ASSUMPTION = "ai_assumption"
RECORD_CLASS_VERIFICATION_CORRECTION = "verification_correction"

RECORD_CLASSES: frozenset[str] = frozenset(
    {
        RECORD_CLASS_FACT,
        RECORD_CLASS_SUMMARY,
        RECORD_CLASS_ACTION_OUTCOME,
        RECORD_CLASS_AI_ASSUMPTION,
        RECORD_CLASS_VERIFICATION_CORRECTION,
    }
)

# The mapping from the existing ``statements.ALLOWED_LAYERS`` enum onto the five
# conceptual classes. Defined ONCE here; the enum itself is owned by statements.py
# and only *referenced*. The known-then/presented-then/ai-thought-then/corrected-later/
# actual-later five-way IS the issue's fact/summary/ai_assumption/verification/
# action-outcome separation.
LAYER_TO_RECORD_CLASS: dict[str, str] = {
    "known_then": RECORD_CLASS_FACT,
    "presented_then": RECORD_CLASS_SUMMARY,
    "actual_later": RECORD_CLASS_ACTION_OUTCOME,
    "ai_thought_then": RECORD_CLASS_AI_ASSUMPTION,
    "corrected_later": RECORD_CLASS_VERIFICATION_CORRECTION,
}

# The layer that marks a *past AI assumption* (the §4 unit). Named once.
LAYER_AI_ASSUMPTION = "ai_thought_then"

# Parity guard (mirrors the statements.py produced_by parity assertion): the
# five-way mapping must be TOTAL over the SSOT layer enum — every allowed layer maps
# to exactly one class, and no mapped layer is outside the SSOT. A future widening of
# ALLOWED_LAYERS that this map does not cover fails at import time, never silently.
assert set(LAYER_TO_RECORD_CLASS) == set(st.ALLOWED_LAYERS), (
    "LAYER_TO_RECORD_CLASS drifted from statements.ALLOWED_LAYERS: "
    f"{set(LAYER_TO_RECORD_CLASS) ^ set(st.ALLOWED_LAYERS)}"
)
assert set(LAYER_TO_RECORD_CLASS.values()) <= RECORD_CLASSES


def record_class(layer: Any) -> str | None:
    """Map a served record's ``layer`` onto its five-way record class, or ``None``.

    Pure lookup over :data:`LAYER_TO_RECORD_CLASS`. An absent/poisoned layer returns
    ``None`` (the record is surfaced uncategorized — a gap, never silently bucketed as
    a fact). Load-bearing for §0: neutering this to a constant collapses the five-way
    separation, which the separation guard catches.
    """
    if not isinstance(layer, str):
        return None
    return LAYER_TO_RECORD_CLASS.get(layer)


def _source_trail(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The web-safe source trail for a served record (one entry per evidence link).

    Only already-allowlisted public locators ride along (``to_source_id`` /
    ``original_url`` / ``archive_url`` / ``scan_date`` / ``relation`` /
    ``locator_kind``). No raw path, no transcript_path/deep_link (stripped upstream).
    """
    trail: list[dict[str, Any]] = []
    for link in record.get("evidence", []):
        trail.append(
            {
                "toSourceId": link.get("to_source_id"),
                "originalUrl": _web_url_or_none(link.get("original_url")),
                "archiveUrl": _web_url_or_none(link.get("archive_url")),
                "scanDate": link.get("scan_date"),
                "relation": link.get("relation"),
                "locatorKind": link.get("locator_kind"),
            }
        )
    return trail


def build_record_separation(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One entry per served record, bucketed into its five-way class (§0, queryable).

    Each entry preserves the record's source trail + review status (verification +
    provenance) so every meaningful claim is addressable in exactly one layer. A record
    whose ``layer`` does not resolve carries ``recordClass = None`` + the
    ``layer_unresolved`` gap (never silently treated as a fact). Order is by statement
    id (byte-stable).
    """
    records = read_api.reviewer_internal_records(conn)
    entries: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r["statement_id"]):
        cls = record_class(record.get("layer"))
        entries.append(
            {
                "statementId": record["statement_id"],
                "ref": _record_card_id(record),
                "layer": record.get("layer"),
                "recordClass": cls,
                "status": card_feed._compose_record_status(record),
                "reviewStatus": {
                    "verificationStatus": record.get("verification_status"),
                    "provenanceStatus": record.get("provenance_status"),
                },
                "sourceTrail": _source_trail(record),
                "gaps": [] if cls is not None else [GAP_LAYER_UNRESOLVED],
            }
        )
    return entries


GAP_LAYER_UNRESOLVED = "layer_unresolved"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _record_card_id(record: dict[str, Any]) -> str:
    """The Stage-3 card handle for a served record (matches a feed item's ``cardIds``)."""
    card_type = card_feed._resolve_record_type(record)
    return card_feed.card_handle(card_type, record["statement_id"])


def _web_url_or_none(value: Any) -> str | None:
    """A value only when it is a genuine public ``http(s)://`` URL, else ``None``.

    Defense-in-depth over the upstream non-web-URL strip: a ``file://`` / vault URI
    that somehow rode a column is dropped here too (and the transport sweep is the loud
    backstop). http(s)-only — contract Model 3 invariant.
    """
    if isinstance(value, str) and read_api._is_web_url(value):
        return value
    return None


def _superseding_index(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{updated_statement_id: [superseding_statement_id, ...]}`` over served records.

    Inverts the forward-only ``updates_statement_id`` correction spine (migration 0007
    §D-4) over the *served* set, built ONLY from web-safe served fields (no raw DB
    re-read), so it can never reintroduce a stripped column. Deterministic order.
    """
    index: dict[str, list[str]] = {}
    for record in records:
        updates = record.get("updates_statement_id")
        if updates:
            index.setdefault(updates, []).append(record["statement_id"])
    for key in index:
        index[key] = sorted(index[key])
    return index


def _replaced_sources(conn: sqlite3.Connection) -> frozenset[str]:
    """Source ids whose merged 5.03 lifecycle state is ``replaced`` (consumed by ref)."""
    body = inv.build_inventory(conn)
    return frozenset(
        entry["source_id"]
        for entry in body.get("sources", [])
        if entry.get("lifecycle", {}).get("state") == inv.LIFECYCLE_REPLACED
    )


# ---------------------------------------------------------------------------
# §1 — Model 1: correction state model (forward-only + effective-date semantics)
# ---------------------------------------------------------------------------

# A record is a *corrected record* when its claim-level ``correction_status`` is an
# active correction state, OR its source's 5.03 lifecycle is ``replaced``. The active
# set is exactly the Model 1 ``correctionStatus`` enum. A frozenset so a future value is
# a conscious, reviewed change — never accidental drift.
CORRECTION_STATUSES: frozenset[str] = frozenset(
    {"replaced", "superseded", "corrected", "amended"}
)

GAP_CORRECTION_UNRESOLVED = "correction_unresolved"


def is_corrected_record(
    record: dict[str, Any], replaced_sources: frozenset[str]
) -> bool:
    """True iff ``record`` is a corrected record (the §1 trigger set).

    Either its claim-level ``correction_status`` is in :data:`CORRECTION_STATUSES`, or
    one of its evidence sources has 5.03 ``lifecycle == replaced``. Pure function of the
    served record + the precomputed replaced-source set.
    """
    if record.get("correction_status") in CORRECTION_STATUSES:
        return True
    return any(
        link.get("to_source_id") in replaced_sources
        for link in record.get("evidence", [])
    )


def resolve_correction_edge(
    corrected_statement_id: str,
    superseding_index: dict[str, list[str]],
    records_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve the superseding ref + effective date for a corrected record, or ``None``.

    Load-bearing (I5): reads the ``updates_statement_id`` correction spine
    (``superseding_index``) to find the served record that supersedes
    ``corrected_statement_id``. Returns ``{supersedingStatementId, supersedingRef,
    correctionEffectiveFrom}`` for the FIRST superseding record, or ``None`` when no
    superseding record is served — the fail-closed branch the ledger surfaces as
    :data:`GAP_CORRECTION_UNRESOLVED`. ``correctionEffectiveFrom`` is the superseding
    record's own grounded card date (the correction date), never invented. Never
    fabricates a ref.
    """
    superseders = superseding_index.get(corrected_statement_id, [])
    for superseder_id in superseders:
        superseder = records_by_id.get(superseder_id)
        if superseder is not None:
            return {
                "supersedingStatementId": superseder_id,
                "supersedingRef": _record_card_id(superseder),
                "correctionEffectiveFrom": card_feed._card_date(superseder),
            }
    return None


def build_corrections(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One typed forward-only correction edge per corrected record (§1, deterministic).

    Each edge preserves the corrected ``known_then`` context (status / recordDate /
    correctionStatus, never mutated) and points it at its resolved superseding ref +
    ``correctionEffectiveFrom`` — or carries the ``correction_unresolved`` gap when none
    resolves. Order is by the corrected statement id so the same DB yields a byte-
    identical ledger.
    """
    records = read_api.reviewer_internal_records(conn)
    records_by_id = {r["statement_id"]: r for r in records}
    superseding_index = _superseding_index(records)
    replaced_sources = _replaced_sources(conn)

    edges: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r["statement_id"]):
        if not is_corrected_record(record, replaced_sources):
            continue
        statement_id = record["statement_id"]
        resolved = resolve_correction_edge(statement_id, superseding_index, records_by_id)
        edge: dict[str, Any] = {
            "correctedStatementId": statement_id,
            "correctedRef": _record_card_id(record),
            "correctionStatus": record.get("correction_status"),
            # known-then context preserved verbatim (never rewritten — D-4).
            "knownThen": {
                "status": card_feed._compose_record_status(record),
                "recordDate": card_feed._card_date(record),
                "sourceConfidence": record.get("confidence_label"),
            },
            "supersedingStatementId": resolved["supersedingStatementId"] if resolved else None,
            "supersedingRef": resolved["supersedingRef"] if resolved else None,
            "correctionEffectiveFrom": resolved["correctionEffectiveFrom"] if resolved else None,
            "resolved": resolved is not None,
            "gaps": [] if resolved is not None else [GAP_CORRECTION_UNRESOLVED],
        }
        edges.append(edge)
    return edges


def effective_view_at(
    corrections: list[dict[str, Any]], as_of: str
) -> list[dict[str, Any]]:
    """The corrections in force at time ``as_of`` — forward-only, no history rewrite.

    A record's effective view at time T reflects corrections whose
    ``correctionEffectiveFrom`` is non-null and ``<= as_of`` (ISO date string compare,
    which is lexicographically date-ordered). A correction dated AFTER ``as_of`` — or
    an unresolved one with no effective date — is NOT yet in force, so the historical
    then-known record stays preserved + addressable (never rewritten retroactively).
    Pure function of the already-built corrections list + the query date.
    """
    in_force: list[dict[str, Any]] = []
    for edge in corrections:
        effective = edge.get("correctionEffectiveFrom")
        if isinstance(effective, str) and effective and effective <= as_of:
            in_force.append(edge)
    return in_force


# ---------------------------------------------------------------------------
# §2 — Model 2: hot-topic reason model (WHO/WHAT marked + WHY, deterministic)
# ---------------------------------------------------------------------------

# WHO/WHAT marked a topic (contract Model 2). A frozenset SSOT so a future value is a
# conscious, reviewed change. The deterministic build emits only the markers it can
# GROUND in a registry signal: ``changed_record`` (a corrected record on the topic),
# ``repeated_discussion`` (activity at/above the floor), ``system_signal`` (recent
# activity in the corpus recency window). The human/external markers ``auditor`` /
# ``isaac_admin`` / ``public_attention`` stay in the vocab for a future human-sourced
# marker path but are NEVER fabricated here without a grounding source (fail closed).
MARKED_BY_SYSTEM_SIGNAL = "system_signal"
MARKED_BY_AUDITOR = "auditor"
MARKED_BY_ISAAC_ADMIN = "isaac_admin"
MARKED_BY_PUBLIC_ATTENTION = "public_attention"
MARKED_BY_REPEATED_DISCUSSION = "repeated_discussion"
MARKED_BY_CHANGED_RECORD = "changed_record"

MARKED_BY_VALUES: frozenset[str] = frozenset(
    {
        MARKED_BY_SYSTEM_SIGNAL,
        MARKED_BY_AUDITOR,
        MARKED_BY_ISAAC_ADMIN,
        MARKED_BY_PUBLIC_ATTENTION,
        MARKED_BY_REPEATED_DISCUSSION,
        MARKED_BY_CHANGED_RECORD,
    }
)

# A topic with at least this many anchored records is a "repeated discussion".
REPEATED_DISCUSSION_FLOOR = 2

GAP_TOPIC_ANCHOR_MISSING = "topic_anchor_missing"


def _record_topic_anchors(record: dict[str, Any]) -> list[str]:
    """The topic/issue anchors a served record belongs to (the §2 marker unit).

    Primary: any explicit ``topic_id`` on the web-safe evidence drawer (the direct
    topic edge — forward-looking, sparse today). Fallback: the record's
    ``agenda_item_id`` — the agenda thread the claim sits in, which Isaac's concept map
    treats as the issue/topic anchor. A record with neither is uncategorized and
    contributes to no topic (an honest gap, surfaced as ``topic_anchor_missing``). All
    web-safe ids — no raw locator.
    """
    anchors = {
        link.get("topic_id")
        for link in record.get("evidence", [])
        if link.get("topic_id")
    }
    agenda_item_id = record.get("agenda_item_id")
    if not anchors and agenda_item_id:
        anchors.add(agenda_item_id)
    return sorted(a for a in anchors if a)


def build_hot_topic_reasons(conn: sqlite3.Connection) -> dict[str, Any]:
    """The hot-topic *reason* model: WHO/WHAT marked each topic + WHY (§2).

    Returns ``{topics: [...], unanchored: [...]}``. Each topic carries one or more
    deterministic markers, each a ``{markedBy, why}`` with ``why`` grounding the marker
    in resolvable record refs (never editorialized free text). ``unanchored`` lists the
    refs of served records whose topic/agenda anchor did not resolve — each surfaced
    with the ``topic_anchor_missing`` gap, resolving the 5.05 latent agenda_thread
    anchor honestly. Deterministic ordering throughout (topicId asc; markedBy asc).
    """
    records = read_api.reviewer_internal_records(conn)
    replaced_sources = _replaced_sources(conn)
    anchor = _corpus_recency_anchor(records)

    per_topic: dict[str, dict[str, Any]] = {}
    unanchored: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r["statement_id"]):
        anchors = _record_topic_anchors(record)
        ref = _record_card_id(record)
        if not anchors:
            unanchored.append({"ref": ref, "gaps": [GAP_TOPIC_ANCHOR_MISSING]})
            continue
        corrected = is_corrected_record(record, replaced_sources)
        recent = _in_recency_window(record, anchor)
        for topic_id in anchors:
            bucket = per_topic.setdefault(
                topic_id,
                {"activity": [], "corrected": [], "recent": []},
            )
            bucket["activity"].append(ref)
            if corrected:
                bucket["corrected"].append(ref)
            if recent:
                bucket["recent"].append(ref)

    topics: list[dict[str, Any]] = []
    for topic_id in sorted(per_topic):
        bucket = per_topic[topic_id]
        markers: list[dict[str, Any]] = []
        if bucket["corrected"]:
            markers.append(
                {
                    "markedBy": MARKED_BY_CHANGED_RECORD,
                    "why": {
                        "reason": "a record on this topic was corrected/superseded",
                        "groundingRefs": sorted(bucket["corrected"]),
                    },
                }
            )
        if len(bucket["activity"]) >= REPEATED_DISCUSSION_FLOOR:
            markers.append(
                {
                    "markedBy": MARKED_BY_REPEATED_DISCUSSION,
                    "why": {
                        "reason": "multiple records anchor to this topic",
                        "groundingRefs": sorted(bucket["activity"]),
                    },
                }
            )
        if bucket["recent"]:
            markers.append(
                {
                    "markedBy": MARKED_BY_SYSTEM_SIGNAL,
                    "why": {
                        "reason": "recent activity within the corpus recency window",
                        "groundingRefs": sorted(bucket["recent"]),
                    },
                }
            )
        # markers already appended in a fixed order; sort by markedBy for byte-stability.
        markers.sort(key=lambda m: m["markedBy"])
        topics.append(
            {
                "topicId": topic_id,
                "activityCount": len(bucket["activity"]),
                "markers": markers,
            }
        )
    return {"topics": topics, "unanchored": unanchored}


# --- recency window (deterministic, anchored to the corpus's own newest scan) -------

# Recency window (days) around the corpus anchor — mirrors the 5.05 window. Anchored to
# the data's own newest scan, NOT a wall clock, so the model is a pure function of the
# DB (idempotent — no ``Date.now()``).
RECENCY_WINDOW_DAYS = 31


def _record_scan_date(record: dict[str, Any]) -> str | None:
    """The record's newest evidence ``scan_date`` (a web-safe ISO date, never a locator)."""
    scans = [
        link.get("scan_date")
        for link in record.get("evidence", [])
        if isinstance(link.get("scan_date"), str) and link.get("scan_date")
    ]
    if scans:
        return max(scans)
    return card_feed._card_date(record)


def _corpus_recency_anchor(records: list[dict[str, Any]]):
    """The corpus's own newest scan date — the deterministic recency window anchor."""
    import datetime as _dt  # local: only the recency helpers need it

    dates = []
    for record in records:
        raw = _record_scan_date(record)
        if isinstance(raw, str) and raw:
            try:
                dates.append(_dt.date.fromisoformat(raw))
            except ValueError:
                continue
    return max(dates) if dates else None


def _in_recency_window(record: dict[str, Any], anchor) -> bool:
    """True iff the record's scan date is within :data:`RECENCY_WINDOW_DAYS` of ``anchor``."""
    import datetime as _dt

    if anchor is None:
        return False
    raw = _record_scan_date(record)
    if not isinstance(raw, str) or not raw:
        return False
    try:
        scan = _dt.date.fromisoformat(raw)
    except ValueError:
        return False
    return abs((anchor - scan).days) <= RECENCY_WINDOW_DAYS


# ---------------------------------------------------------------------------
# §3 — Model 3: source-change + archive verification model
# ---------------------------------------------------------------------------

# The lifecycle ↔ archive binding (contract Model 3). A non-``unchanged`` source SHOULD
# be archive-backed to remain representable after it changed; an ``unchanged`` source is
# a live source (archive optional).
ARCHIVE_BINDING_LIVE = "live_source"
ARCHIVE_BINDING_BACKED = "archive_backed"
ARCHIVE_BINDING_GAP = "archive_gap"

ARCHIVE_BINDINGS: frozenset[str] = frozenset(
    {ARCHIVE_BINDING_LIVE, ARCHIVE_BINDING_BACKED, ARCHIVE_BINDING_GAP}
)

GAP_ARCHIVE_UNAVAILABLE_FOR_CHANGED = "archive_unavailable_for_changed_source"

# The lifecycle states for which an archive snapshot is REQUIRED to stay representable.
_CHANGED_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {inv.LIFECYCLE_CHANGED, inv.LIFECYCLE_DISAPPEARED, inv.LIFECYCLE_REPLACED}
)


def derive_archive_binding(lifecycle_state: Any, snapshot_availability: Any) -> dict[str, Any]:
    """Derive the lifecycle ↔ archive binding for one source (§3, fail-closed).

    * ``unchanged`` -> ``live_source`` (archive optional; no gap).
    * ``changed`` / ``disappeared`` / ``replaced`` WITH an available-near-scan snapshot
      -> ``archive_backed`` (the changed source is still representable).
    * ``changed`` / ``disappeared`` / ``replaced`` WITHOUT one -> ``archive_gap`` +
      the ``archive_unavailable_for_changed_source`` gap (honestly flagged, never
      hidden).

    Pure function of the two derived labels from the 5.03 inventory.
    """
    if lifecycle_state in _CHANGED_LIFECYCLE_STATES:
        if snapshot_availability == inv.SNAPSHOT_AVAILABLE:
            return {"binding": ARCHIVE_BINDING_BACKED, "gaps": []}
        return {
            "binding": ARCHIVE_BINDING_GAP,
            "gaps": [GAP_ARCHIVE_UNAVAILABLE_FOR_CHANGED],
        }
    return {"binding": ARCHIVE_BINDING_LIVE, "gaps": []}


def build_source_change_archive(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One source-change/archive entry per registered source (§3, deterministic).

    Reuses the 5.03 inventory verbatim (lifecycle + archiveAvailability) and attaches
    the formalized lifecycle ↔ archive binding. ``originalUrl`` is http(s)-only
    (re-guarded). Order follows the inventory (``source_class, source_id``) so the same
    DB yields a byte-identical list.
    """
    entries: list[dict[str, Any]] = []
    for source in inv.source_inventory(conn):
        lifecycle = source.get("lifecycle", {})
        archive = source.get("archiveAvailability", {})
        binding = derive_archive_binding(
            lifecycle.get("state"), archive.get("snapshotAvailability")
        )
        entries.append(
            {
                "sourceId": source.get("source_id"),
                "originalUrl": _web_url_or_none(source.get("original_url")),
                "lifecycleState": lifecycle.get("state"),
                "scanDate": archive.get("scanDate"),
                "archiveStatus": archive.get("archiveStatus"),
                "snapshotAvailability": archive.get("snapshotAvailability"),
                "nearestSnapshotRef": _web_url_or_none(archive.get("nearestSnapshotRef")),
                "archiveBinding": binding["binding"],
                "gaps": binding["gaps"],
            }
        )
    return entries


# ---------------------------------------------------------------------------
# §4 — Model 4: future-fact verification model (past AI assumptions)
# ---------------------------------------------------------------------------

# A past AI assumption's later verification outcome (contract Model 4). A frozenset SSOT.
VERIFICATION_SUPPORTED = "supported"
VERIFICATION_CONTRADICTED = "contradicted"
VERIFICATION_PARTIALLY_SUPPORTED = "partially_supported"
VERIFICATION_CORRECTED = "corrected"
VERIFICATION_UNRESOLVED = "unresolved"  # the fail-closed default

VERIFICATION_OUTCOMES: frozenset[str] = frozenset(
    {
        VERIFICATION_SUPPORTED,
        VERIFICATION_CONTRADICTED,
        VERIFICATION_PARTIALLY_SUPPORTED,
        VERIFICATION_CORRECTED,
        VERIFICATION_UNRESOLVED,
    }
)


def _verifying_relation(verifier: dict[str, Any]) -> str | None:
    """The verification *method* — the relation of the verifying record's evidence.

    The first evidence ``relation`` (``substantiates`` / ``contradicts`` / ``corrects``
    / ...) describes HOW the later record relates to the assumption it verifies. A
    web-safe enum, never free text. ``None`` when the verifier carries no evidence.
    """
    for link in verifier.get("evidence", []):
        relation = link.get("relation")
        if relation:
            return relation
    return None


def _verifying_source_ref(verifier: dict[str, Any]) -> str | None:
    """The first ``to_source_id`` the verifying record cites (its verifying source)."""
    for link in verifier.get("evidence", []):
        source_id = link.get("to_source_id")
        if source_id:
            return source_id
    return None


def resolve_verification_outcome(
    verifier: dict[str, Any] | None,
) -> str:
    """Map a verifying record onto the assumption's verification outcome (§4, I5 load-bearing).

    Fail-closed: ``None`` (no later record attached to the assumption) ->
    :data:`VERIFICATION_UNRESOLVED` — an un-reverified assumption is NEVER silently
    upgraded to fact. Otherwise, derived from the verifying record's reviewed signals,
    most-specific-wins:

    1. ``correction_status == 'corrected'`` -> ``corrected``;
    2. ``correction_status ∈ {'replaced', 'superseded'}`` -> ``contradicted``;
    3. composed status ``verified`` (grounded source-backed) -> ``supported``;
    4. any other resolved-but-not-grounded later record -> ``partially_supported``.

    Neutering this to a constant ``unresolved`` makes the supported/corrected outcome
    assertion go RED while the read surface still serves both records — a
    non-tautological RED-proof.
    """
    if verifier is None:
        return VERIFICATION_UNRESOLVED
    correction = verifier.get("correction_status")
    if correction == "corrected":
        return VERIFICATION_CORRECTED
    if correction in {"replaced", "superseded"}:
        return VERIFICATION_CONTRADICTED
    if card_feed._compose_record_status(verifier) == card_feed.STATUS_VERIFIED:
        return VERIFICATION_SUPPORTED
    return VERIFICATION_PARTIALLY_SUPPORTED


def build_assumption_verifications(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One entry per past AI assumption (``layer == ai_thought_then``) (§4, deterministic).

    For each assumption, finds the later record attached to it via the forward-only
    ``updates_statement_id`` spine (the verifying record) and derives the
    ``verificationOutcome`` + ``verificationOrigin`` (who/what produced the
    verification) + ``verificationMethod`` (how) + verifying source ref + verification
    date. The original assumption is PRESERVED in ``assumptionThen`` (never mutated).
    Fail-closed: an assumption with no verifying record reads ``unresolved``. Order is
    by the assumption's statement id (byte-stable).
    """
    records = read_api.reviewer_internal_records(conn)
    records_by_id = {r["statement_id"]: r for r in records}
    superseding_index = _superseding_index(records)

    entries: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r["statement_id"]):
        if record.get("layer") != LAYER_AI_ASSUMPTION:
            continue
        statement_id = record["statement_id"]
        # The verifying record is the first served later record that updates this
        # assumption (the same forward-only spine the corrections model uses).
        verifier: dict[str, Any] | None = None
        for verifier_id in superseding_index.get(statement_id, []):
            candidate = records_by_id.get(verifier_id)
            if candidate is not None:
                verifier = candidate
                break

        outcome = resolve_verification_outcome(verifier)
        resolved = verifier is not None
        entries.append(
            {
                "assumptionStatementId": statement_id,
                "assumptionRef": _record_card_id(record),
                # the original assumption preserved (never mutated — forward-only).
                "assumptionThen": {
                    "status": card_feed._compose_record_status(record),
                    "recordDate": card_feed._card_date(record),
                    "sourceConfidence": record.get("confidence_label"),
                },
                "verificationOutcome": outcome,
                "verificationOrigin": verifier.get("produced_by") if resolved else None,
                "verificationMethod": _verifying_relation(verifier) if resolved else None,
                "verifyingStatementId": verifier["statement_id"] if resolved else None,
                "verifyingRef": _record_card_id(verifier) if resolved else None,
                "verifyingSourceRef": _verifying_source_ref(verifier) if resolved else None,
                "verificationDate": card_feed._card_date(verifier) if resolved else None,
                "resolved": resolved,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# §5 — the single envelope digest (I3) + the assembled trust body
# ---------------------------------------------------------------------------


def _trust_digest(
    record_separation: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
    hot_topic_reasons: dict[str, Any],
    source_change_archive: list[dict[str, Any]],
    assumption_verifications: list[dict[str, Any]],
) -> str:
    """A single sha256 over the five canonical model envelopes (I3).

    The ONLY hash exposed by the whole body — there is no per-source raw-content hash.
    Computed over the already-web-safe envelopes so it cannot encode a raw path.
    """
    payload = json.dumps(
        {
            "recordSeparation": record_separation,
            "corrections": corrections,
            "hotTopicReasons": hot_topic_reasons,
            "sourceChangeArchive": source_change_archive,
            "assumptionVerifications": assumption_verifications,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_trust_model(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the full reviewer-internal trust-model body + sweep it.

    Exactly one hash is exposed (the envelope ``trustDigest``). The whole body is swept
    by :func:`read_api.assert_no_raw_paths`, so a FS path / ``.sha256`` / vault marker /
    ``file://`` that slipped a column fails LOUDLY at the boundary (I1 backstop). Pure
    function of the DB — same DB -> byte-identical trust model.
    """
    record_separation = build_record_separation(conn)
    corrections = build_corrections(conn)
    hot_topic_reasons = build_hot_topic_reasons(conn)
    source_change_archive = build_source_change_archive(conn)
    assumption_verifications = build_assumption_verifications(conn)
    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public" — I6
        "recordSeparation": record_separation,
        "corrections": corrections,
        "hotTopicReasons": hot_topic_reasons,
        "sourceChangeArchive": source_change_archive,
        "assumptionVerifications": assumption_verifications,
        "trustDigest": _trust_digest(
            record_separation,
            corrections,
            hot_topic_reasons,
            source_change_archive,
            assumption_verifications,
        ),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# §6 — contract guards (load-bearing, non-tautological checks)
# ---------------------------------------------------------------------------

_HEX64 = frozenset("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value.lower())
    )


def assert_record_separation_total(body: dict[str, Any]) -> bool:
    """RED if a separated record's ``recordClass`` is neither a frozen class nor gapped.

    Every record must EITHER map to exactly one of the five frozen
    :data:`RECORD_CLASSES` OR carry the ``layer_unresolved`` gap (a poisoned/absent
    layer is never silently bucketed as a fact). Neutering :func:`record_class` to a
    constant collapses the five-way separation; this guard catches it.
    """
    for entry in body.get("recordSeparation", []):
        cls = entry.get("recordClass")
        gapped = GAP_LAYER_UNRESOLVED in entry.get("gaps", [])
        if cls is None and not gapped:
            raise TrustModelError(
                f"record {entry.get('statementId')!r} has no recordClass and no "
                f"{GAP_LAYER_UNRESOLVED!r} gap"
            )
        if cls is not None and cls not in RECORD_CLASSES:
            raise TrustModelError(
                f"record {entry.get('statementId')!r} recordClass {cls!r} outside the "
                "frozen RECORD_CLASSES"
            )
    return True


def assert_corrections_resolved_or_gapped(body: dict[str, Any]) -> bool:
    """RED if a correction edge is neither resolved nor fail-closed gapped (Model 1).

    Every corrected record must EITHER carry a superseding ref (with a
    ``correctionEffectiveFrom``) OR the ``correction_unresolved`` gap. A resolved edge
    must carry a ref + effective date and NOT the gap; an unresolved edge MUST carry the
    gap and no ref. ``correctionStatus`` (when present) must be in the frozen vocab.
    """
    for edge in body.get("corrections", []):
        resolved = edge.get("resolved")
        has_ref = edge.get("supersedingRef") is not None
        has_effective = edge.get("correctionEffectiveFrom") is not None
        gapped = GAP_CORRECTION_UNRESOLVED in edge.get("gaps", [])
        status = edge.get("correctionStatus")
        if status is not None and status not in CORRECTION_STATUSES:
            raise TrustModelError(
                f"correction edge {edge.get('correctedStatementId')!r} status {status!r} "
                "outside the frozen CORRECTION_STATUSES"
            )
        if resolved and not (has_ref and has_effective):
            raise TrustModelError(
                f"correction edge {edge.get('correctedStatementId')!r} marked resolved "
                "but missing supersedingRef or correctionEffectiveFrom"
            )
        if not resolved and not gapped:
            raise TrustModelError(
                f"correction edge {edge.get('correctedStatementId')!r} is unresolved "
                f"but missing the {GAP_CORRECTION_UNRESOLVED!r} gap label"
            )
        if resolved and gapped:
            raise TrustModelError(
                f"correction edge {edge.get('correctedStatementId')!r} is both resolved "
                "and gapped (contradictory)"
            )
    return True


def assert_hot_topic_markers_valid(body: dict[str, Any]) -> bool:
    """RED if a hot-topic marker is out of vocab or ungrounded; unanchored must be gapped.

    Every ``markedBy`` must be in the frozen :data:`MARKED_BY_VALUES`, every marker must
    carry at least one grounding ref (a marker is never an editorialized claim without a
    record ref), and every unanchored record must carry the ``topic_anchor_missing``
    gap.
    """
    reasons = body.get("hotTopicReasons", {})
    for topic in reasons.get("topics", []):
        for marker in topic.get("markers", []):
            marked_by = marker.get("markedBy")
            if marked_by not in MARKED_BY_VALUES:
                raise TrustModelError(
                    f"topic {topic.get('topicId')!r} markedBy {marked_by!r} outside the "
                    "frozen MARKED_BY_VALUES"
                )
            if not marker.get("why", {}).get("groundingRefs"):
                raise TrustModelError(
                    f"topic {topic.get('topicId')!r} marker {marked_by!r} carries no "
                    "groundingRefs (ungrounded reason)"
                )
    for entry in reasons.get("unanchored", []):
        if GAP_TOPIC_ANCHOR_MISSING not in entry.get("gaps", []):
            raise TrustModelError(
                f"unanchored record {entry.get('ref')!r} missing the "
                f"{GAP_TOPIC_ANCHOR_MISSING!r} gap"
            )
    return True


def assert_archive_binding_consistent(body: dict[str, Any]) -> bool:
    """RED if a source's archive binding contradicts its lifecycle/snapshot (Model 3).

    A changed/disappeared/replaced source with no available-near-scan snapshot MUST read
    ``archive_gap`` (+ gap); one WITH a snapshot MUST read ``archive_backed``; an
    unchanged source MUST read ``live_source``. The binding must be in the frozen vocab.
    A re-derivation cross-check on the EMITTED body (not a copy of the build).
    """
    for entry in body.get("sourceChangeArchive", []):
        binding = entry.get("archiveBinding")
        if binding not in ARCHIVE_BINDINGS:
            raise TrustModelError(
                f"source {entry.get('sourceId')!r} archiveBinding {binding!r} outside "
                "the frozen ARCHIVE_BINDINGS"
            )
        expected = derive_archive_binding(
            entry.get("lifecycleState"), entry.get("snapshotAvailability")
        )
        if binding != expected["binding"] or entry.get("gaps", []) != expected["gaps"]:
            raise TrustModelError(
                f"source {entry.get('sourceId')!r} archive binding {binding!r}/"
                f"{entry.get('gaps')!r} contradicts lifecycle "
                f"{entry.get('lifecycleState')!r} + snapshot "
                f"{entry.get('snapshotAvailability')!r}"
            )
    return True


def assert_verifications_fail_closed(body: dict[str, Any]) -> bool:
    """RED if an assumption verification breaks the fail-closed rule (Model 4).

    Every ``verificationOutcome`` must be in the frozen :data:`VERIFICATION_OUTCOMES`. An
    UNRESOLVED assumption (no verifying record) must NOT carry an origin/method/verifying
    ref (no fabricated verifier); a RESOLVED one MUST carry a verifying ref. This is the
    "never silently upgraded to fact" invariant.
    """
    for entry in body.get("assumptionVerifications", []):
        outcome = entry.get("verificationOutcome")
        if outcome not in VERIFICATION_OUTCOMES:
            raise TrustModelError(
                f"assumption {entry.get('assumptionStatementId')!r} outcome {outcome!r} "
                "outside the frozen VERIFICATION_OUTCOMES"
            )
        resolved = entry.get("resolved")
        has_verifier = entry.get("verifyingRef") is not None
        if not resolved:
            if outcome != VERIFICATION_UNRESOLVED:
                raise TrustModelError(
                    f"assumption {entry.get('assumptionStatementId')!r} has no verifier "
                    f"but outcome is {outcome!r} (must be {VERIFICATION_UNRESOLVED!r})"
                )
            if has_verifier or entry.get("verificationOrigin") is not None:
                raise TrustModelError(
                    f"assumption {entry.get('assumptionStatementId')!r} is unresolved but "
                    "carries a fabricated verifier ref/origin"
                )
        elif not has_verifier:
            raise TrustModelError(
                f"assumption {entry.get('assumptionStatementId')!r} marked resolved but "
                "carries no verifyingRef"
            )
    return True


def assert_single_envelope_digest(body: dict[str, Any]) -> bool:
    """RED if any 64-hex string appears outside the top-level ``trustDigest`` (I3)."""
    if not _is_hex64(body.get("trustDigest")):
        raise TrustModelError("envelope trustDigest is not a sha256")
    for key, value in body.items():
        if key == "trustDigest":
            continue
        for text in read_api._iter_strings(value):
            if _is_hex64(text):
                raise TrustModelError(
                    f"per-source 64-hex hash leaked under {key!r}: {text!r}"
                )
    return True


def check_trust_model(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build + run every load-bearing contract guard. Raises on the first defect."""
    body = build_trust_model(conn)
    assert_record_separation_total(body)
    assert_corrections_resolved_or_gapped(body)
    assert_hot_topic_markers_valid(body)
    assert_archive_binding_consistent(body)
    assert_verifications_fail_closed(body)
    assert_single_envelope_digest(body)
    return body


# ---------------------------------------------------------------------------
# CLI (read-only — emits the reviewer-internal Alpine trust-model envelope)
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.07 reviewer-internal Alpine transcript/evidence/statement "
        "trust model (GOV-531)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the separation / corrections / hot-topic / archive / verification / "
        "single-envelope guards (CI gate; exit 1 on any defect)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.check:
            body = check_trust_model(conn)
        else:
            body = build_trust_model(conn)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
