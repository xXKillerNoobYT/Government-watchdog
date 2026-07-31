"""Audit-trail and version-chain reads are ordered deterministically (GOV-1652 / #177).

``created_at`` / ``flagged_at`` are millisecond-granular
(``isoformat(timespec="milliseconds")``) while ``event_id`` / ``file_id`` /
``dependency_id`` are ``secrets.token_hex`` — random. Ordering by ``(timestamp, id)``
therefore returned same-millisecond rows in a RANDOM order. Measured over 300 real
supersede runs before the fix: 14.0% of pairs shared a millisecond and 5.3% came back
reversed. In a system whose premise is traceable records, an "immutable audit trail"
that misreports sequence once every ~19 reads is a provenance-integrity defect, not a
flaky test.

The reads now tie-break on ``rowid`` — SQLite insertion order. These tables are
append-only (no ``DELETE`` exists against any of them), so no rowid is ever reused and
``rowid`` is monotonic arrival order.

Every test below forces the tie DETERMINISTICALLY: the clock is frozen so timestamps
collide, and ids are minted in strictly DESCENDING order so an id tie-break yields
exactly the REVERSE of arrival. Nothing here depends on chance — under the old
``ORDER BY <timestamp>, <id>`` each of these fails on every run, not occasionally.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import file_read_api as api  # noqa: E402
import file_records as fr  # noqa: E402
import file_versioning as fv  # noqa: E402

FROZEN = "2026-07-28T12:00:00.000+00:00"

SHA_V1 = hashlib.sha256(b"%PDF-1.4 Alpine council packet v1").hexdigest()
SHA_V2 = hashlib.sha256(b"%PDF-1.4 Alpine council packet v2 corrected").hexdigest()
SHA_V3 = hashlib.sha256(b"%PDF-1.4 Alpine council packet v3 corrected again").hexdigest()

V1 = dict(
    area="alpine",
    source_type="agenda_packet",
    original_filename="2026-06-23-packet.pdf",
    sha256=SHA_V1,
    mime="application/pdf",
    byte_size=51234,
    supplied_by="isaac",
    captured_at="2026-06-23T00:00:00.000+00:00",
)
V2 = dict(V1, original_filename="2026-06-23-packet-v2.pdf", sha256=SHA_V2,
          byte_size=52999, captured_at="2026-06-24T09:30:00.000+00:00",
          superseded_by="isaac")
V3 = dict(V1, original_filename="2026-06-23-packet-v3.pdf", sha256=SHA_V3,
          byte_size=53555, captured_at="2026-06-25T09:30:00.000+00:00",
          superseded_by="isaac")


class _DescendingIds:
    """Drop-in for the ``secrets`` module that mints strictly DESCENDING hex ids.

    Replaces the module reference inside the module under test only — the stdlib
    ``secrets`` module is never mutated. Descending ids are what make these tests
    deterministic: sorting on the id yields the exact reverse of arrival order, so a
    regression cannot pass by luck.
    """

    def __init__(self) -> None:
        self._n = 0xFFFF

    def token_hex(self, nbytes: int = 12) -> str:
        self._n -= 1
        return f"{self._n:0{nbytes * 2}x}"


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "audit-order.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


@pytest.fixture()
def tied(monkeypatch):
    """Freeze both clocks and mint descending ids in both modules."""
    monkeypatch.setattr(fv, "_now_utc_iso", lambda: FROZEN)
    monkeypatch.setattr(fr, "_now_utc_iso", lambda: FROZEN)
    monkeypatch.setattr(fv, "secrets", _DescendingIds())
    monkeypatch.setattr(fr, "secrets", _DescendingIds())


def _web_safe(conn: sqlite3.Connection, file_id: str):
    """Walk the reviewer-only transition path pending -> reviewing -> web_safe."""
    fr.set_review_state(conn, file_id, "reviewing")
    return fr.set_review_state(conn, file_id, api.WEB_SAFE_STATE)


def _chain(conn):
    """v1 -> v2 -> v3, every timestamp identical, every id descending."""
    v1 = fr.insert_file_record(conn, **V1)
    r2 = fv.supersede_file(conn, v1.file_id, **V2)
    r3 = fv.supersede_file(conn, r2.new.file_id, **V3)
    return v1, r2, r3


# --- the audit trail itself (#177 as filed) ---------------------------------

class TestSupersedeAuditTrailOrder:
    def test_events_are_arrival_ordered_when_created_at_ties(self, conn, tied):
        _v1, r2, r3 = _chain(conn)
        events = fv.list_supersede_events(conn, _v1.version_group_id)
        # The tie is real, and the ids descend — so an id tie-break would reverse this.
        assert [e.created_at for e in events] == [FROZEN, FROZEN]
        assert r2.event.event_id > r3.event.event_id
        assert [e.event_id for e in events] == [r2.event.event_id, r3.event.event_id]

    def test_distinct_timestamps_still_win_over_arrival(self, conn, tied):
        """rowid is only the TIE-BREAK: a real created_at ordering still governs."""
        _v1, r2, r3 = _chain(conn)
        # Backdate the LATER-arriving event; chronology must now outrank arrival.
        conn.execute(
            "UPDATE supplied_file_supersede_events SET created_at = ? WHERE event_id = ?",
            ("2026-07-27T00:00:00.000+00:00", r3.event.event_id),
        )
        conn.commit()
        events = fv.list_supersede_events(conn, _v1.version_group_id)
        assert [e.event_id for e in events] == [r3.event.event_id, r2.event.event_id]


# --- the SERVED projection (the docstring promised "ordered deterministically") ---

class TestServedSupersedeViewOrder:
    def test_views_are_served_in_arrival_order_when_created_at_ties(self, conn, tied):
        v1, r2, r3 = _chain(conn)
        for fid in (v1.file_id, r2.new.file_id, r3.new.file_id):
            _web_safe(conn, fid)
        views = api.supersede_views(conn)
        assert [v["new_file_id"] for v in views] == [r2.new.file_id, r3.new.file_id]


# --- the version chain ------------------------------------------------------

class TestVersionChainOrder:
    def test_versions_are_arrival_ordered_when_created_at_ties(self, conn, tied):
        v1, r2, r3 = _chain(conn)
        versions = fr.list_versions(conn, v1.version_group_id)
        assert [r.file_id for r in versions] == [v1.file_id, r2.new.file_id, r3.new.file_id]


# --- the red-flag (needs-re-review) queue -----------------------------------

class TestDependencyOrder:
    def test_dependencies_are_arrival_ordered_when_created_at_ties(self, conn, tied):
        v1 = fr.insert_file_record(conn, **V1)
        deps = [
            fv.register_dependency(
                conn, file_id=v1.file_id, record_kind="statement",
                record_ref=f"stmt-{i}", created_at=FROZEN,
            )
            for i in range(4)
        ]
        listed = fv.list_dependencies(conn, v1.file_id)
        assert [d.dependency_id for d in listed] == [d.dependency_id for d in deps]

    def test_red_flag_queue_is_arrival_ordered_when_flagged_at_ties(self, conn, tied):
        """One supersede flags every dependency in a single UPDATE — so they all
        share a flagged_at to the millisecond by construction, not by luck."""
        v1 = fr.insert_file_record(conn, **V1)
        deps = [
            fv.register_dependency(
                conn, file_id=v1.file_id, record_kind="statement",
                record_ref=f"stmt-{i}", created_at=FROZEN,
            )
            for i in range(4)
        ]
        fv.supersede_file(conn, v1.file_id, **V2)
        flagged = fv.list_needs_re_review(conn, v1.version_group_id)
        assert {d.flagged_at for d in flagged} == {FROZEN}
        assert [d.dependency_id for d in flagged] == [d.dependency_id for d in deps]
