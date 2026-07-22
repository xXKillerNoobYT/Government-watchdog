"""GOV-1544 (P3b of GOV-1523) — F2: real SMTP adapter + hash-only email logging.

Spec: ``docs/gov1543-deploy-execution-plan.md`` §3 F2 (website repo). Rules
proven here:

  * registration truth table: complete env ⇒ registered; missing/partial or
    invalid env ⇒ refused with a warning naming variable NAMES only.
  * resolution stays fail-closed (INV-5): flag off ⇒ null even when smtp is
    registered; flag on + registered ⇒ SmtpAdapter.
  * hash-only logging: NO handler on the ``email_gateway`` logger ever emits a
    string matching an email regex — for null sends, smtp sends, and smtp
    failures alike. ``to_hash=sha256(lowercased)[:12]`` is the only address form.
  * smoke: a REAL smtplib handshake against an in-process loopback SMTP sink
    (stdlib socket server — no dependency, no network beyond 127.0.0.1, no
    real provider; pre-P3d rule).
"""

from __future__ import annotations

import logging
import re
import socket
import socketserver
import threading

import pytest

from email_gateway import adapters, flags

# Same shape as the §2 deny-list clause-3 scanner: an RFC-5322-ish address.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

FULL_ENV = {
    "GW_SMTP_HOST": "127.0.0.1",
    "GW_SMTP_PORT": "2525",
    "GW_SMTP_USERNAME": "",
    "GW_SMTP_PASSWORD": "",
    "GW_MAIL_FROM": "beta@gov-watchdog.test",
    "GW_SMTP_SECURITY": "none",
}


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    adapters.unregister_adapter(adapters.SMTP_ADAPTER_NAME)


def _assert_no_plaintext_email(caplog):
    for record in caplog.records:
        assert not EMAIL_RE.search(record.getMessage()), (
            f"plaintext email leaked into log: {record.getMessage()!r}")


# --- email_hash ---------------------------------------------------------------

def test_email_hash_is_lowercased_truncated_sha256():
    import hashlib
    expect = hashlib.sha256(b"user@example.com").hexdigest()[:12]
    assert adapters.email_hash("  User@Example.COM ") == expect
    assert not EMAIL_RE.search(adapters.email_hash("user@example.com"))


# --- registration truth table -------------------------------------------------

def test_no_env_at_all_is_a_silent_noop(caplog):
    with caplog.at_level(logging.DEBUG, logger="email_gateway"):
        assert adapters.register_smtp_from_env({}) is False
    assert adapters.registered_real_adapters() == ()
    assert not caplog.records  # dev default: no spam


@pytest.mark.parametrize("omitted", adapters.SMTP_ENV_VARS)
def test_partial_env_refuses_and_names_missing_vars_only(omitted, caplog):
    env = {k: v for k, v in FULL_ENV.items() if k != omitted}
    with caplog.at_level(logging.WARNING, logger="email_gateway"):
        assert adapters.register_smtp_from_env(env) is False
    assert adapters.registered_real_adapters() == ()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert omitted in joined
    _assert_no_plaintext_email(caplog)


def test_non_integer_port_refuses(caplog):
    env = dict(FULL_ENV, GW_SMTP_PORT="not-a-port")
    with caplog.at_level(logging.WARNING, logger="email_gateway"):
        assert adapters.register_smtp_from_env(env) is False
    assert adapters.registered_real_adapters() == ()


def test_complete_env_registers_smtp():
    assert adapters.register_smtp_from_env(dict(FULL_ENV)) is True
    assert adapters.registered_real_adapters() == (adapters.SMTP_ADAPTER_NAME,)


def test_security_none_off_loopback_refuses(caplog):
    env = dict(FULL_ENV, GW_SMTP_HOST="smtp.example.net")
    with caplog.at_level(logging.WARNING, logger="email_gateway"):
        assert adapters.register_smtp_from_env(env) is False
    assert adapters.registered_real_adapters() == ()


def test_unknown_security_refuses():
    env = dict(FULL_ENV, GW_SMTP_SECURITY="tls-someday")
    assert adapters.register_smtp_from_env(env) is False
    assert adapters.registered_real_adapters() == ()


