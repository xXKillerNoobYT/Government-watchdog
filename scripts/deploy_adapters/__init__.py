"""Declared deployment-adapter boundary (DEPLOY-2026, GOV-722 leg 2).

The single home for provider-specific / backend-specific persistence code. Every
civic-domain module imports *only* the adapter interface (:mod:`.base`) — never a
provider SDK — so a migration from the local SQLite backend to a managed
PostgreSQL backend is a config change, not a code change (PORT-1/PORT-3).

Design (GOV-722 plan §3 D1/D5):

* :mod:`.base` — the transport-agnostic contract: the retention-class export
  spec (§5 b/c/d column allowlists), canonical serialization + sha256 hashing,
  the :class:`~.base.DatabaseAdapter` interface, the stdlib SQLite adapter, the
  leak scanner, the synthetic-only guard, and the frozen-gate access-decision
  probe.
* :mod:`.postgres_adapter` — the ONE module allowed to carry backend-specific
  code. It drives ``psql`` over stdlib :mod:`subprocess` (no ``psycopg`` import),
  so the runtime stays stdlib-only while proving the managed-DB round-trip.

The lock-in lint (``tests/test_deploy_lockin_lint.py``) excludes this package
from its provider-SDK ban precisely because this is where such code is *allowed*
to live; everywhere else it is forbidden.
"""

from __future__ import annotations

from .base import (
    EXPORT_SPEC,
    EXCLUDED_TABLES,
    CanonicalExport,
    DatabaseAdapter,
    SqliteAdapter,
    access_decisions,
    assert_synthetic_path,
    scan_export_for_leaks,
)

__all__ = [
    "EXPORT_SPEC",
    "EXCLUDED_TABLES",
    "CanonicalExport",
    "DatabaseAdapter",
    "SqliteAdapter",
    "access_decisions",
    "assert_synthetic_path",
    "scan_export_for_leaks",
]
