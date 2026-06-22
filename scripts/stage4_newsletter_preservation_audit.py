"""Stage 4.04 newsletter raw-preservation & reproducibility auditor (GOV-453) — reviewer-internal.

Implements the GOV-453 contract
(``Docs/stage4-04-newsletter-preservation-reproducibility-contract.md``) §4 verbatim: a
read-time auditor that *proves* the three newsletter-feed invariants over the EXISTING
Stage-4 reviewer-internal Alpine feed (GOV-449), emits the reviewer-internal preservation
overlay (§3), and routes it through the existing ``read_api.assert_no_raw_paths`` backstop.

This is the Stage-3.04 read-time-auditor precedent (:mod:`stage3_preservation_audit`,
GOV-367) lifted ONE layer up — from the raw file/hash layer to the *projection* layer:

* **NF-1 reproducibility** (:func:`assert_reproducible`) — the feed / readiness / validation
  artifacts are pure functions of the read surface, so two consecutive builds are
  byte-identical. A non-deterministic build goes RED.
* **NF-2 lossless provenance** (:func:`provenance_violations`) — every emitted item traces
  to one real reviewed Stage-3 record: its card handle is one the served read surface
  mandates and its ``sourceIds`` / ``sourceTrail`` source set is *exactly* that record's
  evidence source set. The ground-truth index is re-derived INDEPENDENTLY from
  :func:`read_api.reviewer_internal_records` + :mod:`stage3_card_feed` (never from the feed
  builder), so the diff is a genuine cross-check, not a tautology.
* **NF-3 zero raw mutation** (:func:`raw_mutation_violations`) — building any artifact leaves
  the reviewed raw tables (``statements`` / ``evidence_links`` / ``sources``) byte-stable; a
  write to any of them goes RED naming the table.

Extend-not-fork (contract §0): every projection decision is delegated to
:mod:`stage4_newsletter_feed` — this module re-assembles NO item, re-sorts nothing, and
re-declares none of the feed's constants. ``read_api.py`` / ``publication.py`` /
``stage4_newsletter_feed.py`` all stay byte-0-diff; their guards/constants are imported.

Reviewer-internal lane ONLY (``access: reviewer_internal``); never a public lane (public is
GOV-420 / Isaac-gated). Pure read-time function of the corpus — no crawl, no re-fetch, no
schema/migration, no mutation (NF-3 proves it). No AI; AI output is never primary evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402  (read-only: transport guard + served read surface, no mutation)
import stage3_card_feed as card_feed  # noqa: E402  (read-only: card-identity SSOT)
import stage4_newsletter_feed as nl  # noqa: E402  (the projection — described, never forked)

# The reviewed raw record tables the feed projects FROM. NF-3 proves the projection
# leaves every one byte-stable. Fixed allowlist (never user input) — safe to f-string.
_RAW_TABLES: tuple[str, ...] = ("statements", "evidence_links", "sources")


class NewsletterReproducibilityError(AssertionError):
    """Raised when a feed artifact is not byte-identical on re-projection (NF-1)."""


# ---------------------------------------------------------------------------
# Canonical serialisation helpers (one fingerprint per artifact / per table)
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    """Stable canonical JSON (sorted keys) — the byte-identity yardstick for NF-1."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


def _table_digest(conn: sqlite3.Connection, table: str) -> str:
    """Order-independent content fingerprint of a raw table (NF-3 yardstick).

    Rows are serialised then sorted, so the digest reflects table *content*, not row
    order — a pure read that touches nothing yields the same digest every time, and any
    insert/update/delete changes it.
    """
    rows = [[_jsonable(v) for v in tuple(r)] for r in conn.execute(f"SELECT * FROM {table}")]
    rows_text = sorted(json.dumps(row, ensure_ascii=False) for row in rows)
    return _digest("\n".join(rows_text))


def _raw_snapshot(conn: sqlite3.Connection) -> dict[str, str]:
    return {table: _table_digest(conn, table) for table in _RAW_TABLES}


# ---------------------------------------------------------------------------
# NF-1 — reproducibility (idempotent regeneration)
# ---------------------------------------------------------------------------


def assert_reproducible(conn: sqlite3.Connection) -> str:
    """Prove each feed artifact is byte-identical on re-projection; return the feed digest.

    Builds the item feed, readiness record, and source-link validation log twice each
    and compares canonical JSON. Raises :class:`NewsletterReproducibilityError` on any
    drift (NF-1 fail-closed). Returns the ``sha256`` of the canonical item feed — the
    opaque envelope-level reproducibility fingerprint.
    """
    builders: tuple[tuple[str, Callable[[sqlite3.Connection], Any]], ...] = (
        ("feed", nl.build_newsletter_feed),
        ("readiness", nl.build_readiness_record),
        ("validation", nl.source_link_validation),
    )
    feed_canonical = ""
    for name, build in builders:
        first = _canonical(build(conn))
        second = _canonical(build(conn))
        if first != second:
            raise NewsletterReproducibilityError(
                f"{name!r} artifact not byte-identical on re-projection (non-deterministic build)"
            )
        if name == "feed":
            feed_canonical = first
    return _digest(feed_canonical)


# ---------------------------------------------------------------------------
# NF-2 — lossless provenance (independent cross-check)
# ---------------------------------------------------------------------------


