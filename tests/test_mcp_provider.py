"""§6.7 provider protocol + PORT-3 import boundary.

Proves the provider interface is swappable via the deterministic fake adapter
(round-trip through GenerationRequest/Result) and that concrete provider adapters
live ONLY under `scripts/mcp_service/providers/` — the domain core never imports a
provider implementation (PORT-3). Also BUD-5: a freshly registered provider is
un-callable until an owner sets enabled + a non-zero budget.
"""

from __future__ import annotations

import ast
from pathlib import Path

from mcp_service.providers import base as pbase
from mcp_service.providers.fake import FakeAdapter

PKG = Path(__file__).resolve().parent.parent / "scripts" / "mcp_service"


def test_fake_adapter_satisfies_protocol():
    adapter = FakeAdapter()
    assert isinstance(adapter, pbase.ProviderAdapter)
    caps = adapter.capabilities()
    assert caps["network"] is False and caps["deterministic"] is True


def test_fake_adapter_round_trip_is_deterministic():
    adapter = FakeAdapter()
    req = pbase.GenerationRequest(model="fake-1", minimized_context_parts=["alpha", "beta"])
    r1 = adapter.generate(req)
    r2 = adapter.generate(req)
    assert isinstance(r1, pbase.GenerationResult)
    assert r1.text == r2.text  # deterministic, no network, zero spend
    assert r1.output_units > 0


def test_registered_provider_is_uncallable_at_budget_zero(mcp_conn):
    pbase.register_provider(mcp_conn, provider_id="fake", kind="fake")
    row = mcp_conn.execute(
        "SELECT enabled, budget_cap_units FROM mcp_provider_registry WHERE provider_id='fake'"
    ).fetchone()
    assert row["enabled"] == 0 and row["budget_cap_units"] == 0
    assert pbase.is_callable(mcp_conn, "fake") is False  # BUD-5


def test_provider_becomes_callable_only_with_budget_and_enabled(mcp_conn):
    pbase.register_provider(mcp_conn, provider_id="fake", kind="fake")
    mcp_conn.execute(
        "UPDATE mcp_provider_registry SET enabled=1, budget_cap_units=100 WHERE provider_id='fake'")
    mcp_conn.commit()
    assert pbase.is_callable(mcp_conn, "fake") is True


def test_port3_no_provider_impl_import_in_domain_core():
    """No module OUTSIDE providers/ may import a concrete provider adapter.

    The domain core depends only on `providers.base` (protocol + registry). This
    scan is the PORT-3 dependency rule: a future real SDK must land under
    providers/ and stay behind the protocol.
    """
    for path in PKG.rglob("*.py"):
        if "providers" in path.parts:
            continue  # adapters legitimately import their own SDKs here
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.endswith("providers.fake"), (
                    f"{path.name} imports a concrete adapter (PORT-3 violation)")
                # importing providers.base (protocol/registry) is allowed;
                # importing the providers package or a concrete adapter is not.
                assert node.module not in ("mcp_service.providers",
                                           "mcp_service.providers.fake"), (
                    f"{path.name} imports provider impl (PORT-3 violation)")
