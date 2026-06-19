"""GOV-275 Stage 2.05 — `transcript_class` migration 0018 + backfill + SSOT parity.

First Stage 2.05 backend slice. Asserts the contract bar from GOV-233/GOV-230:

- migration 0018 applies on a fresh DB and is a no-op on a DB that already has the
  column (idempotent), with the db.py runner staying green;
- the Python SSOT frozenset == the 0018 `transcript_class` CHECK literal (parity),
  plus the import-time self-asserts hold and the enum matches GOV-230 exactly;
- the CHECK rejects an unknown value and allows NULL (unclassified) + every enum;
- the deterministic backfill is reproducible/byte-stable (same input -> same
  assignment), classifies timed vs untimed from observable structure, fails closed
  to the default, and never overwrites an already-set class;
- `to_web_safe` strips `transcript_class` (0 public projection) and the column is
  absent from the allowlist + named in the explicit unsafe set;
- additive-only: no Stage-1 transcripts column is changed; only the new column +
  its index are added.

No network, no AI, no dependency on the real local corpus — pure sqlite + tmp files.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import publication  # noqa: E402
import transcript_class as tc  # noqa: E402

MIGRATION_0018 = (
    Path(__file__).resolve().parent.parent
    / "Database" / "migrations" / "0018_transcript_class.sql"
)

# `transcripts` columns as landed by Stage 1 (0001 init + 0002 title + 0003
# source_id) — all must survive 0018 untouched.
STAGE1_TRANSCRIPT_COLUMNS = {
    "id", "video_id", "video_url", "channel_id", "channel_title", "upload_date",
    "meeting_date", "duration_seconds", "language", "segment_count", "full_text",
    "timestamped_text", "local_path", "sha256", "fetch_time_utc", "title",
    "source_id",
}

TIMED_TEXT = (
    "Alpine Town Council Regular Meeting\n"
    "00:00 Mayor calls the meeting to order.\n"
    "00:12 Roll call of the council members.\n"
    "01:30 Discussion of the water system capital project.\n"
)
UNTIMED_TEXT = (
    "Town of Alpine work session.\n"
    "The council discussed the proposed budget amendment at length.\n"
    "No formal vote was taken; the item was tabled to the next regular meeting.\n"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _migrated(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "gov275.db"
    db.apply_migrations(db_path)
    return db.open_db(db_path)


def _seed_transcript(conn, vid: str, *, full_text: str, timestamped: str | None,
                     transcript_class: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, full_text, timestamped_text, "
        "local_path, sha256, fetch_time_utc, transcript_class) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (vid, f"file:///tmp/{vid}", full_text, timestamped,
         f"Raw-Corpus/{vid}.txt", "0" * 64, _now(), transcript_class),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- migration: applies + idempotent ------------------------------------------

def test_migration_0018_applies_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must be a no-op (ADD COLUMN guard)
    with db.open_db(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transcripts)")}
    assert "transcript_class" in cols


def test_additive_only_stage1_columns_untouched(tmp_path: Path) -> None:
    """0018 adds exactly one column to `transcripts` and touches no Stage-1 column."""
    with _migrated(tmp_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transcripts)")}
    assert STAGE1_TRANSCRIPT_COLUMNS <= cols, "a Stage-1 transcripts column went missing"
    assert cols - STAGE1_TRANSCRIPT_COLUMNS == {"transcript_class"}, (
        f"0018 added unexpected columns: {cols - STAGE1_TRANSCRIPT_COLUMNS - {'transcript_class'}}"
    )


def test_migration_0018_is_purely_additive() -> None:
    """0018 makes exactly one ADD COLUMN (transcript_class) + an index; no rebuild,
    no DROP/RENAME/UPDATE, no CHECK-widening on an existing column."""
    sql = MIGRATION_0018.read_text(encoding="utf-8")
    body = "\n".join(
        ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")
    ).upper()
    add_cols = re.findall(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)", body)
    assert add_cols == [("TRANSCRIPTS", "TRANSCRIPT_CLASS")], add_cols
    for forbidden in ("DROP ", "RENAME ", "UPDATE ", "DELETE ", "CREATE TABLE"):
        assert forbidden not in body, f"0018 contains a non-additive op: {forbidden!r}"


# --- SSOT parity + self-asserts -----------------------------------------------

def test_transcript_class_ssot_matches_migration_check() -> None:
    """The Python SSOT frozenset must equal the 0018 `transcript_class` CHECK literal."""
    sql = MIGRATION_0018.read_text(encoding="utf-8")
    block = re.search(
        r"transcript_class\s+TEXT\s+CHECK \(transcript_class IN \((.*?)\)\)",
        sql, re.DOTALL,
    )
    assert block, "could not find transcript_class CHECK literal in 0018"
    sql_values = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert sql_values == set(tc.TRANSCRIPT_CLASSES), (
        f"transcript_class drift — SQL={sorted(sql_values)} "
        f"SSOT={sorted(tc.TRANSCRIPT_CLASSES)}"
    )


def test_import_time_self_asserts_hold() -> None:
    assert tc.DEFAULT_TRANSCRIPT_CLASS == "auto_caption_untimed"
    assert tc.DEFAULT_TRANSCRIPT_CLASS in tc.TRANSCRIPT_CLASSES
    assert len(tc.TRANSCRIPT_CLASSES) == 6  # GOV-230 froze a 6-value enum
    # Matches the GOV-230 contract enum exactly (inheritance-by-reference).
    assert set(tc.TRANSCRIPT_CLASSES) == {
        "official_transcript", "auto_caption_timed", "auto_caption_untimed",
        "minutes_only", "derived_md_only", "no_transcript",
    }


# --- CHECK behaviour ----------------------------------------------------------

def test_check_rejects_unknown_value(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _seed_transcript(conn, "bad", full_text="x", timestamped=None,
                             transcript_class="totally_made_up")


def test_check_allows_null_and_every_enum_value(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        # NULL == unclassified, must be legal (fail-closed: backfill populates).
        _seed_transcript(conn, "null_row", full_text="x", timestamped=None,
                         transcript_class=None)
        for i, value in enumerate(sorted(tc.TRANSCRIPT_CLASSES)):
            _seed_transcript(conn, f"row_{i}", full_text="x", timestamped=None,
                             transcript_class=value)
        n = conn.execute("SELECT count(*) FROM transcripts").fetchone()[0]
    assert n == 1 + len(tc.TRANSCRIPT_CLASSES)


# --- deterministic backfill ---------------------------------------------------

def test_backfill_classifies_timed_and_untimed(tmp_path: Path) -> None:
    with _migrated(tmp_path) as conn:
        timed_id = _seed_transcript(conn, "timed", full_text=TIMED_TEXT,
                                    timestamped=TIMED_TEXT)
        untimed_id = _seed_transcript(conn, "untimed", full_text=UNTIMED_TEXT,
                                      timestamped=None)
        summary = tc.backfill_transcript_class(conn, apply=True)
        rows = dict(conn.execute(
            "SELECT id, transcript_class FROM transcripts").fetchall())
    assert rows[timed_id] == "auto_caption_timed"
    assert rows[untimed_id] == "auto_caption_untimed"  # fail-closed default
    assert summary["scanned"] == 2
    assert summary["updated"] == 2
    assert summary["by_class"] == {"auto_caption_timed": 1, "auto_caption_untimed": 1}


def test_backfill_is_deterministic_and_byte_stable(tmp_path: Path) -> None:
    """Same input -> same summary across independent runs (dry-run == apply shape)."""
    def run(subdir: str, apply: bool):
        with _migrated(tmp_path / subdir) as conn:  # apply_migrations makes the dir
            _seed_transcript(conn, "t", full_text=TIMED_TEXT, timestamped=TIMED_TEXT)
            _seed_transcript(conn, "u", full_text=UNTIMED_TEXT, timestamped=None)
            return tc.backfill_transcript_class(conn, apply=apply)

    dry = run("dry", apply=False)
    wet = run("wet", apply=True)
    assert dry["by_class"] == wet["by_class"] == {
        "auto_caption_timed": 1, "auto_caption_untimed": 1}
    assert dry["scanned"] == wet["scanned"] == 2
    assert dry["updated"] == 0 and wet["updated"] == 2  # dry-run writes nothing


def test_backfill_does_not_overwrite_existing_class(tmp_path: Path) -> None:
    """An already-set class (e.g. a reviewer upgrade) is never re-derived."""
    with _migrated(tmp_path) as conn:
        kept = _seed_transcript(conn, "official", full_text=TIMED_TEXT,
                                timestamped=TIMED_TEXT,
                                transcript_class="official_transcript")
        summary = tc.backfill_transcript_class(conn, apply=True)
        cls = conn.execute("SELECT transcript_class FROM transcripts WHERE id=?",
                           (kept,)).fetchone()[0]
    assert cls == "official_transcript"  # untouched by the deterministic pass
    assert summary["scanned"] == 0  # only NULL rows are scanned


def test_empty_corpus_backfill_is_a_noop(tmp_path: Path) -> None:
    """The real Alpine build has 0 transcripts rows (GOV-262); backfill = 0 count."""
    with _migrated(tmp_path) as conn:
        summary = tc.backfill_transcript_class(conn, apply=True)
    assert summary == {"mode": "apply", "scanned": 0, "updated": 0, "by_class": {}}


def test_single_stray_timestamp_fails_closed_to_untimed() -> None:
    """One stray 'start time' in prose must not flip an ASR transcript to timed."""
    prose = "The meeting began at 7:30 and the council reviewed the budget."
    assert tc.classify_transcript({"timestamped_text": prose}) == "auto_caption_untimed"
    assert tc.classify_transcript({"timestamped_text": None}) == "auto_caption_untimed"


# --- web-safe exclusion (0 public projection) ---------------------------------

def test_transcript_class_not_web_safe() -> None:
    assert "transcript_class" not in publication.WEB_SAFE_FIELD_ALLOWLIST
    assert "transcript_class" in publication.WEB_UNSAFE_FIELDS
    projected = publication.to_web_safe({
        "source_id": "alpine_local_corpus",
        "name": "Town of Alpine",
        "transcript_class": "auto_caption_untimed",
    })
    assert "transcript_class" not in projected
    assert projected == {"source_id": "alpine_local_corpus", "name": "Town of Alpine"}
