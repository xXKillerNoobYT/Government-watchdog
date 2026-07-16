"""ACCT-2026 leg 2 (GOV-754): email gateway — AC-4/5/9, INV-2/5/8, AC-1 mail.

INV-5 RED-proof lives in the resolution truth-table tests: neuter the
``flags.is_enabled`` check inside ``adapters.resolve_adapter`` and
test_no_flag_row_resolves_null / test_disabled_latest_row_resolves_null go
RED (the fake "real" adapter would receive sends without an owner card).

AC-9: the only "real" adapter in this file is an in-memory list — nothing in
this suite can reach a network.
"""

from __future__ import annotations

import json

import pytest

from accounts import consent, service
from email_gateway import adapters, flags, outbox, templates
from conftest import CIVIC_MARKER


class RecordingAdapter:
    """In-memory fake 'real' adapter (AC-9: no network, ever)."""

    name = "fake-real"

    sent: list[dict] = []  # class-level so resolve_adapter's factory copy shares it

    def send(self, *, to_email, subject, body_text, body_html):
        RecordingAdapter.sent.append(
            {"to": to_email, "subject": subject, "body_text": body_text})
        return f"fake-ref-{len(RecordingAdapter.sent)}"


@pytest.fixture()
def conn(acct2_conn):
    RecordingAdapter.sent = []
    adapters.register_adapter("fake-real", RecordingAdapter)
    yield acct2_conn
    adapters.unregister_adapter("fake-real")


def _consented_user(conn, email="c@example.com"):
    uid = service.create_user(conn, email=email)
    token = consent.grant_email_consent(conn, uid)
    return uid, token


def _outbox_count(conn):
    return conn.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0]


# --- AC-4 / INV-2: consent gate before any outbox row -------------------------------

def test_no_consent_row_means_no_outbox_row(conn):
    uid = service.create_user(conn, email="n@example.com")
    with pytest.raises(outbox.ConsentMissing):
        outbox.queue_email(conn, user_id=uid, template_id="account_approved")
    assert _outbox_count(conn) == 0


def test_consent_without_unsubscribe_token_still_refused(conn):
    """Hand-desynced row (consent=1, token NULL): BOTH conditions are checked."""
    uid = service.create_user(conn, email="d@example.com")
    conn.execute(
        "INSERT INTO consent_preferences (user_id, email_consent) VALUES (?, 1)",
        (uid,))
    conn.commit()
    with pytest.raises(outbox.ConsentMissing):
        outbox.queue_email(conn, user_id=uid, template_id="account_approved")
    assert _outbox_count(conn) == 0


def test_consented_user_queues_and_body_carries_unsubscribe_token(conn):
    uid, token = _consented_user(conn)
    outbox_id = outbox.queue_email(conn, user_id=uid,
                                   template_id="account_approved")
    row = conn.execute("SELECT status, body_text FROM email_outbox"
                       " WHERE outbox_id = ?", (outbox_id,)).fetchone()
    assert row["status"] == "pending"
    assert token in row["body_text"]


# --- INV-8: unsubscribe token lifecycle ---------------------------------------------

def test_unsubscribe_token_is_generated_fresh_and_rotates(conn):
    uid, t1 = _consented_user(conn, "rot@example.com")
    assert len(t1) >= 42  # token_urlsafe(32) => 43 chars
    assert consent.unsubscribe(conn, t1) == uid
    prefs = consent.get_preferences(conn, uid)
    assert prefs["email_consent"] == 0
    t2 = consent.grant_email_consent(conn, uid)  # re-consent rotates (never reused)
    assert t2 != t1
    assert consent.unsubscribe(conn, t1) is None, "old token must be dead"


# --- INV-5 / D1: fail-closed adapter resolution -------------------------------------

def test_no_flag_row_resolves_null_even_with_real_adapter_registered(conn):
    uid, _ = _consented_user(conn)
    outbox.queue_email(conn, user_id=uid, template_id="account_approved")
    results = outbox.send_pending(conn)
    assert results[0]["adapter"] == "null"
    assert RecordingAdapter.sent == [], "real adapter reached without a flag row"
    assert conn.execute("SELECT adapter_used FROM email_outbox").fetchone()[0] == "null"


