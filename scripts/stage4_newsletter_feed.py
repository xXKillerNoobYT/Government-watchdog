"""Stage 4.03 newsletter item feed/fixture (GOV-449) — reviewer-internal, Alpine.

Implements the GOV-448 contract
(``Docs/.../2026-06-22-GOV-448-Stage-4-03-Newsletter-Source-Data-Inventory-Contract.md``):
a deterministic **reviewed Alpine newsletter item feed** projected ENTIRELY on top
of the already-web-safe Stage-3 read surface — :func:`read_api.reviewer_internal_records`
— exactly mirroring the Stage-3 projection discipline of :mod:`stage3_card_feed`
(GOV-347) and :mod:`stage3_source_inventory` (GOV-364).

The newsletter is a **presentation projection over the Stage-3 graph, not a new
source of truth** (4.01 spec §4). Every emitted item traces to one real reviewed
Stage-3 record; no item is invented. Three deliverables, all from the contract:

* **§2 item feed** (:func:`build_newsletter_feed`) — the contract item shape
  (``id`` / ``newsletterId`` / ``itemType`` / ``coveragePeriod`` / ``recordDate`` /
  ``topicIds`` / ``cardIds`` / ``meetingIds`` / ``sourceIds`` / ``status`` /
  ``labels.*`` / ``links.*`` / ``sourceTrail[]``), one item per served record.
* **§2.4 chronology** (:func:`assert_chronology`) — non-decreasing oldest→newest
  ``recordDate`` within each ``newsletterId`` batch (feeds EG-3).
* **§3 readiness record** (:func:`build_readiness_record`) — the source-set /
  backfill progress record (feeds EG-3).
* **§4 traceability + orphan routing** (:func:`source_link_validation`) — one row
  per item with ``links_present``; orphans are held out of the feed and routed to
  VerificationSafetyReviewer (feeds EG-4 / EG-11 / EG-12).

Boundary rules (contract §6, restated as feed invariants):

* the newsletter layer **never** calls ``to_web_safe`` / ``publication.py`` (the
  read surface already crossed both web-safe layers) and **never** re-derives a
  field the read surface dropped; item identity / status / dates reuse the Stage-3
  card projection (:mod:`stage3_card_feed`) so the Stage-3 label vocabulary is the
  *only* vocabulary — **zero new labels** (§2.3 / EG-7);
* it runs entirely at ``access: reviewer_internal`` and ``scope: alpine`` —
  Alpine-only, never a non-Alpine coverage claim (§6);
* every emitted artifact is transport-swept by :func:`read_api.assert_no_raw_paths`
  — a ``file://`` / FS-path / ``.sha256`` / raw-id leak fails LOUDLY at the
  boundary, never silently downstream (the contract's local-safe rule). The
  reviewer-internal vault ``localSourcePath`` is therefore emitted as ``null`` —
  the raw path stays local and never enters the artifact.

Pure function of the read surface: same DB -> byte-identical artifacts (idempotent
re-projection). No mutation, no AI, no network, no public publication.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402
import stage3_card_feed as card_feed  # noqa: E402

# ---------------------------------------------------------------------------
# Fixed envelope scope (Alpine-only; broader = planned, never implied — §6)
# ---------------------------------------------------------------------------

ACCESS = "reviewer_internal"
SCOPE = "alpine"
# The jurisdiction descriptor is verbatim from the GOV-448 contract item shape
# (§2.1) — structural metadata naming Alpine's actual county, NOT a coverage claim
# beyond Alpine (§6: no Lincoln County / Wyoming coverage).
JURISDICTION = {"state": "WY", "county": "Lincoln County", "town": "Alpine"}

# The newsletter never publishes; reviewer-internal items sit on the "draft"
# publication axis (contract §2.1 example). This is the publication-control axis,
# distinct from the claim-status axis the zero-new-label rule governs.
PUBLICATION_STATUS_DRAFT = "draft"

# ---------------------------------------------------------------------------
# §2.3 allowed vocabularies (verbatim from GOV-15 / contract §2.3) — fail-closed
# ---------------------------------------------------------------------------

# Item types — the only 11 permitted ``itemType`` values.
ALLOWED_ITEM_TYPES = frozenset({
    "processed_records", "timeline_chunk", "meeting", "document", "topic",
    "source_link", "correction", "conflict", "later_outcome",
    "unverified_item", "ai_presented_context",
})

# Claim/status label vocabulary — the EXACT Stage-3 card-layer claim/status set
# (contract §2.3). The zero-new-label rule (EG-7) is a diff vs THIS set == 0.
STAGE3_CLAIM_VOCAB = frozenset({
    "verified", "unverified", "ai_presented", "disputed", "corrected",
    "source_changed", "source_missing", "speaker_unidentified",
    "needs_human_review",
})

# Structural null for the correction axis (not a claim label; matches the §2.1
# example ``"correctionStatus": "none"``).
CORRECTION_NONE = "none"

# Map the Stage-3 card *type* (stage3_card_feed §3.2) onto the newsletter
# ``itemType`` vocabulary. The card type carries the structural kind; the visible
# claim label rides ``status`` / ``labels.claimStatus`` (so an AI/unverified item
# keeps the same label as the card layer, never styled as verified fact — §2.2).
_ITEM_TYPE_BY_CARD_TYPE = {
    card_feed.TYPE_AI_PRESENTED: "ai_presented_context",
    card_feed.TYPE_CORRECTION: "correction",
    card_feed.TYPE_STATEMENT: "timeline_chunk",
    card_feed.TYPE_INFO: "timeline_chunk",
}


class NewsletterContractError(AssertionError):
    """Raised when an emitted item/feed violates a GOV-448 contract invariant."""


# ---------------------------------------------------------------------------
# Transport sweep (contract §6 local-safe) — reuse read_api's leak vocabulary.
# ---------------------------------------------------------------------------

# App-relative reviewer-internal route prefix: the only non-web, leading-``/``
# strings the contract item shape carries (``links.*`` — §2.1). These are frontend
# ROUTE references, not filesystem paths, so they are exempt from the absolute-path
# rule — but NOT from the raw-marker / path-traversal scan, so a route can never
# smuggle a vault path or ``..``.
_ROUTE_PREFIX = "/alpine/"


def _iter_strings(obj: Any):
    """Walk every string (keys + values, nested) in ``obj`` — mirrors read_api."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for entry in obj:
            yield from _iter_strings(entry)


