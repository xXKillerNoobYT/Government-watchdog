"""GOV-1685 Stage 5 R1/Slice 2 — late-change detection + structured before/after
source diff (home slot 5.16).

Proves the detector + structured diff (:mod:`stage5_source_diff`, migration 0034)
against the issue's acceptance criteria. It consumes Slice 1's preserved version
pair (:mod:`source_version_store`, migration 0033) — it never re-opens or re-hashes
those rows. Each test names the AC it discharges:

- **AC-1** change detected on differing versions: a version pair with differing
  ``content_hash`` yields exactly one detected change bound to that pair; identical
  hashes yield NO detection (consistent with Slice 1's no-op-on-unchanged).
- **AC-2** structured, anchored diff: the change carries a segment with
  ``{anchor_type (closed set), before, after, materiality_reason}`` all non-null; an
  unknown ``anchor_type`` is REJECTED (code + DB CHECK), not stored.
- **AC-3** deterministic + idempotent: re-running yields a byte-identical artifact
  (stable ``change_hash``) and creates no duplicate row/artifact.
- **AC-4** version-pair binding (D-1 trace seed): the change references both
  ``source_versions`` rows by id; a single join reaches source -> both versions ->
  change with no dangling hop.
- **AC-5** audit-record presence (D-3 backend half): materiality reason,
  before/after change detail, and both-version source citations are present in the
  audit record (rendering is Slice 7 — presence, not a surface).
- **AC-6** read-site path containment: an absolute/``..``-escaping stored snapshot
  path is rejected AT READ via ``is_relative_to``, raising (red-then-green guard).
- **AC-7** migration hygiene: migration 0034 re-runs clean (INV-5 / IF NOT EXISTS)
  — the slot-collision half is ``tests/test_migration_slots.py``.

Plus the Stage-5 **X2** deterministic late-change red flag — a fires / does-not-fire
fixture pair — and load-bearing **RED-proofs** (house rule "a guard is not shipped
until observed failing"): neutering the diff/lateness derivations flips the
expectations, proving the assertions are non-tautological.

Pure sqlite + in-memory fixtures: no network, no AI, no real-corpus dependency.
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
import stage5_source_diff as sd  # noqa: E402

_URL = "https://www.alpinewy.gov/agenda-2026-08-04.pdf"
_MEETING = "2026-08-04T18:00:00+00:00"

# A material agenda-item change: the hearing time moved 18:00 -> 16:00.
_OLD = [{"anchor_type": "agenda_item", "anchor_ref": "item-3",
         "fields": {"title": "Zoning variance hearing", "time": "18:00"}}]
_NEW = [{"anchor_type": "agenda_item", "anchor_ref": "item-3",
         "fields": {"title": "Zoning variance hearing", "time": "16:00"}}]


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A migrated, file-backed DB (BEGIN IMMEDIATE needs a real file, not :memory:)."""
    db_path = tmp_path / "gov.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    yield connection
    connection.close()


def _preserve(connection, content: bytes, run: int, *, retrieval_time="2026-08-04T09:00:00+00:00", **kw):
    return svs.preserve_source_version(
        connection,
        source_url=kw.pop("source_url", _URL),
        retrieval_time=retrieval_time,
        provenance={"crawl_run_id": run, "fetch_method": "http_get"},
        content=content,
        **kw,
    )


def _two_versions(connection, *, v1=b"agenda v1", v2=b"agenda v2 CHANGED"):
    a = _preserve(connection, v1, 1, retrieval_time="2026-08-04T09:00:00+00:00")
    b = _preserve(connection, v2, 2, retrieval_time="2026-08-04T17:00:00+00:00")
    return a, b


# ---------------------------------------------------------------------------
# AC-1 — change detected on differing versions; identical -> no detection
# ---------------------------------------------------------------------------


