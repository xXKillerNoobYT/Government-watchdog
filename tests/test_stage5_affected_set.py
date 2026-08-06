"""GOV-1688 Stage 5 R1/Slice 3 — affected-set resolver + selective invalidation +
statement/evidence<->diff binding (home slots 5.05 + 5.07, requirement A3).

Proves :mod:`stage5_affected_set` (migration 0035) against the issue's acceptance
criteria. It consumes Slice 2's structured diff (:mod:`stage5_source_diff`,
migration 0034) by ``change_id`` — it never re-diffs or re-hashes. Each test names
the AC it discharges:

- **AC-1** the affected set is anchor-keyed and scoped: a change whose segment(s)
  touch a subset of anchors resolves exactly the records whose civic locator
  matches a segment ``(anchor_type, anchor_ref)``; records at untouched anchors are
  absent. Covered for agenda_item / meeting / page / attachment anchors.
- **AC-2** selective + idempotent invalidation: only affected records are marked;
  every unaffected canonical table is byte-identical before and after; a second
  resolve+invalidate on the same change inserts no row and re-marks nothing.
- **AC-3** statement/evidence<->diff binding (D-1 seed): a single join reaches
  source -> source_versions -> source_version_changes ->
  source_version_diff_segments -> affected statement/evidence with no dangling hop.
- **AC-4** fail-closed on unresolvable anchor: a segment localizing no concrete
  record is flagged (``unresolved`` sentinel), never dropped; an unknown
  ``anchor_type`` reaching the resolver denies (red-then-green guards).
- **AC-5** determinism / no model in the loop: the affected set + markers are a
  pure function of the stored diff — re-derivation is byte-stable (stable ordering,
  content-addressed ids).
- **AC-6** path containment: N/A here — the resolver reads no snapshot path (only
  ids/hashes). Proven by a no-raw-path sweep over the emitted binding trace.
- **AC-7** migration hygiene: migration 0035 re-runs clean (INV-5 / IF NOT EXISTS).

Pure sqlite + in-memory fixtures: no network, no AI, no real-corpus dependency.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402  (reused: transport no-raw-path sweep)
import source_version_store as svs  # noqa: E402
import stage5_affected_set as af  # noqa: E402
import stage5_source_diff as sd  # noqa: E402

_URL = "https://www.alpinewy.gov/agenda-2026-08-04.pdf"
_SRC = "src-agenda-2026-08-04"
_OTHER_SRC = "src-unrelated"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated, file-backed DB (BEGIN IMMEDIATE / concurrency need a real file)."""
    db_path = tmp_path / "gov.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_registry(connection)
    yield connection
    connection.close()


def _seed_registry(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, url, source_type) VALUES (?, ?, ?, ?)",
        (_SRC, "Alpine agenda packet", _URL, "agenda_packet"),
    )
    conn.execute(
        "INSERT INTO sources (source_id, name, url, source_type) VALUES (?, ?, ?, ?)",
        (_OTHER_SRC, "Unrelated source", "https://www.alpinewy.gov/other.pdf", "agenda_packet"),
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) VALUES (?, ?, ?, ?)",
        (1, "2026-08-04", "Town Council", "2026-08-04T09:00:00+00:00"),
    )
    conn.commit()


def _two_versions(conn: sqlite3.Connection, *, v1=b"agenda v1", v2=b"agenda v2 CHANGED") -> None:
    svs.preserve_source_version(
        conn, source_url=_URL, retrieval_time="2026-08-04T09:00:00+00:00",
        provenance={"crawl_run_id": 1}, content=v1, source_id=_SRC,
    )
    svs.preserve_source_version(
        conn, source_url=_URL, retrieval_time="2026-08-04T17:00:00+00:00",
        provenance={"crawl_run_id": 2}, content=v2, source_id=_SRC,
    )


def _change(conn: sqlite3.Connection, old_content, new_content) -> str:
    """Seed two versions + a detected change from the given anchored content."""
    _two_versions(conn)
    result = sd.detect_and_store(
        conn, source_url=_URL, old_content=old_content, new_content=new_content
    )
    assert result["detected"] is True
    return result["change_id"]


