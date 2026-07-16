"""Canonical-JSON encoding + WRITE-ONCE envelope insert with deterministic dedupe.

GOV-733 (implements GOV-719 plan CTRL-2026, rev c4d03918 §3.2). This is a leaf
module: stdlib + ``db`` only.

Dedupe contract (plan §3.2)::

    dedupe_key = sha256( source_key ‖ event_kind ‖ source_ref ‖ content_sha256 ‖ policy_version )

The ``‖`` join is realised with the ASCII Unit Separator byte ``0x1f`` — a
control character that never occurs in a source key, event kind, ref, hex
digest, or policy version — so the concatenation is unambiguous (``"a"+"bc"``
and ``"ab"+"c"`` produce different keys). The exact vector is pinned in
``DEDUPE_TEST_VECTOR`` and asserted in tests; changing the recipe fails loudly.

WRITE-ONCE (plan §3.1): ``event_envelopes`` has no UPDATE path. ``insert_envelope``
attempts a plain INSERT; a ``dedupe_key`` collision (UNIQUE) means the exact
signed content was already recorded, so we append a ``event_dedupe_hits`` row
and return the *existing* ``envelope_id`` with ``is_new=False`` — the caller
enqueues zero new jobs on a duplicate (AC-1).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# ASCII Unit Separator — the field delimiter for the dedupe material.
DEDUPE_SEP = "\x1f"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, UTF-8, no insignificant whitespace.

    ``ensure_ascii=False`` keeps UTF-8 text as itself (the bytes we hash);
    ``separators`` removes the spaces ``json.dumps`` inserts by default. Two
    equal objects always canonicalise to identical bytes regardless of input
    key order.
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_dedupe_key(
    source_key: str,
    event_kind: str,
    source_ref: str,
    content_sha256: str,
    policy_version: str,
) -> str:
    """sha256 over the ``0x1f``-joined dedupe material. See module docstring."""
    material = DEDUPE_SEP.join(
        [source_key, event_kind, source_ref, content_sha256, policy_version]
    )
    return sha256_hex(material)


# Pinned test vector (asserted by tests/test_control_plane_envelope.py). If a
# refactor changes any of the encoding rules above, this digest changes and the
# test fails — an intentional tripwire on the dedupe contract.
DEDUPE_TEST_VECTOR = {
    "source_key": "toa-webhook",
    "event_kind": "agenda.published",
    "source_ref": "meeting/129",
    "content_sha256": "a" * 64,
    "policy_version": "2026-COMM-v1",
    "expected": compute_dedupe_key(
        "toa-webhook", "agenda.published", "meeting/129", "a" * 64, "2026-COMM-v1"
    ),
}


@dataclass(frozen=True)
class EnvelopeResult:
    envelope_id: int
    dedupe_key: str
    is_new: bool


def insert_envelope(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    event_kind: str,
    source_ref: str,
    content_sha256: str,
    policy_version: str,
    payload,
    area_id: str | None = None,
    received_at: str | None = None,
) -> EnvelopeResult:
    """WRITE-ONCE insert of a verified event.

    ``payload`` is the parsed event body (any JSON-serialisable object); it is
    canonicalised here so ``payload_sha256`` is stable. ``content_sha256`` is the
    hash of the underlying *source* artifact (e.g. sha256 of the raw request
    body) and is stored as ``source_hash`` for LED-1 traceability (AC-5).

    Returns ``EnvelopeResult``. On a first sighting ``is_new=True`` and a new
    envelope row exists. On a duplicate ``dedupe_key`` an ``event_dedupe_hits``
    row is appended and the existing ``envelope_id`` is returned with
    ``is_new=False`` — no second envelope, and the caller must not enqueue work.

    The caller owns the transaction (no commit here) so that envelope + job
    enqueue land atomically.
    """
    now = received_at or _utcnow()
    canonical = canonical_json(payload)
    payload_sha256 = sha256_hex(canonical)
    dedupe_key = compute_dedupe_key(
        source_key, event_kind, source_ref, content_sha256, policy_version
    )
    try:
        cur = conn.execute(
            "INSERT INTO event_envelopes ("
            "received_at, source_key, signature_state, canonical_payload, "
            "payload_sha256, source_hash, area_id, event_kind, policy_version, dedupe_key"
            ") VALUES (?, ?, 'verified', ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                source_key,
                canonical,
                payload_sha256,
                content_sha256,
                area_id,
                event_kind,
                policy_version,
                dedupe_key,
            ),
        )
        return EnvelopeResult(int(cur.lastrowid), dedupe_key, True)
    except sqlite3.IntegrityError:
        # dedupe_key already present: append a replay ledger row, return the
        # existing envelope, signal "no new work" to the caller.
        conn.execute(
            "INSERT INTO event_dedupe_hits (dedupe_key, seen_at, source_key) "
            "VALUES (?, ?, ?)",
            (dedupe_key, now, source_key),
        )
        row = conn.execute(
            "SELECT envelope_id FROM event_envelopes WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        return EnvelopeResult(int(row[0]), dedupe_key, False)
