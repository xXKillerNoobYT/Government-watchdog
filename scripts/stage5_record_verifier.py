"""Stage 5.04 record verifier / producer (GOV-488) — reviewer-internal, Alpine.

Drives **at least one** Alpine source record to ``verified`` by resolving — and then
recording — the real evidence a verified record requires, then *proving* the
transition through the REAL read surface -> newsletter feed -> 4.05 digest pipeline
(never a self-asserted flag). The four evidence elements (issue scope):

1. **resolvable primary ``originalUrl``** — the live Alpine government source page
   (:func:`is_resolvable_primary_url`: a well-formed public ``http(s)://`` locator,
   never a raw fetched path);
2. **resolvable ``archiveUrl``** — a real Wayback snapshot *near the scan date*,
   consumed from the merged 5.03 contract's
   ``archiveAvailability.nearestSnapshotRef`` and resolved by
   :func:`resolve_archive_snapshot` (still a reviewer-internal POINTER, never a
   fetched local path);
3. **primary sources for the ``ai_presented`` observations** —
   :func:`source_ai_observation` attaches a resolvable primary source to an
   ``ai_presented`` observation so the count of *unsourced* ``ai_presented``
   observations drops (:func:`count_unsourced_ai_presented`);
4. **real record/event date** — carried on the grounding evidence's ``scan_date`` so
   the served record buckets into a real ``alpine-historical-YYYY-WW`` ISO coverage
   week (never a ``…-undated`` bucket).

The producer is **deterministic and idempotent**: the single missing ingredient over
an already reviewed, source-backed, promoted reviewer-internal record is
``provenance_status == grounded`` — which needs a *preserved raw predecessor*
(:func:`stage2_traceability.raw_linked`). :func:`verify_record` records that preserved
raw (a ``documents`` child carrying the content ``sha256``; the raw bytes/path stay
backend-only and never reach a served body) and confirms the archive snapshot, so the
record's provenance turns ``grounded`` and it composes to ``verified`` through
:func:`stage3_card_feed._compose_record_status`. No URL, date, or hash is fabricated.

Boundary rules (mirrors every Stage-3/4/5 slice):

* it **never** mutates ``read_api.py`` / ``publication.py`` /
  ``stage4_newsletter_feed.py`` / ``stage4_newsletter_digest_assembler.py`` /
  ``stage5_source_inventory.py`` — it consumes them by reference (I4);
* every emitted artifact is transport-swept by :func:`read_api.assert_no_raw_paths`,
  so a FS path / ``.sha256`` / vault marker / ``file://`` that slipped a column fails
  LOUDLY at the boundary (I1); ``localSourcePath`` is never emitted (I2); exactly one
  envelope ``verificationDigest`` is exposed — no per-source raw-content hash (I3);
* it runs entirely at ``access: reviewer_internal`` / ``scope: alpine`` and is absent
  from any public / ``published_records`` path (I6); public launch stays Isaac-gated
  (GOV-420), untouched here.

Pure function of the registry once the evidence is recorded: same DB -> byte-identical
verified-record envelope (idempotent re-projection). No AI, no network in the resolve
or assert path; the only writes are the additive reviewer-internal grounding rows.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
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
import stage4_newsletter_digest_assembler as digest  # noqa: E402  (real downstream check)
import stage4_newsletter_feed as nl  # noqa: E402  (feed projection + date helpers)
import stage5_source_inventory as inv  # noqa: E402  (the merged 5.03 contract — consumed)

SCOPE = "alpine"
ACCESS = "reviewer_internal"  # never "public" — I6

# The six ``ai_presented`` observations named in the GOV-488 scope: verification must
# drop the count of *unsourced* ones below this baseline.
AI_PRESENTED_BASELINE = 6

# A Wayback snapshot reference: ``…/web/YYYYMMDDhhmmss/<original>``. The leading 8
# digits are the snapshot's calendar date — all we resolve (time-of-day is ignored).
_WAYBACK_RE = re.compile(
    r"^https?://web\.archive\.org/web/(\d{4})(\d{2})(\d{2})\d*/", re.IGNORECASE
)
# A snapshot counts as "near the scan date" when within this many days — a month
# window so a same-period archive grab grounds the record without claiming a
# day-exact capture we did not make.
ARCHIVE_NEARNESS_DAYS = 31


class RecordVerifyError(AssertionError):
    """Raised when a record that should be ``verified`` fails a 5.04 contract guard."""


# ---------------------------------------------------------------------------
# Pure resolvability predicates (no network in this path — structural resolve)
# ---------------------------------------------------------------------------


def is_resolvable_primary_url(url: Any) -> bool:
    """True iff ``url`` is a resolvable primary-source locator (I-scope element 1).

    A resolvable primary ``originalUrl`` is a well-formed PUBLIC ``http(s)://`` URL
    carrying a network host — never a ``file://`` vault URI, a raw fetched path, or a
    bare marker. Reuses :func:`read_api._is_web_url` (single-sources the web-URL rule)
    and rejects any value carrying a raw-path marker, so a poisoned locator can never
    pass as "resolvable".
    """
    if not isinstance(url, str) or not read_api._is_web_url(url):
        return False
    if any(marker in url for marker in read_api.RAW_PATH_MARKERS):
        return False
    # a host must follow the scheme (``https://`` alone is not resolvable).
    rest = url.split("://", 1)[1]
    host = rest.split("/", 1)[0]
    return bool(host) and "." in host


def resolve_archive_snapshot(scan_date: Any, archive_url: Any) -> dict[str, Any] | None:
    """Resolve a Wayback ``archive_url`` to a snapshot near ``scan_date`` (element 2).

    Consumes the reviewer-internal pointer the 5.03 contract surfaces
    (``archiveAvailability.nearestSnapshotRef``) and confirms it is a genuine
    ``web.archive.org/web/<timestamp>/…`` reference whose snapshot date is within
    :data:`ARCHIVE_NEARNESS_DAYS` of the record's ``scan_date``. Returns the resolved
    pointer ``{snapshotRef, snapshotDate, deltaDays, nearScanDate}`` (still a pointer,
    never a fetched path), or ``None`` when the ref is absent/malformed. Pure function
    of its two inputs — no network.
    """
    if not isinstance(archive_url, str):
        return None
    match = _WAYBACK_RE.match(archive_url)
    if match is None:
        return None
    try:
        snap = _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    scan = nl._iso_date(scan_date)
    delta = abs((snap - scan).days) if scan is not None else None
    return {
        "snapshotRef": archive_url,
        "snapshotDate": snap.isoformat(),
        "deltaDays": delta,
        "nearScanDate": delta is not None and delta <= ARCHIVE_NEARNESS_DAYS,
    }


# ---------------------------------------------------------------------------
# Consume the merged 5.03 inventory contract (read-only)
# ---------------------------------------------------------------------------


def inventory_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{source_id: inventory entry}`` from the merged 5.03 contract (GOV-484).

    The 5.04 producer consumes the Stage-5 inventory as its source-of-record for each
    source's ``archiveAvailability`` (and ``nearestSnapshotRef``) rather than re-deriving
    it — extend-not-fork over the merged contract.
    """
    body = inv.build_inventory(conn)
    return {entry["source_id"]: entry for entry in body.get("sources", [])}


