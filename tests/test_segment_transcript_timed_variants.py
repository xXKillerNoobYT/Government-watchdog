"""GOV-642: deterministic timed-line contract extension (GOV-641 §1-§6).

Extends the segmenter's single timed-line contract (`_LINE_RE`) from the legacy
``MM:SS``/``HH:MM:SS`` shape to the full deterministic locator family measured in
the Town-of-Alpine corpus:

    V1  ``[3.3] text``       bracketed decimal seconds
    V2  ``[100:00] text``    bracketed colon (total-minutes up to 4 digits)
    V3  ``[188.96s] text``   bracketed decimal seconds with a trailing ``s`` unit
    V4  ``746.32\ttext``     bare decimal seconds + a LITERAL TAB
    legacy ``72:15`` / ``1:12:15``  unchanged

Fixtures are real-FORMAT / synthetic-TEXT (generic procedural sentences, no PII):
`tests/fixtures/timed_line_variants.json`. No AI, no network — pure sqlite + the
committed fixture. Existing `tests/test_segment_transcript.py` is untouched; this
file is purely additive.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import segment_transcript as seg  # noqa: E402
import transcript_from_documents as tfd  # noqa: E402  (single-source-of-truth check)

FIXTURE = ROOT / "tests" / "fixtures" / "timed_line_variants.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _variants() -> dict:
    return _fixture()["variants"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _insert_transcript(conn, video_id: str, timestamped_text: str) -> int:
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, full_text, "
        "timestamped_text, local_path, sha256, fetch_time_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            video_id,
            f"file:///vault/{video_id}.txt",
            "2026-06-23",
            timestamped_text,
            timestamped_text,
            f"Transcripts/2026/{video_id}.txt",
            "0" * 64,
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# --- §6.1 per-variant positive parse_timestamp units -----------------------

@pytest.mark.parametrize(
    "token, expected",
    [
        # bracketed decimal (V1) — dot floored
        ("[3.3]", 3),
        ("[7.85]", 7),
        ("[65.4]", 65),
        # bracketed colon (V2) — total-minutes convention, up to 4 digits
        ("[00:24]", 24),
        ("[05:09]", 309),
        ("[100:00]", 6000),
        # bracketed decimal + s unit (V3) — strip s then floor
        ("[188.96s]", 188),
        ("[212.5s]", 212),
        # bare decimal (V4) — floored (regex requires the tab, parse takes the token)
        ("746.32", 746),
        ("1203.5", 1203),
        # legacy colon (no regression)
        ("00:00", 0),
        ("72:15", 4335),
        ("1:12:15", 4335),
    ],
)
def test_parse_timestamp_per_variant(token: str, expected: int) -> None:
    assert seg.parse_timestamp(token) == expected


def test_decimal_is_floored_never_rounded() -> None:
    # 7.85 must floor to 7 (a seek lands <=1s before the utterance, never after)
    assert seg.parse_timestamp("[7.85]") == 7
    assert seg.parse_timestamp("[999.99s]") == 999


# --- §6.2 negative units — the grammar must NOT match ----------------------

def test_negative_lines_do_not_match_line_re() -> None:
    for bad in _fixture()["negatives"]:
        assert seg._LINE_RE.match(bad.strip()) is None, f"unexpectedly matched: {bad!r}"


def test_bare_decimal_requires_tab_not_space() -> None:
    # space-separated packet/section numbers must never parse as a locator
    assert seg._LINE_RE.match("12.5 Discussion of the budget") is None
    # the same number with a literal TAB is a valid V4 locator
    assert seg._LINE_RE.match("12.5\tDiscussion of the budget") is not None


def test_integer_bracket_never_matches() -> None:
    # footnote / list markers like [1], [42] carry no dot -> never a locator
    assert seg._LINE_RE.match("[1] see appendix") is None
    assert seg._LINE_RE.match("[42] footnote text") is None


def test_token_only_lines_are_skipped() -> None:
    # a locator with no trailing text yields no segment (no empty rows)
    assert seg.parse_timestamped_text("[3.3]\n746.32\n[00:24]\n") == []


# --- §6.3 end-to-end per variant (segment_transcript over a real DB) --------

@pytest.mark.parametrize("key", ["V1_bracket_decimal", "V2_bracket_colon",
                                 "V3_bracket_decimal_s", "V4_bare_decimal_tab",
                                 "legacy_colon"])
def test_end_to_end_segments_per_variant(tmp_path: Path, key: str) -> None:
    variant = _variants()[key]
    expected = [(int(t), s) for t, s in variant["expected"]]
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        tid = _insert_transcript(conn, key, variant["timestamped_text"])
        records = seg.segment_transcript(conn, tid)
        rows = conn.execute(
            "SELECT segment_id, segment_index, timestamp_seconds, timestamp_human, "
            "segment_text, is_verbatim FROM transcript_segments "
            "WHERE transcript_id = ? ORDER BY segment_index",
            (tid,),
        ).fetchall()

    assert len(records) == len(expected)
    assert len(rows) == len(expected)
    for i, (row, (exp_ts, exp_text)) in enumerate(zip(rows, expected)):
        assert row["segment_id"] == f"{key}:seg-{i:04d}"
        assert row["segment_index"] == i
        assert row["timestamp_seconds"] == exp_ts
        assert row["timestamp_human"] == seg.format_human(exp_ts)
        assert row["segment_text"] == exp_text
        assert row["is_verbatim"] == 1


def test_end_to_end_idempotent_reinsert_zero(tmp_path: Path) -> None:
    variant = _variants()["V1_bracket_decimal"]
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        tid = _insert_transcript(conn, "V1_idem", variant["timestamped_text"])
        first = seg.segment_transcript(conn, tid)
        seg.segment_transcript(conn, tid)  # re-run must not duplicate
        count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE transcript_id = ?", (tid,)
        ).fetchone()[0]
    assert count == len(first) == len(variant["expected"])


# --- §6.4 single-source-of-truth: has_parseable_timestamps agrees ----------

def test_has_parseable_timestamps_uses_the_same_regex_object() -> None:
    # structural guarantee: the bridge imports THE segmenter's regex, not a copy
    assert tfd._LINE_RE is seg._LINE_RE


@pytest.mark.parametrize("key", ["V1_bracket_decimal", "V2_bracket_colon",
                                 "V3_bracket_decimal_s", "V4_bare_decimal_tab",
                                 "legacy_colon"])
def test_classifier_true_on_every_variant(key: str) -> None:
    assert tfd.has_parseable_timestamps(_variants()[key]["timestamped_text"]) is True


def test_classifier_false_on_negatives_and_untimed() -> None:
    untimed = "\n".join(_fixture()["negatives"]) + "\n"
    assert tfd.has_parseable_timestamps(untimed) is False
    assert tfd.has_parseable_timestamps("") is False
    assert tfd.has_parseable_timestamps("just a wall of prose, no locators here.") is False


# --- §6.5 monotonicity regression (observed, never enforced) ---------------

@pytest.mark.parametrize("key", ["V1_bracket_decimal", "V2_bracket_colon",
                                 "V3_bracket_decimal_s", "V4_bare_decimal_tab",
                                 "legacy_colon"])
def test_fixtures_are_non_decreasing(key: str) -> None:
    parsed = seg.parse_timestamped_text(_variants()[key]["timestamped_text"])
    seconds = [ts for ts, _ in parsed]
    assert seconds == sorted(seconds), f"{key} fixture is not monotonic"


def test_count_nonmonotonic_observes_but_never_reorders() -> None:
    # out-of-order lines are counted (observability) but NEVER dropped/reordered:
    # segment order stays file order, so a backwards jump is preserved verbatim.
    text = "[10.0] first line here.\n[5.0] backwards line here.\n[20.0] forward again here.\n"
    parsed = seg.parse_timestamped_text(text)
    assert [ts for ts, _ in parsed] == [10, 5, 20]  # file order preserved, not sorted
    records = [{"timestamp_seconds": ts} for ts, _ in parsed]
    assert seg.count_nonmonotonic(records) == 1
    # and a monotonic sequence reports zero
    assert seg.count_nonmonotonic([{"timestamp_seconds": s} for s in (0, 3, 3, 65)]) == 0


# --- §6.6 speaker-name guard: markers stay verbatim, never attributed ------

def test_turn_markers_stay_inside_verbatim_text_no_speaker_field(tmp_path: Path) -> None:
    variant = _variants()["speaker_marker_verbatim"]
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as conn:
        tid = _insert_transcript(conn, "speaker_guard", variant["timestamped_text"])
        records = seg.segment_transcript(conn, tid)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transcript_segments)")}

    # the ">>" turn marker is preserved inside the verbatim slice, not stripped
    assert records[0]["segment_text"].startswith(">> Thank you")
    # NO field anywhere carries an attributed/guessed speaker name
    assert not any("speaker" in c.lower() for c in cols)
    for rec in records:
        assert not any("speaker" in k.lower() for k in rec)