def _agenda_item(conn, item_id, *, source_id=_SRC, meeting_id=1) -> None:
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, title, agenda_doc_source_id) "
        "VALUES (?, ?, ?, ?)",
        (item_id, meeting_id, f"Item {item_id}", source_id),
    )


def _statement(conn, sid, *, agenda_item_id) -> None:
    conn.execute(
        "INSERT INTO statements (statement_id, agenda_item_id, statement_text) VALUES (?, ?, ?)",
        (sid, agenda_item_id, f"claim {sid}"),
    )


def _evidence(conn, eid, *, agenda_item_id=None, locator_kind="section", section=None,
              page=None, to_source_id=_SRC) -> None:
    conn.execute(
        "INSERT INTO evidence_links (evidence_link_id, from_node_id, to_source_id, "
        "relation, locator_kind, section, page, agenda_item_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, "stmt-x", to_source_id, "references", locator_kind, section, page, agenda_item_id),
    )


def _review(conn, did, *, statement_id) -> None:
    conn.execute(
        "INSERT INTO reviewer_decisions (decision_id, statement_id, reviewer_id, decision, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (did, statement_id, "reviewer:isaac", "approved", "looks good"),
    )


def _alias(conn, aid, *, meeting_id=None, source_id=_SRC) -> None:
    conn.execute(
        "INSERT INTO node_label_aliases (alias_id, node_id, node_type, term, alias_type, "
        "source_ref_source_id, first_seen_meeting_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (aid, "topic-1", "topic", f"gov term {aid}", "government_term", source_id, meeting_id),
    )


# A material agenda-item change at item-3 (time 18:00 -> 16:00); item-9 unchanged.
_OLD = [
    {"anchor_type": "agenda_item", "anchor_ref": "item-3", "fields": {"title": "Hearing", "time": "18:00"}},
    {"anchor_type": "agenda_item", "anchor_ref": "item-9", "fields": {"title": "Budget", "time": "10:00"}},
]
_NEW = [
    {"anchor_type": "agenda_item", "anchor_ref": "item-3", "fields": {"title": "Hearing", "time": "16:00"}},
    {"anchor_type": "agenda_item", "anchor_ref": "item-9", "fields": {"title": "Budget", "time": "10:00"}},
]


def _seed_full_canonical(conn) -> str:
    """A change touching only agenda_item item-3, with records at item-3 (affected)
    and item-9 (untouched), plus a review of an affected statement."""
    change_id = _change(conn, _OLD, _NEW)
    _agenda_item(conn, "item-3")
    _agenda_item(conn, "item-9")
    _statement(conn, "s1", agenda_item_id="item-3")
    _statement(conn, "s2", agenda_item_id="item-3")
    _statement(conn, "s3", agenda_item_id="item-9")  # untouched anchor
    _evidence(conn, "e1", agenda_item_id="item-3", section="A")
    _evidence(conn, "e2", agenda_item_id="item-9", section="B")  # untouched anchor
    _review(conn, "d1", statement_id="s1")   # affected transitively
    _review(conn, "d9", statement_id="s3")   # untouched anchor -> must NOT be affected
    conn.commit()
    return change_id


def _ids_by_class(records, record_class):
    return {r["record_id"] for r in records if r["record_class"] == record_class}


# ---------------------------------------------------------------------------
# AC-1 — anchor-keyed and scoped
# ---------------------------------------------------------------------------


def test_ac1_agenda_item_anchor_scoped(conn):
    change_id = _seed_full_canonical(conn)
    records = af.resolve_affected(conn, change_id)

    # only item-3's records resolve; item-9's are absent.
    assert _ids_by_class(records, af.RC_STATEMENT) == {"s1", "s2"}
    assert _ids_by_class(records, af.RC_EVIDENCE_LINK) == {"e1"}
    assert _ids_by_class(records, af.RC_REVIEW) == {"d1"}  # review of s1, not s3
    all_ids = {r["record_id"] for r in records}
    assert "s3" not in all_ids and "e2" not in all_ids and "d9" not in all_ids
    # every resolved row is bound to the single changed anchor.
    assert {(r["anchor_type"], r["anchor_ref"]) for r in records} == {("agenda_item", "item-3")}
    assert all(r["resolution"] == af.RESOLUTION_RESOLVED for r in records)


