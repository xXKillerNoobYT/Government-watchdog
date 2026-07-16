"""In-app notification package (ACCT-2026 v0.2, GOV-721 plan / GOV-754 leg 2).

An additive leaf package — stdlib only, writes/reads only the 0025
``notification_events`` table. It NEVER imports the accounts or email
packages (they import *it*), and never touches the four frozen serving
surfaces (AC-8/INV-1).

Notification bodies are account-lifecycle strings composed here from fixed
templates — no civic data (statements, segments, sources) ever flows into
``notification_events.body_text``, so pending/revoked users reading their own
notifications cannot leak civic content (AC-1 posture).

Module map:
  * :mod:`.service` — writer (:func:`service.record` + the five AC-6
    lifecycle emitters), reader (:func:`service.query`), and the
    session-authenticated query endpoint (:func:`service.query_for_token`).
"""

from __future__ import annotations

__all__ = ["service"]
