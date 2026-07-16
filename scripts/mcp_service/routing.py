"""Data-driven provider routing + the single generation chokepoint (§3.3, D1/D2/D7).

Two responsibilities, one invariant:

* **D1 — routing is data.** :func:`evaluate_policy` reads a ``mcp_routing_policies``
  row and :func:`select_provider` walks its ordered ``provider_preference``,
  returning the first provider that is *eligible*: registered ∧ enabled ∧
  ``budget_cap_units > 0`` (``providers.base.is_callable``, BUD-5) ∧ not
  health-degraded ∧ (for a ``local_only`` context) local. Selection is
  deterministic — ordered preference, no randomness.

* **D2 — one chokepoint.** :func:`route_and_generate` is the ONLY function in the
  codebase that calls ``adapter.generate()``. A static test asserts no other
  module does. It enforces, in order: policy eval → provider selection → D7
  local-only guard → fail-closed budget pre-flight → the single adapter call →
  exactly one health row + one audit row per attempt.

The adapter instances are *injected* by the caller (the analysis runner / CLI is
the composition root). This module imports only ``providers.base`` — the protocol
and registry helpers — never a concrete adapter, preserving the PORT-3 boundary.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Mapping

from . import audit, budget as budget_mod, health
from .errors import DENY_BUDGET, DENY_CAPABILITY, DENY_UNSUPPORTED, MCPDenied
from .providers.base import GenerationRequest, GenerationResult, ProviderAdapter, is_callable

# A provider is "local" purely by its registered kind — data on the existing
# mcp_provider_registry row, no schema change and no adapter modification. A
# future paid cloud provider registers a kind outside this set and is therefore
# refused for any local_only context (D7).
LOCAL_KINDS = frozenset({"ollama", "fake", "local"})

CONTEXT_LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class RoutingPolicy:
    policy_id: str
    version: str
    job_kind: str
    context_class: str
    provider_preference: tuple[str, ...]
    model: str
    max_output_units: int


@dataclass(frozen=True)
class RouteResult:
    """What a successful route produced, plus the metadata the runner records."""

    provider_id: str
    model: str
    policy_id: str
    policy_version: str
    result: GenerationResult
    audit_id: str


def evaluate_policy(
    conn: sqlite3.Connection,
    *,
    job_kind: str,
    context_class: str = CONTEXT_LOCAL_ONLY,
    policy_id: str | None = None,
) -> RoutingPolicy:
    """Resolve the routing policy for ``(job_kind, context_class)``.

    Deterministic: filters by job_kind + context_class (+ optional policy_id) and
    picks the highest ``version`` (string-sorted). Fail-closed — an unmatched
    lookup is ``denied:unsupported``, never a silent default provider.
    """
    sql = ("SELECT * FROM mcp_routing_policies "
           "WHERE job_kind = ? AND context_class = ?")
    params: list[Any] = [job_kind, context_class]
    if policy_id is not None:
        sql += " AND policy_id = ?"
        params.append(policy_id)
    sql += " ORDER BY version DESC, policy_id"
    row = conn.execute(sql, tuple(params)).fetchone()
    if row is None:
        raise MCPDenied(
            DENY_UNSUPPORTED,
            f"no routing policy for job_kind={job_kind!r} context={context_class!r}",
        )
    row = dict(row)
    try:
        pref = json.loads(row["provider_preference"] or "[]")
    except Exception:  # noqa: BLE001 — a malformed preference is fail-closed empty.
        pref = []
    pref = tuple(str(p) for p in pref) if isinstance(pref, list) else ()
    return RoutingPolicy(
        policy_id=row["policy_id"], version=row["version"], job_kind=row["job_kind"],
        context_class=row["context_class"], provider_preference=pref,
        model=row["model"], max_output_units=int(row["max_output_units"]),
    )


def _provider_kind(conn: sqlite3.Connection, provider_id: str) -> str | None:
    row = conn.execute(
        "SELECT kind FROM mcp_provider_registry WHERE provider_id = ?", (provider_id,)
    ).fetchone()
    return row["kind"] if row else None


def is_local_provider(conn: sqlite3.Connection, provider_id: str) -> bool:
    kind = _provider_kind(conn, provider_id)
    return kind in LOCAL_KINDS if kind is not None else False


def select_provider(
    conn: sqlite3.Connection,
    policy: RoutingPolicy,
    *,
    degrade_threshold: int = health.DEFAULT_DEGRADE_THRESHOLD,
) -> str | None:
    """First eligible provider in the policy's ordered preference, or ``None``.

    Eligibility (all required): callable (enabled + budget cap > 0, BUD-5),
    not degraded, and — when the context is ``local_only`` — local (D7). A
    non-local provider is silently skipped for a local_only policy here; the hard
    fail-closed refusal is re-asserted in :func:`route_and_generate` so the guard
    holds even if a caller hands in a provider directly.
    """
    local_only = policy.context_class == CONTEXT_LOCAL_ONLY
    for provider_id in policy.provider_preference:
        if not is_callable(conn, provider_id):
            continue
        if local_only and not is_local_provider(conn, provider_id):
            continue
        if health.is_degraded(conn, provider_id, threshold=degrade_threshold):
            continue
        return provider_id
    return None


def route_and_generate(
    conn: sqlite3.Connection,
    *,
    job_kind: str,
    context_class: str = CONTEXT_LOCAL_ONLY,
    adapters: Mapping[str, ProviderAdapter],
    context_parts: list[str],
    policy_id: str | None = None,
    job_id: str | None = None,
    area_id: str | None = None,
    lens_version: str | None = None,
    degrade_threshold: int = health.DEFAULT_DEGRADE_THRESHOLD,
    now_utc: str | None = None,
) -> RouteResult:
    """THE single call site of any ``adapter.generate()`` (D2).

    Order of enforcement (each fail-closed):

    1. resolve policy (D1);
    2. select an eligible provider — none ⇒ ``denied:capability`` (no fallback);
    3. re-assert the D7 local-only guard on the chosen provider;
    4. confirm the caller actually injected that provider's adapter;
    5. budget pre-flight — projected breach pauses + refuses (D3);
    6. call the adapter exactly once, timing it locally;
    7. write exactly one health row and one audit row for the attempt.

    On any refusal before the adapter call, one *deny* audit row is written and no
    health row (there was no attempt). On an adapter exception, one *error* health
    row and one *deny* audit row are written and the error re-raised.
    """
    policy = evaluate_policy(conn, job_kind=job_kind, context_class=context_class,
                             policy_id=policy_id)
    provider_id = select_provider(conn, policy, degrade_threshold=degrade_threshold)
    if provider_id is None:
        _deny_audit(conn, name=job_kind, error_code=DENY_CAPABILITY, job_id=job_id,
                    area_id=area_id, policy=policy, lens_version=lens_version)
        raise MCPDenied(DENY_CAPABILITY, f"no eligible provider for policy {policy.policy_id}")

    # D7: never route a local_only context to a non-local provider — fail closed
    # even if selection logic somehow yielded one.
    if context_class == CONTEXT_LOCAL_ONLY and not is_local_provider(conn, provider_id):
        _deny_audit(conn, name=job_kind, error_code=DENY_CAPABILITY, job_id=job_id,
                    area_id=area_id, provider=provider_id, policy=policy,
                    lens_version=lens_version)
        raise MCPDenied(DENY_CAPABILITY,
                        f"local_only context cannot route to non-local {provider_id!r} (D7)")

    adapter = adapters.get(provider_id)
    if adapter is None:
        _deny_audit(conn, name=job_kind, error_code=DENY_UNSUPPORTED, job_id=job_id,
                    area_id=area_id, provider=provider_id, policy=policy,
                    lens_version=lens_version)
        raise MCPDenied(DENY_UNSUPPORTED, f"no adapter injected for provider {provider_id!r}")

    budget = budget_mod.budget_for_provider(conn, provider_id)
    # Fail-closed pre-flight. estimated_units uses the policy ceiling as a
    # conservative upper bound so we never authorize a call that could breach.
    budget_mod.preflight(conn, budget, estimated_units=policy.max_output_units,
                         now_utc=now_utc)

    request = GenerationRequest(
        model=policy.model,
        minimized_context_parts=list(context_parts),
        max_output_units=policy.max_output_units,
    )
    t0 = time.monotonic()
    try:
        result = adapter.generate(request)
    except Exception as exc:  # noqa: BLE001 — any adapter failure is fail-closed.
        latency_ms = int((time.monotonic() - t0) * 1000)
        health.record(conn, provider_id=provider_id, outcome="error",
                     latency_ms=latency_ms, error_code=type(exc).__name__)
        _deny_audit(conn, name=job_kind, error_code=DENY_CAPABILITY, job_id=job_id,
                    area_id=area_id, provider=provider_id, policy=policy,
                    lens_version=lens_version, latency_ms=latency_ms)
        raise MCPDenied(DENY_CAPABILITY, f"provider {provider_id!r} generate failed: {exc}")

    latency_ms = result.latency_ms or int((time.monotonic() - t0) * 1000)
    health.record(conn, provider_id=provider_id, outcome="ok", latency_ms=latency_ms)
    audit_id = audit.record(
        conn, kind="provider", name=job_kind, outcome="allow", job_id=job_id,
        area_id=area_id, provider=provider_id, model=policy.model,
        input_units=result.input_units, output_units=result.output_units,
        direct_cost_units=0, latency_ms=latency_ms,
        policy_version=policy.version, lens_version=lens_version,
    )
    return RouteResult(
        provider_id=provider_id, model=policy.model, policy_id=policy.policy_id,
        policy_version=policy.version, result=result, audit_id=audit_id,
    )


def _deny_audit(
    conn: sqlite3.Connection,
    *,
    name: str,
    error_code: str,
    job_id: str | None,
    area_id: str | None,
    policy: RoutingPolicy | None = None,
    provider: str | None = None,
    lens_version: str | None = None,
    latency_ms: int | None = None,
) -> str:
    """Write the single deny audit row for a refused/failed attempt."""
    return audit.record(
        conn, kind="provider", name=name, outcome="deny", error_code=error_code,
        job_id=job_id, area_id=area_id, provider=provider,
        model=policy.model if policy else None,
        policy_version=policy.version if policy else None,
        lens_version=lens_version, latency_ms=latency_ms,
    )