def test_ac1_page_anchor_is_source_scoped(conn):
    # a page-anchored change; only evidence on that page OF THIS SOURCE is affected.
    old = [{"anchor_type": "page", "anchor_ref": "5", "fields": {"title": "x"}}]
    new = [{"anchor_type": "page", "anchor_ref": "5", "fields": {"title": "y"}}]
    change_id = _change(conn, old, new)
    _evidence(conn, "e_here", locator_kind="page", page=5, to_source_id=_SRC)
    _evidence(conn, "e_other", locator_kind="page", page=5, to_source_id=_OTHER_SRC)
    _evidence(conn, "e_pg6", locator_kind="page", page=6, to_source_id=_SRC)
    conn.commit()

    records = af.resolve_affected(conn, change_id)
    assert _ids_by_class(records, af.RC_EVIDENCE_LINK) == {"e_here"}  # scoped to source + page


def test_ac1_meeting_anchor_spans_agenda_and_normalization(conn):
    old = [{"anchor_type": "meeting", "anchor_ref": "1", "fields": {"title": "old"}}]
    new = [{"anchor_type": "meeting", "anchor_ref": "1", "fields": {"title": "new"}}]
    change_id = _change(conn, old, new)
    _agenda_item(conn, "item-3", meeting_id=1)
    _statement(conn, "s1", agenda_item_id="item-3")
    _alias(conn, "a1", meeting_id=1)
    _alias(conn, "a2", meeting_id=None)  # not first-seen at this meeting -> unaffected
    conn.commit()

    records = af.resolve_affected(conn, change_id)
    assert _ids_by_class(records, af.RC_STATEMENT) == {"s1"}
    assert _ids_by_class(records, af.RC_NORMALIZATION) == {"a1"}


def test_ac1_attachment_anchor_is_whole_source(conn):
    old = [{"anchor_type": "attachment", "anchor_ref": _URL, "fields": {"h": "1"}}]
    new = [{"anchor_type": "attachment", "anchor_ref": _URL, "fields": {"h": "2"}}]
    change_id = _change(conn, old, new)
    _evidence(conn, "e_src", to_source_id=_SRC, section="Z")
    _evidence(conn, "e_other", to_source_id=_OTHER_SRC, section="Z")
    _alias(conn, "a_src", source_id=_SRC)
    conn.commit()

    records = af.resolve_affected(conn, change_id)
    assert _ids_by_class(records, af.RC_EVIDENCE_LINK) == {"e_src"}
    assert _ids_by_class(records, af.RC_NORMALIZATION) == {"a_src"}


def test_ac1_red_proof_over_broad_resolver_would_pull_untouched_records(conn, monkeypatch):
    """RED-proof: the scoping in AC-1 is load-bearing. An over-broad statement
    resolver (ignoring the anchor) pulls item-9's s3 into the set — so the passing
    test's `s3 not in` assertion would catch a scoping regression."""
    change_id = _seed_full_canonical(conn)
    monkeypatch.setattr(
        af, "_agenda_item_statements",
        lambda conn, ref, source_id: af._sorted_col(
            conn.execute("SELECT statement_id FROM statements").fetchall()
        ),
    )
    # rebuild the registry entry to point at the patched fn.
    monkeypatch.setitem(
        af.RESOLVER_RULES, sd.ANCHOR_AGENDA_ITEM,
        [(af.RC_STATEMENT, af._agenda_item_statements), (af.RC_EVIDENCE_LINK, af._agenda_item_evidence)],
    )
    records = af.resolve_affected(conn, change_id)
    assert "s3" in _ids_by_class(records, af.RC_STATEMENT)  # the bug is observable


# ---------------------------------------------------------------------------
# AC-2 — selective + idempotent invalidation
# ---------------------------------------------------------------------------


_CANONICAL_TABLES = ("statements", "evidence_links", "reviewer_decisions",
                     "node_label_aliases", "agenda_items", "source_versions",
                     "source_version_changes", "source_version_diff_segments")


