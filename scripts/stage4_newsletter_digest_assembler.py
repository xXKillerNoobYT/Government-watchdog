"""Stage 4.05 deterministic digest assembler (GOV-457) — reviewer-internal, Alpine.

Implements the GOV-457 contract
(``docs/stage4-05-newsletter-digest-assembler-contract.md``): a deterministic
**digest assembler** that consumes the GOV-449 newsletter item feed
(:func:`stage4_newsletter_feed.build_newsletter_feed`) and groups its items into one
**structured reviewer-internal Alpine weekly digest per coverage period**, emitting the
required **GOV-15 newsletter template sections as structured data** (never prose).

This layer is a **pure structured projection over the existing item feed, not a new
source of truth** — it invents no item, generates no prose, performs no rendering/markup
(that is 4.06), and applies no editorial voice or summarization (that is 4.08). Every
item is carried through **verbatim**: its Stage-3 label and ``sourceTrail[]`` are never
re-derived (the feed already crossed the web-safe and transport-sweep layers). Five
deliverables, all from the contract:

* **§1 grouping** (:func:`assemble_digests`) — partition the feed items by
  ``newsletterId`` (the ``alpine-historical-YYYY-WW`` ISO-week batch), one digest each,
  digests ordered by ``newsletterId`` and items kept in the feed's oldest→newest order.
* **§2 GOV-15 sections** (:func:`_digest_sections`) — classify the digest's items into
  the GOV-15 template buckets (``processedRecords`` / ``sourceSetProgress`` /
  ``timelineChunks`` / ``keyMeetings`` / ``keyDocuments`` / ``topics`` / ``corrections`` /
  ``conflicts`` / ``laterOutcomes`` / ``unverifiedItems`` / ``sourceTrail``) as structured
  data, classifying existing Stage-3 labels — never minting a new label.
* **§4 EG-5 section presence** (:func:`assert_section_presence`) and **EG-3 chronology**
  (:func:`assert_digest_chronology`) — the two exit-gate guards over the assembled digests.
* **§4 preservation** (:func:`assert_labels_preserved` / :func:`assert_source_trail_preserved`)
  — every digest item's ``labels`` / ``sourceTrail`` equals the feed item of the same id.
* **§4 reproducibility** (:func:`assert_reproducible`) and the **§3 audit overlay**
  (:func:`build_digest_overlay`) — re-assembly is byte-identical; the overlay is the
  swept reviewer-internal summary (GOV-453 precedent).

Boundary rules (contract §6, restated as assembler invariants):

* the assembler **never** calls ``to_web_safe`` / ``publication.py`` and **never**
  re-derives a feed field — it reuses :mod:`stage4_newsletter_feed` (``SCOPE`` / ``ACCESS``
  / ``STAGE3_CLAIM_VOCAB`` / ``CORRECTION_NONE`` / ``_sort_key``) and :mod:`read_api`
  (``assert_no_raw_paths`` / ``RAW_PATH_MARKERS``) **by reference**, re-declaring none of
  their constants — so the public contract surfaces stay byte-0-diff;
* it runs entirely at ``access: reviewer_internal`` and ``scope: alpine`` — Alpine-only;
* every emitted artifact (the digest object and the overlay) is transport-swept by
  :func:`read_api.assert_no_raw_paths` — a leak fails LOUDLY at the boundary.

Pure function of the feed: same DB -> byte-identical digest object (idempotent
re-assembly). No mutation, no AI, no network, no public publication.
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
import read_api  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402

# ---------------------------------------------------------------------------
# §2 GOV-15 section classification sets — buckets over the EXISTING Stage-3
# claim vocabulary (nl.STAGE3_CLAIM_VOCAB), never new labels. fail-closed.
# ---------------------------------------------------------------------------

# A claim that records a dispute/conflict between sources or accounts.
CONFLICT_STATUSES = frozenset({"disputed"})
# Outcomes that emerged after the initial coverage window (source moved / vanished).
LATER_OUTCOME_STATUSES = frozenset({"source_changed", "source_missing"})
# Anything not affirmatively verified is surfaced as unverified — conservative, so an
# AI/unreviewed/disputed item is never presented as established fact (contract §2).
UNVERIFIED_STATUSES = frozenset(nl.STAGE3_CLAIM_VOCAB - {"verified"})

# The GOV-15 template sections every digest must expose as structured data (EG-5).
REQUIRED_SECTIONS = (
    "processedRecords", "sourceSetProgress", "timelineChunks", "keyMeetings",
    "keyDocuments", "topics", "corrections", "conflicts", "laterOutcomes",
    "unverifiedItems", "sourceTrail",
)


class DigestContractError(AssertionError):
    """Raised when an assembled digest violates a GOV-457 contract invariant."""


class DigestSectionError(DigestContractError):
    """A digest is missing a required GOV-15 section, or a section carries prose (EG-5)."""


class DigestChronologyError(DigestContractError):
    """A digest's items are not non-decreasing by ``recordDate`` (EG-3)."""


