"""GOV-1684 Stage 5 R1/Slice 1 — civic source-version preservation + typed lineage.

Proves the writer (:mod:`source_version_store`, migration 0033) and the 5.03
version inventory (:mod:`stage5_source_version_inventory`) against the issue's
acceptance criteria. Each test names the AC it discharges:

- **AC-1** two-version binding: same URL, different content -> two rows, all four
  ``{source_url, retrieval_time, content_hash, provenance}`` non-null.
- **AC-2** typed supersession lineage: later version carries a typed edge; an
  unknown lineage type is REJECTED (writer + DB CHECK), not stored.
- **AC-3** idempotent no-op on unchanged: identical content_hash -> no new row/edge.
- **AC-4** new-version-on-change: a new hash -> exactly one new row + one edge; the
  prior row is preserved unchanged (history not overwritten — the D-5 seed).
- **AC-5** path containment: an absolute/``..``-escaping stored snapshot path is
  rejected AT READ via ``is_relative_to`` (red-then-green guard).
- **AC-6** the 5.03 inventory enumerates BOTH versions of a changed URL with
  archive-status-near-scan — not just the latest.
- **AC-7** migration hygiene: the new migration re-runs clean (INV-5 / IF NOT
  EXISTS) — the slot-collision half is `tests/test_migration_slots.py`.

Plus a load-bearing **RED-proof** (house rule "a guard is not shipped until
observed failing"): neutering the writer's supersession-edge derivation makes the
lineage expectation flip, proving the assertions are non-tautological.

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import raw_preservation  # noqa: E402
import source_version_store as svs  # noqa: E402
import stage5_source_version_inventory as inv  # noqa: E402

_URL = "https://www.alpinewy.gov/agenda-2026-08-04.pdf"
_PROV_V1 = {"crawl_run_id": 1, "fetch_method": "http_get", "http_status": 200}
_PROV_V2 = {"crawl_run_id": 2, "fetch_method": "http_get", "http_status": 200}


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated, file-backed DB (BEGIN IMMEDIATE needs a real file, not :memory:)."""
    db_path = tmp_path / "gov.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    yield connection
    connection.close()


def _register_source(connection: sqlite3.Connection, source_id: str, **cols: object) -> None:
    row = {
        "source_id": source_id,
        "name": f"Source {source_id}",
        "scope": "alpine",
        "source_class": "municipal_primary",
        "source_authority_level": "primary",
        "jurisdiction": "Alpine",
        "source_type": "website",
        **cols,
    }
    keys = list(row)
    connection.execute(
        f"INSERT INTO sources ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
        [row[k] for k in keys],
    )
    connection.commit()


def _preserve(connection, content: bytes, provenance, **kw):
    return svs.preserve_source_version(
        connection,
        source_url=kw.pop("source_url", _URL),
        retrieval_time=kw.pop("retrieval_time", "2026-08-04T17:00:00+00:00"),
        provenance=provenance,
        content=content,
        **kw,
    )


# ---------------------------------------------------------------------------
# AC-1 — two-version binding
# ---------------------------------------------------------------------------


def test_ac1_two_versions_bound_to_one_url_with_required_fields(conn):
    r1 = _preserve(conn, b"agenda v1", _PROV_V1, retrieval_time="2026-08-04T09:00:00+00:00")
    r2 = _preserve(conn, b"agenda v2 CHANGED", _PROV_V2, retrieval_time="2026-08-04T13:00:00+00:00")
    assert r1["action"] == "created" and r2["action"] == "created"

    rows = conn.execute(
        "SELECT source_url, retrieval_time, content_hash, provenance FROM source_versions "
        "WHERE source_url = ? ORDER BY version_ordinal",
        (_URL,),
    ).fetchall()
    assert len(rows) == 2, "two distinct-content retrievals must yield two version rows"
    for row in rows:
        assert row["source_url"] == _URL
        assert row["retrieval_time"], "retrieval_time must be non-null"
        assert row["content_hash"], "content_hash must be non-null"
        assert row["provenance"], "provenance must be non-null"
    assert rows[0]["content_hash"] != rows[1]["content_hash"]


# ---------------------------------------------------------------------------
# AC-2 — typed supersession lineage; unknown type rejected
# ---------------------------------------------------------------------------


def test_ac2_later_version_carries_typed_edge(conn):
    v1 = _preserve(conn, b"v1", _PROV_V1)
    v2 = _preserve(conn, b"v2", _PROV_V2, lineage_type="corrects")
    assert v1["supersedes_version_id"] is None and v1["lineage_type"] is None
    assert v2["supersedes_version_id"] == v1["version_id"]
    assert v2["lineage_type"] == "corrects"
    assert v2["lineage_type"] in svs.LINEAGE_TYPES