def _assert_local_safe(body: dict[str, Any]) -> dict[str, Any]:
    """Transport sweep over a newsletter artifact (contract §6 local-safe).

    Single-sources the leak vocabulary from :mod:`read_api` (``RAW_PATH_MARKERS`` /
    ``RawPathLeak`` / ``_is_web_url``) so it can never drift from the read surface's
    own guard. Every string is scanned for a raw marker (vault path, ``file://``,
    ``.sha256``, ``transcript_path`` …); a genuine ``/alpine/`` route is exempt from
    the leading-``/`` *filesystem-path* rule (but still marker-scanned and rejected
    on ``..``), and any other absolute/FS path fails LOUDLY. Returns ``body``
    unchanged on success so it can wrap a response inline.
    """
    for text in _iter_strings(body):
        if read_api._is_web_url(text):
            continue  # public http(s) URL — exempt (same exemption read_api uses).
        for marker in read_api.RAW_PATH_MARKERS:
            if marker in text:
                raise read_api.RawPathLeak(
                    f"raw marker {marker!r} in newsletter artifact: {text!r}"
                )
        if text.startswith(_ROUTE_PREFIX):
            if ".." in text:
                raise read_api.RawPathLeak(f"path traversal in route: {text!r}")
            continue
        if text.startswith("/") or (len(text) > 1 and text[1] == ":" and text[2:3] in ("\\", "/")):
            raise read_api.RawPathLeak(
                f"absolute/filesystem path in newsletter artifact: {text!r}"
            )
    return body


# ---------------------------------------------------------------------------
# §2.4 — date / coverage-period derivation (never invented)
# ---------------------------------------------------------------------------