class DigestPreservationError(DigestContractError):
    """A digest item's ``labels`` / ``sourceTrail`` diverged from the feed item."""


class DigestReproducibilityError(DigestContractError):
    """Re-assembling the digest object did not produce byte-identical JSON (NF-A)."""


# ---------------------------------------------------------------------------
# §1 — grouping the item feed into one digest per coverage period
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    """Canonical, key-sorted JSON — the byte-comparison form for NF-A / fingerprints."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _digest_sections(items: list[dict[str, Any]], readiness: dict[str, Any]) -> dict[str, Any]:
    """Classify a digest's items into the GOV-15 template sections (§2).

    Every section is structured data derived from fields the feed already emits — id
    lists (kept in the feed's chronological order) plus sorted, deduped graph
    aggregates. The classification sets are buckets over the imported Stage-3 claim
    vocabulary, so no new label is ever introduced.
    """
    item_ids = [item["id"] for item in items]

    def _claim(item: dict[str, Any]) -> Any:
        return item.get("labels", {}).get("claimStatus")

    def _correction(item: dict[str, Any]) -> Any:
        return item.get("labels", {}).get("correctionStatus")

    # source-set / backfill progress — per-digest categories + range; corpus-wide
    # gap framing carried verbatim from the readiness record (never re-derived).
    categories = sorted({
        entry.get("sourceType")
        for item in items
        for entry in item.get("sourceTrail", [])
        if entry.get("sourceType")
    })
    dates = sorted(item["recordDate"] for item in items if item.get("recordDate"))
    chronological_range = {"oldest": dates[0], "newest": dates[-1]} if dates else None

    # sourceTrail: dedup by sourceId (first occurrence kept — deterministic since items
    # are in feed order), sorted by sourceId, each entry carried UNCHANGED.
    seen_sources: dict[Any, dict[str, Any]] = {}
    for item in items:
        for entry in item.get("sourceTrail", []):
            sid = entry.get("sourceId")
            if sid not in seen_sources:
                seen_sources[sid] = entry
    source_trail = [seen_sources[sid] for sid in sorted(seen_sources, key=lambda s: (s is None, s))]

    return {
        "processedRecords": {"count": len(items), "itemIds": item_ids},
        "sourceSetProgress": {
            "sourceCategoriesReviewed": categories,
            "chronologicalRange": chronological_range,
            "orderingPreserved": "oldest_to_newest",
            "knownGaps": list(readiness.get("knownGaps", [])),
            "completionFraming": readiness.get("completionFraming"),
        },
        "timelineChunks": [it["id"] for it in items if it.get("itemType") == "timeline_chunk"],
        "keyMeetings": sorted({mid for it in items for mid in it.get("meetingIds", [])}),
        "keyDocuments": sorted({sid for it in items for sid in it.get("sourceIds", [])}),
        "topics": sorted({tid for it in items for tid in it.get("topicIds", [])}),
        "corrections": [
            it["id"] for it in items
            if it.get("itemType") == "correction"
            or (_correction(it) not in (None, nl.CORRECTION_NONE))
        ],
        "conflicts": [it["id"] for it in items if _claim(it) in CONFLICT_STATUSES],
        "laterOutcomes": [it["id"] for it in items if _claim(it) in LATER_OUTCOME_STATUSES],
        "unverifiedItems": [it["id"] for it in items if _claim(it) in UNVERIFIED_STATUSES],
        "sourceTrail": source_trail,
    }


def assemble_digests(
    conn: sqlite3.Connection, feed: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Group the GOV-449 item feed into one structured digest per coverage period (§1).

    A pure partition of ``build_newsletter_feed(conn)["items"]`` by ``newsletterId``:
    digests are ordered by ``newsletterId``; within a digest the items keep the feed's
    global ``_sort_key`` order (oldest→newest), so the same DB yields a byte-identical
    digest object. Each digest carries its items VERBATIM (labels + ``sourceTrail``
    unchanged) plus the §2 GOV-15 sections. The whole body is swept by
    :func:`read_api.assert_no_raw_paths`, so a leak fails LOUDLY at the boundary.
    """
    if feed is None:
        feed = nl.build_newsletter_feed(conn)
    readiness = nl.build_readiness_record(conn)

    groups: dict[str, list[dict[str, Any]]] = {}
    for item in feed.get("items", []):
        groups.setdefault(item.get("newsletterId"), []).append(item)

    digests: list[dict[str, Any]] = []
    for newsletter_id in sorted(groups, key=lambda n: (n is None, n)):
        items = sorted(groups[newsletter_id], key=nl._sort_key)
        digests.append({
            "newsletterId": newsletter_id,
            # all dated items in an ISO-week batch share the same coveragePeriod (it is
            # derived from the same week); the undated batch carries null.
            "coveragePeriod": items[0].get("coveragePeriod"),
            "items": items,
            "sections": _digest_sections(items, readiness),
        })

    out = {"scope": nl.SCOPE, "access": nl.ACCESS, "digests": digests}
    # The digest object embeds the feed items, which carry reviewer-internal ``/alpine/``
    # ROUTE references (links.*). Reuse the feed's own route-aware transport sweep
    # (:func:`stage4_newsletter_feed._assert_local_safe`) — it single-sources read_api's
    # leak vocabulary but exempts those routes from the absolute-path rule, so the route
    # exemption lives in exactly one place (extend-not-fork). A raw vault path / ``..`` /
    # ``file://`` still fails LOUDLY.
    return nl._assert_local_safe(out)


# ---------------------------------------------------------------------------
# §4 — EG-5 section presence + EG-3 chronology guards
# ---------------------------------------------------------------------------


def assert_section_presence(digests: list[dict[str, Any]]) -> bool:
    """RED if any digest is missing a required GOV-15 section, or a section is prose (EG-5).

    Section values must be structured data (list / dict); a bare string is a prose smell
    — this slice emits data, never narrative (4.08 is the gated editorial layer).
    """
    for d in digests:
        sections = d.get("sections", {})
        missing = [name for name in REQUIRED_SECTIONS if name not in sections]
        if missing:
            raise DigestSectionError(
                f"digest {d.get('newsletterId')!r} missing GOV-15 section(s): {missing}"
            )
        for name in REQUIRED_SECTIONS:
            if isinstance(sections[name], str):
                raise DigestSectionError(
                    f"digest {d.get('newsletterId')!r} section {name!r} is prose, not data"
                )
    return True


def assert_digest_chronology(digests: list[dict[str, Any]]) -> bool:
    """RED if any digest's items are not non-decreasing by ``recordDate`` (EG-3).

    Operates on the EMITTED item order (not a recompute), reusing the feed's total
    ``_sort_key``; undated records sort last consistently under the sentinel.
    """
    for d in digests:
        keys = [nl._sort_key(item) for item in d.get("items", [])]
        for earlier, later in zip(keys, keys[1:]):
            if earlier > later:
                raise DigestChronologyError(
                    f"digest {d.get('newsletterId')!r} not oldest->newest: {earlier} > {later}"
                )
    return True


# ---------------------------------------------------------------------------
# §4 — label + sourceTrail preservation (carried unchanged from the feed)
# ---------------------------------------------------------------------------


def _feed_index(conn: sqlite3.Connection, feed: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if feed is None:
        feed = nl.build_newsletter_feed(conn)
    return {item["id"]: item for item in feed.get("items", [])}


def assert_labels_preserved(
    conn: sqlite3.Connection, digests: list[dict[str, Any]], feed: dict[str, Any] | None = None
) -> bool:
    """RED if any digest item's ``labels`` differs from the feed item of the same id."""
    index = _feed_index(conn, feed)
    for d in digests:
        for item in d.get("items", []):
            source = index.get(item.get("id"))
            if source is None:
                raise DigestPreservationError(
                    f"digest item {item.get('id')!r} not in the feed"
                )
            if _canonical(item.get("labels")) != _canonical(source.get("labels")):
                raise DigestPreservationError(
                    f"labels mutated for digest item {item.get('id')!r}"
                )
    return True


def assert_source_trail_preserved(
    conn: sqlite3.Connection, digests: list[dict[str, Any]], feed: dict[str, Any] | None = None
) -> bool:
    """RED if any digest item's ``sourceTrail`` differs from the feed item of the same id."""
    index = _feed_index(conn, feed)
    for d in digests:
        for item in d.get("items", []):
            source = index.get(item.get("id"))
            if source is None:
                raise DigestPreservationError(
                    f"digest item {item.get('id')!r} not in the feed"
                )
            if _canonical(item.get("sourceTrail")) != _canonical(source.get("sourceTrail")):
                raise DigestPreservationError(
                    f"sourceTrail mutated for digest item {item.get('id')!r}"
                )
    return True


# ---------------------------------------------------------------------------
# §4 — NF-A reproducibility (idempotent re-assembly) + opaque fingerprint
# ---------------------------------------------------------------------------


def digest_fingerprint(out: dict[str, Any]) -> str:
    """sha256 of the canonical digest object — single opaque envelope-level fingerprint."""
    return hashlib.sha256(_canonical(out).encode("utf-8")).hexdigest()


def assert_reproducible(conn: sqlite3.Connection) -> str:
    """RED if re-assembling the digest object is not byte-identical (NF-A). Returns the digest.

    Assembles twice and compares the canonical JSON; a tautological self-compare would
    still pass, so the guard re-runs the full assembly each time.
    """
    first = _canonical(assemble_digests(conn))
    second = _canonical(assemble_digests(conn))
    if first != second:
        raise DigestReproducibilityError("digest assembly is not byte-identical across runs")
    return hashlib.sha256(first.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# §3 — reviewer-internal audit overlay (GOV-453 precedent)
# ---------------------------------------------------------------------------


def build_digest_overlay(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the §3 reviewer-internal audit overlay and transport-sweep it.

    Runs the five contract guards (section presence / chronology / label + sourceTrail
    preservation / reproducibility), collecting violations rather than raising so the
    overlay can report a non-clean corpus honestly, then sweeps the envelope.
    """
    out = assemble_digests(conn)
    digests = out["digests"]
    item_count = sum(len(d["items"]) for d in digests)

    violations: dict[str, list[str]] = {
        "sections": [], "chronology": [], "labels": [], "source_trail": [],
        "reproducibility": [],
    }
    try:
        assert_section_presence(digests)
    except DigestSectionError as exc:  # pragma: no cover - clean corpus path tested
        violations["sections"].append(str(exc))
    try:
        assert_digest_chronology(digests)
    except DigestChronologyError as exc:  # pragma: no cover
        violations["chronology"].append(str(exc))
    try:
        assert_labels_preserved(conn, digests)
    except DigestPreservationError as exc:  # pragma: no cover
        violations["labels"].append(str(exc))
    try:
        assert_source_trail_preserved(conn, digests)
    except DigestPreservationError as exc:  # pragma: no cover
        violations["source_trail"].append(str(exc))
    try:
        assert_reproducible(conn)
    except DigestReproducibilityError as exc:  # pragma: no cover
        violations["reproducibility"].append(str(exc))

    overlay = {
        "scope": nl.SCOPE,
        "access": nl.ACCESS,
        "digest_count": len(digests),
        "item_count": item_count,
        "sections_complete": not violations["sections"],
        "chronology_ok": not violations["chronology"],
        "labels_preserved": not violations["labels"],
        "source_trail_preserved": not violations["source_trail"],
        "reproducible": not violations["reproducibility"],
        "digest_digest": digest_fingerprint(out),
        "violations": violations,
    }
    return read_api.assert_no_raw_paths(overlay)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 4.05 reviewer-internal Alpine newsletter digest assembler (GOV-457)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--artifact",
        choices=("digest", "overlay"),
        default="digest",
        help="which artifact to emit (default: the digest object)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the section-presence + chronology + preservation + reproducibility guards",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.artifact == "digest":
            out: dict[str, Any] = assemble_digests(conn)
            if args.check:
                digests = out["digests"]
                assert_section_presence(digests)
                assert_digest_chronology(digests)
                assert_labels_preserved(conn, digests)
                assert_source_trail_preserved(conn, digests)
                assert_reproducible(conn)
        else:
            out = build_digest_overlay(conn)
            if args.check:
                # the overlay already ran the guards; surface any violation as a non-zero exit.
                if any(out["violations"].values()):
                    raise DigestContractError(f"overlay violations: {out['violations']}")
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
