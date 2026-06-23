"""Stage 5.03 source/data inventory contract (GOV-484) — reviewer-internal, Alpine.

Implements the GOV-484 contract
(``Docs/stage5-03-source-inventory-contract.md``): the Stage-5 data + behavior
contract that lets the crawler/tooling represent a source's **lifecycle state**
(``unchanged | changed | disappeared | replaced``) and its **archive availability
near the original scan date**, as a deterministic projection of the existing
reviewer-internal registry.

This is the Stage-5 analog of the Stage X.03 inventory slices and a *thin additive
layer* over the already-web-safe Stage-3.03 source inventory
(:func:`stage3_source_inventory.source_inventory`, GOV-364). It REUSES that
projection verbatim (every flat field is already in
``publication.WEB_SAFE_FIELD_ALLOWLIST``; raw columns are never SELECTed there) and
attaches two derived honesty envelopes AFTER projection:

* **§1 lifecycle** (:func:`derive_lifecycle_state`) — the
  ``unchanged|changed|disappeared|replaced`` state plus the ``evidence`` fields that
  justify it, derived fail-closed (most-degraded-state-wins) from the registry's
  ``source_changed`` / ``verification_status`` / ``correction_status`` /
  ``archive_status`` signal columns. Those columns are READ to derive the label and
  are **never emitted raw**; an ``evidence.*`` field echoes a value only when it is a
  member of the frozen trigger set (a poisoned free-text status falls through to
  ``None``).
* **§2 archiveAvailability** (:func:`archive_availability`) — keyed to the immutable
  original ``scan_date``: the clamped categorical ``archiveStatus``, a derived
  ``snapshotAvailability`` honesty label, and a ``nearestSnapshotRef`` reviewer-
  internal POINTER (the public Wayback web URL when present, else ``None``) — never a
  raw fetched path.

Boundary rules (contract §4, restated as inventory invariants):

* the layer **never** calls ``to_web_safe`` / mutates ``publication.py`` /
  ``read_api.py`` / ``stage3_source_inventory.py`` — it is a *separate additive
  module* that consumes them read-only (I4/I7);
* exactly one hash is exposed — the top-level envelope ``inventoryDigest`` over the
  already-web-safe sources list; **no per-source raw-content hash** (``raw_sha256``
  is never SELECTed) (I3);
* the whole assembled body is swept by :func:`read_api.assert_no_raw_paths`, so a FS
  path / ``.sha256`` / vault marker / ``file://`` that slipped a column fails LOUDLY
  at the boundary (I1);
* it runs entirely at ``access: reviewer_internal`` / ``scope: alpine`` and is
  **absent** from any public / ``published_records`` path (I6).

Pure function of the registry + Stage-3.03 read surface: same DB -> byte-identical
inventory (idempotent re-projection). No mutation, no AI, no network.
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
import read_api  # noqa: E402  (consumed read-only: transport guard + url helpers)
import stage3_source_inventory as base  # noqa: E402  (consumed read-only: §1/§2 projection)

SCOPE = "alpine"  # envelope scope (fixed; broader = planned — contract §0)
ACCESS = "reviewer_internal"  # never "public" — contract §0/I6


class SourceInventoryContractError(AssertionError):
    """Raised when an emitted inventory record violates a GOV-484 contract invariant."""


# ---------------------------------------------------------------------------
# §1 — source lifecycle state (frozen SSOT) + fail-closed derivation
# ---------------------------------------------------------------------------

LIFECYCLE_UNCHANGED = "unchanged"
LIFECYCLE_CHANGED = "changed"
LIFECYCLE_DISAPPEARED = "disappeared"
LIFECYCLE_REPLACED = "replaced"

# The only four permitted lifecycle states. A `frozenset` so any future value is a
# conscious, reviewed change — never an accidental drift.
SOURCE_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {LIFECYCLE_UNCHANGED, LIFECYCLE_CHANGED, LIFECYCLE_DISAPPEARED, LIFECYCLE_REPLACED}
)

# Frozen trigger vocabularies (contract §1.2). A signal column value only moves the
# lifecycle state — and is only echoed into ``evidence.*`` — when it is a member of
# the matching frozen set. An arbitrary/poisoned free-text status therefore never
# changes the state and is never echoed (fail closed).
_DISAPPEARED_VERIFICATION: frozenset[str] = frozenset({"source_missing"})
_DISAPPEARED_ARCHIVE: frozenset[str] = frozenset({"unavailable"})
_REPLACED_CORRECTION: frozenset[str] = frozenset({"replaced", "superseded"})
_CHANGED_VERIFICATION: frozenset[str] = frozenset({"source_changed"})

# The categorical/flag columns READ to derive the lifecycle envelope. DELIBERATELY
# none is a locator/path/hash/PII column; none is emitted raw — only derived labels
# and frozen-vocab echoes reach the body.
_LIFECYCLE_SIGNAL_COLUMNS: tuple[str, ...] = (
    "source_id",
    "source_changed",
    "verification_status",
    "correction_status",
    "archive_status",
)


def derive_lifecycle_state(signals: dict[str, Any]) -> dict[str, Any]:
    """Derive the ``{state, evidence}`` lifecycle envelope for one source (§1.2).

    Fail-closed, most-degraded-state-wins precedence so the inventory never claims
    ``unchanged`` while a degradation signal is present:

    1. ``disappeared`` — ``verification_status == 'source_missing'`` OR
       ``archive_status == 'unavailable'``;
    2. ``replaced`` — ``correction_status ∈ {'replaced','superseded'}``;
    3. ``changed`` — ``source_changed`` truthy OR
       ``verification_status == 'source_changed'``;
    4. ``unchanged`` — the default ONLY when no degradation signal exists.

    Each ``evidence.*`` field echoes a value only when it is a member of the frozen
    trigger set; otherwise ``None`` (a poisoned free-text status is never echoed).
    Pure function of the signal dict — same input -> same envelope.
    """
    changed_flag = bool(signals.get("source_changed"))
    verification = signals.get("verification_status")
    correction = signals.get("correction_status")
    archive_status = signals.get("archive_status")

    disappeared = (
        verification in _DISAPPEARED_VERIFICATION
        or archive_status in _DISAPPEARED_ARCHIVE
    )
    replaced = correction in _REPLACED_CORRECTION
    changed = changed_flag or verification in _CHANGED_VERIFICATION

    if disappeared:
        state = LIFECYCLE_DISAPPEARED
    elif replaced:
        state = LIFECYCLE_REPLACED
    elif changed:
        state = LIFECYCLE_CHANGED
    else:
        state = LIFECYCLE_UNCHANGED

    if verification in _DISAPPEARED_VERIFICATION:
        disappearance_signal = verification
    elif archive_status in _DISAPPEARED_ARCHIVE:
        disappearance_signal = archive_status
    else:
        disappearance_signal = None

    return {
        "state": state,
        "evidence": {
            "sourceChangedFlag": changed_flag,
            "changeSignal": (
                verification if verification in _CHANGED_VERIFICATION else None
            ),
            "disappearanceSignal": disappearance_signal,
            "replacementSignal": (
                correction if correction in _REPLACED_CORRECTION else None
            ),
        },
    }


# ---------------------------------------------------------------------------
# §2 — archive availability keyed to the immutable original scan date
# ---------------------------------------------------------------------------

SNAPSHOT_AVAILABLE = "available_near_scan"
SNAPSHOT_NOT_AVAILABLE = "not_available"
SNAPSHOT_NOT_CHECKED = "not_checked"

# Frozen 3-value SSOT honesty label for archive availability (contract §2.1).
ARCHIVE_AVAILABILITY_STATES: frozenset[str] = frozenset(
    {SNAPSHOT_AVAILABLE, SNAPSHOT_NOT_AVAILABLE, SNAPSHOT_NOT_CHECKED}
)

# Categorical ``archive_status`` vocabulary (migration 0003 default ``not_checked``).
# An unknown value fails closed to ``not_checked`` so drift is never surfaced as a
# coverage/availability claim — and the emitted ``archiveStatus`` is always a clean
# categorical enum, never a smuggled free-text path.
ARCHIVE_STATUS_NOT_CHECKED = "not_checked"
ARCHIVE_STATUS_AVAILABLE = "available"
ARCHIVE_STATUS_UNAVAILABLE = "unavailable"
_ARCHIVE_STATUS_VOCAB: frozenset[str] = frozenset(
    {ARCHIVE_STATUS_NOT_CHECKED, ARCHIVE_STATUS_AVAILABLE, ARCHIVE_STATUS_UNAVAILABLE}
)


def archive_availability(
    scan_date: Any, archive_status: Any, archive_url: Any
) -> dict[str, Any]:
    """The §2 archive-availability envelope, keyed to the immutable ``scan_date``.

    ``nearestSnapshotRef`` is a reviewer-internal POINTER — the public Wayback web
    URL when ``archive_url`` is a genuine ``http(s)://`` URL, else ``None`` (a
    ``file://`` / vault URI was already dropped by the Stage-3.03 projection, and the
    final transport sweep is the loud backstop). ``archiveStatus`` is clamped to the
    frozen vocab (unknown -> ``not_checked``). ``snapshotAvailability`` is the derived
    honesty label. Pure function of its three inputs.
    """
    status = (
        archive_status
        if archive_status in _ARCHIVE_STATUS_VOCAB
        else ARCHIVE_STATUS_NOT_CHECKED
    )
    ref = (
        archive_url
        if isinstance(archive_url, str) and read_api._is_web_url(archive_url)
        else None
    )
    if status == ARCHIVE_STATUS_AVAILABLE and ref is not None:
        availability = SNAPSHOT_AVAILABLE
    elif status == ARCHIVE_STATUS_UNAVAILABLE:
        availability = SNAPSHOT_NOT_AVAILABLE
    else:
        availability = SNAPSHOT_NOT_CHECKED
    return {
        "scanDate": scan_date if isinstance(scan_date, str) and scan_date else None,
        "archiveStatus": status,
        "snapshotAvailability": availability,
        "nearestSnapshotRef": ref,
    }


# ---------------------------------------------------------------------------
# §1/§2 — the per-source signal read (categorical/flag only, never a locator)
# ---------------------------------------------------------------------------


def _lifecycle_signals(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{source_id: {signal columns}}`` — the categorical/flag inputs to §1.

    Reads ONLY :data:`_LIFECYCLE_SIGNAL_COLUMNS` (no locator, path, hash, or PII
    column). The values are consumed to DERIVE the lifecycle label and are never
    emitted raw.
    """
    signals: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        f"SELECT {', '.join(_LIFECYCLE_SIGNAL_COLUMNS)} FROM sources"
    ):
        record = dict(row)
        signals[record["source_id"]] = {
            "source_changed": record.get("source_changed"),
            "verification_status": record.get("verification_status"),
            "correction_status": record.get("correction_status"),
            "archive_status": record.get("archive_status"),
        }
    return signals


