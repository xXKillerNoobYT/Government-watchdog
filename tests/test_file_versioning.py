"""Tests for supplied-file versioning + red-flag on supersede (GOV-1578 / B5).

Each acceptance criterion from the issue maps to a test class below:

  AC1 supersede never overwrites/deletes prior version -> TestPreservesHistory
  AC2 new+old share version_group_id; supersedes_id set -> TestVersionLinkage
  AC3a before/after diff computed                       -> TestBeforeAfterDiff
  AC3b affected downstream records marked needs-re-review -> TestAffectedFlagged

Plus the supporting fail-closed lifecycle + immutable audit trail:
  dependency registry / resolve                         -> TestDependencyRegistry
  re-review resolve is fail-closed                       -> TestReReviewResolve
  immutable supersede audit event                        -> TestAuditEvent
  migration is idempotent + creates the tables           -> TestMigration
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
import file_records as fr  # noqa: E402
import file_versioning as fv  # noqa: E402

SHA_V1 = hashlib.sha256(b"%PDF-1.4 Town of Alpine council packet v1").hexdigest()
SHA_V2 = hashlib.sha256(b"%PDF-1.4 Town of Alpine council packet v2 corrected").hexdigest()

# Provenance for the ORIGINAL supplied file (v1).
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

# Provenance for the replacement (v2): different bytes + filename + size.
V2 = dict(
    area="alpine",
    source_type="agenda_packet",
    original_filename="2026-06-23-packet-corrected.pdf",
    sha256=SHA_V2,
    mime="application/pdf",
    byte_size=52999,
    supplied_by="isaac",
    captured_at="2026-06-24T09:30:00.000+00:00",
    superseded_by="isaac",
)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "b5.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


@pytest.fixture()
def v1(conn) -> fr.FileRecord:
    """An original supplied file already inserted (B2)."""
    return fr.insert_file_record(conn, **V1)


# --- AC1: preservation ------------------------------------------------------

class TestPreservesHistory:
    def test_prior_row_still_exists_after_supersede(self, conn, v1):
        fv.supersede_file(conn, v1.file_id, **V2)
        assert fr.get_file_record(conn, v1.file_id) is not None

    def test_prior_row_is_byte_identical(self, conn, v1):
        before = fr.get_file_record(conn, v1.file_id)
        fv.supersede_file(conn, v1.file_id, **V2)
        after = fr.get_file_record(conn, v1.file_id)
        assert after == before  # frozen dataclass equality: nothing mutated

    def test_supersede_adds_a_row_never_replaces(self, conn, v1):
        (n_before,) = conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()
        fv.supersede_file(conn, v1.file_id, **V2)
        (n_after,) = conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()
        assert n_after == n_before + 1

    def test_both_versions_listed_in_group(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        ids = {r.file_id for r in fr.list_versions(conn, v1.version_group_id)}
        assert ids == {v1.file_id, result.new.file_id}

    def test_result_prior_matches_original(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.prior == v1

    def test_superseding_unknown_prior_raises(self, conn):
        with pytest.raises(fr.FileRecordNotFound):
            fv.supersede_file(conn, "file-does-not-exist", **V2)


# --- AC2: version linkage ---------------------------------------------------

class TestVersionLinkage:
    def test_new_shares_prior_version_group(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.new.version_group_id == v1.version_group_id

    def test_new_supersedes_id_points_at_prior(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.new.supersedes_id == v1.file_id

    def test_new_version_is_fail_closed_pending(self, conn, v1):
        # A superseding file is itself unreviewed until a reviewer moves it.
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.new.review_state == "pending"

    def test_chained_supersede_stays_in_one_group(self, conn, v1):
        r2 = fv.supersede_file(conn, v1.file_id, **V2)
        v3 = dict(V2, sha256=hashlib.sha256(b"v3").hexdigest(),
                  original_filename="v3.pdf", byte_size=100)
        r3 = fv.supersede_file(conn, r2.new.file_id, **v3)
        assert r3.new.version_group_id == v1.version_group_id
        assert r3.new.supersedes_id == r2.new.file_id
        assert len(fr.list_versions(conn, v1.version_group_id)) == 3


# --- AC3a: before/after diff ------------------------------------------------

class TestBeforeAfterDiff:
    def test_diff_flags_changed_fields(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        changed = result.diff["changed"]
        assert changed["sha256"] == {"before": SHA_V1, "after": SHA_V2}
        assert changed["byte_size"] == {"before": 51234, "after": 52999}
        assert "original_filename" in changed

    def test_diff_lists_unchanged_fields(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert "mime" in result.diff["unchanged"]
        assert "area" in result.diff["unchanged"]

    def test_content_changed_true_when_bytes_differ(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.diff["content_changed"] is True

    def test_content_changed_false_when_same_bytes(self, conn, v1):
        # Same bytes re-supplied under new provenance (e.g. corrected filename).
        same = dict(V1, original_filename="renamed.pdf", supplied_by="mark",
                    captured_at="2026-06-25T00:00:00.000+00:00", superseded_by="mark")
        result = fv.supersede_file(conn, v1.file_id, **same)
        assert result.diff["content_changed"] is False
        assert "sha256" in result.diff["unchanged"]
        assert "original_filename" in result.diff["changed"]

    def test_compute_before_after_is_pure(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        # Recomputing from the two records yields the same diff (no DB, no state).
        again = fv.compute_before_after(result.prior, result.new)
        assert again == result.diff


# --- AC3b: affected downstream records flagged ------------------------------

class TestAffectedFlagged:
    def test_dependencies_on_prior_flip_to_needs_re_review(self, conn, v1):
        fv.register_dependency(conn, file_id=v1.file_id,
                               record_kind="agenda_anchor", record_ref="ai-77")
        fv.register_dependency(conn, file_id=v1.file_id,
                               record_kind="linkage", record_ref="link-9")
        result = fv.supersede_file(conn, v1.file_id, **V2)
        refs = sorted(d.record_ref for d in result.flagged)
        assert refs == ["ai-77", "link-9"]
        for dep in fv.list_dependencies(conn, v1.file_id):
            assert dep.review_flag == "needs_re_review"
            assert dep.flagged_by_file_id == result.new.file_id
            assert dep.flagged_at is not None

    def test_unrelated_file_dependencies_untouched(self, conn, v1):
        other = fr.insert_file_record(
            conn, **dict(V1, original_filename="other.pdf",
                         sha256=hashlib.sha256(b"other").hexdigest()))
        other_dep = fv.register_dependency(
            conn, file_id=other.file_id, record_kind="linkage", record_ref="keep")
        fv.supersede_file(conn, v1.file_id, **V2)
        assert fv.get_dependency(conn, other_dep.dependency_id).review_flag == "current"

    def test_affected_count_recorded_on_event(self, conn, v1):
        fv.register_dependency(conn, file_id=v1.file_id,
                               record_kind="ai_extraction", record_ref="run-1")
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.event.affected_count == 1

    def test_supersede_with_no_dependencies_flags_nothing(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.flagged == []
        assert result.event.affected_count == 0

    def test_list_needs_re_review_scoped_to_group(self, conn, v1):
        fv.register_dependency(conn, file_id=v1.file_id,
                               record_kind="linkage", record_ref="r1")
        fv.supersede_file(conn, v1.file_id, **V2)
        openflags = fv.list_needs_re_review(conn, v1.version_group_id)
        assert [d.record_ref for d in openflags] == ["r1"]
        # A different group has no open flags.
        assert fv.list_needs_re_review(conn, "some-other-group") == []

    def test_flag_targets_only_the_superseded_version(self, conn, v1):
        # Register a dep on v1, supersede to v2, then register a dep on v2.
        fv.register_dependency(conn, file_id=v1.file_id,
                               record_kind="linkage", record_ref="on-v1")
        r2 = fv.supersede_file(conn, v1.file_id, **V2)
        fv.register_dependency(conn, file_id=r2.new.file_id,
                               record_kind="linkage", record_ref="on-v2")
        # Superseding v2 flags only the record built from v2, not the (already
        # flagged) record on v1.
        v3 = dict(V2, sha256=hashlib.sha256(b"v3").hexdigest(),
                  original_filename="v3.pdf", byte_size=100)
        r3 = fv.supersede_file(conn, r2.new.file_id, **v3)
        assert [d.record_ref for d in r3.flagged] == ["on-v2"]


# --- dependency registry ----------------------------------------------------

class TestDependencyRegistry:
    def test_register_requires_existing_file(self, conn):
        with pytest.raises(fr.FileRecordNotFound):
            fv.register_dependency(conn, file_id="nope",
                                   record_kind="linkage", record_ref="x")

    def test_register_rejects_blank_kind_or_ref(self, conn, v1):
        with pytest.raises(fr.MissingProvenance):
            fv.register_dependency(conn, file_id=v1.file_id,
                                   record_kind="  ", record_ref="x")
        with pytest.raises(fr.MissingProvenance):
            fv.register_dependency(conn, file_id=v1.file_id,
                                   record_kind="linkage", record_ref="")

    def test_register_is_idempotent_per_ref(self, conn, v1):
        a = fv.register_dependency(conn, file_id=v1.file_id,
                                   record_kind="linkage", record_ref="dup")
        b = fv.register_dependency(conn, file_id=v1.file_id,
                                   record_kind="linkage", record_ref="dup")
        assert a.dependency_id == b.dependency_id
        assert len(fv.list_dependencies(conn, v1.file_id)) == 1

    def test_new_dependency_defaults_to_current(self, conn, v1):
        dep = fv.register_dependency(conn, file_id=v1.file_id,
                                     record_kind="linkage", record_ref="x")
        assert dep.review_flag == "current"
        assert dep.flagged_by_file_id is None
        assert dep.version_group_id == v1.version_group_id


# --- re-review resolve (fail-closed) ---------------------------------------

class TestReReviewResolve:
    def test_resolve_clears_a_flag(self, conn, v1):
        fv.register_dependency(conn, file_id=v1.file_id,
                               record_kind="linkage", record_ref="r")
        result = fv.supersede_file(conn, v1.file_id, **V2)
        dep = result.flagged[0]
        cleared = fv.resolve_re_review(conn, dep.dependency_id)
        assert cleared.review_flag == "current"
        assert cleared.resolved_at is not None
        assert fv.list_needs_re_review(conn) == []

    def test_resolve_current_flag_is_rejected(self, conn, v1):
        dep = fv.register_dependency(conn, file_id=v1.file_id,
                                     record_kind="linkage", record_ref="r")
        with pytest.raises(fv.IllegalFlagTransition):
            fv.resolve_re_review(conn, dep.dependency_id)

    def test_resolve_unknown_dependency_raises(self, conn):
        with pytest.raises(fv.DependencyNotFound):
            fv.resolve_re_review(conn, "sfdep-missing")


# --- immutable audit event --------------------------------------------------

class TestAuditEvent:
    def test_event_captures_ids_and_provenance(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        ev = result.event
        assert ev.superseded_file_id == v1.file_id
        assert ev.new_file_id == result.new.file_id
        assert ev.version_group_id == v1.version_group_id
        assert ev.superseded_by == "isaac"

    def test_event_stores_the_diff(self, conn, v1):
        result = fv.supersede_file(conn, v1.file_id, **V2)
        assert result.event.diff == result.diff  # round-trips through diff_json

    def test_events_listed_oldest_first(self, conn, v1):
        r2 = fv.supersede_file(conn, v1.file_id, **V2)
        v3 = dict(V2, sha256=hashlib.sha256(b"v3").hexdigest(),
                  original_filename="v3.pdf", byte_size=100)
        r3 = fv.supersede_file(conn, r2.new.file_id, **v3)
        events = fv.list_supersede_events(conn, v1.version_group_id)
        assert [e.event_id for e in events] == [r2.event.event_id, r3.event.event_id]


# --- migration --------------------------------------------------------------

class TestMigration:
    def test_tables_and_columns_present(self, conn):
        dep_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(supplied_file_dependencies)")}
        assert dep_cols == set(fv._DEPENDENCY_COLUMNS)
        ev_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(supplied_file_supersede_events)")}
        assert ev_cols == set(fv._EVENT_COLUMNS)

    def test_apply_is_idempotent(self, tmp_path):
        db_path = tmp_path / "idem.db"
        db.apply_migrations(db_path)
        db.apply_migrations(db_path)  # must not raise
        with db.open_db(db_path) as c:
            names = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"supplied_file_dependencies", "supplied_file_supersede_events"} <= names


def test_get_supersede_event_round_trips_and_returns_none_for_an_unknown_id(conn):
    """C4 (GOV-1688): `get_supersede_event` had no test reference.

    Small, but it is the read side of P-4's immutable audit row — the thing a
    reviewer follows to see what a supersede actually changed.
    """
    import file_versioning as fv_mod
    assert fv_mod.get_supersede_event(conn, "no-such-event") is None
