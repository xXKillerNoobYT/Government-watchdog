"""Stage 5.05 Watchdog signals layer (GOV-520) — reviewer-internal, Alpine.

Authors the **Watchdog signals layer** per the CTO contract recorded on GOV-519
(``Docs/stage5-05-watchdog-signals-contract.md``): three deterministic, idempotent
reviewer-internal *signal envelopes* derived purely from the already-merged registry —
no AI, no network, no new claims. Each envelope is a presentation-precursor projection
over the Stage-3/4/5 read surface, NOT a new source of truth:

1. **§1 correctionsLedger** (:func:`build_corrections_ledger`) — one typed correction
   edge per *corrected record* (``correctionStatus ∈ {replaced, superseded, corrected,
   amended}`` OR its source's 5.03 ``lifecycle == replaced``). The edge points the
   corrected ``known_then`` record at its **superseding record/document**, resolved from
   the forward-only ``updates_statement_id`` correction spine (migration 0007 §D-4) —
   never mutating the known-then context. When no superseding ref resolves, the edge
   carries the fail-closed ``correction_unresolved`` gap label (never a fabricated ref).
2. **§2 hotTopics** (:func:`build_hot_topics`) — a deterministic salience score per topic
   = pure arithmetic (:func:`salience_score`) over the topic's *activity count* + *recency*
   (items whose ``scanDate`` falls in the window anchored to the corpus's own newest scan)
   + *correction churn* (items that are corrected records). Ranked; a topic below the
   activity floor carries the ``insufficientData`` label. No AI, no editorializing.
3. **§3 watchdogView** (:func:`build_watchdog_view`) — a composed Kanban-precursor status
   surface (lane ∈ ``{upcoming, active, pending-decision, decided, follow-up, correction}``)
   over verified / source-linked records only, each with its derived ``sourceConfidence``
   (the GOV-283 read-time label) and fail-closed ``gaps[]`` labels.

A single envelope hash — :func:`_watchdog_digest` over the three envelopes — is the only
hash exposed (no per-source raw-content hash). The ``--check`` CLI is the CI gate (exit 1
on any defect).

Boundary rules (contract §4 / premium I1–I8), restated as signal invariants:

* it **never** mutates / re-derives ``read_api.py`` / ``publication.py`` /
  ``stage4_newsletter_feed.py`` / ``stage4_newsletter_digest_assembler.py`` /
  ``stage5_source_inventory.py`` / ``stage5_record_verifier.py`` — it consumes them
  read-only (I4 / I7), so the merged read surface is the *only* vocabulary;
* every emitted artifact is transport-swept by :func:`read_api.assert_no_raw_paths`, so a
  FS path / ``.sha256`` / vault marker / ``file://`` that slipped a column fails LOUDLY at
  the boundary (I1); ``localSourcePath`` is never emitted (I2); exactly one envelope
  ``watchdogDigest`` is exposed — no per-source raw hash (I3);
* it runs entirely at ``access: reviewer_internal`` / ``scope: alpine`` and is absent
  from any public / ``published_records`` path (I6); public launch stays Isaac-gated
  (GOV-420), untouched here.

Pure function of the registry: same DB -> byte-identical signal envelope (idempotent
re-projection). No mutation, no AI, no network.
"""

from __future__ import annotations

import argparse
import datetime as _dt
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
import stage3_card_feed as card_feed  # noqa: E402  (status + card-handle, by reference)
import stage4_newsletter_feed as nl  # noqa: E402  (feed projection + date helpers)
import stage5_source_inventory as inv  # noqa: E402  (the merged 5.03 contract — consumed)

SCOPE = "alpine"
ACCESS = "reviewer_internal"  # never "public" — I6


class WatchdogSignalsError(AssertionError):
    """Raised when an emitted Watchdog signal violates a GOV-520 contract invariant."""


# ---------------------------------------------------------------------------
# §1 — corrections ledger: typed correction edges over the read surface
# ---------------------------------------------------------------------------

