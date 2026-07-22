"""Gated-beta front door: magic-link auth + allowlist + waitlist (GOV-801).

The backend for the GOV-799 minimal public landing. An additive leaf package
over the five 0026 ``beta_*`` tables. It reuses the merged fail-closed email
adapter (:mod:`email_gateway.adapters`) and feature-flag gate
(:mod:`email_gateway.flags`) but NEVER touches the four frozen serving surfaces
(read_api, ai_risk_gate, stage5_agenda_board, mcp_service/).

Passwordless by design — distinct from the password/bearer ``accounts`` stack:

  * :mod:`.tokens`    — one-time-use, 15-min magic tokens (sha256-only storage)
  * :mod:`.sessions`  — 7-day cookie sessions (sha256-only storage)
  * :mod:`.allowlist` — owner-gated allow/revoke; revoke cascades to sessions
  * :mod:`.waitlist`  — public intake
  * :mod:`.audit`     — append-only log; email is hashed, IP is a truncated hash
  * :mod:`.mailer`    — magic_link / waitlist_confirmation via the null adapter
  * :mod:`.service`   — orchestration (rate limits, neutral responses)
  * :mod:`.http_api`  — the four loopback routes, fail-closed behind a flag
  * :mod:`.admin`     — owner CLI to seed/revoke the allowlist

Fail-closed activation: merging this package activates nothing. The HTTP surface
answers 404 until the owner-gated ``beta_gate_enabled`` flag is enabled, and no
email leaves the machine until a real email adapter is owner-registered.
"""

from __future__ import annotations

__all__ = ["common", "tokens", "sessions", "allowlist", "waitlist", "audit",
           "ratelimit", "mailer", "service", "http_api"]
