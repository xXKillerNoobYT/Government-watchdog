"""ProviderAdapter protocol + registry helpers (CONTRACT-2026-MCP §3.5, PORT-3/BUD-5).

The boundary between domain code and *any* generation backend. Domain code
depends only on the :class:`ProviderAdapter` structural protocol and the two
typed dataclasses; it never imports a provider SDK. Registering a provider
writes a ``mcp_provider_registry`` row with ``budget_cap_units`` defaulting to 0
— it is structurally un-callable until an owner sets a budget (BUD-5). This leg
wires the zero-default and proves swappability with a fake adapter; enforcement
of the cap at call time is GOV-718.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class GenerationRequest:
    """Minimized-context generation request. ``minimized_context_parts`` are the
    already-allowlisted evidence fragments — no raw rows, no paths."""

    model: str
    minimized_context_parts: list[str]
    max_output_units: int = 0
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationResult:
    text: str
    input_units: int
    output_units: int
    latency_ms: int
    provider_meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ProviderAdapter(Protocol):
    """Structural contract every adapter satisfies. Swap = new class, no core change."""

    provider_id: str
    kind: str

    def capabilities(self) -> dict[str, Any]:
        ...

    def generate(self, request: GenerationRequest) -> GenerationResult:
        ...


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def register_provider(
    conn: sqlite3.Connection,
    *,
    provider_id: str,
    kind: str,
    budget_cap_units: int = 0,
) -> None:
    """Register a provider. ``enabled`` and ``budget_cap_units`` both start at 0:
    a fresh provider cannot be called until an owner budget is set (BUD-5)."""
    conn.execute(
        "INSERT OR IGNORE INTO mcp_provider_registry "
        "(provider_id, kind, enabled, budget_cap_units, created_utc) "
        "VALUES (?, ?, 0, ?, ?)",
        (provider_id, kind, int(budget_cap_units), _utcnow()),
    )
    conn.commit()


def is_callable(conn: sqlite3.Connection, provider_id: str) -> bool:
    """A provider is callable only if enabled AND its budget cap is > 0 (BUD-5)."""
    row = conn.execute(
        "SELECT enabled, budget_cap_units FROM mcp_provider_registry WHERE provider_id = ?",
        (provider_id,),
    ).fetchone()
    if row is None:
        return False
    return bool(row["enabled"]) and int(row["budget_cap_units"]) > 0