def served_provenance_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """``{card_handle: sorted(sourceIds)}`` re-derived straight from the read surface.

    Derived INDEPENDENTLY of :func:`stage4_newsletter_feed.build_newsletter_feed` — from
    :func:`read_api.reviewer_internal_records` (the served raw records) + the
    :mod:`stage3_card_feed` card-identity SSOT — so diffing the feed against it is a real
    cross-check, not a tautology. The source set is the served record's own evidence
    drawer (``to_source_id``s): the ground truth the feed must carry through losslessly.
    """
    index: dict[str, list[str]] = {}
    for record in read_api.reviewer_internal_records(conn):
        card_type = card_feed._resolve_record_type(record)
        handle = card_feed.card_handle(card_type, record["statement_id"])
        sources = sorted(
            {link.get("to_source_id") for link in record.get("evidence", []) if link.get("to_source_id")}
        )
        index[handle] = sources
    return index


def provenance_violations(
    conn: sqlite3.Connection, feed: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Return one entry per emitted item that does NOT trace losslessly to a real record.

    Two failure modes (NF-2): a ``fabricated_item`` (its card handle is not one the served
    read surface mandates), or a ``lossy_source_linkage`` (its ``sourceIds`` /
    ``sourceTrail`` source set diverges from the served record's evidence source set — a
    source invented or dropped). An empty list is the proof every item traces through.
    """
    if feed is None:
        feed = nl.build_newsletter_feed(conn)
    index = served_provenance_index(conn)
    violations: list[dict[str, Any]] = []
    for item in feed.get("items", []):
        item_id = item.get("id")
        cards = item.get("cardIds") or []
        handle = cards[0] if cards else None
        if handle not in index:
            violations.append({"item_id": item_id, "reason": "fabricated_item", "card_id": handle})
            continue
        expected = index[handle]
        item_sources = sorted(set(item.get("sourceIds", [])))
        trail_sources = sorted(
            {entry.get("sourceId") for entry in item.get("sourceTrail", []) if entry.get("sourceId")}
        )
        if item_sources != expected or trail_sources != expected:
            violations.append({
                "item_id": item_id,
                "reason": "lossy_source_linkage",
                "card_id": handle,
                "expected_sources": expected,
                "item_sources": item_sources,
                "trail_sources": trail_sources,
            })
    return violations


# ---------------------------------------------------------------------------
# NF-3 — zero raw mutation (read-only projection)
# ---------------------------------------------------------------------------


def _build_all_artifacts(conn: sqlite3.Connection) -> None:
    """Default NF-3 probe: exercise every feed artifact (all read-only)."""
    nl.build_newsletter_feed(conn)
    nl.build_readiness_record(conn)
    nl.source_link_validation(conn)


def raw_mutation_violations(
    conn: sqlite3.Connection, build: Callable[[sqlite3.Connection], Any] | None = None
) -> list[dict[str, Any]]:
    """Return one entry per raw table whose content changed across ``build`` (NF-3).

    Snapshots a content fingerprint of ``statements`` / ``evidence_links`` / ``sources``,
    runs ``build`` (default: every feed artifact), and re-snapshots. An empty list is the
    read-time proof that projecting the feed mutates no reviewed raw record.
    """
    if build is None:
        build = _build_all_artifacts
    before = _raw_snapshot(conn)
    build(conn)
    after = _raw_snapshot(conn)
    return [
        {"table": table, "reason": "raw_table_mutated"}
        for table in _RAW_TABLES
        if before[table] != after[table]
    ]


# ---------------------------------------------------------------------------
# Public API — the reviewer-internal preservation overlay (§3)
# ---------------------------------------------------------------------------


def build_preservation_overlay(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the reviewer-internal preservation overlay and transport-sweep it.

    Envelope shape (contract §3): ``{scope, access, reproducible, provenance_ok,
    raw_mutation_ok, item_count, feed_digest, violations}``. Each invariant is surfaced
    (defects reported, never silently swallowed); ``feed_digest`` is the single opaque
    envelope-level fingerprint. The whole body is swept by
    :func:`read_api.assert_no_raw_paths` — a path / ``.sha256`` / vault marker fails
    LOUDLY at the boundary.
    """
    repro_violations: list[str] = []
    try:
        feed_digest = assert_reproducible(conn)
        reproducible = True
    except NewsletterReproducibilityError as exc:
        reproducible = False
        repro_violations = [str(exc)]
        feed_digest = _digest(_canonical(nl.build_newsletter_feed(conn)))

    prov_violations = provenance_violations(conn)
    raw_violations = raw_mutation_violations(conn)
    feed = nl.build_newsletter_feed(conn)

    overlay: dict[str, Any] = {
        "scope": nl.SCOPE,                       # "alpine"
        "access": nl.ACCESS,                     # "reviewer_internal" — never public (§6)
        "reproducible": reproducible,            # NF-1
        "provenance_ok": not prov_violations,    # NF-2
        "raw_mutation_ok": not raw_violations,   # NF-3
        "item_count": len(feed.get("items", [])),
        "feed_digest": feed_digest,
        "violations": {
            "reproducibility": repro_violations,
            "provenance": prov_violations,
            "raw_mutation": raw_violations,
        },
    }
    return read_api.assert_no_raw_paths(overlay)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 4.04 reviewer-internal newsletter preservation/reproducibility audit (GOV-453)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        overlay = build_preservation_overlay(conn)
    print(json.dumps(overlay, indent=2, sort_keys=True))
    # Exit non-zero if any invariant failed (fail-closed for CI gating).
    ok = overlay["reproducible"] and overlay["provenance_ok"] and overlay["raw_mutation_ok"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
