"""Stage 3.03 source/data inventory (GOV-364) — reviewer-internal coverage projection.

The reviewer-internal Alpine *source inventory*: a deterministic JSON projection
of the `sources` registry (GOV-74) + the real data already ingested behind each
source, built ENTIRELY on top of the already-web-safe Stage 2 read surface. It
implements the GOV-362 contract (``Docs/stage3-03-source-inventory-contract.md``):

* **§1 field set** — one row per registered source, every flat field already in
  ``publication.WEB_SAFE_FIELD_ALLOWLIST`` (:data:`SOURCE_INVENTORY_FIELDS`). The
  reviewer-internal columns (``raw_local_path`` / ``raw_sha256`` / ``notes`` /
  ``owner_agent`` / ``local_note_path`` / ``robots_policy`` / ``registered_utc`` /
  ``raw_preservation_status``) are **never SELECTed** — they can never reach a body.
* **§2 coverage** — a derived, fail-closed ``coverage`` envelope per source
  (counts + a 3-value :data:`SOURCE_COVERAGE_STATES` honesty label), attached
  AFTER projection so no raw column is ever added to the web-safe surface
  (:func:`_coverage`).
* **§3 envelope** — the ``{scope, access, sources[]}`` shape, deterministic order
  ``(source_class, source_id)``, transport-swept (:func:`build_inventory`).

Boundary rules (GOV-362 §4, restated as inventory invariants):

* the inventory layer **never** calls ``to_web_safe`` / mutates ``publication.py``
  / ``read_api.py`` (this is a *separate additive module* — GOV-347 precedent);
  it SELECTs only the §1 allowlisted columns directly (INV-2/3);
* URL-typed fields (``url`` / ``original_url`` / ``archive_url``) pass through
  :func:`read_api._strip_non_web_urls`, so a ``file://`` vault URI is dropped while
  source identity still rides ``source_id`` (INV-4);
* the whole assembled feed is swept by :func:`read_api.assert_no_raw_paths`, so a
  FS path / ``.sha256`` / vault marker fails LOUDLY at the boundary (INV-5 / GOV-34);
* it runs entirely at ``access: reviewer_internal`` and is **absent** from any
  public / ``published_records`` path (INV-1) — surfacing seed/registry rows
  publicly would imply coverage that does not exist.

Fail-closed / honesty posture (GOV-362 §2.2 / INV-7):

* coverage is computed at read time from existing rows only — no crawl, no
  mutation; a seed-only source reads ``0/0/0`` with the most conservative
  ``state: "seeded"`` and is STILL emitted (the gap is shown, never hidden/padded);
* ``reviewable_statements`` **reuses the Stage 2 reviewer-internal lane verbatim**
  (:func:`read_api.reviewer_internal_records`) — a statement counts only if it
  already passes every gate there; eligibility is never re-derived here.

Pure function of the registry + read surface: same DB -> byte-identical inventory
(idempotent re-projection). No mutation, no AI, no network.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import publication as pub  # noqa: E402  (consumed read-only: SSOT allowlist, no mutation)
import read_api  # noqa: E402  (consumed read-only: lane + transport guards, no mutation)

JURISDICTION = "alpine"  # envelope scope (fixed; broader = planned — GOV-362 §0)

# ---------------------------------------------------------------------------
# §1 — the inventory field set (frozen, allowlist-subset by construction)
# ---------------------------------------------------------------------------

# The EXACT registry columns the inventory SELECTs — one row per registered
# source. ORDER MATTERS for the SELECT; the frozenset below is the membership
# SSOT the no-leak test asserts ⊆ WEB_SAFE_FIELD_ALLOWLIST. The reviewer-internal
# columns (raw_local_path / raw_sha256 / local_note_path / notes / owner_agent /
# robots_policy / registered_utc / raw_preservation_status) are DELIBERATELY
# absent — never SELECTed, so they can never reach a projected body (INV-3),
# exactly as completeness_gap_cards omits its internal cols.
_INVENTORY_COLUMNS: tuple[str, ...] = (
    "source_id",               # stable slug handle (registry natural key, GOV-74)
    "name",                    # human label
    "source_class",            # municipal_primary / county_relevant / …
    "source_authority_level",  # primary / secondary
    "jurisdiction",            # Alpine / Lincoln County (Alpine-relevant) / …
    "source_type",             # website / legal_code / video_channel
    "scan_date",               # as-of #1 — immutable first-scan date
    "last_validated_utc",      # as-of #2 — latest validation
    "archive_status",          # not_checked / available / … (Wayback leg)
    "url",                     # public locators only (stripped if non-web, INV-4)
    "original_url",
    "archive_url",
)

# Frozen membership set (like GAP_CARD_FIELDS / PROVENANCE_STATUS_VALUES): every
# projected flat field must be in here, and every member must be in the web-safe
# allowlist (asserted at test time, T-1). A future field add is a conscious,
# reviewed change — never an accidental column leak.
SOURCE_INVENTORY_FIELDS: frozenset[str] = frozenset(_INVENTORY_COLUMNS)

# Fail-closed at import (defense in depth over the T-1 test): the inventory field
# set MUST be a subset of the publication SSOT allowlist. If a future edit adds a
# column to _INVENTORY_COLUMNS that is not web-safe, the module refuses to import
# rather than silently projecting an unsafe field. publication is consumed
# read-only — this never mutates the allowlist.
_UNSAFE = SOURCE_INVENTORY_FIELDS - pub.WEB_SAFE_FIELD_ALLOWLIST
if _UNSAFE:  # NOT `assert` — see GOV-1687: `python -O` deletes assert entirely,
    # so an assert cannot "refuse to import". Measured: under -O the leak survives
    # and the process exits 0.
    raise RuntimeError(
        f"source inventory projects a non-web-safe field: {sorted(_UNSAFE)}")
del _UNSAFE

# ---------------------------------------------------------------------------
# §2 — the derived coverage metric (fail-closed, read-time)
# ---------------------------------------------------------------------------

# Frozen 3-value SSOT (GOV-362 §2.2). The default for an empty/seed source is the
# most conservative "seeded" — coverage is NEVER optimistically overstated.
COVERAGE_SEEDED = "seeded"        # registered, no artifacts behind it yet
COVERAGE_INGESTED = "ingested"    # ≥1 document/transcript, but no reviewable statement
COVERAGE_REVIEWABLE = "reviewable"  # ≥1 statement served by the reviewer-internal lane
SOURCE_COVERAGE_STATES: frozenset[str] = frozenset(
    {COVERAGE_SEEDED, COVERAGE_INGESTED, COVERAGE_REVIEWABLE}
)

# The exact coverage envelope key set (so the no-leak test can assert subset).
SOURCE_COVERAGE_KEYS: frozenset[str] = frozenset(
    {"state", "documents_total", "transcripts_total", "reviewable_statements"}
)


def _artifact_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    """``{source_id: COUNT(*)}`` over ``documents`` / ``transcripts`` (read-only).

    Aggregate integers only — no path, no PII, no raw locator. A source with no
    artifacts is simply absent from the map (read 0 via ``.get``), so a seed-only
    source honestly reads 0 rather than being padded.
    """
    return {
        row[0]: row[1]
        for row in conn.execute(
            f"SELECT source_id, COUNT(*) FROM {table} "  # noqa: S608 - table is a literal
            f"WHERE source_id IS NOT NULL GROUP BY source_id"
        )
    }


def _record_source_ids(conn: sqlite3.Connection, record: dict[str, Any]) -> set[str]:
    """The set of source ids a served reviewer-internal record traces to (GOV-362 §2.1).

    Two trace paths, exactly as the contract pins:

    * the evidence-drawer ``to_source_id`` of each already-web-safe served evidence
      link (rides straight off the reviewer-internal projection — no new query);
    * the ``segment_id -> transcript_segments -> transcripts.source_id`` join (the
      record's segment_id is reviewer-internal/stripped from the served body, so it
      is re-read read-only from ``statements`` here; only the safe ``source_id``
      slug is selected).

    Reuses the served record verbatim — eligibility is NOT re-derived (the record
    is in the list only because it already passed every reviewer-internal gate).
    """
    sources: set[str] = set()
    for link in record.get("evidence", []):
        sid = link.get("to_source_id")
        if isinstance(sid, str) and sid:
            sources.add(sid)
    statement_id = record.get("statement_id")
    if statement_id:
        row = conn.execute(
            "SELECT segment_id FROM statements WHERE statement_id = ?", (statement_id,)
        ).fetchone()
        segment_id = row["segment_id"] if row is not None else None
        if segment_id:
            srow = conn.execute(
                "SELECT t.source_id AS source_id FROM transcript_segments ts "
                "JOIN transcripts t ON t.id = ts.transcript_id WHERE ts.segment_id = ?",
                (segment_id,),
            ).fetchone()
            if srow is not None and srow["source_id"]:
                sources.add(srow["source_id"])
    return sources


def _reviewable_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """``{source_id: # reviewer-internal served statements tracing to it}`` (GOV-362 §2.1).

    Iterates :func:`read_api.reviewer_internal_records` (the Stage 2 lane, verbatim)
    and tallies each served statement against every source it traces to. A statement
    that traces to two sources counts toward both (it IS reviewable for both).
    """
    counts: dict[str, int] = {}
    for record in read_api.reviewer_internal_records(conn):
        for sid in _record_source_ids(conn, record):
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def _coverage(
    source_id: str,
    documents: dict[str, int],
    transcripts: dict[str, int],
    reviewable: dict[str, int],
) -> dict[str, Any]:
    """The derived, fail-closed coverage envelope for one source (GOV-362 §2.2).

    ``state`` collapses to the most conservative value its counts support and is
    always a member of the frozen :data:`SOURCE_COVERAGE_STATES`. It is a derived
    honesty label — it never reads or surfaces the raw ``raw_preservation_status``
    column.
    """
    documents_total = documents.get(source_id, 0)
    transcripts_total = transcripts.get(source_id, 0)
    reviewable_statements = reviewable.get(source_id, 0)
    if reviewable_statements > 0:
        state = COVERAGE_REVIEWABLE
    elif (documents_total + transcripts_total) > 0:
        state = COVERAGE_INGESTED
    else:
        state = COVERAGE_SEEDED
    return {
        "state": state,
        "documents_total": documents_total,
        "transcripts_total": transcripts_total,
        "reviewable_statements": reviewable_statements,
    }


# ---------------------------------------------------------------------------
# §1/§3 — the per-source entry + the inventory list
# ---------------------------------------------------------------------------


def source_inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One inventory entry per registered source, deterministic order (GOV-362 §3).

    Each entry's flat fields are a subset of :data:`SOURCE_INVENTORY_FIELDS` (a None
    column is omitted, never emitted as ``null``), URL fields are stripped to public
    web URLs only (:func:`read_api._strip_non_web_urls`, INV-4), and a derived
    ``coverage`` envelope (§2) is attached AFTER projection. Order is
    ``(source_class, source_id)`` so the same DB yields a byte-identical list.
    """
    documents = _artifact_counts(conn, "documents")
    transcripts = _artifact_counts(conn, "transcripts")
    reviewable = _reviewable_counts(conn)

    entries: list[dict[str, Any]] = []
    for row in conn.execute(
        f"SELECT {', '.join(_INVENTORY_COLUMNS)} FROM sources "
        f"ORDER BY source_class, source_id"
    ):
        record = dict(row)
        # Omit absent columns (never an invented value / null noise); the result is
        # a subset of the allowlisted field set by construction.
        entry = {k: v for k, v in record.items() if v is not None and v != ""}
        # INV-4: drop a non-web (e.g. file://) url/original_url/archive_url; the
        # source identity still rides via source_id.
        entry = read_api._strip_non_web_urls(entry)
        entry["coverage"] = _coverage(record["source_id"], documents, transcripts, reviewable)
        entries.append(entry)
    return entries


def build_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the ``{scope, access, sources[]}`` inventory and transport-sweep it.

    A 1:1 projection of the registry — every registered source is emitted, including
    a seed-only ``0/0/0`` source (INV-7, never hidden). The whole body is then swept
    by :func:`read_api.assert_no_raw_paths` (INV-5 / GOV-34 backstop), so a FS path /
    ``.sha256`` / vault marker that slipped past the column-omission fails LOUDLY at
    the boundary, independent of INV-2/3.
    """
    inventory: dict[str, Any] = {
        "scope": JURISDICTION,
        "access": "reviewer_internal",  # never "public" — INV-1
        "sources": source_inventory(conn),
    }
    return read_api.assert_no_raw_paths(inventory)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3.03 reviewer-internal Alpine source/data inventory (GOV-364)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        inventory = build_inventory(conn)
    print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