def test_ac1_differing_versions_yield_exactly_one_change(conn):
    _two_versions(conn)
    r = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    assert r["detected"] is True and r["action"] == "created"
    # exactly one detected change bound to that version pair
    assert conn.execute("SELECT COUNT(*) FROM source_version_changes").fetchone()[0] == 1
    row = conn.execute(
        "SELECT old_version_id, new_version_id FROM source_version_changes"
    ).fetchone()
    ordinals = conn.execute(
        "SELECT version_ordinal FROM source_versions WHERE version_id IN (?, ?) "
        "ORDER BY version_ordinal",
        (row["old_version_id"], row["new_version_id"]),
    ).fetchall()
    assert [o["version_ordinal"] for o in ordinals] == [1, 2]


def test_ac1_identical_content_yields_no_detection(conn):
    """Two rows can never share a hash (Slice-1 UNIQUE), so identical content is a
    Slice-1 no-op -> only one version exists -> the detector refuses (nothing to
    diff). A directly-planted equal-hash pair proves the in-detector guard too."""
    _preserve(conn, b"same bytes", 1)
    again = _preserve(conn, b"same bytes", 2, retrieval_time="2026-08-05T00:00:00+00:00")
    assert again["action"] == "noop"  # Slice-1 collapsed it — one version only
    with pytest.raises(sd.SourceDiffError):
        sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    assert conn.execute("SELECT COUNT(*) FROM source_version_changes").fetchone()[0] == 0


# (An equal-hash pair for ONE url is impossible by Slice-1's UNIQUE(source_url,
# content_hash); the identical-content case above is therefore the only reachable
# "no detection" path. The in-detector content_hash guard is cheap defense-in-depth
# behind that constraint.)


# ---------------------------------------------------------------------------
# AC-2 — structured, anchored diff; unknown anchor_type rejected
# ---------------------------------------------------------------------------


def test_ac2_structured_anchored_diff_fields_all_present(conn):
    _two_versions(conn)
    r = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    seg = conn.execute(
        "SELECT anchor_type, anchor_ref, before_detail, after_detail, materiality_reason "
        "FROM source_version_diff_segments"
    ).fetchone()
    assert seg["anchor_type"] in sd.ANCHOR_TYPES
    assert seg["anchor_ref"] == "item-3"
    # structured, not a raw blob — round-trips as JSON with the changed field
    before, after = json.loads(seg["before_detail"]), json.loads(seg["after_detail"])
    assert before["time"] == "18:00" and after["time"] == "16:00"
    assert seg["materiality_reason"] == "material_field_change:time"
    # all four AC-2 fields non-null
    for col in ("anchor_type", "anchor_ref", "before_detail", "after_detail", "materiality_reason"):
        assert seg[col] is not None
    assert r["segments"][0]["materiality_reason"] == "material_field_change:time"


def test_ac2_unknown_anchor_type_rejected_by_code(conn):
    _two_versions(conn)
    bad = [{"anchor_type": "sidebar", "anchor_ref": "x", "fields": {"a": 1}}]
    with pytest.raises(sd.UnknownAnchorType):
        sd.detect_and_store(conn, source_url=_URL, old_content=[], new_content=bad)
    # nothing stored for the rejected change
    assert conn.execute("SELECT COUNT(*) FROM source_version_changes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_version_diff_segments").fetchone()[0] == 0


def test_ac2_unknown_anchor_type_rejected_by_db_check(conn):
    """The DB CHECK is the backstop if a future writer bypasses the vocab guard."""
    a, b = _two_versions(conn)
    cid = sd._change_id(a["version_id"], b["version_id"])
    conn.execute(
        "INSERT INTO source_version_changes (change_id, source_url, old_version_id, "
        "new_version_id, change_hash, late_change, lateness_basis, detected_utc) "
        "VALUES (?, ?, ?, ?, 'h', 0, NULL, 'u')",
        (cid, _URL, a["version_id"], b["version_id"]),
    )
    conn.commit()
    conn.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO source_version_diff_segments (segment_id, change_id, "
            "anchor_type, anchor_ref, before_detail, after_detail, materiality_reason, "
            "segment_ordinal) VALUES ('s', ?, 'bogus', 'r', '{}', '{}', 'm', 1)",
            (cid,),
        )
    conn.rollback()


