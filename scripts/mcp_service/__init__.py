"""Self-contained MCP service layer (CONTRACT-2026-MCP v1.0, GOV-717/GOV-731).

An additive leaf package: a typed, least-privilege boundary that lets workers
(local AI jobs today, future paid providers) read job-scoped canonical evidence
and policy packs and submit derived outputs into staging — without ever seeing
raw filesystem paths, PII, reviewer notes, or the registry itself, and with no
generic shell/system capability.

Design (plan §2):
  * D1 transport-agnostic core + thin stdio JSON-RPC binding, no new deps
    (:mod:`.jsonrpc`);
  * D2 redaction choke-point importing the frozen ``read_api`` /
    ``ai_risk_gate`` scanners (:mod:`.redaction`);
  * D3 deny-by-default field allowlists (:mod:`.allowlists`);
  * D4 job-scoped HMAC capability tokens + grant store (:mod:`.capability`);
  * D5 writes land in ``mcp_job_outputs`` staging only (:mod:`.tools`);
  * D6 ``ProviderAdapter`` protocol + registry + a deterministic fake adapter
    (:mod:`.providers`).

The frozen serving surfaces (``read_api``, ``ai_risk_gate``,
``stage5_agenda_board``) are imported, never modified — byte-0 diff is an
acceptance gate. Registry and raw data stay local (INV-7).
"""

from __future__ import annotations

# Importing contracts registers every JSON Schema under {schema_id, semver}.
from . import contracts  # noqa: F401
from .service import call_tool, read_resource  # noqa: F401

__all__ = ["call_tool", "read_resource", "contracts"]
