"""Provider-agnostic email abstraction (ACCT-2026 v0.2, GOV-754 leg 2).

NAMING NOTE (plan deviation, documented for the SecPriv/CTO gates): the plan
says ``scripts/email/``, but a package literally named ``email`` shadows the
Python STDLIB ``email`` module for every process with ``scripts/`` on
``sys.path`` — which is this whole repo. ``http.client`` does
``import email.parser`` internally, so ``requests``/``urllib`` (and with
them the frozen serving surfaces) would break at RUNTIME while staying
byte-identical. Hence ``email_gateway`` — same scope, safe name. Recorded in
the plan doc's Leg-2 implementation notes and on GOV-754.

Fail-closed activation (D1/INV-5): adapter resolution reads the LATEST
``feature_flags`` row for ``email_adapter_enabled`` (ordered
``(at_utc, flag_seq)``). No row, or latest row disabled, or no real adapter
registered → the null adapter (no-op, logging only). No env var is
authoritative — ``ENABLE_EMAIL_ADAPTER`` is dropped entirely. Activation and
deactivation each append a flag row with a non-null ``owner_decision_ref``
(an explicit Isaac board card).

Consent gate (AC-4/INV-2/INV-8): nothing enters ``email_outbox`` unless
``consent_preferences.email_consent = 1`` AND ``unsubscribe_token`` is
populated. Zero-leak (AC-1): mail bodies come only from the fixed templates
in :mod:`.templates`; civic-content templates are refused for non-approved
recipients.

Module map:
  * :mod:`.flags`     — append-only feature-flag reader/writer (D1)
  * :mod:`.adapters`  — adapter protocol, null adapter, registry, fail-closed
    resolution (INV-5, AC-5)
  * :mod:`.templates` — fixed lifecycle/civic templates + non-approved
    allowlist (AC-1 mail bodies)
  * :mod:`.outbox`    — consent-gated queue + resolve-then-send + delivery
    log (AC-4/AC-9)
"""

from __future__ import annotations

__all__ = ["flags", "adapters", "templates", "outbox"]
