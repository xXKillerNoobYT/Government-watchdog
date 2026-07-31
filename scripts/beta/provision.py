"""Bridge the beta front door to the accounts lane (GOV-1663, resolves #192).

The two access lanes used to be disjoint: the beta gate decided who may *sign
in*, the accounts lane decided who may *read civic data*, and nothing joined
them — so a session obtained through the only HTTP login the system exposes
could never authorize a civic-data read (``Docs/gov801-access-gate-contract.md``
§7). Owner decision 2026-07-30: the beta front door **provisions** an accounts
row on first verified sign-in, and ``accounts.gate`` stays the single civic
gate. One owner decision admits a person in both lanes; one revocation lever.

**Passwordless by owner direction (2026-07-30).** The provisioned user carries
``password_hash IS NULL``. That is not a gap to fill later — it is the posture:
``accounts.service.login`` refuses a NULL-password row with the same constant
``LoginFailed`` every other failure raises (``service.py:183``), so a
provisioned account cannot be password-logged-into at all. The magic link is
the only credential until the owner says otherwise.

**The authority is the beta allowlist's own ``owner_decision_ref``**, carried
verbatim onto the ``access_grants`` row. Nothing here mints a synthetic
approval: no active allowlist row means no ref, and no ref means no grant —
``access_grants``' schema CHECK would reject an ownerless 'approved' anyway
(0025 §3), so this is defence in depth rather than a single gate.

**This function never resurrects a closed account.** If the accounts tier is
already ``revoked`` or ``paused``, signing in through the beta door leaves it
exactly so. The two lanes deny independently and either one is sufficient: an
owner who revokes in the accounts lane is not undone by an allowlist row
somebody forgot to revoke. That asymmetry — provisioning may only ever open a
door that was never opened, never reopen one that was shut — is the security
property this module exists to hold, and it is what
``tests/test_gov1663_beta_account_provisioning.py`` pins hardest.

No new audit event: ``audit.EVENTS`` and the ``beta_audit_log`` CHECK enum are
a matched pair (see #193) that a new event would desynchronize, and the
append-only ``access_grants`` row — carrying who approved, when, and under
which decision — is already the better trail for an authorization change.
"""

from __future__ import annotations

import sqlite3

from beta import allowlist

#: Tiers a beta sign-in must never overwrite. An owner (or a reviewer) closed
#: this account in the accounts lane; the beta door does not reopen it.
CLOSED_TIERS = frozenset({"revoked", "paused"})

#: Recorded on the grant so the row says where the approval came from.
GRANT_NOTE = "provisioned by beta magic-link sign-in (GOV-1663)"


def provision_account(conn: sqlite3.Connection, email: str) -> str | None:
    """Ensure ``email`` has an approved accounts row; returns its ``user_id``.

    Returns the ``user_id`` in every case where a user exists or was created —
    including the cases where no grant was written — so a caller can correlate
    the two lanes. Returns ``None`` only when the address cannot become an
    account at all (``accounts.service`` rejects it as invalid).

    Idempotent: signing in ten times appends at most one 'approved' grant.
    """
    # Deferred, matching notifications/service.py:117 — keeps `beta` a leaf at
    # import time so the package can be vendored without dragging accounts in.
    from accounts import service as accounts_service

    try:
        user_id = accounts_service.find_user_by_email(conn, email)
        if user_id is None:
            user_id = accounts_service.create_user(conn, email=email,
                                                   password=None)
    except accounts_service.DuplicateEmail:
        # Lost a create race with a concurrent sign-in; the winner's row is
        # authoritative and equivalent, so adopt it rather than failing.
        user_id = accounts_service.find_user_by_email(conn, email)
        if user_id is None:  # pragma: no cover - only if the row vanished
            return None
    except ValueError:
        return None  # accounts rejects the address; no account is possible

    tier = accounts_service.current_tier(conn, user_id)
    if tier in CLOSED_TIERS:
        return user_id  # closed in the accounts lane; never reopened here
    if tier == "approved":
        return user_id  # already approved; no duplicate grant

    decision_ref = allowlist.decision_ref(conn, email)
    if decision_ref is None:
        return user_id  # not allowlist-active: fail closed, no grant written

    accounts_service.approve(conn, user_id, owner_decision_ref=decision_ref,
                             note=GRANT_NOTE)
    return user_id