# ---------------------------------------------------------------------------
# Producer — idempotent, additive reviewer-internal grounding writes
# ---------------------------------------------------------------------------


def record_preserved_raw(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    sha256: str,
    fetched_url: str,
    local_path: str,
    fetch_time_utc: str,
    doc_date: str | None = None,
    title: str | None = None,
    commit: bool = True,
) -> bool:
    """Record a preserved raw predecessor for ``source_id`` (grounds the citation).

    Inserts ONE ``documents`` child carrying the content ``sha256`` — the preserved
    raw predecessor :func:`stage2_traceability.raw_linked` looks for (its documents-
    child branch). The raw ``local_path`` / fetched bytes stay BACKEND-ONLY: the
    ``documents`` table is never served by :mod:`read_api`, so the path never reaches a
    web-safe body (the transport sweep over every EMITTED artifact is the backstop).

    Idempotent: a child with the same ``source_url`` is left untouched (no churn, no
    re-fetch, no hash overwrite — the absolute drift rule). Returns ``True`` when a row
    was newly inserted, ``False`` when one already existed.
    """
    existing = conn.execute(
        "SELECT 1 FROM documents WHERE source_url = ?", (fetched_url,)
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO documents (source_url, title, doc_type, doc_date, local_path, "
        "sha256, fetch_time_utc, source_id) VALUES (?, ?, 'primary_source', ?, ?, ?, ?, ?)",
        (fetched_url, title, doc_date, local_path, sha256, fetch_time_utc, source_id),
    )
    if commit:
        conn.commit()
    return True


def confirm_archive_snapshot(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    archive_url: str,
    scan_date: str,
    commit: bool = True,
) -> bool:
    """Confirm the statement's grounding evidence carries the near-scan archive snapshot.

    Sets ``archive_url`` / ``archive_status='available'`` / ``scan_date`` on the
    statement's evidence link(s) so the served record reads ``archived-source-backed``
    (or ``source-backed``) and the snapshot pointer rides the ``sourceTrail``. Refuses a
    snapshot that does not resolve near the scan date (:func:`resolve_archive_snapshot`)
    — never records a fabricated/placeholder archive. Idempotent: re-running with the
    same inputs is a byte-stable no-op. Returns ``True`` iff a row changed.
    """
    snapshot = resolve_archive_snapshot(scan_date, archive_url)
    if snapshot is None or not snapshot["nearScanDate"]:
        raise RecordVerifyError(
            f"archive_url {archive_url!r} does not resolve to a snapshot near "
            f"scan_date {scan_date!r} (within {ARCHIVE_NEARNESS_DAYS}d)"
        )
    cursor = conn.execute(
        "UPDATE evidence_links SET archive_url = ?, archive_status = 'available', "
        "scan_date = ? WHERE from_node_id = ? AND from_node_type = 'statement' "
        "AND (archive_url IS NOT ? OR archive_status IS NOT 'available' OR scan_date IS NOT ?)",
        (archive_url, scan_date, statement_id, archive_url, scan_date),
    )
    if commit:
        conn.commit()
    return cursor.rowcount > 0


def source_ai_observation(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    source_id: str,
    original_url: str,
    archive_url: str | None,
    scan_date: str,
    commit: bool = True,
) -> bool:
    """Attach a resolvable primary source to an ``ai_presented`` observation (element 3).

    Adds one evidence link from the AI observation to a primary source
    (minutes / agenda / notice / recording) carrying a resolvable ``original_url`` — so
    the observation is no longer *unsourced* and :func:`count_unsourced_ai_presented`
    drops. The AI flag still dominates its single status (``ai_presented`` is honest —
    we do NOT launder an AI claim into ``verified``); only its *sourcing* improves.
    Idempotent on ``(statement_id, source_id)``. Raises on a non-resolvable
    ``original_url`` (no fabricated source).
    """
    if not is_resolvable_primary_url(original_url):
        raise RecordVerifyError(
            f"ai-observation source original_url {original_url!r} is not resolvable"
        )
    existing = conn.execute(
        "SELECT 1 FROM evidence_links WHERE from_node_id = ? AND from_node_type = "
        "'statement' AND to_source_id = ?",
        (statement_id, source_id),
    ).fetchone()
    if existing is not None:
        return False
    archive_status = "available" if archive_url else "not_checked"
    link_id = f"{statement_id}:ev-src-{source_id}"
    conn.execute(
        "INSERT INTO evidence_links (evidence_link_id, from_node_id, from_node_type, "
        "to_source_id, relation, original_url, final_url, archive_url, archive_status, "
        "scan_date, locator_kind) VALUES (?, ?, 'statement', ?, 'substantiates', ?, ?, ?, ?, ?, 'page')",
        (link_id, statement_id, source_id, original_url, original_url, archive_url, archive_status, scan_date),
    )
    if commit:
        conn.commit()
    return True


def verify_record(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    source_id: str,
    sha256: str,
    fetched_url: str,
    local_path: str,
    fetch_time_utc: str,
    archive_url: str,
    scan_date: str,
    commit: bool = True,
) -> dict[str, Any]:
    """Drive ``statement_id`` to ``verified`` by recording its resolved evidence.

    Orchestrates the two grounding completions over an already reviewed, source-backed,
    promoted reviewer-internal record: record the preserved raw predecessor
    (:func:`record_preserved_raw` -> ``raw_linked`` -> ``provenance grounded``) and
    confirm the near-scan archive snapshot (:func:`confirm_archive_snapshot`). After
    this the record composes to ``verified`` through the real pipeline. Deterministic
    and idempotent — returns the resolution (:func:`resolve_verification`).
    """
    record_preserved_raw(
        conn, source_id, sha256=sha256, fetched_url=fetched_url,
        local_path=local_path, fetch_time_utc=fetch_time_utc, doc_date=scan_date,
        commit=False,
    )
    confirm_archive_snapshot(
        conn, statement_id, archive_url=archive_url, scan_date=scan_date, commit=False
    )
    if commit:
        conn.commit()
    return resolve_verification(conn, statement_id)


# ---------------------------------------------------------------------------
# Resolution / inspection (pure read over the real serve + feed)
# ---------------------------------------------------------------------------


def _served_record(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any] | None:
    for record in read_api.reviewer_internal_records(conn):
        if record.get("statement_id") == statement_id:
            return record
    return None


def _card_id_for(conn: sqlite3.Connection, statement_id: str) -> str | None:
    """The Stage-3 card handle for a served statement (matches a feed item's ``cardIds``)."""
    record = _served_record(conn, statement_id)
    if record is None:
        return None
    card_type = card_feed._resolve_record_type(record)
    return card_feed.card_handle(card_type, statement_id)


def _feed_item_for(
    conn: sqlite3.Connection, statement_id: str, feed: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """The newsletter feed item projecting ``statement_id`` (matched by card handle)."""
    card_id = _card_id_for(conn, statement_id)
    if card_id is None:
        return None
    if feed is None:
        feed = nl.build_newsletter_feed(conn)
    for item in feed.get("items", []):
        if card_id in item.get("cardIds", []):
            return item
    return None


def resolve_verification(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any]:
    """The resolved verification view of ``statement_id`` over the real serve + feed.

    Pure read: projects the served record's feed item (status / recordDate /
    newsletterId / sourceTrail) plus the resolved primary ``originalUrl`` /
    ``archiveUrl`` snapshot and the source's 5.03 ``archiveAvailability``. Returns
    ``status='absent'`` when the record is not served (e.g. not yet grounded enough to
    pass the reviewer-internal gate).
    """
    item = _feed_item_for(conn, statement_id)
    if item is None:
        return {"statementId": statement_id, "status": "absent", "verified": False}

    trail = item.get("sourceTrail", [])
    original_url = next(
        (e.get("originalUrl") for e in trail if is_resolvable_primary_url(e.get("originalUrl"))),
        None,
    )
    scan_date = next((e.get("scanDate") for e in trail if e.get("scanDate")), None)
    archive_url = next((e.get("archiveUrl") for e in trail if e.get("archiveUrl")), None)
    snapshot = resolve_archive_snapshot(scan_date or item.get("recordDate"), archive_url)
    source_id = trail[0].get("sourceId") if trail else None
    inventory_entry = inventory_index(conn).get(source_id, {}) if source_id else {}

    return {
        "statementId": statement_id,
        "cardId": item.get("cardIds", [None])[0],
        "status": item.get("status"),
        "verified": item.get("status") == card_feed.STATUS_VERIFIED,
        "originalUrl": original_url,
        "originalUrlResolvable": is_resolvable_primary_url(original_url),
        "archiveUrl": archive_url,
        "archiveSnapshot": snapshot,
        "recordDate": item.get("recordDate"),
        "newsletterId": item.get("newsletterId"),
        "coveragePeriod": item.get("coveragePeriod"),
        "undated": item.get("newsletterId") == nl._UNDATED_BATCH,
        "archiveAvailability": inventory_entry.get("archiveAvailability"),
    }


def count_unsourced_ai_presented(conn: sqlite3.Connection) -> int:
    """Count served ``ai_presented`` observations with NO resolvable primary source.

    The honesty metric the issue tracks: an ``ai_presented`` item whose ``sourceTrail``
    carries no resolvable primary ``originalUrl`` is an *unsourced* AI observation.
    Sourcing one (:func:`source_ai_observation`) drops this count below the baseline.
    """
    count = 0
    for item in nl.build_newsletter_feed(conn).get("items", []):
        if item.get("labels", {}).get("claimStatus") != card_feed.STATUS_AI_PRESENTED:
            continue
        has_primary = any(
            is_resolvable_primary_url(entry.get("originalUrl"))
            for entry in item.get("sourceTrail", [])
        )
        if not has_primary:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Emit the reviewer-internal verified-record envelope (swept; single digest)
# ---------------------------------------------------------------------------


def _verification_digest(payload: dict[str, Any]) -> str:
    """A single sha256 envelope over the canonical resolved evidence (I3)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_verified_record(
    conn: sqlite3.Connection, statement_id: str, *, before_status: str | None = None
) -> dict[str, Any]:
    """Assemble the reviewer-internal verified-record envelope (I6 before/after) + sweep it.

    Carries the four evidence elements (resolvable ``originalUrl`` / near-scan
    ``archiveUrl`` snapshot / real ``recordDate`` -> real coverage week / unsourced-
    ``ai_presented`` count) plus the optional ``before`` status for the I6 before/after
    proof. Exactly one hash is exposed (the envelope ``verificationDigest``);
    ``localSourcePath`` is never present (I2). The whole body is swept by
    :func:`read_api.assert_no_raw_paths`, so any leaked path / hash / vault marker fails
    LOUDLY at the boundary (I1 backstop).
    """
    resolution = resolve_verification(conn, statement_id)
    evidence = {
        "originalUrl": resolution.get("originalUrl"),
        "originalUrlResolvable": resolution.get("originalUrlResolvable"),
        "archiveUrl": resolution.get("archiveUrl"),
        "archiveSnapshot": resolution.get("archiveSnapshot"),
        "recordDate": resolution.get("recordDate"),
        "newsletterId": resolution.get("newsletterId"),
        "coveragePeriod": resolution.get("coveragePeriod"),
        "undated": resolution.get("undated"),
        "unsourcedAiPresented": count_unsourced_ai_presented(conn),
        "aiPresentedBaseline": AI_PRESENTED_BASELINE,
    }
    body = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public" — I6
        "statementId": statement_id,
        "cardId": resolution.get("cardId"),
        "verified": resolution.get("verified"),
        "before": {"status": before_status},
        "after": {"status": resolution.get("status")},
        "evidence": evidence,
        "verificationDigest": _verification_digest(evidence),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# Load-bearing guards (non-tautological — flow through the real digest pipeline)
# ---------------------------------------------------------------------------

_HEX64 = frozenset("0123456789abcdef")

# A real Alpine ISO-week coverage bucket (vs the ``…-undated`` fallback).
_ISO_WEEK_RE = re.compile(r"^alpine-historical-\d{4}-\d{2}$")


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value.lower())
    )


def assert_record_verified(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any]:
    """RED unless ``statement_id`` reaches ``verified`` with all four evidence elements (I6).

    The genuine downstream check (I5): it does NOT read a stored flag — it assembles the
    REAL 4.05 digest (:func:`stage4_newsletter_digest_assembler.assemble_digests`), finds
    the digest item projecting this record, and asserts:

    * ``status == "verified"`` (composed by the real card layer from the grounded
      provenance + source-backed ui_status);
    * its ``sourceTrail`` carries a resolvable primary ``originalUrl`` AND a resolvable
      near-scan ``archiveUrl`` snapshot;
    * its ``newsletterId`` is a real ``alpine-historical-YYYY-WW`` ISO week — NOT the
      ``…-undated`` bucket.

    Returns the matched digest item on success. Because every assertion reads the
    assembled digest (not the resolver's own output), neutering the resolver/grounding
    makes a real assertion go RED here — a non-tautological RED-proof.
    """
    card_id = _card_id_for(conn, statement_id)
    if card_id is None:
        raise RecordVerifyError(f"statement {statement_id!r} is not served (not grounded)")

    out = digest.assemble_digests(conn)
    match: dict[str, Any] | None = None
    owning_newsletter_id: str | None = None
    for d in out.get("digests", []):
        for item in d.get("items", []):
            if card_id in item.get("cardIds", []):
                match = item
                owning_newsletter_id = d.get("newsletterId")
                break
        if match is not None:
            break
    if match is None:
        raise RecordVerifyError(f"no digest item projects card {card_id!r}")

    if match.get("status") != card_feed.STATUS_VERIFIED:
        raise RecordVerifyError(
            f"card {card_id!r} status is {match.get('status')!r}, not 'verified'"
        )
    trail = match.get("sourceTrail", [])
    if not any(is_resolvable_primary_url(e.get("originalUrl")) for e in trail):
        raise RecordVerifyError(f"card {card_id!r} has no resolvable primary originalUrl")
    near_snapshot = any(
        (snap := resolve_archive_snapshot(e.get("scanDate"), e.get("archiveUrl"))) is not None
        and snap["nearScanDate"]
        for e in trail
    )
    if not near_snapshot:
        raise RecordVerifyError(f"card {card_id!r} has no resolvable near-scan archiveUrl")
    if owning_newsletter_id == nl._UNDATED_BATCH or not _ISO_WEEK_RE.match(owning_newsletter_id or ""):
        raise RecordVerifyError(
            f"card {card_id!r} bucketed into {owning_newsletter_id!r}, not a real ISO week"
        )
    return match


def assert_unsourced_ai_presented_dropped(
    conn: sqlite3.Connection, *, baseline: int = AI_PRESENTED_BASELINE
) -> bool:
    """RED unless the unsourced-``ai_presented`` count is strictly below ``baseline`` (element 3)."""
    remaining = count_unsourced_ai_presented(conn)
    if remaining >= baseline:
        raise RecordVerifyError(
            f"unsourced ai_presented count {remaining} did not drop below baseline {baseline}"
        )
    return True


def assert_single_envelope_digest(body: dict[str, Any]) -> bool:
    """RED if any 64-hex string appears outside the top-level ``verificationDigest`` (I3)."""
    if not _is_hex64(body.get("verificationDigest")):
        raise RecordVerifyError("envelope verificationDigest is not a sha256")
    for key, value in body.items():
        if key == "verificationDigest":
            continue
        for text in read_api._iter_strings(value):
            if _is_hex64(text):
                raise RecordVerifyError(f"per-source 64-hex hash leaked under {key!r}: {text!r}")
    return True


# ---------------------------------------------------------------------------
# CLI (read-only by default — emits the verified-record envelope for a statement)
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.04 reviewer-internal Alpine record verifier/producer (GOV-488)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--statement-id", required=True, help="the statement to resolve / assert verified"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the verified-record + single-envelope-digest + ai-sourced guards",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        body = build_verified_record(conn, args.statement_id)
        if args.check:
            assert_record_verified(conn, args.statement_id)
            assert_single_envelope_digest(body)
            assert_unsourced_ai_presented_dropped(conn)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