def test_disabled_latest_row_resolves_null(conn):
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="card-on")
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=False,
                   owner_decision_ref="card-off")
    uid, _ = _consented_user(conn)
    outbox.queue_email(conn, user_id=uid, template_id="account_approved")
    outbox.send_pending(conn)
    assert RecordingAdapter.sent == []


def test_enabled_latest_row_resolves_real_adapter(conn):
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="card-on")
    uid, _ = _consented_user(conn)
    outbox.queue_email(conn, user_id=uid, template_id="account_approved")
    results = outbox.send_pending(conn)
    assert results[0]["adapter"] == "fake-real"
    assert len(RecordingAdapter.sent) == 1
    log = conn.execute("SELECT event_kind, provider_ref FROM email_delivery_log"
                       ).fetchone()
    assert log["event_kind"] == "sent" and log["provider_ref"] == "fake-ref-1"


def test_flag_without_no_real_adapter_registered_falls_back_null(conn):
    adapters.unregister_adapter("fake-real")
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="card-on")
    assert adapters.resolve_adapter(conn).name == "null"


@pytest.mark.parametrize("ref", [None, ""])
def test_flag_append_requires_owner_decision_ref_both_directions(conn, ref):
    for enabled in (True, False):
        with pytest.raises(flags.OwnerlessFlagChange):
            flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=enabled,
                           owner_decision_ref=ref)
    assert conn.execute("SELECT COUNT(*) FROM feature_flags").fetchone()[0] == 0


def test_null_adapter_send_is_noop(conn):
    assert adapters.NullAdapter().send(
        to_email="x@example.com", subject="s", body_text="b",
        body_html=None) is None


# --- unsubscribe-after-queue suppression --------------------------------------------

def test_unsubscribed_since_queue_is_suppressed_not_sent(conn):
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="card-on")
    uid, token = _consented_user(conn)
    outbox.queue_email(conn, user_id=uid, template_id="account_approved")
    consent.unsubscribe(conn, token)
    results = outbox.send_pending(conn)
    assert results[0]["status"] == "suppressed"
    assert RecordingAdapter.sent == []
    assert conn.execute("SELECT event_kind FROM email_delivery_log"
                        ).fetchone()[0] == "suppressed"


# --- AC-1: zero-leak extends to mail bodies -----------------------------------------

def test_civic_template_refused_for_non_approved_recipient(conn):
    uid, _ = _consented_user(conn)  # tier: waitlisted
    with pytest.raises(outbox.ZeroLeakViolation):
        outbox.queue_email(conn, user_id=uid, template_id="civic_digest",
                           context={"digest_text": CIVIC_MARKER})
    assert _outbox_count(conn) == 0


def test_civic_template_allowed_for_approved_recipient(conn):
    uid, _ = _consented_user(conn)
    service.approve(conn, uid, owner_decision_ref="card-appr")
    outbox.queue_email(conn, user_id=uid, template_id="civic_digest",
                       context={"digest_text": "reviewed digest content"})
    assert _outbox_count(conn) == 1


def test_lifecycle_mail_bodies_carry_no_civic_marker(conn):
    uid, _ = _consented_user(conn)
    for tpl in sorted(templates.NON_APPROVED_ALLOWED):
        ctx = {"to_cohort": "beta-2"} if tpl == "cohort_advanced" else None
        outbox.queue_email(conn, user_id=uid, template_id=tpl, context=ctx)
    bodies = json.dumps([tuple(r) for r in conn.execute(
        "SELECT subject, body_text, body_html FROM email_outbox")])
    assert CIVIC_MARKER not in bodies


def test_free_form_bodies_are_not_a_thing(conn):
    uid, _ = _consented_user(conn)
    with pytest.raises(templates.UnknownTemplate):
        outbox.queue_email(conn, user_id=uid, template_id="totally-custom")
