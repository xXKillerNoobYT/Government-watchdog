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

import db
from accounts import consent, service
from email_gateway import adapters, flags, outbox, templates
from conftest import CIVIC_MARKER, ROOT

ROOT_SCRIPTS = ROOT / "scripts"


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
    ctx_by_tpl = {"cohort_advanced": {"to_cohort": "beta-2"},
                  "magic_link": {"verify_url": "http://127.0.0.1:8801/x",
                                 "code": "004217"}}
    for tpl in sorted(templates.NON_APPROVED_ALLOWED):
        outbox.queue_email(conn, user_id=uid, template_id=tpl,
                           context=ctx_by_tpl.get(tpl))
    bodies = json.dumps([tuple(r) for r in conn.execute(
        "SELECT subject, body_text, body_html FROM email_outbox")])
    assert CIVIC_MARKER not in bodies


def test_free_form_bodies_are_not_a_thing(conn):
    uid, _ = _consented_user(conn)
    with pytest.raises(templates.UnknownTemplate):
        outbox.queue_email(conn, user_id=uid, template_id="totally-custom")


# --- GOV-1673 (C1b): INV-5's "no env var is authoritative" half --------------

def test_env_var_cannot_override_the_database_flag(conn, monkeypatch):
    """D1/INV-5: `ENABLE_EMAIL_ADAPTER` was dropped ENTIRELY, not deprecated.

    The flag tests prove the DB row decides correctly. None of them proves an
    env var CANNOT decide instead — so re-introducing the override would leave
    the suite green while reversing an explicit CTO decision: *one source of
    truth; an env var cannot carry who/when/which-card.*

    With no flag row and a real adapter registered, the env var set to every
    plausible truthy spelling must still resolve to the null adapter.
    """
    adapters.register_adapter("envtest", RecordingAdapter)
    try:
        for value in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("ENABLE_EMAIL_ADAPTER", value)
            resolved = adapters.resolve_adapter(conn)
            assert isinstance(resolved, adapters.NullAdapter), (
                f"ENABLE_EMAIL_ADAPTER={value!r} reached the resolver")
    finally:
        adapters.unregister_adapter("envtest")


