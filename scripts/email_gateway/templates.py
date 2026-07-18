"""Fixed email templates + the non-approved allowlist (AC-1 mail bodies).

Mail bodies are rendered ONLY from the templates below — the outbox accepts
no free-form subject/body. That makes the zero-leak property structural:
civic data can reach ``email_outbox.body_text``/``body_html`` only through a
template that carries it, and every such template is marked ``civic=True``
and refused for non-approved recipients (and for anyone without consent,
like all mail).

Lifecycle templates carry account-state strings only. Context values are
interpolated with ``str.format``; unknown/missing keys raise, extra keys are
ignored.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailTemplate:
    template_id: str
    subject: str
    body_text: str
    civic: bool  # True => may carry civic data => approved recipients only


_TEMPLATES = {
    t.template_id: t for t in (
        EmailTemplate(
            "waitlist_received",
            "Government Watchdog: waitlist request received",
            "We received your beta waitlist request. You will hear from us "
            "after an owner review. No action is needed.",
            civic=False),
        EmailTemplate(
            "account_approved",
            "Government Watchdog: your account is approved",
            "Your account has been approved for beta access. You can now "
            "sign in.",
            civic=False),
        EmailTemplate(
            "account_revoked",
            "Government Watchdog: your access was revoked",
            "Your beta access has been revoked. Reply to this address if "
            "you believe this is an error.",
            civic=False),
        EmailTemplate(
            "cohort_advanced",
            "Government Watchdog: cohort update",
            "Your account was added to cohort {to_cohort}.",
            civic=False),
        EmailTemplate(
            "consent_recorded",
            "Government Watchdog: email preference recorded",
            "Your email consent preference was recorded.",
            civic=False),
        EmailTemplate(
            "unsubscribe_confirmed",
            "Government Watchdog: unsubscribed",
            "You have been unsubscribed. No further email will be sent.",
            civic=False),
        # GOV-801 gated-beta front door. Both carry account-state strings only
        # (no civic data => civic=False). magic_link interpolates a one-time
        # sign-in URL that expires in 15 minutes; the beta mailer sends both
        # through the fail-closed null adapter until an owner enables a real one.
        EmailTemplate(
            "magic_link",
            "Government Watchdog: your beta sign-in link",
            "Use this one-time link to sign in to the Government Watchdog "
            "beta. It expires in 15 minutes and works only once:\n\n"
            "{verify_url}\n\n"
            "If you did not request this, you can ignore this email.",
            civic=False),
        EmailTemplate(
            "waitlist_confirmation",
            "Government Watchdog: you're on the beta waitlist",
            "Thanks for your interest in the Government Watchdog beta. "
            "You're on the waitlist. We'll email you if a spot opens after "
            "an owner review. No action is needed.",
            civic=False),
        # Civic-content template shape for FUTURE digest cards. Approved
        # recipients only (AC-1); its existence lets tests pin the refusal.
        EmailTemplate(
            "civic_digest",
            "Government Watchdog: civic digest",
            "Civic digest:\n{digest_text}",
            civic=True),
    )
}

#: Template ids a NON-approved recipient may ever receive (AC-1 mail bodies).
NON_APPROVED_ALLOWED = frozenset(
    t.template_id for t in _TEMPLATES.values() if not t.civic)


class UnknownTemplate(KeyError):
    """template_id not defined here — free-form mail is not a thing."""


def get(template_id: str) -> EmailTemplate:
    try:
        return _TEMPLATES[template_id]
    except KeyError:
        raise UnknownTemplate(template_id) from None


def render(template_id: str, context: dict | None = None) -> tuple[str, str]:
    """Returns ``(subject, body_text)``; raises on unknown template/keys."""
    tpl = get(template_id)
    ctx = context or {}
    return tpl.subject.format(**ctx), tpl.body_text.format(**ctx)
