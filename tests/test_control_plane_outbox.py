"""GOV-733 CTRL-2026 — outbox transactionality, idempotency, whitelist (AC-6).

Proves a rolled-back state change leaves no outbox row, relay re-runs are
idempotent, and safe_summary structurally excludes payload/source-text/PII/
reviewer-note fields.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import event_envelope as ee  # noqa: E402
import job_queue as jq  # noqa: E402
import paperclip_outbox as outbox  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "ctrl.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    c.execute(
        "INSERT INTO webhook_sources (source_key, secret_ref, active, created_at) "
        "VALUES ('s', 'GW_SECRET_S', 1, '2026-07-15T00:00:00.000+00:00')"
    )
    c.commit()
    yield c
    c.close()


def _dead_letter_job(conn, *, commit):
    r = ee.insert_envelope(
        conn, source_key="s", event_kind="k", source_ref="ref",
        content_sha256="a" * 64, policy_version="p", payload={"x": 1}, area_id="AREA-1",
    )
    jid = jq.enqueue_job(conn, envelope_id=r.envelope_id, lane="noop_synthetic",
                         area_id="AREA-1", max_attempts=1)
    conn.commit()
    now = "2026-07-15T00:00:00.000+00:00"
    jq.lease_job(conn, jid, "w", now=now)
    jq.record_failure(conn, jid, error="boom", now=now)  # -> dead_letter + outbox row
    if commit:
        conn.commit()
    return jid


# --- AC-6: same-transaction atomicity ---------------------------------------

def test_rollback_leaves_no_outbox_row(conn):
    jid = _dead_letter_job(conn, commit=False)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM paperclip_outbox").fetchone()[0] == 0
    # the dead_letter transition rolled back too — job is back to leased/queued
    state = conn.execute("SELECT state FROM event_jobs WHERE job_id=?", (jid,)).fetchone()
    assert state is None or state["state"] != "dead_letter"


def test_commit_persists_exactly_one_outbox_row(conn):
    _dead_letter_job(conn, commit=True)
    assert conn.execute("SELECT COUNT(*) FROM paperclip_outbox").fetchone()[0] == 1


# --- idempotency -------------------------------------------------------------

def test_write_outbox_row_idempotent_on_dedupe_key(conn):
    kw = dict(kind="dead_letter", dedupe_key="outbox:dead-letter:l:AREA-1:2026-07-15",
              umbrella_key="umbrella:dead-letter:2026-07-15",
              summary={"kind": "dead_letter", "lane": "l"})
    first = outbox.write_outbox_row(conn, **kw)
    second = outbox.write_outbox_row(conn, **kw)
    conn.commit()
    assert first is not None
    assert second is None  # INSERT OR IGNORE — no duplicate
    assert conn.execute("SELECT COUNT(*) FROM paperclip_outbox").fetchone()[0] == 1


def test_relay_is_idempotent_across_reruns(conn):
    outbox.write_outbox_row(
        conn, kind="dead_letter", dedupe_key="k1",
        umbrella_key="u1", summary={"kind": "dead_letter", "count": 1},
    )
    conn.commit()
    calls = []

    def transport(method, url, body):
        calls.append((method, url))
        return {"ref": "GOV-999"}

    first = outbox.relay(conn, apply=True, transport=transport)
    second = outbox.relay(conn, apply=True, transport=transport)  # nothing pending now
    assert first["delivered"] == 1
    assert second["delivered"] == 0
    assert len(calls) == 1  # only the first run posted
    row = conn.execute("SELECT state, paperclip_ref FROM paperclip_outbox").fetchone()
    assert row["state"] == "delivered"
    assert row["paperclip_ref"] == "GOV-999"


def test_relay_dry_run_mutates_nothing(conn):
    outbox.write_outbox_row(conn, kind="dead_letter", dedupe_key="k1",
                            umbrella_key="u1", summary={"kind": "dead_letter"})
    conn.commit()
    plan = outbox.relay(conn, apply=False)
    assert plan["applied"] is False
    assert conn.execute(
        "SELECT COUNT(*) FROM paperclip_outbox WHERE state='pending'"
    ).fetchone()[0] == 1


def test_relay_groups_by_umbrella(conn):
    for i in range(3):
        outbox.write_outbox_row(conn, kind="dead_letter", dedupe_key=f"a{i}",
                                umbrella_key="U-A", summary={"count": i})
    outbox.write_outbox_row(conn, kind="dead_letter", dedupe_key="b0",
                            umbrella_key="U-B", summary={"count": 9})
    conn.commit()
    calls = []
    outbox.relay(conn, apply=True, transport=lambda m, u, b: calls.append(u) or {"ref": "x"})
    # one post per umbrella, not per row (flood bound)
    assert len(calls) == 2


# --- AC-6: whitelist serializer (negative test) -----------------------------

def test_safe_summary_drops_unsafe_fields():
    dirty = {
        "kind": "dead_letter", "lane": "noop_synthetic", "area_id": "AREA-1",
        "job_id": 7, "attempt_count": 5, "day": "2026-07-15",
        # everything below must be structurally absent:
        "canonical_payload": '{"secret":"x"}',
        "source_text": "the mayor said ...",
        "payload": {"raw": "data"},
        "pii": "jane@example.com",
        "reviewer_note": "internal reviewer comment",
        "body": "raw body bytes",
    }
    clean = outbox.safe_summary(dirty)
    assert clean == {
        "kind": "dead_letter", "lane": "noop_synthetic", "area_id": "AREA-1",
        "job_id": 7, "attempt_count": 5, "day": "2026-07-15",
    }
    for forbidden in ("canonical_payload", "source_text", "payload", "pii",
                      "reviewer_note", "body"):
        assert forbidden not in clean


def test_safe_summary_drops_nonscalar_even_if_whitelisted():
    # an allow-listed key smuggling a nested object is dropped (could carry text)
    clean = outbox.safe_summary({"lane": {"nested": "text"}, "count": 3})
    assert clean == {"count": 3}


def test_persisted_summary_contains_no_raw_text(conn):
    _dead_letter_job(conn, commit=True)
    raw = conn.execute("SELECT safe_summary FROM paperclip_outbox").fetchone()[0]
    for forbidden in ("canonical_payload", "source_text", "reviewer_note", "payload"):
        assert forbidden not in raw


# --- report (LED-6) ----------------------------------------------------------

def test_report_counts_dedupe_and_dead_letters(conn):
    _dead_letter_job(conn, commit=True)
    # add a replayed envelope to exercise dedupe-hit-rate
    for _ in range(2):
        ee.insert_envelope(conn, source_key="s", event_kind="k2", source_ref="r2",
                           content_sha256="b" * 64, policy_version="p",
                           payload={"y": 1}, area_id="AREA-1")
    conn.commit()
    rep = outbox.report(conn)
    assert rep["dedupe_hit_count"] == 1
    assert rep["envelope_count"] == 2
    area = next(a for a in rep["areas"] if a["area_id"] == "AREA-1")
    assert area["dead_letter_count"] == 1
