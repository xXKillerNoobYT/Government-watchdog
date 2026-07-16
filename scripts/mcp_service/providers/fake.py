"""Deterministic fake provider adapter (CONTRACT-2026-MCP §3.5, D6).

The ONLY adapter GOV-717 ships. It performs no network call and spends no credit
— output is a pure function of the request, so contract tests are reproducible.
Its sole job is to prove the ``ProviderAdapter`` protocol is swappable: GOV-718
drops in a real Ollama adapter behind the same interface with no domain change.
"""

from __future__ import annotations

import hashlib

from .base import GenerationRequest, GenerationResult


class FakeAdapter:
    """A stand-in generator. Deterministic, offline, zero-cost."""

    def __init__(self, provider_id: str = "fake", model: str = "fake-1") -> None:
        self.provider_id = provider_id
        self.kind = "fake"
        self._model = model

    def capabilities(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "models": [self._model],
            "network": False,
            "deterministic": True,
        }

    def generate(self, request: GenerationRequest) -> GenerationResult:
        joined = "\n".join(request.minimized_context_parts)
        digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
        text = f"[fake:{request.model}] summary({len(request.minimized_context_parts)} parts) {digest}"
        return GenerationResult(
            text=text,
            input_units=len(joined.split()),
            output_units=len(text.split()),
            latency_ms=0,
            provider_meta={"deterministic": True, "digest": digest},
        )
