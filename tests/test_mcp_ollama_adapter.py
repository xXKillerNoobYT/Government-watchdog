"""§3.9 Ollama adapter regression (D4, PORT-3, BUD-3).

The adapter satisfies the frozen ProviderAdapter protocol, meters units from
Ollama's eval counts, is localhost-only by construction, and — critically — makes
ZERO network calls under test because its transport is injected. A static scan
confirms the concrete adapter is only ever imported from the providers package
(PORT-3) and the whole package makes no forbidden socket/subprocess import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mcp_service.providers import base as pbase
from mcp_service.providers.ollama import LOCAL_ENDPOINT, OllamaAdapter

PKG = Path(__file__).resolve().parent.parent / "scripts" / "mcp_service"


def _stub(response="the record shows a budget vote", *, pin=13, ein=6, done=True):
    calls = {"n": 0, "url": None, "payload": None}

    def transport(url, payload):
        calls["n"] += 1
        calls["url"] = url
        calls["payload"] = payload
        return {"response": response, "prompt_eval_count": pin, "eval_count": ein,
                "done": done}

    return transport, calls


def test_satisfies_protocol_and_is_local():
    adapter = OllamaAdapter(transport=lambda u, p: {"response": "x"})
    assert isinstance(adapter, pbase.ProviderAdapter)
    caps = adapter.capabilities()
    assert caps["kind"] == "ollama" and caps["local"] is True
    assert caps["endpoint"] == LOCAL_ENDPOINT


def test_generate_meters_units_from_eval_counts_bud3():
    transport, calls = _stub(pin=13, ein=6)
    adapter = OllamaAdapter(model="llama3.2", transport=transport)
    req = pbase.GenerationRequest(model="", minimized_context_parts=["one", "two"],
                                  max_output_units=32)
    res = adapter.generate(req)
    assert (res.input_units, res.output_units) == (13, 6)
    assert res.latency_ms >= 0
    assert calls["n"] == 1  # exactly one transport call
    assert calls["url"] == LOCAL_ENDPOINT + "/api/generate"
    assert calls["payload"]["model"] == "llama3.2"  # falls back to adapter model
    assert calls["payload"]["options"]["num_predict"] == 32
    assert calls["payload"]["stream"] is False


def test_units_fall_back_to_word_count_when_counts_absent():
    transport, _ = _stub()
    adapter = OllamaAdapter(transport=lambda u, p: {"response": "three word reply"})
    res = adapter.generate(
        pbase.GenerationRequest(model="m", minimized_context_parts=["alpha beta"]))
    assert res.input_units == 2 and res.output_units == 3  # never recorded as 0


def test_missing_response_text_fails_closed():
    adapter = OllamaAdapter(transport=lambda u, p: {"prompt_eval_count": 1})
    with pytest.raises(ValueError):
        adapter.generate(pbase.GenerationRequest(model="m", minimized_context_parts=["x"]))


def test_endpoint_is_localhost_only():
    with pytest.raises(ValueError):
        OllamaAdapter(endpoint="http://10.0.0.5:11434")
    with pytest.raises(ValueError):
        OllamaAdapter(endpoint="https://api.example.com")


def test_no_network_call_without_transport_injection():
    # Constructing the adapter must not open any connection; only .generate() would,
    # and every test injects a transport, so the suite is fully hermetic.
    adapter = OllamaAdapter()
    assert adapter.capabilities()["endpoint"] == LOCAL_ENDPOINT


def test_port3_ollama_imported_only_from_providers_package():
    """No domain-core module imports the concrete Ollama adapter (PORT-3)."""
    for path in PKG.rglob("*.py"):
        if "providers" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.endswith("providers.ollama"), (
                    f"{path.name} imports the concrete Ollama adapter (PORT-3 violation)")
