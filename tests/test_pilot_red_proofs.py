"""PILOT-2026 §5.3 test 3: RED-proofs reusing the merged fail-closed patterns.

Each probe drives the REAL guard (not a mock) and asserts the fail-closed
evidence landed in the substrate: send-without-consent hard-fails (AM-5), a
revoked grant is denied (AM-2 family), a synthetic budget breach pauses + emits
an event (AM-4), and the redaction tripwire denies (AM-9 family). GOV-781.
"""

from __future__ import annotations

import pytest

from accounts import service as accounts_service
from email_gateway import outbox as email_outbox


# --- AM-5: send-without-consent hard-fails ------------------------------------

def test_no_consent_send_hard_fails(pilot_conn):
    user_id = accounts_service.create_user(pilot_conn, email="noconsent@example.invalid")
    with pytest.raises(email_outbox.ConsentMissing):
        email_outbox.queue_email(pilot_conn, user_id=user_id,
                                 template_id="consent_recorded")
    # No outbox row was written (refusal is BEFORE the insert).
    n = pilot_conn.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0]
    assert n == 0


def test_run_records_no_consent_refusals(pilot_applied):
    _, rep = pilot_applied
    assert rep["counts"]["WL-5"]["no_consent_refused"] == 2


# --- AM-4: synthetic budget breach pauses + emits an event --------------------

def test_budget_breach_pauses_and_logs(pilot_applied):
    conn, _ = pilot_applied
    row = conn.execute(
        "SELECT paused_at FROM mcp_budgets WHERE budget_id = 'budget-pilot-breach-probe'"
    ).fetchone()
    assert row is not None and row[0], "breach probe budget must be paused (D3/AM-4)"
    events = conn.execute(
        "SELECT COUNT(*) FROM mcp_budget_events WHERE event_kind = 'breach'"
        " AND budget_id = 'budget-pilot-breach-probe'").fetchone()[0]
    assert events >= 1
    # AM-4 also enqueues a bounded, throttled outbox notice.
    outbox = conn.execute(
        "SELECT COUNT(*) FROM paperclip_outbox WHERE kind = 'mcp-budget-breach'"
    ).fetchone()[0]
    assert outbox >= 1


# --- AM-2 family: a revoked grant is denied ------------------------------------

def test_revoked_grant_denied_and_audited(pilot_applied):
    conn, rep = pilot_applied
    revoked = conn.execute(
        "SELECT COUNT(*) FROM mcp_capability_grants WHERE revoked = 1").fetchone()[0]
    assert revoked >= 1
    # The revocation probe produced a capability deny audit row in the window.
    denies = conn.execute(
        "SELECT COUNT(*) FROM mcp_audit_events WHERE outcome = 'deny'"
        " AND error_code = 'denied:capability' AND substr(created_at,1,7) = ?",
        (rep["period"],)).fetchone()[0]
    assert denies >= 1


# --- AM-9 family: redaction tripwire denies (frozen scanner fires) -------------

def test_redaction_tripwire_denied_and_audited(pilot_applied):
    conn, rep = pilot_applied
    redaction = conn.execute(
        "SELECT COUNT(*) FROM mcp_audit_events WHERE error_code = 'denied:redaction'"
        " AND substr(created_at,1,7) = ?", (rep["period"],)).fetchone()[0]
    assert redaction == 1