def test_no_module_reads_the_dropped_env_var():
    """The source half: nothing may READ it, even inertly.

    A read that currently changes no behaviour is how the override comes back —
    the next edit wires it to something. Matched only where the name appears
    alongside an environment lookup, so `email_gateway/__init__.py`'s docstring
    (which states the variable is dropped) does not trip the guard it describes.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "scripts"
    reader = re.compile(r"(os\.environ|getenv|environ\.get)[^\n]*ENABLE_EMAIL_ADAPTER"
                        r"|ENABLE_EMAIL_ADAPTER[^\n]*(os\.environ|getenv|environ\.get)")

    offenders = [str(m.relative_to(root)) for m in sorted(root.rglob("*.py"))
                 if reader.search(m.read_text(encoding="utf-8"))]
    assert offenders == [], offenders

    # Non-vacuous: the name must still be PRESENT somewhere (the docstring that
    # records the decision), so deleting all mention of it fails instead of
    # silently making this guard meaningless.
    mentioned = [m for m in root.rglob("*.py")
                 if "ENABLE_EMAIL_ADAPTER" in m.read_text(encoding="utf-8")]
    assert mentioned, ("no module mentions ENABLE_EMAIL_ADAPTER any more — if D1 "
                       "was reversed, update INV-5 and delete this test rather "
                       "than leaving it to pass vacuously")


# --- GOV-1676 (C7b): at-most-once delivery ------------------------------------
#
# Found by the C7b hunting mandate. `send_pending` used to call `adapter.send()`
# -- an irreversible outward side effect -- and commit the resulting 'sent'
# status only after the WHOLE batch loop. Measured on main @ 5076e83, with two
# emails confirmed delivered (the adapter returned normally) and then a crash:
#
#     what the database survived with: {'pending': 3}
#     delivered AGAIN on restart: ['u0@...', 'u1@...', 'u2@...']
#     *** 2 recipient(s) got the SAME email twice ***
#
# An over-admitted cohort member can be corrected in the database. A delivered
# email cannot be undelivered, which is what makes this worse than #128.


class CrashingAdapter:
    """Delivers normally until ``fail_after`` sends, then raises."""

    name = "fake-real"
    sent: list[str] = []
    fail_after = 10_000

    def send(self, *, to_email, subject, body_text, body_html):
        if len(CrashingAdapter.sent) >= CrashingAdapter.fail_after:
            raise RuntimeError("SMTP dropped / process died")
        CrashingAdapter.sent.append(to_email)
        return f"crash-ref-{len(CrashingAdapter.sent)}"


@pytest.fixture()
def crashing(conn):
    """Swap the recording adapter for one that can fail mid-batch."""
    adapters.unregister_adapter("fake-real")
    CrashingAdapter.sent = []
    CrashingAdapter.fail_after = 10_000
    adapters.register_adapter("fake-real", CrashingAdapter)
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="card-on")
    yield CrashingAdapter
    adapters.unregister_adapter("fake-real")


def _db_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()[2]


def _queue(conn, n):
    for i in range(n):
        uid, _ = _consented_user(conn, email=f"batch{i}@example.com")
        outbox.queue_email(conn, user_id=uid, template_id="account_approved")


def test_crash_after_delivery_does_not_resend_on_restart(conn, crashing):
    """The headline: a confirmed delivery is never delivered a second time."""
    import db as db_mod

    _queue(conn, 3)
    crashing.fail_after = 2                      # rows 0 and 1 really go out
    with pytest.raises(RuntimeError):
        outbox.send_pending(conn)
    delivered = list(crashing.sent)
    assert len(delivered) == 2, "precondition: two confirmed deliveries"

    path = _db_path(conn)
    conn.close()                                 # the process dies

    restarted = db_mod.open_db(path)             # fresh process, fresh connection
    try:
        crashing.fail_after = 10_000             # the transient fault is over
        outbox.send_pending(restarted)
        assert crashing.sent[2:] == [], (
            "a row whose send already succeeded was delivered again; "
            f"full delivery log: {crashing.sent}")
        # Nothing is left 'pending' to be picked up by a later sweep either.
        assert restarted.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE status = 'pending'"
        ).fetchone()[0] == 0
    finally:
        restarted.close()


def test_a_crashed_send_lands_on_failed_not_pending(conn, crashing):
    """Fail-closed: an unknown outcome must be visible, never silently retried."""
    _queue(conn, 1)
    crashing.fail_after = 0
    with pytest.raises(RuntimeError):
        outbox.send_pending(conn)
    status = conn.execute("SELECT status FROM email_outbox").fetchone()[0]
    assert status == "failed", (
        "a send with an unknown outcome must not return to 'pending' — that is "
        "exactly what re-delivers it")
    assert conn.execute(
        "SELECT event_kind FROM email_delivery_log").fetchone()[0] == "failed"


def test_one_failing_row_does_not_discard_earlier_successes(conn, crashing):
    """Previously every UPDATE waited on one commit after the loop."""
    _queue(conn, 3)
    crashing.fail_after = 2
    with pytest.raises(RuntimeError):
        outbox.send_pending(conn)
    sent = conn.execute(
        "SELECT COUNT(*) FROM email_outbox WHERE status = 'sent'").fetchone()[0]
    assert sent == 2, (
        "the two rows that were successfully delivered must stay recorded as "
        f"sent; got {sent}")


def test_row_claimed_by_another_sender_between_select_and_claim_is_skipped(
        conn, crashing, monkeypatch):
    """The claim window, exercised deterministically.

    An earlier draft of this test just called `send_pending` twice in sequence
    and asserted nothing was sent twice. That proves nothing: the second call's
    SELECT filters on `status = 'pending'` and finds an empty queue, so the
    claim is never contended and the test would pass with the `AND status =
    'pending'` guard deleted.

    The window that actually matters is between the batch SELECT (which runs in
    autocommit, holding no lock) and the claim UPDATE. This steals the row
    inside that window — which is precisely what a second concurrent sender
    does — and asserts the loser skips instead of delivering.
    """
    _queue(conn, 1)
    outbox_id = conn.execute("SELECT outbox_id FROM email_outbox").fetchone()[0]
    real_consent_ok = outbox._consent_ok

    def steal_the_row(c, user_id):
        # Runs after the SELECT saw the row as pending, before we claim it.
        conn.execute("UPDATE email_outbox SET status = 'failed'"
                     " WHERE outbox_id = ?", (outbox_id,))
        conn.commit()
        return real_consent_ok(c, user_id)

    monkeypatch.setattr(outbox, "_consent_ok", steal_the_row)
    results = outbox.send_pending(conn)

    assert crashing.sent == [], (
        "a row already claimed by another sender was delivered anyway — the "
        "claim is not single-winner")
    assert results == [{"outbox_id": outbox_id, "status": "skipped_not_pending"}]


def test_hard_process_death_mid_send_leaves_the_claim_committed(conn, tmp_path):
    """The one line an in-process test cannot cover, covered.

    RED-PROOF NOTE. Deleting the `conn.commit()` that follows the claim left all
    other tests in this file green. That is not because the line is redundant —
    it is because every *in-process* failure path reaches one of the later
    per-row commits, which flush the claim as a side effect. The line is
    load-bearing only when the process dies **during** `adapter.send()` with no
    unwinding at all: an OOM kill, a deploy restart, a power loss, a long SMTP
    timeout hit by a supervisor.

    `os._exit` reproduces exactly that — it skips atexit hooks, garbage
    collection and sqlite3's own connection teardown, so an uncommitted claim is
    simply lost. Run in a subprocess because it terminates the interpreter.
    """
    import subprocess
    import sys as _sys

    db_file = tmp_path / "hardkill.db"
    child = f'''
import sys
sys.path.insert(0, {str(ROOT_SCRIPTS)!r})
import os
import db
from accounts import consent, service
from email_gateway import adapters, flags, outbox


class DiesMidSend:
    name = "fake-real"

    def send(self, **kw):
        os._exit(9)          # hard death: no unwind, no teardown, no rollback


conn = db.open_db({str(db_file)!r})
flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
               owner_decision_ref="card-on")
adapters.register_adapter("fake-real", DiesMidSend)
uid = service.create_user(conn, email="hardkill@example.com")
consent.grant_email_consent(conn, uid)
outbox.queue_email(conn, user_id=uid, template_id="account_approved")
outbox.send_pending(conn)
'''
    db.apply_migrations(db_file)
    proc = subprocess.run([_sys.executable, "-c", child], capture_output=True)
    assert proc.returncode == 9, (
        f"child did not die where expected: rc={proc.returncode} "
        f"stderr={proc.stderr.decode()[-500:]}")

    after = db.open_db(db_file)
    try:
        status = after.execute("SELECT status FROM email_outbox").fetchone()[0]
    finally:
        after.close()
    assert status == "failed", (
        "the claim was not durable when the process died mid-send: the row is "
        f"{status!r}, so the next sweep would deliver it a second time")