def _snapshot(conn) -> str:
    h = hashlib.sha256()
    for table in _CANONICAL_TABLES:
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall():
            h.update(repr(tuple(row)).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


def test_ac2_only_affected_marked_and_unaffected_bytes_identical(conn):
    change_id = _seed_full_canonical(conn)
    before = _snapshot(conn)

    result = af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")
    after = _snapshot(conn)

    # canonical content is NEVER touched — Slice 3 marks, it does not overwrite.
    assert before == after
    # exactly the affected records got a marker row.
    assert result["created"] == 4  # s1, s2, e1, d1
    ledger = {
        (r["record_class"], r["record_id"])
        for r in conn.execute(
            "SELECT record_class, record_id FROM source_change_affected_records WHERE change_id = ?",
            (change_id,),
        ).fetchall()
    }
    assert ledger == {("statement", "s1"), ("statement", "s2"), ("evidence_link", "e1"), ("review", "d1")}


def test_ac2_reinvalidate_is_a_noop(conn):
    change_id = _seed_full_canonical(conn)
    first = af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")
    count_1 = conn.execute(
        "SELECT COUNT(*) FROM source_change_affected_records WHERE change_id = ?", (change_id,)
    ).fetchone()[0]

    second = af.invalidate(conn, change_id, now="2026-08-05T09:99:99+00:00")  # different clock
    count_2 = conn.execute(
        "SELECT COUNT(*) FROM source_change_affected_records WHERE change_id = ?", (change_id,)
    ).fetchone()[0]

    assert first["created"] == count_1
    assert second["created"] == 0 and second["skipped"] == count_1
    assert count_1 == count_2  # no duplicate, no re-marking


# ---------------------------------------------------------------------------
# AC-3 — statement/evidence <-> diff binding (D-1 trace seed)
# ---------------------------------------------------------------------------


def test_ac3_single_join_reaches_source_with_no_dangling_hop(conn):
    change_id = _seed_full_canonical(conn)
    af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")

    trace = af.affected_trace(conn, change_id)
    # every ledger row survives the full source->version->change->segment->record join.
    assert len(trace) == 4
    for row in trace:
        assert row["source_url"] == _URL
        assert row["change_id"] == change_id
        assert row["segment_id"] and row["new_version_id"] and row["old_version_id"]
    # a statement reaches its source in one hop-chain.
    s1 = next(r for r in trace if r["record_id"] == "s1")
    assert s1["record_class"] == "statement" and s1["source_url"] == _URL
    assert af.assert_binding_no_dangling_hop(conn, change_id) is True


def test_ac3_red_proof_dangling_hop_is_caught(conn):
    """RED-proof: force a ledger row whose segment_id joins to nothing (FK off) and
    watch the binding guard go RED."""
    change_id = _seed_full_canonical(conn)
    af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")
    seg_id = conn.execute(
        "SELECT segment_id FROM source_version_diff_segments WHERE change_id = ?", (change_id,)
    ).fetchone()[0]

    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO source_change_affected_records (affected_id, change_id, segment_id, "
        "anchor_type, anchor_ref, record_class, record_id, resolution, marked_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("srcaff:dangling", change_id, "srcseg:does-not-exist", "agenda_item", "item-3",
         "statement", "ghost", "resolved", "2026-08-05T00:00:00+00:00"),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(af.AffectedSetAuditError):
        af.assert_binding_no_dangling_hop(conn, change_id)


# ---------------------------------------------------------------------------
# AC-4 — fail-closed on unresolvable anchor
# ---------------------------------------------------------------------------


def test_ac4_segment_localizing_nothing_is_flagged_not_dropped(conn):
    # a change at an agenda item that has NO downstream records.
    old = [{"anchor_type": "agenda_item", "anchor_ref": "orphan", "fields": {"time": "18:00"}}]
    new = [{"anchor_type": "agenda_item", "anchor_ref": "orphan", "fields": {"time": "16:00"}}]
    change_id = _change(conn, old, new)

    records = af.resolve_affected(conn, change_id)
    assert len(records) == 1
    flag = records[0]
    assert flag["record_class"] == af.RC_UNRESOLVED
    assert flag["resolution"] == af.RESOLUTION_FLAGGED
    assert flag["record_id"] == "agenda_item:orphan"

    af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")
    assert af.assert_every_segment_covered(conn, change_id) is True


def test_ac4_red_proof_dropped_segment_is_caught(conn):
    """RED-proof: if a segment's marker were silently dropped, the coverage guard
    goes RED (the exact regression the 'unresolved' sentinel prevents)."""
    old = [{"anchor_type": "agenda_item", "anchor_ref": "orphan", "fields": {"time": "18:00"}}]
    new = [{"anchor_type": "agenda_item", "anchor_ref": "orphan", "fields": {"time": "16:00"}}]
    change_id = _change(conn, old, new)
    af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")

    conn.execute("DELETE FROM source_change_affected_records WHERE change_id = ?", (change_id,))
    conn.commit()
    with pytest.raises(af.AffectedSetAuditError):
        af.assert_every_segment_covered(conn, change_id)


def test_ac4_unknown_anchor_type_reaching_resolver_denies(conn, monkeypatch):
    change_id = _seed_full_canonical(conn)
    # defense-in-depth behind the DB CHECK: a segment with a bogus anchor_type is refused.
    real = af._load_segments

    def _poison(c, cid):
        segs = real(c, cid)
        segs[0]["anchor_type"] = "wormhole"
        return segs

    monkeypatch.setattr(af, "_load_segments", _poison)
    with pytest.raises(sd.UnknownAnchorType):
        af.resolve_affected(conn, change_id)


def test_ac4_db_check_rejects_unknown_record_class(conn):
    # the closed record_class vocabulary is enforced at the DB, not just in code.
    change_id = _seed_full_canonical(conn)
    seg_id = conn.execute(
        "SELECT segment_id FROM source_version_diff_segments WHERE change_id = ?", (change_id,)
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_change_affected_records (affected_id, change_id, segment_id, "
            "anchor_type, anchor_ref, record_class, record_id, resolution, marked_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("srcaff:bad", change_id, seg_id, "agenda_item", "item-3",
             "not_a_real_class", "x", "resolved", "2026-08-05T00:00:00+00:00"),
        )


# ---------------------------------------------------------------------------
# AC-5 — determinism / no model in the loop
# ---------------------------------------------------------------------------


def test_ac5_resolution_is_byte_stable(conn):
    change_id = _seed_full_canonical(conn)
    first = af.resolve_affected(conn, change_id)
    second = af.resolve_affected(conn, change_id)
    assert first == second  # identical order + content, no randomness/clock


def test_ac5_affected_ids_are_content_addressed_and_stable(conn):
    change_id = _seed_full_canonical(conn)
    af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")
    ids_1 = sorted(
        r[0] for r in conn.execute(
            "SELECT affected_id FROM source_change_affected_records WHERE change_id = ?",
            (change_id,),
        ).fetchall()
    )
    # re-invalidate with a different clock must not mint different ids.
    af.invalidate(conn, change_id, now="2026-08-05T12:00:00+00:00")
    ids_2 = sorted(
        r[0] for r in conn.execute(
            "SELECT affected_id FROM source_change_affected_records WHERE change_id = ?",
            (change_id,),
        ).fetchall()
    )
    assert ids_1 == ids_2
    # the id is exactly sha256(change,segment,class,record) — no wall-clock in it.
    row = conn.execute(
        "SELECT affected_id, segment_id, record_class, record_id "
        "FROM source_change_affected_records WHERE change_id = ? AND record_id = 's1'",
        (change_id,),
    ).fetchone()
    assert row["affected_id"] == af._affected_id(change_id, row["segment_id"], "statement", "s1")


# ---------------------------------------------------------------------------
# AC-6 — no snapshot path is read / emitted
# ---------------------------------------------------------------------------


def test_ac6_binding_trace_carries_no_raw_path(conn):
    change_id = _seed_full_canonical(conn)
    af.invalidate(conn, change_id, now="2026-08-05T00:00:00+00:00")
    trace = af.affected_trace(conn, change_id)
    # the resolver reads no snapshot_path; the emitted trace is swept as a backstop.
    assert read_api.assert_no_raw_paths({"trace": trace}) is not None


# ---------------------------------------------------------------------------
# AC-7 — migration hygiene
# ---------------------------------------------------------------------------


def test_ac7_migration_reruns_clean(tmp_path):
    db_path = tmp_path / "rerun.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # IF NOT EXISTS — must not raise (INV-5)
    with db.open_db(db_path) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(source_change_affected_records)")}
    assert {"affected_id", "change_id", "segment_id", "anchor_type", "record_class",
            "record_id", "resolution", "marked_utc"} <= cols
