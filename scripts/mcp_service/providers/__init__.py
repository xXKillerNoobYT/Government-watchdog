"""Provider adapters (PORT-3). Provider SDK imports are confined to THIS package.

The domain core imports only :mod:`.base` (the ``ProviderAdapter`` protocol + the
typed request/result + the registry helpers). GOV-717 ships one deterministic
:class:`~scripts.mcp_service.providers.fake.FakeAdapter` for contract tests; the
real Ollama adapter and routing/budget enforcement are GOV-718. Any third-party
provider SDK must be imported here and nowhere else (PORT-3 import-boundary test).
"""