# A record is a *corrected record* when its claim-level ``correction_status`` is an
# active correction state, OR its source's 5.03 lifecycle is ``replaced``. A frozen
# vocab so a future value is a conscious, reviewed change — never accidental drift.
CORRECTION_ACTIVE: frozenset[str] = frozenset(
    {"replaced", "superseded", "corrected", "amended"}
)

# The fail-closed gap label emitted when a corrected record resolves to no superseding
# ref (the correction is acknowledged but its successor is not yet in the registry).
GAP_CORRECTION_UNRESOLVED = "correction_unresolved"


def _record_card_id(record: dict[str, Any]) -> str:
    """The Stage-3 card handle for a served record (matches a feed item's ``cardIds``)."""
    card_type = card_feed._resolve_record_type(record)
    return card_feed.card_handle(card_type, record["statement_id"])


def _superseding_index(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    """``{corrected_statement_id: [superseding_statement_id, ...]}`` over served records.

    The forward-only correction spine (migration 0007 §D-4): a ``corrected_later`` row
    carries ``updates_statement_id`` pointing back at the ``known_then`` row it
    supersedes. Inverting that pointer over the *served* set yields, for each corrected
    record, the superseding record(s) — built ONLY from web-safe served fields (no raw
    DB re-read), so it can never reintroduce a stripped column.
    """
    index: dict[str, list[str]] = {}
    for record in records:
        updates = record.get("updates_statement_id")
        if updates:
            index.setdefault(updates, []).append(record["statement_id"])
    # Deterministic order independent of serve order.
    for key in index:
        index[key] = sorted(index[key])
    return index


def is_corrected_record(
    record: dict[str, Any], replaced_sources: frozenset[str]
) -> bool:
    """True iff ``record`` is a corrected record (the §1 trigger set).

    Either its claim-level ``correction_status`` is an active correction state, or one
    of its evidence sources has 5.03 ``lifecycle == replaced`` (``replaced_sources``).
    Pure function of the served record + the precomputed replaced-source set.
    """
    if record.get("correction_status") in CORRECTION_ACTIVE:
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
    """Resolve the superseding ref for a corrected record, or ``None`` if unresolved.

    Load-bearing (I5): reads the ``updates_statement_id`` correction spine
    (``superseding_index``) to find the served record that supersedes
    ``corrected_statement_id``. Returns ``{supersedingStatementId, supersedingRef}``
    (the superseding record's web-safe id + card handle) for the FIRST superseding
    record, or ``None`` when no superseding record is served — the fail-closed branch
    the ledger surfaces as :data:`GAP_CORRECTION_UNRESOLVED`. Never fabricates a ref.
    """
    superseders = superseding_index.get(corrected_statement_id, [])
    for superseder_id in superseders:
        superseder = records_by_id.get(superseder_id)
        if superseder is not None:
            return {
                "supersedingStatementId": superseder_id,
                "supersedingRef": _record_card_id(superseder),
            }
    return None


def _replaced_sources(conn: sqlite3.Connection) -> frozenset[str]:
    """Source ids whose merged 5.03 lifecycle state is ``replaced`` (consumed by ref)."""
    body = inv.build_inventory(conn)
    return frozenset(
        entry["source_id"]
        for entry in body.get("sources", [])
        if entry.get("lifecycle", {}).get("state") == inv.LIFECYCLE_REPLACED
    )


def build_corrections_ledger(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One typed correction edge per corrected record, deterministic order (§1).

    Each edge preserves the corrected ``known_then`` context (status / recordDate /
    correctionStatus, never mutated) and points it at its resolved superseding ref —
    or carries the ``correction_unresolved`` gap when none resolves. Order is by the
    corrected statement id so the same DB yields a byte-identical ledger.
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
            "resolved": resolved is not None,
            "gaps": [] if resolved is not None else [GAP_CORRECTION_UNRESOLVED],
        }
        edges.append(edge)
    return edges


# ---------------------------------------------------------------------------
# §2 — hot topics: deterministic salience over activity / recency / churn
# ---------------------------------------------------------------------------

# Integer weights so the score is exact and byte-stable (no float drift). The
# arithmetic — never AI — is the whole salience model (contract §2).
ACTIVITY_WEIGHT = 3
RECENCY_WEIGHT = 2
CHURN_WEIGHT = 5

# A topic with fewer than this many activity items is below the floor: it is still
# emitted (a gap is never hidden) but labelled ``insufficientData`` so a thin topic is
# never ranked as a confident "hot" signal.
INSUFFICIENT_DATA_FLOOR = 2

# Recency window (days) around the corpus anchor — mirrors the 5.04 archive-nearness
# month window. Anchored to the data's own newest scan, NOT a wall clock, so the score
# is a pure function of the DB (idempotent — no ``Date.now()``).
RECENCY_WINDOW_DAYS = 31

SALIENCE_RANKED = "ranked"
SALIENCE_INSUFFICIENT = "insufficientData"


def salience_score(activity: int, recency: int, churn: int) -> int:
    """The deterministic topic salience score — pure arithmetic (§2, I5 load-bearing).

    ``ACTIVITY_WEIGHT*activity + RECENCY_WEIGHT*recency + CHURN_WEIGHT*churn``. Integer
    in, integer out: same counts -> same score on every build. Neutering this function
    (e.g. returning a constant) makes the ranking assertion go RED while the read
    surface still serves the same items — a non-tautological RED-proof.
    """
    return ACTIVITY_WEIGHT * activity + RECENCY_WEIGHT * recency + CHURN_WEIGHT * churn


def _record_topic_anchors(record: dict[str, Any]) -> list[str]:
    """The topic/issue anchors a served record belongs to (the §2 salience unit).

    Primary: any explicit ``topic_id`` on the web-safe evidence drawer (the direct
    topic edge — forward-looking, sparse today). Fallback: the record's
    ``agenda_item_id`` — the agenda thread the claim sits in, which Isaac's concept map
    treats as the issue/topic anchor ("agenda item references topic"). A record with
    neither is uncategorized and contributes to no topic (an honest gap, never invented).
    All web-safe ids — no raw locator.
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


def _record_scan_date(record: dict[str, Any]) -> str | None:
    """The record's newest evidence ``scan_date``, falling back to its card date.

    A plain ISO date string (already web-safe), never a locator.
    """
    scans = [
        link.get("scan_date")
        for link in record.get("evidence", [])
        if isinstance(link.get("scan_date"), str) and link.get("scan_date")
    ]
    if scans:
        return max(scans)
    return card_feed._card_date(record)


def _corpus_recency_anchor(records: list[dict[str, Any]]) -> _dt.date | None:
    """The corpus's own newest scan date — the deterministic recency window anchor."""
    dates = [
        d
        for d in (nl._iso_date(_record_scan_date(r)) for r in records)
        if d is not None
    ]
    return max(dates) if dates else None


def _in_recency_window(record: dict[str, Any], anchor: _dt.date | None) -> bool:
    """True iff the record's scan date is within :data:`RECENCY_WINDOW_DAYS` of ``anchor``."""
    if anchor is None:
        return False
    scan = nl._iso_date(_record_scan_date(record))
    if scan is None:
        return False
    return abs((anchor - scan).days) <= RECENCY_WINDOW_DAYS


def build_hot_topics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Ranked per-topic salience signal over the served reviewer-internal records (§2).

    Activity = records anchored to the topic; recency = those within the window anchored
    to the corpus's newest scan; churn = those that are corrected records. The score is
    pure arithmetic; ranking is ``(score desc, topicId asc)`` for a byte-stable order.
    Topics below the activity floor are emitted with the ``insufficientData`` label.
    """
    records = read_api.reviewer_internal_records(conn)
    anchor = _corpus_recency_anchor(records)
    replaced_sources = _replaced_sources(conn)

    agg: dict[str, dict[str, int]] = {}
    for record in records:
        recent = _in_recency_window(record, anchor)
        is_corrected = is_corrected_record(record, replaced_sources)
        for topic_id in _record_topic_anchors(record):
            bucket = agg.setdefault(
                topic_id, {"activity": 0, "recency": 0, "churn": 0}
            )
            bucket["activity"] += 1
            if recent:
                bucket["recency"] += 1
            if is_corrected:
                bucket["churn"] += 1

    topics: list[dict[str, Any]] = []
    for topic_id, counts in agg.items():
        score = salience_score(counts["activity"], counts["recency"], counts["churn"])
        label = (
            SALIENCE_INSUFFICIENT
            if counts["activity"] < INSUFFICIENT_DATA_FLOOR
            else SALIENCE_RANKED
        )
        topics.append(
            {
                "topicId": topic_id,
                "activityCount": counts["activity"],
                "recencyCount": counts["recency"],
                "correctionChurn": counts["churn"],
                "salienceScore": score,
                "salienceLabel": label,
            }
        )
    topics.sort(key=lambda t: (-t["salienceScore"], t["topicId"]))
    return topics


# ---------------------------------------------------------------------------
# §3 — watchdog view: Kanban-precursor lanes over verified / source-linked records
# ---------------------------------------------------------------------------

LANE_UPCOMING = "upcoming"
LANE_ACTIVE = "active"
LANE_PENDING_DECISION = "pending-decision"
LANE_DECIDED = "decided"
LANE_FOLLOW_UP = "follow-up"
LANE_CORRECTION = "correction"

# The frozen lane vocabulary (contract §3). A `frozenset` so any future lane is a
# conscious, reviewed change — never accidental drift.
WATCHDOG_LANES: frozenset[str] = frozenset(
    {
        LANE_UPCOMING,
        LANE_ACTIVE,
        LANE_PENDING_DECISION,
        LANE_DECIDED,
        LANE_FOLLOW_UP,
        LANE_CORRECTION,
    }
)

GAP_ARCHIVE_UNAVAILABLE = "archive_unavailable"
GAP_LOW_CONFIDENCE = "low_confidence"

# The lowest-confidence GOV-283 label (a source whose class never resolved). Surfaced
# as the ``low_confidence`` gap so a thin source is honestly flagged, never hidden.
_LOW_CONFIDENCE_LABEL = read_api._CONSERVATIVE_CONFIDENCE_LABEL


def _record_sources(record: dict[str, Any]) -> list[str]:
    return [
        link.get("to_source_id")
        for link in record.get("evidence", [])
        if link.get("to_source_id")
    ]


def derive_lane(
    record: dict[str, Any],
    *,
    is_corrected: bool,
    changed_source: bool,
) -> str:
    """Derive the Kanban-precursor lane for one served record (§3, fail-closed).

    Most-specific-signal-wins precedence over the merged read surface:

    1. ``correction`` — the record is a corrected record (in the §1 ledger);
    2. ``follow-up`` — its source changed (``source_changed`` truthy OR its source's
       5.03 lifecycle is ``changed``) — the citation needs a re-check;
    3. ``decided`` — its composed status is ``verified`` (a settled record);
    4. ``pending-decision`` — an ``ai_presented`` observation awaiting a human
       verification decision;
    5. ``active`` — anchored to a meeting/agenda thread but not yet decided;
    6. ``upcoming`` — the source-linked floor (no stronger signal yet).
    """
    if is_corrected:
        return LANE_CORRECTION
    if changed_source or bool(record.get("source_changed")):
        return LANE_FOLLOW_UP
    status = card_feed._compose_record_status(record)
    if status == card_feed.STATUS_VERIFIED:
        return LANE_DECIDED
    if status == card_feed.STATUS_AI_PRESENTED:
        return LANE_PENDING_DECISION
    if any(link.get("meeting_id") for link in record.get("evidence", [])):
        return LANE_ACTIVE
    return LANE_UPCOMING


def build_watchdog_view(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The composed Kanban-precursor lane surface over served records (§3).

    Over verified / source-linked served records ONLY (the read surface already drops
    orphans), each entry carries its lane, composed status, recordDate, derived
    ``sourceConfidence`` (GOV-283), and fail-closed ``gaps[]`` (unresolved correction /
    archive-unavailable source / low confidence). Order is by statement id (byte-stable).
    """
    records = read_api.reviewer_internal_records(conn)
    inventory = {
        entry["source_id"]: entry for entry in inv.build_inventory(conn).get("sources", [])
    }
    replaced_sources = frozenset(
        sid for sid, e in inventory.items()
        if e.get("lifecycle", {}).get("state") == inv.LIFECYCLE_REPLACED
    )
    ledger = build_corrections_ledger(conn)
    unresolved_cards = {
        edge["correctedRef"] for edge in ledger if not edge["resolved"]
    }
    corrected_cards = {edge["correctedRef"] for edge in ledger}

    entries: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda r: r["statement_id"]):
        sources = _record_sources(record)
        # source-linked is guaranteed by the serve gate; assert defensively so a
        # future un-anchored input is never silently given a lane.
        if not sources:
            continue
        card_id = _record_card_id(record)
        changed_source = any(
            inventory.get(sid, {}).get("lifecycle", {}).get("state")
            == inv.LIFECYCLE_CHANGED
            for sid in sources
        )
        lane = derive_lane(
            record,
            is_corrected=card_id in corrected_cards,
            changed_source=changed_source,
        )

        gaps: list[str] = []
        if card_id in unresolved_cards:
            gaps.append(GAP_CORRECTION_UNRESOLVED)
        if any(
            inventory.get(sid, {})
            .get("archiveAvailability", {})
            .get("snapshotAvailability")
            == inv.SNAPSHOT_NOT_AVAILABLE
            for sid in sources
        ):
            gaps.append(GAP_ARCHIVE_UNAVAILABLE)
        if record.get("confidence_label") == _LOW_CONFIDENCE_LABEL:
            gaps.append(GAP_LOW_CONFIDENCE)

        entries.append(
            {
                "cardId": card_id,
                "statementId": record["statement_id"],
                "lane": lane,
                "status": card_feed._compose_record_status(record),
                "recordDate": card_feed._card_date(record),
                "sourceLinked": True,
                "sourceConfidence": record.get("confidence_label"),
                "gaps": gaps,
            }
        )
    return entries