def test_ac2_added_and_removed_anchors_are_typed(conn):
    """An added anchor (no before) and a removed anchor (no after) get the right
    materiality tokens — the fail-closed 'never drop a change' property."""
    _two_versions(conn)
    old = [{"anchor_type": "agenda_item", "anchor_ref": "item-1", "fields": {"title": "Call to order"}}]
    new = [{"anchor_type": "agenda_item", "anchor_ref": "item-9", "fields": {"title": "Emergency item"}}]
    r = sd.detect_and_store(conn, source_url=_URL, old_content=old, new_content=new)
    reasons = {s["anchor_ref"]: s["materiality_reason"] for s in r["segments"]}
    assert reasons == {"item-1": "anchor_removed", "item-9": "anchor_added"}


# ---------------------------------------------------------------------------
# AC-3 — deterministic + idempotent
# ---------------------------------------------------------------------------


def test_ac3_rerun_is_idempotent_with_stable_hash(conn):
    _two_versions(conn)
    first = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    second = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    assert first["action"] == "created" and second["action"] == "noop"
    assert first["change_hash"] == second["change_hash"]
    assert len(first["change_hash"]) == 64
    # no duplicate row/artifact
    assert conn.execute("SELECT COUNT(*) FROM source_version_changes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_version_diff_segments").fetchone()[0] == 1


def test_ac3_change_hash_is_content_addressed_not_wallclock(conn):
    """The stable hash depends only on the segments, not on detected_utc."""
    _two_versions(conn)
    r1 = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW, now="2026-01-01T00:00:00+00:00")
    # recompute the hash from the same segments with a different clock — identical
    r2_hash = sd.diff_change_hash(r1["segments"])
    assert r1["change_hash"] == r2_hash


def test_ac3_reproducibility_mismatch_is_refused(conn):
    """If the same pair ever recomputes a DIFFERENT diff, history is not overwritten
    — the writer refuses (non-determinism caught, fail-closed)."""
    a, b = _two_versions(conn)
    sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    # tamper: corrupt the stored change_hash so the recomputed one won't match
    conn.execute("UPDATE source_version_changes SET change_hash = 'TAMPERED'")
    conn.commit()
    with pytest.raises(sd.SourceDiffError):
        sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)


# ---------------------------------------------------------------------------
# AC-4 — version-pair binding (D-1 trace seed)
# ---------------------------------------------------------------------------


def test_ac4_single_join_reaches_source_versions_and_change(conn):
    a, b = _two_versions(conn)
    r = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    # source_versions -> change, both hops resolved by FK id, no dangle
    joined = conn.execute(
        "SELECT chg.change_id, ov.version_ordinal AS ov, nv.version_ordinal AS nv, "
        "ov.source_url AS src "
        "FROM source_version_changes chg "
        "JOIN source_versions ov ON ov.version_id = chg.old_version_id "
        "JOIN source_versions nv ON nv.version_id = chg.new_version_id "
        "WHERE chg.change_id = ?",
        (r["change_id"],),
    ).fetchone()
    assert joined is not None, "the change must join to BOTH version rows"
    assert joined["ov"] == 1 and joined["nv"] == 2
    assert joined["src"] == _URL
    # no dangling FK anywhere
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_ac4_explicit_pair_orientation_enforced(conn):
    """Passing the newer version as 'old' is a refused orientation error."""
    a, b = _two_versions(conn)
    with pytest.raises(sd.SourceDiffError):
        sd.detect_and_store(
            conn, source_url=_URL, old_version_id=b["version_id"],
            new_version_id=a["version_id"], old_content=_OLD, new_content=_NEW,
        )


# ---------------------------------------------------------------------------
# AC-5 — audit-record presence (D-3 backend half)
# ---------------------------------------------------------------------------