_UNDATED_BATCH = "alpine-historical-undated"


def _newsletter_id(record_date: str | None) -> str:
    """``alpine-historical-YYYY-WW`` from the ISO week of ``record_date``.

    Names the Alpine coverage batch (§2.2). Deterministic by construction (pure
    function of the grounded record date); an undated record falls in a clearly
    named undated batch rather than fabricating a week.
    """
    iso = _iso_date(record_date)
    if iso is None:
        return _UNDATED_BATCH
    year, week, _weekday = iso.isocalendar()
    return f"alpine-historical-{year:04d}-{week:02d}"


def _coverage_period(record_date: str | None) -> dict[str, str] | None:
    """The Mon→Sun ISO-week bounds containing ``record_date`` (§2.2).

    ``recordDate`` falls within ``coveragePeriod`` by construction. Returns ``None``
    when there is no grounded date — the item then shows no coverage period rather
    than inventing one.
    """
    iso = _iso_date(record_date)
    if iso is None:
        return None
    year, week, _weekday = iso.isocalendar()
    monday = _dt.date.fromisocalendar(year, week, 1)
    sunday = _dt.date.fromisocalendar(year, week, 7)
    return {"startDate": monday.isoformat(), "endDate": sunday.isoformat()}


def _iso_date(value: str | None) -> _dt.date | None:
    """Parse an ISO ``YYYY-MM-DD`` (the read surface's date shape); else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# §2 — per-item projection (one item per served reviewer-internal record)
# ---------------------------------------------------------------------------


def _source_meta(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Categorical ``{source_id: {source_type, source_authority_level}}`` map.

    The only off-read-surface read: the ``sources`` table, restricted to the two
    categorical enum columns the ``sourceTrail`` audit trail needs (§2.2). No
    locator, path, or hash column is read, and the whole artifact is transport-swept
    — categorical strings, never a raw locator.
    """
    meta: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT source_id, source_type, source_authority_level FROM sources"
    ):
        meta[row["source_id"]] = {
            "source_type": row["source_type"],
            "authority_level": row["source_authority_level"],
        }
    return meta


