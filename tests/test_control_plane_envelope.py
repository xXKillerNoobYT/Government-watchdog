"""GOV-733 CTRL-2026 — envelope encoding + WRITE-ONCE dedupe (AC-1).

Covers the canonical-JSON contract, the pinned dedupe test vector, and the
"duplicate ⇒ 1 envelope + dedupe hit + existing id, zero new jobs" invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import event_envelope as ee  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "ctrl.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    c.execute(
        "INSERT INTO webhook_sources (source_key, description, secret_ref, active, created_at) "
        "VALUES ('toa-webhook', 'test', 'GW_SECRET_TOA', 1, '2026-07-15T00:00:00.000+00:00')"
    )
    c.commit()
    yield c
    c.close()


def test_canonical_json_is_order_independent():
    a = ee.canonical_json({"b": 1, "a": 2, "nested": {"y": 1, "x": 2}})
    b = ee.canonical_json({"a": 2, "nested": {"x": 2, "y": 1}, "b": 1})
    assert a == b == '{"a":2,"b":1,"nested":{"x":2,"y":1}}'


def test_dedupe_key_pinned_vector():
    v = ee.DEDUPE_TEST_VECTOR
    assert ee.compute_dedupe_key(
        v["source_key"], v["event_kind"], v["source_ref"],
        v["content_sha256"], v["policy_version"],
    ) == v["expected"]
    assert len(v["expected"]) == 64  # sha256 hexdigest


def test_dedupe_key_field_boundaries_unambiguous():
    # Moving a character across a field boundary must change the key
    # (the 0x1f separator makes concatenation injective).
    k1 = ee.compute_dedupe_key("ab", "c", "r", "h", "p")
    k2 = ee.compute_dedupe_key("a", "bc", "r", "h", "p")
    assert k1 != k2


def _insert(conn, **over):
    kw = dict(
        source_key="toa-webhook", event_kind="agenda.published",
        source_ref="meeting/129", content_sha256="a" * 64,
        policy_version="2026-COMM-v1", payload={"n": 1, "m": 2},
    )
    kw.update(over)
    return ee.insert_envelope(conn, **kw)


def test_first_insert_is_new(conn):
    r = _insert(conn)
    assert r.is_new is True
    assert r.envelope_id >= 1
    rows = conn.execute("SELECT COUNT(*) FROM event_envelopes").fetchone()[0]
    assert rows == 1


def test_duplicate_yields_hit_not_second_envelope(conn):
    """AC-1: same content twice ⇒ 1 envelope, 1 dedupe hit, same id."""
    first = _insert(conn)
    second = _insert(conn)
    assert second.is_new is False
    assert second.envelope_id == first.envelope_id
    assert conn.execute("SELECT COUNT(*) FROM event_envelopes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_dedupe_hits").fetchone()[0] == 1


def test_n_replays_record_n_minus_one_hits(conn):
    _insert(conn)
    for _ in range(4):
        _insert(conn)
    assert conn.execute("SELECT COUNT(*) FROM event_envelopes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_dedupe_hits").fetchone()[0] == 4


def test_payload_reorder_same_content_is_duplicate(conn):
    """Different key order but identical content canonicalises equal ⇒ dup."""
    a = _insert(conn, payload={"n": 1, "m": 2})
    b = _insert(conn, payload={"m": 2, "n": 1})
    assert b.is_new is False and b.envelope_id == a.envelope_id


def test_changed_content_is_new_envelope(conn):
    a = _insert(conn, content_sha256="a" * 64)
    b = _insert(conn, content_sha256="b" * 64)
    assert b.is_new is True and b.envelope_id != a.envelope_id
    assert conn.execute("SELECT COUNT(*) FROM event_envelopes").fetchone()[0] == 2


def test_source_hash_and_payload_hash_stored(conn):
    r = _insert(conn, content_sha256="c" * 64, payload={"x": 1})
    row = conn.execute(
        "SELECT source_hash, payload_sha256, canonical_payload FROM event_envelopes "
        "WHERE envelope_id = ?", (r.envelope_id,),
    ).fetchone()
    assert row["source_hash"] == "c" * 64
    assert row["payload_sha256"] == ee.sha256_hex('{"x":1}')
    assert row["canonical_payload"] == '{"x":1}'
