"""Consent-gated outbox: queue -> resolve -> send -> audit (AC-4/5/9, INV-2).

Two write paths only:

* :func:`queue_email` — refuses BEFORE any row exists unless the recipient
  has ``email_consent = 1`` AND a populated ``unsubscribe_token`` (AC-4 /
  INV-2 / INV-8), and refuses civic-content templates for non-approved
  recipients (AC-1 mail bodies). Both checks read the live tables, so a
  hand-desynced row (consent flipped on with no token) still refuses.
* :func:`send_pending` — resolves the adapter through
  :func:`email_gateway.adapters.resolve_adapter` on EVERY run (INV-5
  fail-closed; there is no adapter parameter to bypass the flag), re-checks
  consent at send time (unsubscribed-since-queue rows are suppressed, not
  sent), and appends one ``email_delivery_log`` row per outcome.

Every mail body includes the recipient's unsubscribe token line — the abuse
control the 0025 consent model is built around.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from accounts import consent as consent_mod
from accounts import service as accounts_service
from email_gateway import adapters, templates


class ConsentMissing(ValueError):
    """No consent row, consent off, or unsubscribe token missing (AC-4)."""


class ZeroLeakViolation(ValueError):
    """Civic-content template addressed to a non-approved recipient (AC-1)."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _consent_ok(conn: sqlite3.Connection, user_id: str) -> bool:
    prefs = consent_mod.get_preferences(conn, user_id)
    return bool(prefs and prefs["email_consent"] == 1
                and prefs["unsubscribe_token"])


def queue_email(conn: sqlite3.Connection, *, user_id: str, template_id: str,
                context: dict | None = None) -> str:
    """Render a fixed template into ``email_outbox``; returns ``outbox_id``.

    Refusals (no row written): :class:`ConsentMissing`,
    :class:`ZeroLeakViolation`, :class:`templates.UnknownTemplate`.
    """
    if not _consent_ok(conn, user_id):
        raise ConsentMissing(user_id)
    tpl = templates.get(template_id)
    if tpl.civic and accounts_service.current_tier(conn, user_id) != "approved":
        raise ZeroLeakViolation(
            f"civic template {template_id!r} for non-approved recipient")
    subject, body_text = templates.render(template_id, context)
    prefs = consent_mod.get_preferences(conn, user_id)
    body_text += ("\n\n--\nTo stop these emails, use your unsubscribe token: "
                  f"{prefs['unsubscribe_token']}")
    outbox_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO email_outbox (outbox_id, user_id, template_id, subject,"
        " body_text, queued_utc) VALUES (?, ?, ?, ?, ?, ?)",
        (outbox_id, user_id, template_id, subject, body_text, _utcnow()),
    )
    conn.commit()
    return outbox_id


def _log(conn: sqlite3.Connection, outbox_id: str, event_kind: str,
         provider_ref: str | None) -> None:
    conn.execute(
        "INSERT INTO email_delivery_log (log_id, outbox_id, event_kind,"
        " provider_ref, recorded_utc) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), outbox_id, event_kind, provider_ref, _utcnow()),
    )


def send_pending(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    """Send (or suppress) pending outbox rows; returns per-row outcomes.

    The adapter is resolved fresh from the feature flag — callers cannot
    inject one, so a disabled/absent flag can never be bypassed (INV-5).

    **AT-MOST-ONCE per row (GOV-1676).** A send is an *irreversible outward
    side effect*: an over-admitted cohort member can be corrected in the
    database, a delivered email cannot be undelivered. So each row is CLAIMED
    and the claim COMMITTED before ``adapter.send`` is ever called, and the row
    is flipped to ``sent`` only after the adapter returns.

    The claim writes ``status = 'failed'``, which is not a placeholder — it is
    the literally true state from the moment we decide to send until we know we
    succeeded. A crash anywhere in between therefore leaves the row ``failed``
    rather than ``pending``, so nothing re-delivers it silently; an operator
    sees a failed row and decides. **For an irreversible action, a visible stuck
    row is the correct failure mode and a silent retry is not** — the same
    fail-closed reasoning that governs every other gate in this repo.

    The claim is a single conditional ``UPDATE ... WHERE status = 'pending'``,
    so two concurrent senders cannot both take a row: the loser matches zero
    rows and skips. That is the same technique the magic-code single-use guard
    uses (``WHERE consumed_utc IS NULL``) and it needs no ``BEGIN IMMEDIATE``,
    because the atomicity lives in the ``WHERE`` clause rather than in a
    transaction spanning a read and a write.

    Committing per row also means one bad row no longer discards the batch's
    earlier successes — previously every ``UPDATE`` waited on a single commit
    after the loop, so a raise on row 5 rolled back rows 1-4 whose mail had
    already gone out.
    """
    adapter = adapters.resolve_adapter(conn)
    results = []
    rows = conn.execute(
        "SELECT o.outbox_id, o.user_id, o.subject, o.body_text, o.body_html,"
        " u.email FROM email_outbox o JOIN users u ON u.user_id = o.user_id"
        " WHERE o.status = 'pending' ORDER BY o.queued_utc, o.rowid LIMIT ?",
        (limit,)).fetchall()
    for outbox_id, user_id, subject, body_text, body_html, email in rows:
        if not _consent_ok(conn, user_id):
            conn.execute(
                "UPDATE email_outbox SET status = 'suppressed',"
                " adapter_used = ? WHERE outbox_id = ? AND status = 'pending'",
                (adapter.name, outbox_id))
            _log(conn, outbox_id, "suppressed", None)
            conn.commit()
            results.append({"outbox_id": outbox_id, "status": "suppressed"})
            continue

        # CLAIM — single winner, durable BEFORE anything leaves the building.
        claimed = conn.execute(
            "UPDATE email_outbox SET status = 'failed', adapter_used = ?"
            " WHERE outbox_id = ? AND status = 'pending'",
            (adapter.name, outbox_id)).rowcount
        if claimed != 1:
            conn.rollback()  # another sender took it between SELECT and here
            results.append({"outbox_id": outbox_id,
                            "status": "skipped_not_pending"})
            continue
        conn.commit()

        try:
            provider_ref = adapter.send(to_email=email, subject=subject,
                                        body_text=body_text,
                                        body_html=body_html)
        except Exception:
            # The row is already 'failed' and already committed; record why and
            # let the caller see the error. It will NOT be re-sent silently.
            _log(conn, outbox_id, "failed", None)
            conn.commit()
            raise

        conn.execute(
            "UPDATE email_outbox SET status = 'sent', adapter_used = ?,"
            " sent_utc = ? WHERE outbox_id = ?",
            (adapter.name, _utcnow(), outbox_id))
        _log(conn, outbox_id, "sent", provider_ref)
        conn.commit()
        results.append({"outbox_id": outbox_id, "status": "sent",
                        "adapter": adapter.name})
    return results
