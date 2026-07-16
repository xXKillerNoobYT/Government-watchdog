"""Adapter protocol, null adapter, registry, fail-closed resolution (INV-5/AC-5).

The registry maps adapter names to zero-arg factories. Exactly one adapter
ships in this card: :class:`NullAdapter` (no-op, logging only — nothing ever
leaves the machine, AC-9). A production SMTP/SES adapter is a FUTURE card:
it would register here, and even then :func:`resolve_adapter` hands it out
only while the latest ``email_adapter_enabled`` flag row is enabled.

Resolution truth table (INV-5, fail-closed):

    flag row absent            -> null
    latest flag row disabled   -> null
    flag enabled, no real
      adapter registered       -> null
    flag enabled + registered  -> the registered real adapter

No env var participates (D1). Tests may register in-memory fakes as "real"
adapters to prove the table; nothing in this repo registers one for real.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable, Protocol

from email_gateway import flags

logger = logging.getLogger("email_gateway")

NULL_ADAPTER_NAME = "null"


class EmailAdapter(Protocol):
    """Provider-agnostic send interface. Implementations must not raise on
    ordinary delivery failure — they return None and the caller logs it."""

    name: str

    def send(self, *, to_email: str, subject: str, body_text: str,
             body_html: str | None) -> str | None:
        """Deliver one message; returns a provider reference id or None."""


class NullAdapter:
    """Default adapter: logs the intent, sends NOTHING (AC-5/AC-9)."""

    name = NULL_ADAPTER_NAME

    def send(self, *, to_email: str, subject: str, body_text: str,
             body_html: str | None) -> str | None:
        # Deliberately no body in the log line — mail bodies are a zero-leak
        # surface (AC-1) and logs are not consent-gated.
        logger.info("null adapter: suppressed send to=%s subject=%r",
                    to_email, subject)
        return None


_REGISTRY: dict[str, Callable[[], EmailAdapter]] = {}


def register_adapter(name: str, factory: Callable[[], EmailAdapter]) -> None:
    """Register a REAL adapter factory (future SMTP/SES card, or test fakes)."""
    if name == NULL_ADAPTER_NAME:
        raise ValueError("the null adapter is built in and cannot be replaced")
    _REGISTRY[name] = factory


def unregister_adapter(name: str) -> None:
    _REGISTRY.pop(name, None)


def registered_real_adapters() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def resolve_adapter(conn: sqlite3.Connection) -> EmailAdapter:
    """The ONE resolution path (INV-5). See module truth table."""
    if not flags.is_enabled(conn, flags.EMAIL_ADAPTER_FLAG):
        return NullAdapter()
    if not _REGISTRY:
        logger.warning(
            "email_adapter_enabled is ON but no real adapter is registered; "
            "falling back to null adapter (fail-closed)")
        return NullAdapter()
    if len(_REGISTRY) > 1:
        raise RuntimeError(
            f"ambiguous real adapters registered: {registered_real_adapters()}")
    (factory,) = _REGISTRY.values()
    return factory()
