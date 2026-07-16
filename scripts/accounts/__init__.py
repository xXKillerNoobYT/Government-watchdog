"""Account service package (ACCT-2026 v0.2, GOV-721 plan / GOV-754 leg 2).

Additive package over the eleven 0025 tables. Imports only stdlib,
``argon2-cffi`` (D2), and the sibling ``notifications`` leaf. It NEVER
imports or edits the four frozen serving surfaces (``read_api``,
``ai_risk_gate``, ``stage5_agenda_board``, ``mcp_service/``) — AC-8/INV-1.

Module map (plan §Scope):
  * :mod:`.service`  — create/approve/revoke/pause, tier check, argon2id
    passwords + login (INV-4/7/9)
  * :mod:`.sessions` — bearer-session issue/verify/revoke; sha256-only
    storage (INV-10)
  * :mod:`.consent`  — email-consent + unsubscribe-token lifecycle (INV-8)
  * :mod:`.cohorts`  — owner-gated 2→3→15 transitions, additive membership,
    in-transaction cap recompute (D4, INV-3/6, AC-2/3)
  * :mod:`.gate`     — per-request zero-leak civic-data gate (AC-1)

Everything mutable is owner-gated or append-only: approve/revoke/pause append
``access_grants`` rows (current state = latest row ordered
``(granted_utc, rowid)``), cohort moves append ``cohort_transitions`` rows,
and nothing here activates email or public launch.
"""

from __future__ import annotations

__all__ = ["service", "sessions", "consent", "cohorts", "gate"]
