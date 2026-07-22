"""Adapter protocol, null adapter, registry, fail-closed resolution (INV-5/AC-5).

The registry maps adapter names to zero-arg factories. Two adapters live
here: :class:`NullAdapter` (no-op, hash-only logging — nothing ever leaves
the machine, AC-9; the shipped default) and :class:`SmtpAdapter` (GOV-1543
F2: provider-agnostic SMTP submission). The SMTP adapter is registered ONLY
by :func:`register_smtp_from_env` at service startup, and even then
:func:`resolve_adapter` hands it out only while the latest
``email_adapter_enabled`` flag row is enabled.

Resolution truth table (INV-5, fail-closed):

    flag row absent            -> null
    latest flag row disabled   -> null
    flag enabled, no real
      adapter registered       -> null
    flag enabled + registered  -> the registered real adapter

D1 refinement (GOV-1543 §3 F2): the environment supplies *credentials only*
(``GW_SMTP_*`` / ``GW_MAIL_FROM``); *activation* remains exclusively the DB
flag with an ``owner_decision_ref``. Missing/invalid env ⇒ the factory refuses
to register and everything stays on the null adapter (fail closed). Logging
across this module is hash-only: an email address never reaches a log record —
only ``sha256(lowercased address)[:12]``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import smtplib
import sqlite3
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Callable, Mapping, Protocol

from email_gateway import flags

logger = logging.getLogger("email_gateway")

NULL_ADAPTER_NAME = "null"
SMTP_ADAPTER_NAME = "smtp"

#: Complete env inventory for the SMTP adapter (GOV-1543 §4). All five must be
#: present to register; username+password may both be empty for an
#: unauthenticated loopback relay. ``GW_SMTP_SECURITY`` is optional
#: ("starttls" default; "none" is refused off-loopback).
SMTP_ENV_VARS = ("GW_SMTP_HOST", "GW_SMTP_PORT", "GW_SMTP_USERNAME",
                 "GW_SMTP_PASSWORD", "GW_MAIL_FROM")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def email_hash(to_email: str) -> str:
    """The ONLY form of an address that may reach a log record (F2 rule)."""
    return hashlib.sha256(to_email.strip().lower().encode("utf-8")).hexdigest()[:12]


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
        # surface (AC-1) and logs are not consent-gated. Address is hash-only
        # (GOV-1543 F2): logs must never carry a plaintext email.
        logger.info("null adapter: suppressed send to_hash=%s subject=%r",
                    email_hash(to_email), subject)
        return None


class SmtpAdapter:
    """Provider-agnostic SMTP submission (GOV-1543 F2). STARTTLS by default.

    Deliberately SMTP rather than a vendor SDK: the which-provider decision is
    an owner call on the P3d card, not a code dependency. Credentials come from
    the service-process environment only (never the repo, never the client).
    ``security="none"`` exists for a loopback relay/dev sink and is refused for
    any non-loopback host — plaintext SMTP must never cross a network.
    """

    name = SMTP_ADAPTER_NAME

    def __init__(self, *, host: str, port: int, username: str, password: str,
                 mail_from: str, security: str = "starttls",
                 timeout: float = 30.0) -> None:
        if security not in ("starttls", "none"):
            raise ValueError(f"unknown GW_SMTP_SECURITY {security!r}")
        if security == "none" and host not in _LOOPBACK_HOSTS:
            raise ValueError(
                "GW_SMTP_SECURITY=none is loopback-only; refusing plaintext "
                f"SMTP to non-loopback host {host!r}")
        if bool(username) != bool(password):
            raise ValueError(
                "GW_SMTP_USERNAME and GW_SMTP_PASSWORD must be set together "
                "(both empty = unauthenticated loopback relay)")
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.mail_from = mail_from
        self.security = security
        self.timeout = timeout

    def send(self, *, to_email: str, subject: str, body_text: str,
             body_html: str | None) -> str | None:
        message = EmailMessage()
        message["From"] = self.mail_from
        message["To"] = to_email
        message["Subject"] = subject
        message["Message-ID"] = make_msgid()
        message.set_content(body_text)
        if body_html:
            message.add_alternative(body_html, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port,
                              timeout=self.timeout) as server:
                server.ehlo()
                if self.security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(message)
        except Exception as exc:  # noqa: BLE001 — protocol: never raise on delivery failure
            # Exception TEXT may embed the recipient address (e.g.
            # SMTPRecipientsRefused), so only the exception TYPE is logged.
            logger.warning("smtp adapter: send failed to_hash=%s error=%s",
                           email_hash(to_email), type(exc).__name__)
            return None
        logger.info("smtp adapter: sent to_hash=%s subject=%r",
                    email_hash(to_email), subject)
        return str(message["Message-ID"])


def register_smtp_from_env(environ: Mapping[str, str] | None = None) -> bool:
    """Register the SMTP adapter iff the env config is complete + valid.

    Called once at service startup (the artifact's ``run.py``). Fail closed:
    no env → silent no-op (dev default stays null); partial/invalid env →
    warning naming the missing VARIABLE NAMES (never values) and no
    registration. Registration alone still sends nothing — resolution stays
    behind the ``email_adapter_enabled`` flag (INV-5 truth table).
    """
    env = os.environ if environ is None else environ
    present = [v for v in SMTP_ENV_VARS if v in env]
    if not present:
        return False
    missing = [v for v in SMTP_ENV_VARS if v not in env]
    if missing:
        logger.warning("smtp adapter not registered: missing env %s",
                       ", ".join(missing))
        return False
    try:
        port = int(env["GW_SMTP_PORT"])
    except ValueError:
        logger.warning(
            "smtp adapter not registered: GW_SMTP_PORT is not an integer")
        return False
    config = dict(host=env["GW_SMTP_HOST"], port=port,
                  username=env["GW_SMTP_USERNAME"],
                  password=env["GW_SMTP_PASSWORD"],
                  mail_from=env["GW_MAIL_FROM"],
                  security=env.get("GW_SMTP_SECURITY", "starttls"))
    try:
        SmtpAdapter(**config)  # eager validation: refuse at startup, not send time
    except ValueError as exc:
        logger.warning("smtp adapter not registered: %s", exc)
        return False
    register_adapter(SMTP_ADAPTER_NAME, lambda: SmtpAdapter(**config))
    return True


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
