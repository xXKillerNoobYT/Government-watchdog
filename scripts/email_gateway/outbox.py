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
    """
    adapter = adapters.resolve_adapter(conn)
    results = []
    rows = conn.execute(
        "SELECT o.outbox_id, o.user_id, o.subject, o.body_text, o.body_html,"
        " u.email FROM email_outbox o JOIN users u ON u.user_id = o.user_id"
        " WHERE o.status = 'pending' ORDER BY o.queued_utc, o.rowid LIMIT ?",
        (limit,)).fetchall()
    now = _utcnow()
    for outbox_id, user_id, subject, body_text, body_html, email in rows:
        if not _consent_ok(conn, user_id):
            conn.execute(
                "UPDATE email_outbox SET status = 'suppressed',"
                " adapter_used = ? WHERE outbox_id = ?",
                (adapter.name, outbox_id))
            _log(conn, outbox_id, "suppressed", None)
            results.append({"outbox_id": outbox_id, "status": "suppressed"})
            continue
        provider_ref = adapter.send(to_email=email, subject=subject,
                                    body_text=body_text, body_html=body_html)
        conn.execute(
            "UPDATE email_outbox SET status = 'sent', adapter_used = ?,"
            " sent_utc = ? WHERE outbox_id = ?", (adapter.name, now, outbox_id))
        _log(conn, outbox_id, "sent", provider_ref)
        results.append({"outbox_id": outbox_id, "status": "sent",
                        "adapter": adapter.name})
    conn.commit()
    return results