def _source_trail(
    evidence: list[dict[str, Any]], source_meta: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the ``sourceTrail[]`` audit trail (§2.2) from the web-safe evidence drawer.

    Every value rides straight from an already-web-safe evidence-drawer key, or from
    the categorical ``sources`` enums. ``localSourcePath`` is **always null** — the
    reviewer-internal vault path stays local and never enters the artifact (the
    transport sweep would flag it otherwise; contract §6 local-safe rule).
    """
    trail: list[dict[str, Any]] = []
    for link in evidence:
        sid = link.get("to_source_id")
        meta = source_meta.get(sid, {})
        timestamp = (
            link.get("timestamp_seconds")
            if link.get("locator_kind") == "timestamp"
            else None
        )
        trail.append({
            "sourceId": sid,
            "sourceType": meta.get("source_type"),
            "authorityLevel": meta.get("authority_level"),
            "originalUrl": link.get("original_url"),
            "archiveUrl": link.get("archive_url"),
            "scanDate": link.get("scan_date"),
            "localSourcePath": None,  # reviewer-internal: never emit a raw path.
            "timestampSeconds": timestamp,
            "page": link.get("page"),
            "section": link.get("section"),
            "verificationStatus": link.get("verification_status"),
        })
    return trail


def _ids_from_evidence(evidence: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Collect the Stage-3 graph anchor ids the evidence drawer carries.

    ``sourceIds`` from ``to_source_id`` (non-empty for a served record — the read
    surface drops orphans); ``meetingIds`` / ``topicIds`` from the allowlisted
    ``meeting_id`` / ``topic_id`` keys when present. All sorted+de-duped for a
    deterministic, byte-stable projection.
    """
    sources = {link.get("to_source_id") for link in evidence if link.get("to_source_id")}
    meetings = {link.get("meeting_id") for link in evidence if link.get("meeting_id")}
    topics = {link.get("topic_id") for link in evidence if link.get("topic_id")}
    return {
        "sourceIds": sorted(sources),
        "meetingIds": sorted(meetings),
        "topicIds": sorted(topics),
    }


def _labels(record: dict[str, Any], status: str) -> dict[str, Any]:
    """The ``labels.*`` block — every claim-axis value from the Stage-3 vocabulary.

    ``claimStatus`` mirrors the composed Stage-3 ``status``; ``speakerStatus`` /
    ``correctionStatus`` map onto the Stage-3 claim vocab (fail-closed to the
    conservative member). ``aiPresented`` / ``publicationStatus`` are non-claim
    structural axes (bool / draft). Asserted against the vocab by :func:`_item`.
    """
    correction = record.get("correction_status")
    correction_status = (
        correction if correction in STAGE3_CLAIM_VOCAB else CORRECTION_NONE
    )
    return {
        "claimStatus": status,
        "aiPresented": record.get("produced_by") == "ai",
        # The read surface's safe speaker label is non-identifying; the only
        # claim-vocab speaker state it can ground is ``speaker_unidentified``.
        "speakerStatus": "speaker_unidentified",
        "correctionStatus": correction_status,
        "publicationStatus": PUBLICATION_STATUS_DRAFT,
    }


def _links(card_id: str, ids: dict[str, list[str]], trail: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolvable reviewer-internal route references (§2.2). Structural strings only.

    ``timelineUrl`` always present (anchored on the card handle); topic/meeting/
    source URLs only when a real anchor id exists; ``timestampUrl`` only when an
    evidence entry carries a public web URL — never a fabricated link.
    """
    links: dict[str, Any] = {"timelineUrl": f"/alpine/timeline?card={card_id}"}
    if ids["topicIds"]:
        links["topicUrl"] = f"/alpine/topics/{ids['topicIds'][0]}"
    if ids["meetingIds"]:
        links["meetingUrl"] = f"/alpine/meetings/{ids['meetingIds'][0]}"
    if ids["sourceIds"]:
        links["sourceUrl"] = f"/alpine/sources/{ids['sourceIds'][0]}"
    for entry in trail:
        url = entry.get("originalUrl")
        if isinstance(url, str) and read_api._is_web_url(url):
            ts = entry.get("timestampSeconds")
            links["timestampUrl"] = f"{url}&t={ts}s" if ts else url
            break
    return links


def _item(record: dict[str, Any], source_meta: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Project one served reviewer-internal record onto the contract item shape (§2.1).

    ``cardId`` (the Stage-3 card handle), ``status``, ``recordDate``, and the card
    *type* are reused verbatim from :mod:`stage3_card_feed` — so the newsletter
    cannot drift from the Stage-3 vocabulary (zero new labels). ``id`` /
    ``newsletterId`` are assigned later in :func:`build_newsletter_feed` once the
    deterministic chronological order is known.
    """
    statement_id = record["statement_id"]  # KeyError = orphan served upstream (a bug)
    card_type = card_feed._resolve_record_type(record)
    item_type = _ITEM_TYPE_BY_CARD_TYPE.get(card_type, "timeline_chunk")
    if item_type not in ALLOWED_ITEM_TYPES:  # defense-in-depth (vocab is fixed)
        raise NewsletterContractError(f"itemType {item_type!r} outside the allowed set")
    status = card_feed._compose_record_status(record)
    card_id = card_feed.card_handle(card_type, statement_id)
    record_date = card_feed._card_date(record)
    evidence = list(record.get("evidence", []))
    ids = _ids_from_evidence(evidence)
    trail = _source_trail(evidence, source_meta)
    labels = _labels(record, status)
    # zero-new-label guard (EG-7): every claim-axis value is in the Stage-3 vocab.
    for axis_value in (labels["claimStatus"], labels["speakerStatus"]):
        if axis_value not in STAGE3_CLAIM_VOCAB:
            raise NewsletterContractError(
                f"claim-axis label {axis_value!r} outside the Stage-3 vocabulary"
            )
    if labels["correctionStatus"] not in STAGE3_CLAIM_VOCAB | {CORRECTION_NONE}:
        raise NewsletterContractError(
            f"correctionStatus {labels['correctionStatus']!r} outside the vocabulary"
        )
    item: dict[str, Any] = {
        "itemType": item_type,
        "jurisdiction": dict(JURISDICTION),
        "recordDate": record_date,
        "coveragePeriod": _coverage_period(record_date),
        "topicIds": ids["topicIds"],
        "cardIds": [card_id],
        "meetingIds": ids["meetingIds"],
        "sourceIds": ids["sourceIds"],
        "status": status,
        "labels": labels,
        "links": _links(card_id, ids, trail),
        "sourceTrail": trail,
    }
    # Optional grounded presentation fields (never invented).
    summary = record.get("statement_text")
    if isinstance(summary, str) and summary:
        item["summary"] = summary  # reviewer-internal free text (contract §2.1)
    title = record.get("title")
    if isinstance(title, str) and title:
        item["title"] = title
    return item


# ---------------------------------------------------------------------------
# §4 — traceability / orphan classification (zero invented items)
# ---------------------------------------------------------------------------

VSR = "VerificationSafetyReviewer"


def classify_orphan(item: dict[str, Any]) -> str | None:
    """Return the orphan reason (§4) for ``item``, or ``None`` if it is anchored.

    An item is an orphan when ``sourceIds`` is empty, OR it has no card/topic/meeting
    Stage-3 anchor. (Dangling references cannot occur in this feed — every item is
    projected FROM a real served record — but the rule is checked structurally so a
    future input that violates it is held, never promoted.)
    """
    if not item.get("sourceIds"):
        return "empty_source_ids"
    if not (item.get("cardIds") or item.get("topicIds") or item.get("meetingIds")):
        return "no_stage3_anchor"
    return None


# ---------------------------------------------------------------------------
# §2 — the newsletter feed envelope
# ---------------------------------------------------------------------------


def _sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """Total, deterministic oldest→newest ordering key (§2.4).

    Primary ``recordDate`` then ``coveragePeriod.startDate`` then the stable card
    handle — a total order, so the same DB yields a byte-identical item sequence and
    ``id`` assignment (undated records sort last under the high sentinel).
    """
    record_date = item.get("recordDate") or "9999-99-99"
    period = item.get("coveragePeriod") or {}
    start = period.get("startDate") or "9999-99-99"
    card_id = item["cardIds"][0] if item.get("cardIds") else ""
    return (record_date, start, card_id)


def build_newsletter_feed(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the ``{scope, access, items[]}`` newsletter feed and transport-sweep it.

    One item per served reviewer-internal record (a 1:1 projection with NO silent
    drop — :func:`assert_feed_covers_surface` proves it). Items are ordered
    oldest→newest (§2.4) and assigned a deterministic ``id``
    (``alpine-newsletter-item-NNN``) and ``newsletterId`` (the ISO-week batch).
    Orphans (none, for this surface) are held out and routed by
    :func:`source_link_validation`. The whole body is swept by
    :func:`read_api.assert_no_raw_paths`, so a leak fails LOUDLY at the boundary.
    """
    source_meta = _source_meta(conn)
    candidates = [
        _item(record, source_meta) for record in read_api.reviewer_internal_records(conn)
    ]
    items = [item for item in candidates if classify_orphan(item) is None]
    items.sort(key=_sort_key)
    for index, item in enumerate(items, start=1):
        item["id"] = f"alpine-newsletter-item-{index:03d}"
        item["newsletterId"] = _newsletter_id(item.get("recordDate"))
    feed: dict[str, Any] = {"scope": SCOPE, "access": ACCESS, "items": items}
    return _assert_local_safe(feed)


# ---------------------------------------------------------------------------
# §2.4 — chronology guard (EG-3): non-decreasing oldest→newest within a batch.
# ---------------------------------------------------------------------------


def assert_chronology(feed: dict[str, Any]) -> bool:
    """RED if any ``newsletterId`` batch is not non-decreasing by ``recordDate`` (§2.4).

    Operates on the EMITTED feed order (not a recompute), so a build that emits a
    later record ahead of an earlier one within a batch goes RED — mirroring the
    EG-3 oldest→newest check. Undated records (sentinel) sort last consistently.
    """
    batches: dict[str, list[str]] = {}
    for item in feed.get("items", []):
        key = _sort_key(item)[:2]  # (recordDate, coveragePeriod.startDate)
        batches.setdefault(item.get("newsletterId"), []).append(key)
    for newsletter_id, keys in batches.items():
        for earlier, later in zip(keys, keys[1:]):
            if earlier > later:
                raise NewsletterContractError(
                    f"batch {newsletter_id!r} not oldest->newest: {earlier} > {later}"
                )
    return True


# ---------------------------------------------------------------------------
# Back-gap coverage guard (3.13 / GOV-322 pattern) — feed never silently drops.
# ---------------------------------------------------------------------------


class FeedCoverageError(NewsletterContractError):
    """Raised when the feed drops a served record the read surface still emits."""


def expected_card_ids(conn: sqlite3.Connection) -> set[str]:
    """The card-handle set the read surface mandates — recomputed independently.

    A separate derivation from :func:`build_newsletter_feed` so the guard is a real
    cross-check, not a tautology: it re-projects each served record to its Stage-3
    card handle directly. Orphan records (none on this surface) are excluded, since
    they are held out of the feed.
    """
    handles: set[str] = set()
    source_meta = _source_meta(conn)
    for record in read_api.reviewer_internal_records(conn):
        item = _item(record, source_meta)
        if classify_orphan(item) is None:
            handles.add(item["cardIds"][0])
    return handles


def assert_feed_covers_surface(
    conn: sqlite3.Connection, feed: dict[str, Any] | None = None
) -> bool:
    """RED if any served read-surface record is missing from ``feed`` (back-gap guard)."""
    if feed is None:
        feed = build_newsletter_feed(conn)
    feed_cards = {cid for item in feed.get("items", []) for cid in item.get("cardIds", [])}
    expected = expected_card_ids(conn)
    missing = expected - feed_cards
    if missing:
        raise FeedCoverageError(
            f"feed dropped {len(missing)} read-surface record(s): {sorted(missing)}"
        )
    return True


# ---------------------------------------------------------------------------
# EG-7 — zero-new-label diff vs the Stage-3 claim vocabulary.
# ---------------------------------------------------------------------------


def label_vocabulary_diff(feed: dict[str, Any]) -> set[str]:
    """Claim-axis label values used in ``feed`` that are NOT in the Stage-3 vocab (§2.3).

    EG-7 passes iff this set is empty. Only the claim-status axes are diffed
    (``status`` / ``claimStatus`` / ``speakerStatus`` and a non-``none``
    ``correctionStatus``); ``aiPresented`` (bool) and ``publicationStatus`` (the
    publication-control axis) are structural, not claim labels.
    """
    used: set[str] = set()
    for item in feed.get("items", []):
        used.add(item.get("status"))
        labels = item.get("labels", {})
        used.add(labels.get("claimStatus"))
        used.add(labels.get("speakerStatus"))
        correction = labels.get("correctionStatus")
        if correction and correction != CORRECTION_NONE:
            used.add(correction)
    used.discard(None)
    return used - STAGE3_CLAIM_VOCAB


# ---------------------------------------------------------------------------
# §4 — source-link validation log + orphan→VSR routing (EG-4 / EG-11 / EG-12).
# ---------------------------------------------------------------------------


def source_link_validation(conn: sqlite3.Connection) -> dict[str, Any]:
    """One row per candidate item (``item_id`` / ``links_present`` / ``orphan_reason?``).

    Emits the EG-4/EG-11 validation artifact: every candidate (served record) gets a
    row; an orphan is routed to VSR (``routed_to`` / ``status: held``) and never
    promoted to the feed. ``passed`` is true iff zero orphans are promoted OR every
    orphan carries a routing entry (always true here — orphans are held by
    construction). The artifact is transport-swept.
    """
    source_meta = _source_meta(conn)
    rows: list[dict[str, Any]] = []
    routing: list[dict[str, Any]] = []
    promoted_ids = {
        item["id"]
        for item in build_newsletter_feed(conn).get("items", [])
    }
    # Re-derive the candidate set (pre-orphan-filter) with stable per-card ids so the
    # validation row id is traceable even for a held orphan (which has no feed id).
    for record in read_api.reviewer_internal_records(conn):
        item = _item(record, source_meta)
        item_id = item["cardIds"][0]
        reason = classify_orphan(item)
        row: dict[str, Any] = {
            "item_id": item_id,
            "links_present": reason is None,
        }
        if reason is not None:
            row["orphan_reason"] = reason
            routing.append({
                "item_id": item_id,
                "orphan_reason": reason,
                "routed_to": VSR,
                "status": "held",
            })
        rows.append(row)
    orphans_promoted = any(
        row.get("orphan_reason") and row["item_id"] in promoted_ids for row in rows
    )
    routed_ids = {entry["item_id"] for entry in routing}
    every_orphan_routed = all(
        row["item_id"] in routed_ids for row in rows if row.get("orphan_reason")
    )
    artifact = {
        "scope": SCOPE,
        "access": ACCESS,
        "rows": rows,
        "routing": routing,
        "passed": (not orphans_promoted) and every_orphan_routed,
    }
    return _assert_local_safe(artifact)


# ---------------------------------------------------------------------------
# §3 — source-set + backfill chronology readiness record (EG-3).
# ---------------------------------------------------------------------------


def build_readiness_record(conn: sqlite3.Connection) -> dict[str, Any]:
    """The §3 source-set / backfill progress record (feeds EG-3).

    Names the Alpine source categories reviewed and the oldest→newest range covered
    by the reviewed, source-linked records actually in the feed — never a completion
    overclaim. ``knownGaps`` is populated from the read surface's completeness gaps
    (GOV-298) so the record honestly states what is NOT yet reviewed.
    """
    feed = build_newsletter_feed(conn)
    items = feed.get("items", [])
    source_meta = _source_meta(conn)
    referenced = {
        sid for item in items for sid in item.get("sourceIds", [])
    }
    categories = sorted(
        {
            source_meta.get(sid, {}).get("source_type")
            for sid in referenced
            if source_meta.get(sid, {}).get("source_type")
        }
    )
    dates = sorted(item["recordDate"] for item in items if item.get("recordDate"))
    chronological_range = (
        {"oldest": dates[0], "newest": dates[-1]} if dates else None
    )
    gaps = read_api.completeness_gap_cards(conn)
    known_gaps = sorted(
        {
            f"{gap.get('gap_type')} ({gap.get('severity')})"
            for gap in gaps
        }
    ) or ["no reviewed-vs-unreviewed gap recorded for this batch"]
    record = {
        "scope": SCOPE,
        "access": ACCESS,
        "sourceCategoriesReviewed": categories,
        "chronologicalRangeProcessed": chronological_range,
        "orderingPreserved": "oldest_to_newest",
        "reviewedBatchBoundary": "only reviewed, source-linked Stage-3 records included",
        "knownGaps": known_gaps,
        "completionFraming": (
            "visible browser-reviewable progress; NOT full-history-complete"
        ),
    }
    return _assert_local_safe(record)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 4.03 reviewer-internal Alpine newsletter item feed (GOV-449)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--artifact",
        choices=("feed", "readiness", "validation"),
        default="feed",
        help="which contract artifact to emit (default: the item feed)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the chronology + back-gap coverage + zero-new-label guards",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.artifact == "feed":
            out: dict[str, Any] = build_newsletter_feed(conn)
            if args.check:
                assert_chronology(out)
                assert_feed_covers_surface(conn, out)
                diff = label_vocabulary_diff(out)
                if diff:
                    raise NewsletterContractError(f"new labels introduced: {sorted(diff)}")
        elif args.artifact == "readiness":
            out = build_readiness_record(conn)
        else:
            out = source_link_validation(conn)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
