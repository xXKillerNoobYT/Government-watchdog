"""Tests for supplied-file linkage + gap-detection update (GOV-1577 / B4).

Each issue acceptance criterion maps to a class below:

  AC1 file links to area/meeting/agenda item          -> TestLinkage
  AC2 linking a primary source flips the
      no_primary_source signal (real-gap fixture)      -> TestGapClosesOnPrimaryLink
  AC3 gap computation stays deterministic + source-
      grounded (no AI)                                 -> TestDeterministicSourceGrounded

Plus the structural / fail-closed guarantees B4 depends on:
  schema + CHECK-vocab parity with 0029                -> TestSchema
  upsert idempotency, fk to supplied_files, unlink     -> TestLinkage
  reversibility + human-disposition respect            -> TestGapReversibilityAndDispositions
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import completeness  # noqa: E402
import db  # noqa: E402
import file_linkage as fl  # noqa: E402
import file_records as fr  # noqa: E402

SHA = hashlib.sha256(b"%PDF-1.4 Town of Alpine council packet 2026-06-23").hexdigest()
SHA2 = hashlib.sha256(b"%PDF-1.4 supporting exhibit").hexdigest()

# A real meeting folder date, matching how structure_real_corpus keys the gap
# (subject_node_type='meeting', subject_node_id=<date>).
MEETING_DATE = "2026-06-23"


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "b4.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


def _record(conn, *, sha=SHA, area="alpine", source_type="agenda_packet"):
    """Insert a supplied_files row (B2) so it can be linked (B4)."""
    return fr.insert_file_record(
        conn,
        area=area,
        source_type=source_type,
        original_filename="2026-06-23-packet.pdf",
        sha256=sha,
        mime="application/pdf",
        byte_size=51234,
        supplied_by="isaac",
        captured_at="2026-06-23T00:00:00.000+00:00",
    )


def _seed_no_primary_source_gap(conn, subject_id=MEETING_DATE, subject_type="meeting"):
    """The 'real gap' fixture: a deterministic no_primary_source gap, open, exactly
    as the structuring detector (structure_real_corpus via completeness) emits it."""
    return completeness.record_gap(
        conn,
        subject_node_id=subject_id,
        subject_node_type=subject_type,
        gap_type="no_primary_source",
        detail="meeting folder has only derived material",
    )


# --- AC1: file links to area / meeting / agenda item ------------------------

class TestSchema:
    def test_table_exists_with_expected_columns(self, conn):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(supplied_file_links)")}
        assert cols == {
            "link_id", "file_id", "subject_node_type", "subject_node_id",
            "is_primary_source", "linked_by", "linked_at",
        }

    def test_subject_type_vocab_matches_check(self, conn):
        # SSOT parity: the module vocabulary must be exactly what the 0029 CHECK
        # allows (the concept_map/completeness parity-guard pattern).
        rec = _record(conn)
        for t in fl.LINK_SUBJECT_TYPES:
            fl.link_file(
                conn, file_id=rec.file_id, subject_node_type=t,
                subject_node_id="x", linked_by="isaac",
            )
        # a type outside the vocab is rejected before any write
        with pytest.raises(fl.UnknownSubjectType):
            fl.link_file(
                conn, file_id=rec.file_id, subject_node_type="planet",
                subject_node_id="x", linked_by="isaac",
            )


class TestLinkage:
    @pytest.mark.parametrize("subject_type", ["area", "meeting", "agenda_item"])
    def test_link_to_each_subject_type(self, conn, subject_type):
        rec = _record(conn)
        link = fl.link_file(
            conn, file_id=rec.file_id, subject_node_type=subject_type,
            subject_node_id="subj-1", linked_by="isaac", is_primary_source=True,
        )
        assert link.subject_node_type == subject_type
        assert link.is_primary_source is True
        got = fl.links_for_subject(conn, subject_type, "subj-1")
        assert [l.file_id for l in got] == [rec.file_id]

    def test_link_requires_existing_file_record(self, conn):
        # fk-in-spirit: cannot link a file that B2 never recorded (fail-closed)
        with pytest.raises(fl.FileLinkageError):
            fl.link_file(
                conn, file_id="file-does-not-exist", subject_node_type="meeting",
                subject_node_id=MEETING_DATE, linked_by="isaac",
            )
        # the FK also holds at the DB layer
        assert conn.execute("SELECT COUNT(*) FROM supplied_file_links").fetchone()[0] == 0

    def test_relink_is_idempotent_upsert(self, conn):
        rec = _record(conn)
        fl.link_file(
            conn, file_id=rec.file_id, subject_node_type="meeting",
            subject_node_id=MEETING_DATE, linked_by="isaac", is_primary_source=False,
        )
        # operator corrects the classification: same (file, subject) -> update in place
        link2 = fl.link_file(
            conn, file_id=rec.file_id, subject_node_type="meeting",
            subject_node_id=MEETING_DATE, linked_by="mark", is_primary_source=True,
        )
        rows = fl.links_for_subject(conn, "meeting", MEETING_DATE)
        assert len(rows) == 1  # no duplicate
        assert rows[0].is_primary_source is True
        assert rows[0].linked_by == "mark"
        assert link2.link_id == rows[0].link_id

    def test_links_for_file_lists_every_subject(self, conn):
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac")
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="area",
                     subject_node_id="alpine", linked_by="isaac")
        subjects = {(l.subject_node_type, l.subject_node_id)
                    for l in fl.links_for_file(conn, rec.file_id)}
        assert subjects == {("meeting", MEETING_DATE), ("area", "alpine")}

    def test_unlink_removes_row(self, conn):
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac")
        assert fl.unlink_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                              subject_node_id=MEETING_DATE) is True
        assert fl.links_for_subject(conn, "meeting", MEETING_DATE) == []
        # unlinking a non-existent link is a harmless False
        assert fl.unlink_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                              subject_node_id=MEETING_DATE) is False


# --- AC2: linking a primary source flips the no_primary_source signal --------

class TestGapClosesOnPrimaryLink:
    def test_primary_link_flips_open_gap_to_resolved(self, conn):
        # Real gap fixture: an OPEN no_primary_source gap for the meeting.
        gap_id = _seed_no_primary_source_gap(conn)
        assert completeness.gaps_for(conn, gap_type="no_primary_source",
                                     only_open=True)  # exists + open

        # Supply + link a PRIMARY source to that same meeting.
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)

        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)

        assert result.has_primary_source is True
        assert result.gap_id == gap_id
        assert result.previous_status == "open"
        assert result.new_status == "resolved"
        assert result.changed is True
        # the gap row itself is now resolved (no longer an open gap)
        assert completeness.gaps_for(conn, gap_type="no_primary_source",
                                     only_open=True) == []
        row = conn.execute(
            "SELECT resolved_status FROM completeness_gaps WHERE gap_id = ?", (gap_id,)
        ).fetchone()
        assert row[0] == "resolved"

    def test_non_primary_link_does_not_close_gap(self, conn):
        # A supporting (is_primary_source=False) file must NOT close the gap.
        _seed_no_primary_source_gap(conn)
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=False)
        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert result.has_primary_source is False
        assert result.changed is False
        assert result.new_status == "open"

    def test_rejected_primary_source_does_not_count(self, conn):
        # A repudiated (rejected) file is not a source: gap stays open.
        _seed_no_primary_source_gap(conn)
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        fr.set_review_state(conn, rec.file_id, "rejected")
        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert result.has_primary_source is False
        assert result.new_status == "open"

    def test_pending_primary_source_counts(self, conn):
        # review_state and completeness are DISTINCT axes: a still-pending primary
        # source means the bytes EXIST, so it closes the completeness gap.
        _seed_no_primary_source_gap(conn)
        rec = _record(conn)
        assert rec.review_state == "pending"
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert result.has_primary_source is True
        assert result.new_status == "resolved"

    def test_refresh_never_creates_a_gap(self, conn):
        # No pre-existing gap + a primary link -> nothing invented (B4 resolves,
        # it does not author gaps).
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert result.gap_id is None
        assert result.changed is False
        assert conn.execute("SELECT COUNT(*) FROM completeness_gaps").fetchone()[0] == 0


# --- AC3: deterministic + source-grounded (no AI) ---------------------------

class TestDeterministicSourceGrounded:
    def test_refresh_is_idempotent(self, conn):
        _seed_no_primary_source_gap(conn)
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        first = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        second = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert first.new_status == "resolved" and first.changed is True
        # re-running the same inputs changes nothing (deterministic + idempotent)
        assert second.new_status == "resolved" and second.changed is False

    def test_has_primary_source_reads_only_real_rows(self, conn):
        # source-grounded: verdict is a pure function of the linked supplied_files
        # rows — no linked primary source -> False.
        assert fl.has_primary_source(conn, "meeting", MEETING_DATE) is False
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        assert fl.has_primary_source(conn, "meeting", MEETING_DATE) is True

    def test_produced_by_stays_deterministic(self, conn):
        # B4 never rewrites provenance to 'ai'; the gap it moves remains a
        # deterministic gap.
        gap_id = _seed_no_primary_source_gap(conn)
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        row = conn.execute(
            "SELECT produced_by FROM completeness_gaps WHERE gap_id = ?", (gap_id,)
        ).fetchone()
        assert row[0] == "deterministic"


# --- reversibility + human dispositions -------------------------------------

class TestGapReversibilityAndDispositions:
    def test_unlink_reopens_resolved_gap(self, conn):
        gap_id = _seed_no_primary_source_gap(conn)
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        # the primary source goes away (unlink / supersede in B5)
        fl.unlink_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                       subject_node_id=MEETING_DATE)
        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert result.previous_status == "resolved"
        assert result.new_status == "open"  # gap never lies about a vanished source
        assert result.changed is True

    def test_human_wontfix_is_not_clobbered(self, conn):
        # A reviewer-set disposition (wontfix/acknowledged) is left untouched even
        # when a primary source is present.
        gap_id = _seed_no_primary_source_gap(conn)
        conn.execute(
            "UPDATE completeness_gaps SET resolved_status = 'wontfix' WHERE gap_id = ?",
            (gap_id,),
        )
        conn.commit()
        rec = _record(conn)
        fl.link_file(conn, file_id=rec.file_id, subject_node_type="meeting",
                     subject_node_id=MEETING_DATE, linked_by="isaac",
                     is_primary_source=True)
        result = fl.refresh_no_primary_source_gap(conn, "meeting", MEETING_DATE)
        assert result.gap_id is None  # not an owned gap -> B4 leaves it alone
        assert result.changed is False
        row = conn.execute(
            "SELECT resolved_status FROM completeness_gaps WHERE gap_id = ?", (gap_id,)
        ).fetchone()
        assert row[0] == "wontfix"


# --- GOV-1686 (C1, ingest-provenance): pin P-3, the invariant most likely to be
#     "fixed" by someone reading this module cold ---------------------------------


class TestGapTracksCoverageNotPublishability:
    """`Docs/supplied-file-provenance-contract.md` P-3.

    `NON_COUNTING_REVIEW_STATES` contains **only** `rejected`, so a `pending`,
    entirely unreviewed file closes the `no_primary_source` gap. Read cold that
    looks fail-open in a fail-closed codebase, and the natural "fix" is to
    require `web_safe`.

    It is not a bug. The gap asks *"does this subject have a source at all?"* —
    coverage — while publishability is a **separate** gate (B6's web-safe read
    projection). Conflating them would silently redefine "gap closed" across the
    whole completeness surface.

    These tests exist so that redefinition cannot happen quietly: it has to fail
    here first, and the failure names the contract.
    """

    def test_an_unreviewed_pending_file_closes_the_gap(self, conn):
        rec = fr.insert_file_record(
            conn, area="alpine", source_type="council_packet",
            original_filename="packet.pdf", sha256=SHA, mime="application/pdf",
            byte_size=1024, supplied_by="clerk@example.gov",
            captured_at="2026-06-23T10:00:00+00:00")
        assert rec.review_state == "pending", (
            "a new record must start `pending` — review_state is deliberately not "
            "an insert parameter")
        fl.link_file(conn, subject_node_type="meeting", subject_node_id=MEETING_DATE,
                     file_id=rec.file_id, is_primary_source=True,
                     linked_by="clerk@example.gov")
        assert fl.has_primary_source(conn, "meeting", MEETING_DATE) is True, (
            "P-3: an unreviewed `pending` file MUST still count as coverage. If "
            "this now fails, someone tightened NON_COUNTING_REVIEW_STATES — that "
            "changes what a closed gap MEANS everywhere. See the contract.")

    def test_only_a_repudiated_file_fails_to_count(self, conn):
        """The complement: `rejected` is the ONE state that does not count."""
        rec = fr.insert_file_record(
            conn, area="alpine", source_type="council_packet",
            original_filename="packet.pdf", sha256=SHA, mime="application/pdf",
            byte_size=1024, supplied_by="clerk@example.gov",
            captured_at="2026-06-23T10:00:00+00:00")
        fl.link_file(conn, subject_node_type="meeting", subject_node_id=MEETING_DATE,
                     file_id=rec.file_id, is_primary_source=True,
                     linked_by="clerk@example.gov")
        fr.set_review_state(conn, rec.file_id, "rejected")
        assert fl.has_primary_source(conn, "meeting", MEETING_DATE) is False, (
            "a repudiated file is not a source — `rejected` must not count")
        # And every OTHER state does count, which is what makes the set minimal.
        for state in ("reviewing", "held"):
            fr.set_review_state(conn, rec.file_id, state)
            assert fl.has_primary_source(conn, "meeting", MEETING_DATE) is True, (
                f"P-3: `{state}` must count as coverage; only `rejected` does not")


def test_make_link_id_is_deterministic_and_is_the_uniqueness_key(conn):
    """`Docs/supplied-file-provenance-contract.md` P-5, pinned DIRECTLY.

    C1b (GOV-1687) found P-5 covered only *indirectly*, through
    `test_relink_is_idempotent_upsert`. That test would still pass if the id
    became random, because the upsert also matches on the natural key — so the
    determinism itself was unguarded. C4 closes that: same inputs → same id,
    different inputs → different id.
    """
    a = fl.make_link_id("meeting", MEETING_DATE, "file-1")
    assert a == fl.make_link_id("meeting", MEETING_DATE, "file-1"), (
        "make_link_id must be deterministic — a random id would let the same "
        "file attach to the same subject twice and drift every link count")
    assert a != fl.make_link_id("meeting", MEETING_DATE, "file-2")
    assert a != fl.make_link_id("area", MEETING_DATE, "file-1")
    assert a != fl.make_link_id("meeting", "2026-01-01", "file-1")


# --- GOV-1695 (C9 hunt): P-9 — the hot lookups must stay index-backed ---------
#
# Measured with EXPLAIN QUERY PLAN, because grepping `CREATE INDEX` under-reports
# (a UNIQUE constraint is already an index — CLAUDE.md records that trap).
#
# The failure this guards is SILENT: a migration that renames or drops an index,
# or a query that changes shape, degrades SEARCH -> SCAN with no error and no
# failing test — just a surface that gets slower as the corpus grows. Same
# "correct answer, wrong cost" class as data-model INV-8.

#: (label, sql, params, index-name-fragment) — each MUST use an index.
_INDEX_BACKED = [
    ("has_primary_source",
     "SELECT 1 FROM supplied_file_links l JOIN supplied_files f ON f.file_id = l.file_id"
     " WHERE l.subject_node_type=? AND l.subject_node_id=? AND l.is_primary_source=1"
     " LIMIT 1", ("area", "alpine"), "idx_supplied_file_links"),
    ("links_for_subject",
     "SELECT link_id FROM supplied_file_links WHERE subject_node_type=? AND"
     " subject_node_id=? ORDER BY linked_at, link_id", ("area", "alpine"),
     "idx_supplied_file_links"),
    ("links_for_file",
     "SELECT link_id FROM supplied_file_links WHERE file_id=? ORDER BY linked_at,"
     " link_id", ("f",), "idx_supplied_file_links_file"),
    ("list_versions",
     "SELECT file_id FROM supplied_files WHERE version_group_id=? ORDER BY"
     " created_at, file_id", ("g",), "idx_supplied_files_version_group"),
    ("list_dependencies",
     "SELECT dependency_id FROM supplied_file_dependencies WHERE file_id=?", ("f",),
     "idx_sfdep_file"),
]


def _plan(conn, sql, params) -> str:
    return " | ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


class TestHotLookupsStayIndexBacked:
    """`Docs/supplied-file-provenance-contract.md` P-9."""

    @pytest.mark.parametrize("label,sql,params,index_frag", _INDEX_BACKED,
                             ids=[q[0] for q in _INDEX_BACKED])
    def test_lookup_uses_an_index(self, conn, label, sql, params, index_frag):
        plan = _plan(conn, sql, params)
        assert "SEARCH" in plan, (
            f"{label} degraded to a full scan — plan: {plan}. A selective lookup "
            "that scans is a silent cost regression: same answer, wrong cost.")
        assert index_frag in plan, (
            f"{label} no longer uses an index named like {index_frag!r} — "
            f"plan: {plan}. If the index was deliberately renamed, update P-9.")

    # A sibling test asserting the two full-corpus queries "stay scans" was written
    # here and then DELETED, because its red proof exposed it as both weak and
    # wrong-headed (GOV-1695):
    #
    #   * weak — `"SCAN" in plan` cannot tell `SCAN documents` from
    #     `SCAN documents USING COVERING INDEX ...`; adding an index left it green;
    #   * wrong-headed — tightening it to reject `USING` would fail on a COVERING
    #     INDEX, which is a pure win here: the same rows visited, less I/O. A guard
    #     that fails on a legitimate improvement obstructs rather than protects.
    #
    # The property actually wanted — "this query has no selective predicate, so it
    # visits every row" — belongs to the QUERY, not the plan, and no plan assertion
    # states it cleanly. It is documented in P-9 instead, where a reader can act on it.