# ---------------------------------------------------------------------------
# §4 — the single envelope digest (I3) + the assembled signal body
# ---------------------------------------------------------------------------


def _watchdog_digest(
    corrections: list[dict[str, Any]],
    hot_topics: list[dict[str, Any]],
    watchdog_view: list[dict[str, Any]],
) -> str:
    """A single sha256 over the canonical three signal envelopes (I3).

    The ONLY hash exposed by the whole body — there is no per-source raw-content hash.
    Computed over the already-web-safe envelopes so it cannot encode a raw path.
    """
    payload = json.dumps(
        {
            "correctionsLedger": corrections,
            "hotTopics": hot_topics,
            "watchdogView": watchdog_view,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_signals(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the ``{scope, access, correctionsLedger, hotTopics, watchdogView,
    watchdogDigest}`` body + sweep it.

    Exactly one hash is exposed (the envelope ``watchdogDigest``). The whole body is
    swept by :func:`read_api.assert_no_raw_paths`, so a FS path / ``.sha256`` / vault
    marker / ``file://`` that slipped a column fails LOUDLY at the boundary (I1
    backstop). Pure function of the DB — same DB -> byte-identical signals.
    """
    corrections = build_corrections_ledger(conn)
    hot_topics = build_hot_topics(conn)
    watchdog_view = build_watchdog_view(conn)
    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public" — I6
        "correctionsLedger": corrections,
        "hotTopics": hot_topics,
        "watchdogView": watchdog_view,
        "watchdogDigest": _watchdog_digest(corrections, hot_topics, watchdog_view),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# §5 — contract guards (load-bearing, non-tautological checks)
# ---------------------------------------------------------------------------

_HEX64 = frozenset("0123456789abcdef")


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value.lower())
    )


def assert_lanes_valid(body: dict[str, Any]) -> bool:
    """RED if any watchdog entry's ``lane`` is outside the frozen SSOT (R-3).

    A real cross-check on the EMITTED body — a build that emits an out-of-vocab lane
    (e.g. a neutered derivation returning a typo) goes RED.
    """
    for entry in body.get("watchdogView", []):
        lane = entry.get("lane")
        if lane not in WATCHDOG_LANES:
            raise WatchdogSignalsError(
                f"watchdog entry {entry.get('statementId')!r} lane {lane!r} "
                "outside the frozen WATCHDOG_LANES"
            )
    return True


def assert_corrections_resolved_or_gapped(body: dict[str, Any]) -> bool:
    """RED if a correction edge is neither resolved nor fail-closed gapped (R-1).

    Every corrected record must EITHER carry a superseding ref OR the
    ``correction_unresolved`` gap — never a silent unresolved edge with no flag. A
    resolved edge must NOT carry the gap, and an unresolved edge MUST carry it.
    """
    for edge in body.get("correctionsLedger", []):
        resolved = edge.get("resolved")
        has_ref = edge.get("supersedingRef") is not None
        gapped = GAP_CORRECTION_UNRESOLVED in edge.get("gaps", [])
        if resolved and not has_ref:
            raise WatchdogSignalsError(
                f"correction edge {edge.get('correctedStatementId')!r} marked resolved "
                "but carries no supersedingRef"
            )
        if not resolved and not gapped:
            raise WatchdogSignalsError(
                f"correction edge {edge.get('correctedStatementId')!r} is unresolved "
                f"but missing the {GAP_CORRECTION_UNRESOLVED!r} gap label"
            )
        if resolved and gapped:
            raise WatchdogSignalsError(
                f"correction edge {edge.get('correctedStatementId')!r} is both resolved "
                "and gapped (contradictory)"
            )
    return True


def assert_hot_topics_ranked(body: dict[str, Any]) -> bool:
    """RED if ``hotTopics`` is not in non-increasing salience order (R-2).

    Reads the EMITTED ranking (not a recompute) — so a build whose ranking decoupled
    from the score (e.g. a neutered scorer that flattens every score, leaving a stale
    order) is caught here.
    """
    scores = [t.get("salienceScore") for t in body.get("hotTopics", [])]
    for earlier, later in zip(scores, scores[1:]):
        if earlier is None or later is None or earlier < later:
            raise WatchdogSignalsError(
                f"hotTopics not in non-increasing salience order: {earlier} -> {later}"
            )
    return True


def assert_single_envelope_digest(body: dict[str, Any]) -> bool:
    """RED if any 64-hex string appears outside the top-level ``watchdogDigest`` (R-5/I3)."""
    if not _is_hex64(body.get("watchdogDigest")):
        raise WatchdogSignalsError("envelope watchdogDigest is not a sha256")
    for key, value in body.items():
        if key == "watchdogDigest":
            continue
        for text in read_api._iter_strings(value):
            if _is_hex64(text):
                raise WatchdogSignalsError(
                    f"per-source 64-hex hash leaked under {key!r}: {text!r}"
                )
    return True


def check_signals(conn: sqlite3.Connection) -> dict[str, Any]:
    """Build + run every load-bearing contract guard. Raises on the first defect."""
    body = build_signals(conn)
    assert_lanes_valid(body)
    assert_corrections_resolved_or_gapped(body)
    assert_hot_topics_ranked(body)
    assert_single_envelope_digest(body)
    return body


# ---------------------------------------------------------------------------
# CLI (read-only — emits the reviewer-internal Watchdog signal envelope)
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.05 reviewer-internal Alpine Watchdog signals layer (GOV-520)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the lane / corrections / ranking / single-envelope guards (CI gate)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.check:
            body = check_signals(conn)
        else:
            body = build_signals(conn)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
