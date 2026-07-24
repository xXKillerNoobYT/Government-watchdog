"""Beta transactional email: magic_link + waitlist_confirmation (GOV-801).

Bodies are rendered ONLY from the two fixed templates registered in
:mod:`email_gateway.templates` (the repo's one email-template registry; the
``notifications`` package the ticket names is in-app-only and forbids importing
email — reconciliation recorded on GOV-801). No free-form subject/body exists.

Delivery routes through :func:`email_gateway.adapters.resolve_adapter`, which
is fail-closed: with no real adapter registered and the ``email_adapter_enabled``
flag off — the shipped state — every send is the null adapter (logged, nothing
leaves the machine). This matches the standing owner gate: public deploy / email
spend is NOT authorized until a later Isaac card.

The beta flow has no ``users`` row and no ``consent_preferences`` row, so it does
NOT use the consent-gated :mod:`email_gateway.outbox` (which joins ``users``);
it renders and hands straight to the resolved adapter.
"""

from __future__ import annotations

import sqlite3

from email_gateway import adapters, templates


def send_magic_link(conn: sqlite3.Connection, email: str, *,
                    verify_url: str, code: str) -> str | None:
    """Render + send the one-time sign-in link AND code. Null adapter until on.

    One email carries both credentials (GOV-1538): the tappable link and the
    6-digit code fallback used until the Phase-3 domain can serve an AASA file
    for universal links.
    """
    subject, body_text = templates.render(
        "magic_link", {"verify_url": verify_url, "code": code})
    return adapters.resolve_adapter(conn).send(
        to_email=email, subject=subject, body_text=body_text, body_html=None)


def send_waitlist_confirmation(conn: sqlite3.Connection,
                               email: str) -> str | None:
    """Render + send the waitlist acknowledgement. Null adapter until enabled."""
    subject, body_text = templates.render("waitlist_confirmation")
    return adapters.resolve_adapter(conn).send(
        to_email=email, subject=subject, body_text=body_text, body_html=None)