def test_username_without_password_refuses():
    env = dict(FULL_ENV, GW_SMTP_USERNAME="mailer")
    assert adapters.register_smtp_from_env(env) is False
    assert adapters.registered_real_adapters() == ()


# --- INV-5 resolution stays fail-closed with smtp registered ------------------

def test_flag_off_resolves_null_even_with_smtp_registered(acct2_conn):
    assert adapters.register_smtp_from_env(dict(FULL_ENV)) is True
    adapter = adapters.resolve_adapter(acct2_conn)
    assert adapter.name == adapters.NULL_ADAPTER_NAME


def test_flag_on_with_smtp_registered_resolves_smtp(acct2_conn):
    assert adapters.register_smtp_from_env(dict(FULL_ENV)) is True
    flags.set_flag(acct2_conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="test:GOV-1544")
    adapter = adapters.resolve_adapter(acct2_conn)
    assert isinstance(adapter, adapters.SmtpAdapter)


# --- hash-only logging --------------------------------------------------------

def test_null_adapter_logs_hash_never_address(caplog):
    with caplog.at_level(logging.INFO, logger="email_gateway"):
        adapters.NullAdapter().send(
            to_email="secret.person@example.com", subject="Your sign-in link",
            body_text="link", body_html=None)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "to_hash=" + adapters.email_hash("secret.person@example.com") in joined
    _assert_no_plaintext_email(caplog)


def test_smtp_failure_logs_hash_and_exception_type_only(caplog):
    # Port with no listener -> connection refused -> send() returns None.
    adapter = adapters.SmtpAdapter(
        host="127.0.0.1", port=_free_port(), username="", password="",
        mail_from="beta@gov-watchdog.test", security="none", timeout=2.0)
    with caplog.at_level(logging.WARNING, logger="email_gateway"):
        ref = adapter.send(to_email="secret.person@example.com",
                           subject="s", body_text="b", body_html=None)
    assert ref is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "to_hash=" in joined
    _assert_no_plaintext_email(caplog)


# --- real-handshake smoke against a loopback stdlib SMTP sink -----------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _SmtpSinkHandler(socketserver.StreamRequestHandler):
    """Just enough RFC-5321 to accept one message from smtplib."""

    def handle(self):
        def reply(line: str) -> None:
            self.wfile.write((line + "\r\n").encode("ascii"))

        reply("220 sink ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            verb = raw.decode("ascii", "replace").strip()
            upper = verb.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                reply("250 sink")
            elif upper.startswith("MAIL FROM") or upper.startswith("RCPT TO"):
                reply("250 ok")
            elif upper.startswith("DATA"):
                reply("354 go")
                lines = []
                while True:
                    body_line = self.rfile.readline()
                    if not body_line or body_line in (b".\r\n", b".\n"):
                        break
                    lines.append(body_line)
                self.server.messages.append(b"".join(lines))
                reply("250 accepted")
            elif upper.startswith("QUIT"):
                reply("221 bye")
                return
            else:
                reply("502 not implemented")


@pytest.fixture()
def smtp_sink():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SmtpSinkHandler)
    server.messages = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_smtp_adapter_delivers_via_real_handshake(smtp_sink, caplog):
    port = smtp_sink.server_address[1]
    adapter = adapters.SmtpAdapter(
        host="127.0.0.1", port=port, username="", password="",
        mail_from="beta@gov-watchdog.test", security="none", timeout=5.0)
    with caplog.at_level(logging.INFO, logger="email_gateway"):
        ref = adapter.send(to_email="allowed.user@example.com",
                           subject="Your sign-in link",
                           body_text="https://example.invalid/verify?token=x",
                           body_html=None)
    assert ref is not None and ref.startswith("<")
    assert len(smtp_sink.messages) == 1
    raw = smtp_sink.messages[0].decode("utf-8", "replace")
    assert "To: allowed.user@example.com" in raw
    assert "Subject: Your sign-in link" in raw
    _assert_no_plaintext_email(caplog)  # the WIRE carries the address; logs never do
