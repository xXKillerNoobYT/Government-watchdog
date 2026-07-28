"""Tests for the supplied-file record + provenance model (GOV-1575 / B2).

Each acceptance criterion from the issue maps to a test class below:

  AC1 migration + all fields + review_state defaults 'pending' -> TestSchema
  AC2 provenance mandatory / non-null on insert                -> TestMandatoryProvenance
  AC3 no column stores AI interpretation as fact               -> TestNoAiAsFact
  AC4 version_group_id / supersedes_id support B5 versioning   -> TestVersioning

Plus the fail-closed review lifecycle that backs review-before-display:
                                                                 -> TestReviewLifecycle
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

SHA = hashlib.sha256(b"%PDF-1.4 Town of Alpine council packet").hexdigest()
OTHER_SHA = hashlib.sha256(b"%PDF-1.4 a different agenda").hexdigest()

GOOD = dict(
    area="alpine",
    source_type="agenda_packet",
    original_filename="2026-06-23-packet.pdf",
    sha256=SHA,
    mime="application/pdf",
    byte_size=51234,
    supplied_by="isaac",
    captured_at="2026-06-23T00:00:00.000+00:00",
)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "b2.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


# --- AC1: migration, all fields, fail-closed default ------------------------

class TestSchema:
    def test_table_and_all_fields_present(self, conn):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(supplied_files)")}
        # Every field named in the B2 spec must exist.
        assert cols == set(fr.PROVENANCE_COLUMNS)

    def test_review_state_defaults_pending(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        assert rec.review_state == "pending"
        # confirmed at the DB layer too (default, not just the model)
        row = conn.execute(
            "SELECT review_state FROM supplied_files WHERE file_id = ?", (rec.file_id,)
        ).fetchone()
        assert row[0] == "pending"

    def test_raw_sql_insert_without_review_state_is_pending(self, conn):
        # The DDL DEFAULT is the fail-closed backstop even for a bypassing writer.
        conn.execute(
            "INSERT INTO supplied_files (file_id, area, source_type, original_filename,"
            " supplied_by, captured_at, sha256, mime, byte_size, version_group_id,"
            " created_at) VALUES ('f1','alpine','notice','n.pdf','isaac','t',?,"
            " 'application/pdf', 10, 'f1', 't')",
            (SHA,),
        )
        row = conn.execute("SELECT review_state FROM supplied_files WHERE file_id='f1'").fetchone()
        assert row[0] == "pending"

    def test_review_state_check_rejects_unknown_value(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO supplied_files (file_id, area, source_type,"
                " original_filename, supplied_by, captured_at, sha256, mime, byte_size,"
                " review_state, version_group_id, created_at) VALUES"
                " ('f2','alpine','notice','n.pdf','isaac','t',?, 'application/pdf', 10,"
                " 'bogus', 'f2', 't')",
                (SHA,),
            )

    def test_sha256_length_check(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO supplied_files (file_id, area, source_type,"
                " original_filename, supplied_by, captured_at, sha256, mime, byte_size,"
                " version_group_id, created_at) VALUES"
                " ('f3','alpine','notice','n.pdf','isaac','t','tooshort',"
                " 'application/pdf', 10, 'f3', 't')"
            )


# --- AC2: provenance mandatory / non-null on insert -------------------------

class TestMandatoryProvenance:
    @pytest.mark.parametrize("field", [
        "area", "source_type", "original_filename", "supplied_by", "captured_at", "mime",
    ])
    def test_blank_text_field_rejected(self, conn, field):
        bad = dict(GOOD, **{field: "   "})
        with pytest.raises(fr.MissingProvenance):
            fr.insert_file_record(conn, **bad)

    @pytest.mark.parametrize("field", [
        "area", "source_type", "original_filename", "supplied_by", "captured_at", "mime",
    ])
    def test_none_text_field_rejected(self, conn, field):
        bad = dict(GOOD, **{field: None})
        with pytest.raises(fr.MissingProvenance):
            fr.insert_file_record(conn, **bad)

    def test_malformed_sha256_rejected(self, conn):
        with pytest.raises(fr.MissingProvenance):
            fr.insert_file_record(conn, **dict(GOOD, sha256="not-a-hash"))

    def test_negative_byte_size_rejected(self, conn):
        with pytest.raises(fr.MissingProvenance):
            fr.insert_file_record(conn, **dict(GOOD, byte_size=-1))

    def test_bool_byte_size_rejected(self, conn):
        # bool is an int subclass; guard against True/False sneaking in.
        with pytest.raises(fr.MissingProvenance):
            fr.insert_file_record(conn, **dict(GOOD, byte_size=True))

    def test_db_columns_are_not_null(self, conn):
        info = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(supplied_files)")}
        # notnull flag (row[3]) == 1 for every mandatory column; origin_url/supersedes_id nullable.
        mandatory = [
            "file_id", "area", "source_type", "original_filename", "supplied_by",
            "captured_at", "sha256", "mime", "byte_size", "review_state",
            "version_group_id", "created_at",
        ]
        for col in mandatory:
            assert info[col] == 1, f"{col} must be NOT NULL"
        assert info["origin_url"] == 0
        assert info["supersedes_id"] == 0

    def test_origin_url_is_optional(self, conn):
        rec = fr.insert_file_record(conn, **dict(GOOD, origin_url=None))
        assert rec.origin_url is None
        rec2 = fr.insert_file_record(
            conn, **dict(GOOD, origin_url="https://alpinewy.gov/packet.pdf")
        )
        assert rec2.origin_url == "https://alpinewy.gov/packet.pdf"


# --- AC3: no AI interpretation stored as fact -------------------------------

class TestNoAiAsFact:
    def test_no_ai_flavoured_column_exists(self, conn):
        cols = {row[1].lower() for row in conn.execute("PRAGMA table_info(supplied_files)")}
        banned = ("ai", "model", "summary", "interpret", "classification", "claim",
                  "extract", "confidence", "prediction", "score", "sentiment")
        offenders = [c for c in cols if any(tok in c for tok in banned)]
        assert offenders == [], f"AI-interpretation columns leaked into the record: {offenders}"

    def test_column_set_is_exactly_the_provenance_set(self, conn):
        cols = {row[1] for row in conn.execute("PRAGMA table_info(supplied_files)")}
        # Pin the schema: adding any column requires updating PROVENANCE_COLUMNS
        # deliberately, so an AI field cannot slip in unnoticed.
        assert cols == set(fr.PROVENANCE_COLUMNS)


# --- AC4: version_group_id / supersedes_id support B5 versioning ------------

class TestVersioning:
    def test_new_file_starts_its_own_group(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        assert rec.version_group_id == rec.file_id
        assert rec.supersedes_id is None

    def test_supersede_inherits_group(self, conn):
        v1 = fr.insert_file_record(conn, **GOOD)
        v2 = fr.insert_file_record(
            conn, **dict(GOOD, sha256=OTHER_SHA, original_filename="packet-v2.pdf"),
            supersedes_id=v1.file_id,
        )
        assert v2.supersedes_id == v1.file_id
        assert v2.version_group_id == v1.version_group_id
        assert v2.file_id != v1.file_id

    def test_list_versions_returns_group_oldest_first(self, conn):
        v1 = fr.insert_file_record(conn, **GOOD, created_at="2026-06-23T00:00:00.000+00:00")
        v2 = fr.insert_file_record(
            conn, **dict(GOOD, sha256=OTHER_SHA), supersedes_id=v1.file_id,
            created_at="2026-06-24T00:00:00.000+00:00",
        )
        versions = fr.list_versions(conn, v1.version_group_id)
        assert [v.file_id for v in versions] == [v1.file_id, v2.file_id]

    def test_supersede_unknown_row_rejected(self, conn):
        with pytest.raises(fr.FileRecordNotFound):
            fr.insert_file_record(conn, **GOOD, supersedes_id="does-not-exist")

    def test_group_mismatch_rejected(self, conn):
        v1 = fr.insert_file_record(conn, **GOOD)
        with pytest.raises(fr.FileRecordError):
            fr.insert_file_record(
                conn, **dict(GOOD, sha256=OTHER_SHA), supersedes_id=v1.file_id,
                version_group_id="some-other-group",
            )

    def test_supersedes_fk_enforced_at_db(self, conn):
        # The self-FK is real: a raw insert with a dangling supersedes_id is rejected.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO supplied_files (file_id, area, source_type,"
                " original_filename, supplied_by, captured_at, sha256, mime, byte_size,"
                " version_group_id, supersedes_id, created_at) VALUES"
                " ('fk1','alpine','notice','n.pdf','isaac','t',?, 'application/pdf', 10,"
                " 'fk1', 'ghost', 't')",
                (SHA,),
            )


# --- fail-closed review lifecycle (review-before-display) -------------------

class TestReviewLifecycle:
    def test_cannot_insert_a_web_safe_record(self, conn):
        # review_state is not a parameter: no caller can mint a displayable record.
        with pytest.raises(TypeError):
            fr.insert_file_record(conn, **GOOD, review_state="web_safe")

    def test_legal_progression_to_web_safe(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        rec = fr.set_review_state(conn, rec.file_id, "reviewing")
        assert rec.review_state == "reviewing"
        rec = fr.set_review_state(conn, rec.file_id, "web_safe")
        assert rec.review_state == "web_safe"

    def test_pending_cannot_jump_straight_to_web_safe(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        with pytest.raises(fr.IllegalReviewTransition):
            fr.set_review_state(conn, rec.file_id, "web_safe")
        # nothing was written
        assert fr.get_file_record(conn, rec.file_id).review_state == "pending"

    def test_unknown_state_rejected(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        with pytest.raises(fr.IllegalReviewTransition):
            fr.set_review_state(conn, rec.file_id, "published")

    def test_same_state_is_idempotent_noop(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        same = fr.set_review_state(conn, rec.file_id, "pending")
        assert same.review_state == "pending"

    def test_set_state_on_missing_row_raises(self, conn):
        with pytest.raises(fr.FileRecordNotFound):
            fr.set_review_state(conn, "nope", "reviewing")

    def test_rejected_cannot_go_straight_to_web_safe(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        rec = fr.set_review_state(conn, rec.file_id, "rejected")
        with pytest.raises(fr.IllegalReviewTransition):
            fr.set_review_state(conn, rec.file_id, "web_safe")


# --- GOV-1625: free-text provenance_note alongside validated origin_url -------

class TestProvenanceNote:
    def test_column_is_present_and_nullable(self, conn):
        info = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(supplied_files)")}
        assert "provenance_note" in info
        assert info["provenance_note"] == 0  # nullable, no default (optional note)

    def test_defaults_to_none_when_absent(self, conn):
        rec = fr.insert_file_record(conn, **GOOD)
        assert rec.provenance_note is None

    def test_round_trips_both_fields(self, conn):
        rec = fr.insert_file_record(
            conn, **dict(GOOD,
                         origin_url="https://alpinewy.gov/packet.pdf",
                         provenance_note="handed to me at the June council meeting"),
        )
        got = fr.get_file_record(conn, rec.file_id)
        assert got.origin_url == "https://alpinewy.gov/packet.pdf"
        assert got.provenance_note == "handed to me at the June council meeting"

    def test_note_without_url_round_trips(self, conn):
        rec = fr.insert_file_record(
            conn, **dict(GOOD, origin_url=None, provenance_note="from the clerk by email"))
        got = fr.get_file_record(conn, rec.file_id)
        assert got.origin_url is None
        assert got.provenance_note == "from the clerk by email"

    def test_note_is_not_a_mandatory_field(self, conn):
        # A blank note is NOT rejected the way a blank mandatory field is; the
        # model stores it verbatim (the intake API normalizes blank -> None).
        rec = fr.insert_file_record(conn, **dict(GOOD, provenance_note="   "))
        assert rec.provenance_note == "   "

    def test_note_column_is_not_ai_flavoured(self, conn):
        # provenance_note carries no banned AI token (guards the AC3 denylist).
        banned = ("ai", "model", "summary", "interpret", "classification", "claim",
                  "extract", "confidence", "prediction", "score", "sentiment")
        assert not any(tok in "provenance_note" for tok in banned)


# --- idempotent migration ---------------------------------------------------

def test_migration_is_rerunnable(tmp_path):
    db_path = tmp_path / "rerun.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must be a no-op, not an error
    c = db.open_db(db_path)
    cols = {row[1] for row in c.execute("PRAGMA table_info(supplied_files)")}
    assert cols == set(fr.PROVENANCE_COLUMNS)
    c.close()