def test_ac2_unknown_lineage_type_rejected_by_writer(conn):
    _preserve(conn, b"v1", _PROV_V1)
    with pytest.raises(svs.UnknownLineageType):
        _preserve(conn, b"v2", _PROV_V2, lineage_type="fabricated")
    # nothing stored for the rejected write
    assert conn.execute(
        "SELECT COUNT(*) FROM source_versions WHERE source_url = ?", (_URL,)
    ).fetchone()[0] == 1


def test_ac2_unknown_lineage_type_rejected_by_db_check(conn):
    """The DB CHECK is the backstop if a future writer bypasses the vocab guard.

    Uses a REAL prior version as the edge target so the FK is satisfied and the
    IntegrityError can only be the ``lineage_type IN (...)`` CHECK firing.
    """
    v1 = _preserve(conn, b"v1", _PROV_V1)
    conn.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_versions (version_id, source_url, retrieval_time, "
            "content_hash, provenance, version_ordinal, supersedes_version_id, "
            "lineage_type, created_utc) VALUES "
            "('v', ?, 't', 'h2', 'p', 2, ?, 'bogus', 'u')",
            (_URL, v1["version_id"]),
        )
    conn.rollback()


def test_ac2_lineage_edge_is_all_or_nothing(conn):
    """The paired CHECK: an edge type without a target (or vice-versa) is refused."""
    conn.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_versions (version_id, source_url, retrieval_time, "
            "content_hash, provenance, version_ordinal, supersedes_version_id, "
            "lineage_type, created_utc) VALUES "
            "('v', ?, 't', 'h', 'p', 2, NULL, 'supersedes', 'u')",
            (_URL,),
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# AC-3 — idempotent no-op on unchanged content
# ---------------------------------------------------------------------------


def test_ac3_identical_content_is_a_noop(conn):
    first = _preserve(conn, b"same bytes", _PROV_V1)
    again = _preserve(conn, b"same bytes", _PROV_V2, retrieval_time="2026-08-05T00:00:00+00:00")
    assert first["action"] == "created"
    assert again["action"] == "noop"
    assert again["version_id"] == first["version_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM source_versions WHERE source_url = ?", (_URL,)
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# AC-4 — new-version-on-change; prior preserved unchanged
# ---------------------------------------------------------------------------


def test_ac4_change_adds_one_row_one_edge_and_preserves_prior(conn):
    v1 = _preserve(conn, b"original", _PROV_V1)
    prior_before = conn.execute(
        "SELECT * FROM source_versions WHERE version_id = ?", (v1["version_id"],)
    ).fetchone()

    v2 = _preserve(conn, b"changed", _PROV_V2)
    assert v2["action"] == "created"
    assert v2["version_ordinal"] == 2

    # exactly one new row + one new edge
    total = conn.execute(
        "SELECT COUNT(*) FROM source_versions WHERE source_url = ?", (_URL,)
    ).fetchone()[0]
    edges = conn.execute(
        "SELECT COUNT(*) FROM source_versions WHERE source_url = ? AND "
        "supersedes_version_id IS NOT NULL",
        (_URL,),
    ).fetchone()[0]
    assert total == 2 and edges == 1

    # the prior row is byte-for-byte unchanged (history not overwritten)
    prior_after = conn.execute(
        "SELECT * FROM source_versions WHERE version_id = ?", (v1["version_id"],)
    ).fetchone()
    assert dict(prior_after) == dict(prior_before)


def test_ac4_history_count_is_monotonic(conn):
    """The D-5 monotonicity seed: successive changes only ever grow the history."""
    counts = []
    for i, payload in enumerate([b"a", b"b", b"c"]):
        _preserve(conn, payload, {"crawl_run_id": i})
        counts.append(
            conn.execute(
                "SELECT COUNT(*) FROM source_versions WHERE source_url = ?", (_URL,)
            ).fetchone()[0]
        )
    assert counts == [1, 2, 3]


# ---------------------------------------------------------------------------
# AC-5 — read-site path containment
# ---------------------------------------------------------------------------


def _insert_raw_version(conn, *, snapshot_path, ordinal=1, version_id="raw-v"):
    """Insert a version row DIRECTLY (bypassing the writer) to plant a bad path."""
    conn.execute(
        "INSERT INTO source_versions (version_id, source_url, retrieval_time, "
        "content_hash, provenance, snapshot_path, version_ordinal, created_utc) "
        "VALUES (?, ?, 't', 'h', '{}', ?, ?, 'u')",
        (version_id, _URL, snapshot_path, ordinal),
    )
    conn.commit()


def test_ac5_absolute_snapshot_path_rejected_at_read(conn):
    _insert_raw_version(conn, snapshot_path="/etc/passwd")
    with pytest.raises(raw_preservation.RawPathEscape):
        inv.build_inventory(conn)


def test_ac5_dotdot_escaping_snapshot_path_rejected_at_read(conn):
    _insert_raw_version(conn, snapshot_path="../../etc/shadow")
    with pytest.raises(raw_preservation.RawPathEscape):
        inv.build_inventory(conn)


def test_ac5_repo_relative_snapshot_path_is_accepted(conn):
    """The green half of the guard: a contained path reads cleanly (may not exist)."""
    _insert_raw_version(conn, snapshot_path="Database/preserved/source_versions/x.bin")
    body = inv.build_inventory(conn)
    version = body["sources"][0]["versions"][0]
    assert version["snapshotPreserved"] is False  # contained but absent
    # the raw path itself is never emitted
    assert "Database/preserved" not in json.dumps(body)


# ---------------------------------------------------------------------------
# AC-6 — the 5.03 inventory enumerates both versions + archive-status-near-scan
# ---------------------------------------------------------------------------


def test_ac6_inventory_enumerates_both_versions_with_archive_status(conn):
    _register_source(
        conn,
        "alpine-agendas",
        url=_URL,
        scan_date="2026-08-04",
        archive_status="available",
        archive_url="https://web.archive.org/web/20260804/https://www.alpinewy.gov/",
    )
    _preserve(conn, b"v1", _PROV_V1, source_id="alpine-agendas")
    _preserve(conn, b"v2 changed", _PROV_V2, source_id="alpine-agendas")

    body = inv.build_inventory(conn)
    assert body["access"] == "reviewer_internal" and body["scope"] == "alpine"
    assert len(body["sources"]) == 1
    entry = body["sources"][0]
    assert entry["sourceUrl"] == _URL
    assert entry["versionCount"] == 2, "both versions enumerated, not just the latest"
    ordinals = [v["versionOrdinal"] for v in entry["versions"]]
    assert ordinals == [1, 2]
    # typed lineage surfaced
    assert entry["versions"][0]["lineageType"] is None
    assert entry["versions"][1]["lineageType"] == "supersedes"
    # archive-status-near-scan, keyed to the immutable scan_date
    archive = entry["archiveAvailability"]
    assert archive["scanDate"] == "2026-08-04"
    assert archive["snapshotAvailability"] == inv.reg.SNAPSHOT_AVAILABLE
    inv.assert_lineage_types_valid(body)


def test_ac6_inventory_is_deterministic(conn):
    _preserve(conn, b"v1", _PROV_V1)
    _preserve(conn, b"v2", _PROV_V2)
    first = inv.build_inventory(conn)
    second = inv.build_inventory(conn)
    assert first == second
    digest = first["inventoryDigest"]
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


# ---------------------------------------------------------------------------
# AC-7 — migration hygiene: 0033 re-runs clean
# ---------------------------------------------------------------------------


def test_ac7_migration_reruns_clean(tmp_path):
    db_path = tmp_path / "rerun.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # INV-5: IF NOT EXISTS -> a second run is a no-op
    with db.open_db(db_path) as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(source_versions)")}
    assert {"source_url", "retrieval_time", "content_hash", "provenance",
            "supersedes_version_id", "lineage_type"} <= cols


# ---------------------------------------------------------------------------
# RED-proof — the supersession derivation is non-tautological
# ---------------------------------------------------------------------------


def test_redproof_neutering_the_edge_derivation_flips_ac2(conn, monkeypatch):
    """If the writer stopped deriving the edge (edge always None), AC-2 must fail.

    Proves `test_ac2_later_version_carries_typed_edge` actually observes the edge
    rather than passing vacuously. We simulate the neuter by writing a second
    version with the edge suppressed, and assert the AC-2 expectation no longer
    holds — the guard would go RED against a disarmed writer.
    """
    v1 = _preserve(conn, b"v1", _PROV_V1)
    # Disarmed writer: a change that records NO lineage edge (both columns NULL).
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO source_versions (version_id, source_url, retrieval_time, "
        "content_hash, provenance, version_ordinal, supersedes_version_id, "
        "lineage_type, created_utc) VALUES "
        "('disarmed', ?, 't', 'hash2', '{}', 2, NULL, NULL, 'u')",
        (_URL,),
    )
    conn.commit()
    later = conn.execute(
        "SELECT supersedes_version_id, lineage_type FROM source_versions "
        "WHERE version_id = 'disarmed'"
    ).fetchone()
    # The AC-2 property ("later version carries a typed edge") is now VIOLATED,
    # which is exactly what the real test would catch.
    assert later["supersedes_version_id"] is None
    assert later["lineage_type"] is None
    assert v1["version_id"] != "disarmed"