def test_ac5_audit_record_carries_materiality_detail_and_both_citations(conn):
    a, b = _two_versions(conn)
    r = sd.detect_and_store(
        conn, source_url=_URL, old_content=_OLD, new_content=_NEW, meeting_time=_MEETING
    )
    rec = sd.build_audit_record(conn, r["change_id"])
    assert rec["access"] == "reviewer_internal" and rec["scope"] == "alpine"
    # materiality reason present
    assert rec["materialityReasons"] == ["material_field_change:time"]
    # before/after change detail present and structured
    seg = rec["segments"][0]
    assert seg["before"]["time"] == "18:00" and seg["after"]["time"] == "16:00"
    # both-version source citations present
    assert rec["citations"]["old"]["versionId"] == a["version_id"]
    assert rec["citations"]["new"]["versionId"] == b["version_id"]
    assert rec["citations"]["old"]["contentHash"] and rec["citations"]["new"]["contentHash"]
    # lateness verdict present (reprocessing-status seed)
    assert rec["lateness"]["lateChange"] is True
    # the guards agree
    sd.assert_anchor_types_valid(rec)
    sd.assert_audit_record_complete(rec)


def test_ac5_audit_record_never_emits_a_raw_path(conn):
    """The reviewer-internal record surfaces snapshotPreserved, never the path."""
    a, b = _two_versions(conn)
    # plant a contained snapshot path on the new version (bypassing the writer)
    conn.execute(
        "UPDATE source_versions SET snapshot_path = ? WHERE version_id = ?",
        ("Database/preserved/source_versions/x.bin", b["version_id"]),
    )
    conn.commit()
    r = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    rec = sd.build_audit_record(conn, r["change_id"])
    assert rec["citations"]["new"]["snapshotPreserved"] is False  # contained but absent
    assert "Database/preserved" not in json.dumps(rec)


# ---------------------------------------------------------------------------
# AC-6 — read-site path containment (raise, don't clamp)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "../../etc/shadow"])
def test_ac6_escaping_snapshot_path_rejected_at_read(conn, bad_path):
    a, b = _two_versions(conn)
    conn.execute(
        "UPDATE source_versions SET snapshot_path = ? WHERE version_id = ?",
        (bad_path, a["version_id"]),
    )
    conn.commit()
    r = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    with pytest.raises(raw_preservation.RawPathEscape):
        sd.build_audit_record(conn, r["change_id"])


# ---------------------------------------------------------------------------
# X2 — deterministic late-change red flag: fires / does-not-fire pair
# ---------------------------------------------------------------------------


def test_x2_lateness_fires_within_meeting_proximity(conn):
    _two_versions(conn)  # new version retrieved 2026-08-04T17:00 — one hour pre-meeting
    r = sd.detect_and_store(
        conn, source_url=_URL, old_content=_OLD, new_content=_NEW, meeting_time=_MEETING
    )
    assert r["late_change"] is True
    assert r["lateness_basis"] == sd.LATE_MEETING_PROXIMITY


def test_x2_lateness_does_not_fire_far_from_meeting(conn):
    _two_versions(conn)  # new version retrieved 2026-08-04T17:00
    far_meeting = "2026-09-01T18:00:00+00:00"  # weeks away, outside the 48h window
    r = sd.detect_and_store(
        conn, source_url=_URL, old_content=_OLD, new_content=_NEW, meeting_time=far_meeting
    )
    assert r["late_change"] is False and r["lateness_basis"] is None


def test_x2_lateness_fires_when_changed_after_notified(conn):
    _two_versions(conn)
    r = sd.detect_and_store(
        conn, source_url=_URL, old_content=_OLD, new_content=_NEW,
        notified_after="2026-08-04T10:00:00+00:00",  # prior version notified 10:00, new retrieved 17:00
    )
    assert r["late_change"] is True and r["lateness_basis"] == sd.LATE_AFTER_NOTIFIED


