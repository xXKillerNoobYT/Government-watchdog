"""Area-economics ledger package (LEDGER-2026 v0.1, GOV-720 plan / GOV-743 impl).

An additive leaf package — imports only stdlib + the sibling ``db`` and
``paperclip_outbox`` leaves in ``scripts/``. It NEVER imports a provider SDK
(PORT-3) and NEVER imports or edits the frozen serving surfaces
(``read_api.reviewer_internal_records``, ``ai_risk_gate``, ``stage5_agenda_board``).

It is an AGGREGATION layer over the cost substrate that 0021/0022/0023/0019
already landed — it does not re-instrument any worker (LEDGER-2026 §0).

Module map (plan §2):
  * :mod:`.basis`         — basis vocabulary + report lint (LED-5, AM-7)
  * :mod:`.formulas`      — pure F1-F7 + F-ELIG (REQ-2026-COMM §9)
  * :mod:`.areas`         — rollup spine + owner-gated state machine (AREA-1..5, AM-1)
  * :mod:`.ledger`        — LED-1 aggregation + SLO metrics (AM-12)
  * :mod:`.fixed_cost`    — F2/F7 fixed-cost allocation (LED-4)
  * :mod:`.reviewer_cost` — LED-2 measured-or-proxy reviewer work
  * :mod:`.eligibility`   — F-ELIG / entitlement readiness (recommend-only)
  * :mod:`.capacity`      — LED-F6 deterministic synthetic headroom
  * :mod:`.budget_link`   — BUD-2/AM-4 breach reconciler (fail-closed + outbox)
  * :mod:`.report`        — per-area pack + reproducibility hash
  * :mod:`.export`        — LED-6 CSV/JSON surface (no prices)

Two writers only: ``areas.transition`` (area_state/area_transitions, owner-gated)
and ``report.record_run`` (ledger_report_runs). ``budget_link.reconcile`` writes
only the shared ``paperclip_outbox``. Everything else is read-only aggregation.
"""

from __future__ import annotations

__all__ = [
    "basis",
    "formulas",
    "areas",
    "ledger",
    "fixed_cost",
    "reviewer_cost",
    "eligibility",
    "capacity",
    "budget_link",
    "report",
    "export",
]
