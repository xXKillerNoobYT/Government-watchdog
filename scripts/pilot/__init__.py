"""PILOT-2026 Wave-0 harness (GOV-781, GOV-723 leg 2/7).

Implements PILOT-2026 v1.0 §5 (``Docs/2026-pilot-protocol-PILOT-2026.md``): a
synthetic-workload driver, a metric-snapshot extractor, and a decision-pack
builder that **reuse** the merged serving stack instead of re-implementing it.

Design invariants (protocol §5.2, the leg-3 RED conditions):

* **Additive-only.** Everything lives under ``scripts/pilot/`` + ``tests/``. The
  three frozen serving modules (``read_api``, ``ai_risk_gate``,
  ``stage5_agenda_board``) are IMPORTED transitively (via the MCP redaction
  choke-point) but never edited — byte-0 diff is an acceptance gate.
* **Zero credits, local only.** The workload routes through
  ``mcp_service.routing`` on the deterministic fake adapter (or a local Ollama
  adapter); the paid-provider registry stays at cap 0 (BUD-5). Every run asserts
  no audit row names a non-local provider (§1.2).
* **No new schema by default.** The harness reuses the 0021/0022/0023/0024/0025
  tables. Migration slot 0026 is reserved for ``pilot_support_log`` and is only
  cut under the §2.5 rule (support rows needed as content-hashed pack inputs);
  Wave-0 keeps the support log as append-only JSONL, so no migration ships here.
* **No user-facing surface.** GOV-420 hold: local only, no hosting/DNS/public
  exposure. WL-5 mails only through the null adapter.

The package is import-safe: importing it performs no I/O and touches no DB.
"""

from __future__ import annotations

#: Default synthesis seed. A fixed seed makes the dry-run planned-job manifest
#: byte-identical across runs and machines (§5.3 test 1).
DEFAULT_SEED = "PILOT-2026-wave0-v1"

#: The Alpine town row of the 0024 ``areas`` spine — the only real serving area
#: in scope (INV-5). County/state figures are labeled rollup projections, never
#: new-area ingest.
ALPINE_AREA_ID = "alpine"
LINCOLN_COUNTY_ID = "lincoln"
WYOMING_STATE_ID = "wy"

#: Providers whose audit rows are allowed by the zero-credit assertion (§1.2a).
#: ``None`` (an un-attributable / read-path audit row) is always allowed too.
LOCAL_PROVIDER_IDS = frozenset({"fake", "ollama"})

#: The default per-type call bounds (ASSUMED defaults, overridable by CLI flag).
#: The driver hard-caps total calls per run at :data:`MAX_TOTAL_CALLS`.
DEFAULT_BOUNDS: dict[str, int] = {
    "WL-1": 50,   # typed resource reads
    "WL-2": 25,   # lens analysis jobs PER shipped lens pack (x3 packs)
    "WL-3": 50,   # queue-lane jobs
    "WL-4": 1,    # safety probes: 1 each of (a) scope (b) redaction (c) budget (d) revocation
    "WL-5": 10,   # notification sends (+ 2 fixed no-consent probes)
    "WL-6": 5,    # shared-pool (area_id NULL) attribution probes
}

#: Hard ceiling on total driver calls in a single run (defense against a
#: mis-set bound). WL-2 counts x3 (one per lens pack); WL-5 adds 2 probes.
MAX_TOTAL_CALLS = 1000

#: WL-5 fixed extra: no-consent send probes (NOTIF-1 / AM-5 hard-fail).
WL5_NOCONSENT_PROBES = 2

#: Local log root (gitignored, §5.2). Raw snapshots stay here; only sanitized
#: aggregate metrics may leave the machine.
LOG_ROOT = "Logs/pilot"

__all__ = [
    "DEFAULT_SEED",
    "ALPINE_AREA_ID",
    "LINCOLN_COUNTY_ID",
    "WYOMING_STATE_ID",
    "LOCAL_PROVIDER_IDS",
    "DEFAULT_BOUNDS",
    "MAX_TOTAL_CALLS",
    "WL5_NOCONSENT_PROBES",
    "LOG_ROOT",
]