def test_x2_lateness_basis_paired_check_in_db(conn):
    """A late change must store a basis; the paired CHECK is the DB backstop."""
    a, b = _two_versions(conn)
    conn.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(  # late_change=1 with NULL basis violates the paired CHECK
            "INSERT INTO source_version_changes (change_id, source_url, old_version_id, "
            "new_version_id, change_hash, late_change, lateness_basis, detected_utc) "
            "VALUES ('c', ?, ?, ?, 'h', 1, NULL, 'u')",
            (_URL, a["version_id"], b["version_id"]),
        )
    conn.rollback()


# ---------------------------------------------------------------------------
# Fail-closed: an undiffable change is flagged, never dropped
# ---------------------------------------------------------------------------


def test_undiffable_change_is_flagged_not_dropped(conn):
    """Bytes differ but no structured content localizes it -> one attachment-anchored
    segment tagged undiffable, not silence."""
    _two_versions(conn)
    r = sd.detect_and_store(conn, source_url=_URL)  # no content supplied
    assert r["detected"] is True
    seg = r["segments"][0]
    assert seg["anchor_type"] == sd.ANCHOR_ATTACHMENT
    assert seg["anchor_ref"] == _URL
    assert seg["materiality_reason"] == sd.UNDIFFABLE
    assert conn.execute("SELECT COUNT(*) FROM source_version_diff_segments").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# AC-7 — migration hygiene: 0034 re-runs clean
# ---------------------------------------------------------------------------


def test_ac7_migration_reruns_clean(tmp_path):
    db_path = tmp_path / "rerun.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # INV-5: IF NOT EXISTS -> a second run is a no-op
    with db.open_db(db_path) as c:
        chg = {r["name"] for r in c.execute("PRAGMA table_info(source_version_changes)")}
        seg = {r["name"] for r in c.execute("PRAGMA table_info(source_version_diff_segments)")}
    assert {"old_version_id", "new_version_id", "change_hash", "late_change"} <= chg
    assert {"anchor_type", "anchor_ref", "before_detail", "after_detail",
            "materiality_reason"} <= seg


# ---------------------------------------------------------------------------
# RED-proofs — the derivations are non-tautological
# ---------------------------------------------------------------------------


def test_redproof_neutering_materiality_flips_ac2(monkeypatch):
    """If derive_materiality stopped distinguishing material changes (always the
    non-material token), AC-2's material assertion would fail. Proven at the pure
    layer so the monkeypatch cannot leak into the DB path."""
    monkeypatch.setattr(sd, "derive_materiality", lambda before, after: sd.NONMATERIAL_CHANGE)
    segs = sd.compute_segments(_OLD, _NEW)
    # the disarmed derivation no longer reports the material time change
    assert segs[0]["materiality_reason"] != "material_field_change:time"
    assert segs[0]["materiality_reason"] == sd.NONMATERIAL_CHANGE


def test_redproof_neutering_lateness_flips_x2(monkeypatch):
    """If derive_lateness always returned not-late, the X2 red flag would never
    fire — the fires test would go RED against the disarmed rule."""
    monkeypatch.setattr(sd, "derive_lateness", lambda **kw: {"late": False, "basis": None})
    with_conn = sd.derive_lateness  # bound to the neutered version now
    verdict = with_conn(new_retrieval_time="2026-08-04T17:00:00+00:00", meeting_time=_MEETING)
    assert verdict["late"] is False  # the property X2 asserts is now violated


def test_redproof_dropping_a_segment_flips_completeness(conn):
    """If a build emitted an audit record with no segments, the AC-5 completeness
    guard must go RED rather than pass vacuously."""
    a, b = _two_versions(conn)
    r = sd.detect_and_store(conn, source_url=_URL, old_content=_OLD, new_content=_NEW)
    rec = sd.build_audit_record(conn, r["change_id"])
    rec["segments"] = []  # simulate a build that dropped the diff
    with pytest.raises(sd.SourceDiffAuditError):
        sd.assert_audit_record_complete(rec)
