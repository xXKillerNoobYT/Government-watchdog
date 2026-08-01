"""GOV-1625: reviewer-gated backfill of non-URL origin_url prose -> provenance_note.

Covers the acceptance shape:

  * the plan finds ONLY rows whose origin_url is non-URL prose (URL rows and
    already-split rows are skipped) — the delta, so a re-run after apply is empty;
  * dry-run (no --apply) writes nothing;
  * --apply without a reviewer ref is REFUSED (never a silent rewrite);
  * --apply with a ref moves prose in-place: origin_url -> None, provenance_note
    set, and history count is invariant (in-place UPDATE, never insert/delete);
  * an existing note is joined, not clobbered (no supplier text lost).
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import backfill_provenance_note as bf  # noqa: E402
import db  # noqa: E402
import file_records as fr  # noqa: E402

SHA = hashlib.sha256(b"%PDF-1.4 legacy packet").hexdigest()
SHA2 = hashlib.sha256(b"%PDF-1.4 another packet").hexdigest()
SHA3 = hashlib.sha256(b"%PDF-1.4 third packet").hexdigest()

BASE = dict(
    area="alpine",
    source_type="agenda_packet",
    original_filename="packet.pdf",
    mime="application/pdf",
    byte_size=1234,
    supplied_by="isaac",
    captured_at="2026-06-23T00:00:00.000+00:00",
)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "backfill.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()[0]


def test_plan_targets_only_non_url_prose(conn):
    prose = fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="handed to me at the June meeting"))
    fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA2, origin_url="https://alpinewy.gov/p.pdf"))
    fr.insert_file_record(conn, **dict(BASE, sha256=SHA3, origin_url=None))

    planned = bf.plan_backfill(conn)
    assert [c.file_id for c in planned] == [prose.file_id]
    (change,) = planned
    assert change.before_origin_url == "handed to me at the June meeting"
    assert change.after_origin_url is None
    assert change.after_provenance_note == "handed to me at the June meeting"


def test_dry_run_writes_nothing(conn, tmp_path):
    rec = fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="a paper copy"))
    conn.close()
    # main() opens its own connection; default (no --apply) must not mutate.
    rc = bf.main(["--db", str(tmp_path / "backfill.db")])
    assert rc == 0
    c = db.open_db(tmp_path / "backfill.db")
    try:
        stored = fr.get_file_record(c, rec.file_id)
        assert stored.origin_url == "a paper copy"  # untouched
        assert stored.provenance_note is None
    finally:
        c.close()


def test_apply_without_reviewer_ref_is_refused(conn):
    fr.insert_file_record(conn, **dict(BASE, sha256=SHA, origin_url="a paper copy"))
    planned = bf.plan_backfill(conn)
    with pytest.raises(bf.BackfillRefused):
        bf.apply_backfill(conn, planned, reviewer_ref="   ")


def test_apply_moves_prose_in_place_history_invariant(conn, tmp_path):
    rec = fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="handed to me at the June meeting"))
    before = _count(conn)
    planned = bf.plan_backfill(conn)
    n = bf.apply_backfill(conn, planned, reviewer_ref="GOV-1625 SPA+VSR co-sign",
                          audit_log=tmp_path / "audit.log")
    assert n == 1
    assert _count(conn) == before  # in-place: history never decreases

    stored = fr.get_file_record(conn, rec.file_id)
    assert stored.file_id == rec.file_id  # same row, no lineage break
    assert stored.origin_url is None
    assert stored.provenance_note == "handed to me at the June meeting"
    assert (tmp_path / "audit.log").exists()  # narrated, never silent


def test_apply_is_idempotent(conn, tmp_path):
    fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="a paper copy"))
    bf.apply_backfill(conn, bf.plan_backfill(conn),
                      reviewer_ref="ref", audit_log=tmp_path / "a.log")
    assert bf.plan_backfill(conn) == []  # nothing left to do


def test_existing_note_is_joined_not_clobbered(conn, tmp_path):
    rec = fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="paper copy",
                     provenance_note="from the clerk"))
    bf.apply_backfill(conn, bf.plan_backfill(conn),
                      reviewer_ref="ref", audit_log=tmp_path / "a.log")
    stored = fr.get_file_record(conn, rec.file_id)
    assert stored.origin_url is None
    assert stored.provenance_note == "from the clerk\npaper copy"  # nothing lost


# --- GOV-1698 (C13): the row-count invariant survives `python -O` -------------
#
# The module docstring called the row count an "asserted invariant". It was a
# bare `assert`, which `python -O` DELETES — so a reviewer-gated correction to
# real civic records enforced its own integrity rule in a normal run and not at
# all in an optimised one. Two tests, because the fix had two halves.


def _force_a_row_count_change(conn):
    """Make an UPDATE genuinely add a row, via a trigger — no mocking.

    `sqlite3.Connection.commit` is read-only and cannot be monkeypatched, which
    turned out to be a better constraint than a limitation: a trigger makes the
    count change *through the database*, which is what the invariant is actually
    watching for. SQLite leaves `recursive_triggers` off by default, so the
    INSERT does not re-fire this trigger.
    """
    conn.execute(
        "CREATE TRIGGER intruder AFTER UPDATE ON supplied_files BEGIN"
        " INSERT INTO supplied_files (file_id, area, source_type,"
        " original_filename, mime, byte_size, supplied_by, captured_at,"
        " sha256, review_state, version_group_id, created_at) VALUES"
        " ('intruder-' || NEW.file_id, 'alpine', 'agenda_packet', 'x.pdf',"
        " 'application/pdf', 1, 'someone', '2026-06-23T00:00:00.000+00:00',"
        " 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff', 'pending', 'vg-intruder',"
        " '2026-06-23T00:00:00.000+00:00');"
        " END")
    conn.commit()


def test_a_row_count_change_RAISES_and_is_not_an_assert(conn, tmp_path):
    """`assert` is not an integrity control on a module that rewrites records.

    Pinned as a real exception type so the invariant holds under `python -O`,
    where every assert statement is removed from the bytecode outright.
    """
    fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="handed to me at the June meeting"))
    planned = bf.plan_backfill(conn)
    assert planned, "fixture produced no planned change — the test would be vacuous"

    _force_a_row_count_change(conn)

    with pytest.raises(bf.BackfillIntegrityError, match="history count changed"):
        bf.apply_backfill(conn, planned, reviewer_ref="GOV-1698 SPA+VSR",
                          audit_log=tmp_path / "audit.log")


def test_the_audit_log_SURVIVES_an_integrity_failure(conn, tmp_path):
    """The run that misbehaves must not be the run that leaves no record.

    The original raised before writing the log, so the single case where a
    reviewer most needs to know what was attempted was the one case that
    recorded nothing. The UPDATEs are committed by then; withholding the log
    cannot un-apply them, it only destroys the evidence.
    """
    fr.insert_file_record(
        conn, **dict(BASE, sha256=SHA, origin_url="handed to me at the June meeting"))
    planned = bf.plan_backfill(conn)
    audit = tmp_path / "audit.log"

    _force_a_row_count_change(conn)

    with pytest.raises(bf.BackfillIntegrityError):
        bf.apply_backfill(conn, planned, reviewer_ref="GOV-1698 SPA+VSR",
                          audit_log=audit)

    assert audit.exists(), (
        "the audit log was not written on the failing run — the reviewer has no "
        "record of what the backfill attempted")
    body = audit.read_text(encoding="utf-8")
    assert "GOV-1698 SPA+VSR" in body and planned[0].file_id in body
