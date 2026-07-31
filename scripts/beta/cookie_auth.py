"""Browser-cookie identity for beta sessions (GOV-1653, issue #135).

Single authority for exactly one question:

    does this raw ``Cookie`` header carry a live, allowlisted
    ``gw_beta_session`` that maps to exactly ONE existing canonical account?

Two identity spaces meet here. :mod:`beta.sessions` speaks *normalized email*;
``notification_events`` and every other own-row surface speak
``users.user_id``. This module is the only sanctioned join between them, so
the join's rules live in one place instead of being re-derived per route:

* **Never auto-create.** A live beta session whose email has no ``users`` row
  resolves to nothing. Signing in to the beta is not account creation.
* **Never infer.** Exact normalized equality only — no aliasing, no
  plus-address folding, no domain matching, no "closest" row.
* **Never key civic reads by email.** The caller receives a ``user_id`` or
  nothing; the email never leaves this module.

Fail-closed and re-checked per request (no caching, no memoization): the beta
feature flag, the session's liveness, the allowlist, and the account mapping
are all evaluated on every call, because any of them can be revoked between
two requests and a browser cookie lives for seven days.

Every failure mode returns the same ``None``. Absent, malformed, duplicated,
unknown, expired, revoked, flag-disabled, allowlist-revoked and unmapped are
deliberately indistinguishable to the caller, so no caller can accidentally
build an enumeration oracle out of the difference.

Deliberately NOT used here: :func:`accounts.gate.guard_civic_request`. That
gate additionally requires an approved civic-data tier, which is correct for
civic records and wrong for lifecycle notifications — an *approval* or
*revocation* notice has to reach precisely the users the civic gate shuts
out. Session authentication is the bar; tier is not.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from beta import allowlist, sessions

# Kept in step with ``beta.http_api.COOKIE_NAME`` by an explicit test rather
# than by importing it — importing the transport module into an auth helper
# would invert the dependency (transport should depend on auth, not the
# reverse) and drag the HTTP server into every caller's import graph.
COOKIE_NAME = "gw_beta_session"

BETA_GATE_FLAG = "beta_gate_enabled"

# ``beta.common.new_raw_token`` is ``secrets.token_urlsafe(32)``: base64url,
# unpadded. Anything outside that alphabet was not minted by us, so it is
# rejected before it can reach a database lookup. ``test_cookie_auth`` asserts
# a freshly minted real token satisfies this, so a future change to the token
# generator fails loudly here instead of silently locking every browser out.
_WELL_FORMED_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class CookieScan:
    """Result of looking for exactly one ``gw_beta_session`` in a header.

    Three outcomes, and the caller must distinguish them because they are not
    interchangeable at the credential-arbitration layer:

    * ``token`` set          — exactly one well-formed candidate value.
    * ``invalid`` True       — the cookie is PRESENT but unusable (repeated
      under the same name, or a value we did not mint). This must deny even
      when a valid bearer accompanies it: a request carrying a broken
      credential is ambiguous, not merely unauthenticated.
    * neither (``absent``)   — no ``gw_beta_session`` at all. Unrelated
      cookies land here, so an ordinary browser cookie jar never disturbs a
      bearer-authenticated non-browser client.
    """

    token: str | None = None
    invalid: bool = False

    @property
    def absent(self) -> bool:
        return self.token is None and not self.invalid


def scan_session_cookie(cookie_header: str | None) -> CookieScan:
    """Find the single ``gw_beta_session`` value in one Cookie header.

    Parsed by hand rather than with :class:`http.cookies.SimpleCookie`, which
    resolves a repeated name to the LAST occurrence silently. Silent last-wins
    is precisely the behavior issue #135 requires us to reject: two values
    under one name is an ambiguous credential, and picking one of them is a
    guess.
    """
    if not cookie_header:
        return CookieScan()

    values: list[str] = []
    for part in cookie_header.split(";"):
        name, sep, value = part.partition("=")
        if not sep:
            continue  # a bare cookie-name carries no credential
        if name.strip() != COOKIE_NAME:
            continue
        values.append(value.strip())

    if not values:
        return CookieScan()
    if len(values) > 1:
        return CookieScan(invalid=True)  # repeated name -> ambiguous

    token = values[0]
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        token = token[1:-1]  # RFC 6265 quoted-string form
    if not _WELL_FORMED_TOKEN.fullmatch(token):
        return CookieScan(invalid=True)  # empty or not from our alphabet
    return CookieScan(token=token)


def resolve_token_user_id(conn: sqlite3.Connection, raw_token: str, *,
                          now: datetime | None = None) -> str | None:
    """Raw cookie value -> one ``users.user_id``, or None.

    The four gates run in cheapest-first order, and all four are re-evaluated
    per request:

    1. ``beta_gate_enabled`` — while the beta is off, a cookie minted when it
       was on must stop working. Absent row means off (fail closed).
    2. the session row is live — unknown, expired and revoked are one answer.
    3. the email is still allowlisted — admission is revocable, and a
       seven-day cookie outlives a revocation by default.
    4. exactly one existing account carries that normalized email.
    """
    from email_gateway import flags  # deferred: keep this module import-leaf

    if not raw_token:
        return None
    if not flags.is_enabled(conn, BETA_GATE_FLAG):
        return None
    email = sessions.verify(conn, raw_token, now=now)
    if email is None:
        return None
    if not allowlist.is_allowed(conn, email):
        return None

    from accounts import service as accounts_service  # deferred, same reason

    return accounts_service.find_user_by_email(conn, email)


def resolve_user_id(conn: sqlite3.Connection, cookie_header: str | None, *,
                    now: datetime | None = None) -> str | None:
    """Convenience: scan a Cookie header and resolve it in one step.

    A caller that must distinguish *absent* from *invalid* — anything doing
    credential arbitration against a bearer token — should call
    :func:`scan_session_cookie` itself; both collapse to None here.
    """
    scan = scan_session_cookie(cookie_header)
    if scan.token is None:
        return None
    return resolve_token_user_id(conn, scan.token, now=now)
