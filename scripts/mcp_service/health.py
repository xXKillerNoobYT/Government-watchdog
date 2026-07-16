"""Per-provider health tracking (PLAN-2026-AI §3.5, D7 fail-closed).

Append-only outcome log: :func:`record` writes one ``mcp_provider_health`` row
per adapter call (the health row half of the D2 "one audit + one health per
attempt" invariant). :func:`is_degraded` reads the provider's most recent
``threshold`` rows and reports degraded only when *all* of them are ``error`` —
so a single blip does not sideline a provider, but a genuinely down local
backend is skipped by the router. Degradation never triggers a fallback to a
non-local provider; the router simply refuses (D7), consistent with the
zero-spend, local-only posture.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

DEFAULT_DEGRADE_THRESHOLD = 3


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def record(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    outcome: str,
    latency_ms: int | None = None,
    error_code: str | None = None,
) -> str:
    """Append one health row; return its id. ``outcome`` is ``'ok'`` or ``'error'``."""
    if outcome not in ("ok", "error"):
        raise ValueError(f"outcome must be 'ok'|'error', got {outcome!r}")
    health_id = f"ph-{uuid.uuid4()}"
    conn.execute(
        "INSERT INTO mcp_provider_health "
        "(health_id, provider_id, outcome, latency_ms, error_code, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (health_id, provider_id, outcome,
         None if latency_ms is None else int(latency_ms), error_code, _utcnow()),
    )
    conn.commit()
    return health_id


def is_degraded(
    conn: sqlite3.Connection,
    provider_id: str,
    *,
    threshold: int = DEFAULT_DEGRADE_THRESHOLD,
) -> bool:
    """True when the provider's last ``threshold`` calls were all errors.

    Fewer than ``threshold`` total calls is never degraded (insufficient
    evidence): a brand-new or lightly-used provider is given the benefit of the
    doubt, and one success anywhere in the window clears the streak.
    """
    if threshold <= 0:
        return False
    # rowid is SQLite's monotonic insertion order — reliable recency (health_id is
    # a random uuid, so it must NOT be used for ordering).
    rows = conn.execute(
        "SELECT outcome FROM mcp_provider_health WHERE provider_id = ? "
        "ORDER BY rowid DESC LIMIT ?",
        (provider_id, int(threshold)),
    ).fetchall()
    if len(rows) < threshold:
        return False
    return all(r["outcome"] == "error" for r in rows)
