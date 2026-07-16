"""Local Ollama provider adapter (PLAN-2026-AI §3.4, D4, PORT-3).

A concrete :class:`~scripts.mcp_service.providers.base.ProviderAdapter` proving
GOV-717 swappability: the domain core routes to it with no change. Design rules,
all fail-closed:

* **Localhost only.** The endpoint is fixed to ``http://127.0.0.1:11434`` and any
  other host is rejected at construction — a lens/evidence job's context can
  never be posted to a remote address through this adapter (reinforces D7).
* **Stdlib transport, injectable.** The default transport uses ``urllib`` (no new
  third-party dependency). Tests and CI inject a stub transport, so they make
  **zero network calls**; the adapter's logic is exercised without Ollama
  running.
* **Real unit metering (BUD-3).** Input/output units come from Ollama's
  ``prompt_eval_count`` / ``eval_count`` so a free local call still records
  comparable, budget-enforceable units. Latency is measured locally.

This module is the ONLY place an Ollama-specific wire format lives (PORT-3); the
core never imports it directly — the CLI composition root constructs it.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable

from .base import GenerationRequest, GenerationResult

# The single allowed endpoint. Ollama's default local bind; never a remote host.
LOCAL_ENDPOINT = "http://127.0.0.1:11434"
_GENERATE_PATH = "/api/generate"

# transport(url, payload) -> decoded JSON dict. Injectable for hermetic tests.
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


def _urllib_transport(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Default POST transport (stdlib only). Not exercised in CI/tests."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # pragma: no cover - network
        return json.loads(resp.read().decode("utf-8"))


class OllamaAdapter:
    """Adapter for a locally-running Ollama daemon. Localhost-only, zero-spend."""

    def __init__(
        self,
        *,
        provider_id: str = "ollama",
        model: str = "llama3.2",
        endpoint: str = LOCAL_ENDPOINT,
        transport: Transport | None = None,
    ) -> None:
        if endpoint != LOCAL_ENDPOINT:
            # Fail closed: this adapter is a *local* provider by construction.
            raise ValueError(
                f"OllamaAdapter endpoint must be {LOCAL_ENDPOINT!r}, got {endpoint!r}")
        self.provider_id = provider_id
        self.kind = "ollama"
        self._model = model
        self._endpoint = endpoint
        self._transport: Transport = transport or _urllib_transport

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "models": [self._model],
            "endpoint": self._endpoint,
            "network": True,      # localhost HTTP — still a socket, but never remote
            "local": True,        # D7: a local provider
            "deterministic": False,
        }

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """One local generation call. Units metered from Ollama's eval counts."""
        prompt = "\n".join(request.minimized_context_parts)
        payload: dict[str, Any] = {
            "model": request.model or self._model,
            "prompt": prompt,
            "stream": False,
        }
        options: dict[str, Any] = {}
        if request.max_output_units and request.max_output_units > 0:
            options["num_predict"] = int(request.max_output_units)
        if request.params:
            options.update(request.params)
        if options:
            payload["options"] = options

        t0 = time.monotonic()
        body = self._transport(self._endpoint + _GENERATE_PATH, payload)
        latency_ms = int((time.monotonic() - t0) * 1000)

        if not isinstance(body, dict):
            raise ValueError("ollama transport returned a non-object response")
        text = body.get("response")
        if not isinstance(text, str):
            raise ValueError("ollama response missing 'response' text")
        # Prefer Ollama's own token accounting; fall back to a word count so a
        # local call is never recorded as zero-cost (BUD-3).
        input_units = _as_int(body.get("prompt_eval_count"), default=len(prompt.split()))
        output_units = _as_int(body.get("eval_count"), default=len(text.split()))
        return GenerationResult(
            text=text,
            input_units=input_units,
            output_units=output_units,
            latency_ms=latency_ms,
            provider_meta={
                "provider_id": self.provider_id,
                "model": payload["model"],
                "done": bool(body.get("done", True)),
            },
        )


def _as_int(value: Any, *, default: int) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)