# ---------------------------------------------------------------------------
# §3 — the single envelope digest (I3)
# ---------------------------------------------------------------------------


def _envelope_digest(sources: list[dict[str, Any]]) -> str:
    """A single sha256 over the canonical, already-web-safe sources list (I3).

    Computed AFTER projection over the web-safe entries, so it cannot encode a raw
    path. This is the ONLY hash exposed by the whole body — there is no per-source
    raw-content hash (``raw_sha256`` is never SELECTed).
    """
    payload = json.dumps(sources, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# §1/§2/§3 — the per-source entry + the inventory envelope
# ---------------------------------------------------------------------------


def source_inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One Stage-5 inventory entry per registered source, deterministic order (§3).

    Each entry is the Stage-3.03 web-safe projection (flat allowlisted fields +
    ``coverage``) with the derived ``lifecycle`` (§1) and ``archiveAvailability``
    (§2) envelopes attached AFTER projection. The base entries are copied, never
    mutated in place, so :mod:`stage3_source_inventory` stays a pure read. Order is
    ``(source_class, source_id)`` (inherited from the base projection) so the same DB
    yields a byte-identical list.
    """
    signals = _lifecycle_signals(conn)
    entries: list[dict[str, Any]] = []
    for base_entry in base.source_inventory(conn):
        entry = dict(base_entry)  # copy: never mutate the Stage-3.03 projection
        source_signals = signals.get(entry["source_id"], {})
        entry["lifecycle"] = derive_lifecycle_state(source_signals)
        entry["archiveAvailability"] = archive_availability(
            entry.get("scan_date"),
            source_signals.get("archive_status"),
            entry.get("archive_url"),
        )
        entries.append(entry)
    return entries


def build_inventory(conn: sqlite3.Connection) -> dict[str, Any]:
    """Assemble the ``{scope, access, sources[], inventoryDigest}`` body + sweep it.

    A 1:1 projection of the registry — every registered source is emitted, including
    a disappeared / seed-only source (never hidden). Exactly one hash is exposed (the
    envelope ``inventoryDigest``). The whole body is swept by
    :func:`read_api.assert_no_raw_paths`, so a FS path / ``.sha256`` / vault marker /
    ``file://`` that slipped a column fails LOUDLY at the boundary (I1 backstop).
    """
    sources = source_inventory(conn)
    body: dict[str, Any] = {
        "scope": SCOPE,
        "access": ACCESS,  # never "public" — I6
        "sources": sources,
        "inventoryDigest": _envelope_digest(sources),
    }
    return read_api.assert_no_raw_paths(body)


# ---------------------------------------------------------------------------
# §5 — contract guards (load-bearing, non-tautological checks)
# ---------------------------------------------------------------------------

_HEX64 = frozenset("0123456789abcdef")


def assert_lifecycle_states_valid(body: dict[str, Any]) -> bool:
    """RED if any source's ``lifecycle.state`` is outside the frozen SSOT (R-1).

    A real cross-check on the EMITTED body (not a recompute) — a build that emits an
    out-of-vocab state (e.g. a neutered derivation that returns a typo) goes RED.
    """
    for entry in body.get("sources", []):
        state = entry.get("lifecycle", {}).get("state")
        if state not in SOURCE_LIFECYCLE_STATES:
            raise SourceInventoryContractError(
                f"source {entry.get('source_id')!r} lifecycle state {state!r} "
                "outside the frozen SOURCE_LIFECYCLE_STATES"
            )
        availability = entry.get("archiveAvailability", {}).get("snapshotAvailability")
        if availability not in ARCHIVE_AVAILABILITY_STATES:
            raise SourceInventoryContractError(
                f"source {entry.get('source_id')!r} snapshotAvailability "
                f"{availability!r} outside the frozen ARCHIVE_AVAILABILITY_STATES"
            )
    return True


def _is_hex64(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in _HEX64 for ch in value.lower())
    )


def assert_single_envelope_digest(body: dict[str, Any]) -> bool:
    """RED if any 64-hex string appears outside the top-level ``inventoryDigest`` (R-5/I3).

    Walks every string in each source entry and fails if a per-source 64-hex content
    hash slipped in (e.g. a future edit that SELECTed ``raw_sha256``). The single
    envelope digest is the ONLY permitted hash.
    """
    if not _is_hex64(body.get("inventoryDigest")):
        raise SourceInventoryContractError("envelope inventoryDigest is not a sha256")
    for entry in body.get("sources", []):
        for text in read_api._iter_strings(entry):
            if _is_hex64(text):
                raise SourceInventoryContractError(
                    f"per-source 64-hex hash leaked in {entry.get('source_id')!r}: "
                    f"{text!r}"
                )
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 5.03 reviewer-internal Alpine source/data inventory (GOV-484)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the lifecycle-state + single-envelope-digest contract guards",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        body = build_inventory(conn)
        if args.check:
            assert_lifecycle_states_valid(body)
            assert_single_envelope_digest(body)
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
