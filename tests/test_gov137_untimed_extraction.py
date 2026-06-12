"""Tests for GOV-137 (GOV-126 Phase 1+2): untimed-extraction contract +
production Claude proposer over untimed Alpine transcript prose.

All offline, no network: pure sqlite + an injected deterministic ModelClient stub
+ inline untimed-prose fixtures. The live Claude call is exercised only through a
fake SDK client object (no `anthropic` import, no key).

Covers the GOV-137 acceptance criteria:

- Phase 1 char-span contract: migration 0016 adds the `char_span` locator_kind +
  columns (idempotent, lossless rebuild); `validate_pointer` requires + validates
  char_start/char_end/quoted_text; a char-span-anchored statement (no segment) is
  NOT an orphan; the char-span columns stay vault-only (not web-projected).
- Phase 2 proposer: source-grounded (ungrounded quote dropped fail-closed),
  char-span DERIVED from the verbatim substring match, conservative attribution
  (every speaker name dropped), production proposer injectable while the
  `offline-disabled` fail-closed default is preserved, `assert_no_pii` enforced at
  the write boundary, no secret reaches the ledger.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import db  # noqa: E402
import production_proposer as pp  # noqa: E402
import publication as pub  # noqa: E402
import statements as stmt  # noqa: E402

SOURCE_ID = "alpine:src:2024-10-09-untimed"

# A small untimed-ASR-style prose blob (no MM:SS lines). The grounding gate keys
# off exact substrings of this text.
UNTIMED_PROSE = (
    "The council reviewed the proposed water rate increase for the coming fiscal "
    "year. Staff explained that the reserve fund had fallen below the target level. "
    "A motion was made to table the discussion until the next regular meeting. "
    "The board approved the consent agenda without objection."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _migrated(tmp_path: Path) -> Path:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    return db_path


def _seed_source(conn: sqlite3.Connection, source_id: str = SOURCE_ID) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "Alpine untimed transcript", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _seed_transcript(conn: sqlite3.Connection, *, source_id: str = SOURCE_ID,
                     full_text: str = UNTIMED_PROSE, video_id: str = "localdoc-1") -> None:
    conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, full_text, "
        "timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            video_id, f"gov-source://{source_id}", "2024-10-09", full_text, full_text,
            "Transcripts/2024/localdoc-1.txt", "0" * 64, _now(), source_id,
        ),
    )
    conn.commit()


# A deterministic, offline ModelClient stub: returns a fixed raw-claim list.
class StubModel:
    def __init__(self, claims: list[dict]) -> None:
        self._claims = claims
        self.calls: list[tuple[str, int]] = []

    def extract(self, source_text: str, *, source_id: str) -> list[dict]:
        self.calls.append((source_id, len(source_text)))
        return [dict(c) for c in self._claims]


# ===========================================================================
# Phase 1 — char-span contract
# ===========================================================================

def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_migration_0016_adds_char_span_columns_and_widens_check(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        cols = _columns(conn, "evidence_links")
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='evidence_links'"
        ).fetchone()[0]
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for c in ("char_start", "char_end", "quoted_text"):
        assert c in cols, f"evidence_links.{c} missing"
    assert "ai_extraction_run_id" in cols, "0009 column lost in rebuild"
    assert "char_span" in sql, "locator_kind CHECK not widened to char_span"
    assert "0016_evidence_link_char_span" in ledger


def test_migration_0016_idempotent_and_fk_clean(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must be a no-op, never raise
    with db.open_db(db_path) as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(evidence_links)")].count(
            "evidence_link_id"
        ) == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _char_span_pointer(quote: str, start: int, **over) -> dict:
    pointer = {
        "to_source_id": SOURCE_ID,
        "relation": "references",
        "locator_kind": "char_span",
        "char_start": start,
        "char_end": start + len(quote),
        "quoted_text": quote,
        "original_url": "gov-source://alpine",
        "archive_status": "not_checked",
        "scan_date": "2026-06-11",
        "captured_at_utc": "2026-06-11T00:00:00Z",
        "verification_status": "machine_extracted_unreviewed",
        "confidence": "medium",
    }
    pointer.update(over)
    return pointer


def test_char_span_pointer_valid(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        stmt.validate_pointer(_char_span_pointer("the reserve fund", 5), conn=conn)  # no raise


@pytest.mark.parametrize("missing", ["char_start", "char_end", "quoted_text"])
def test_char_span_requires_all_three_fields(missing: str) -> None:
    pointer = _char_span_pointer("quote", 0)
    pointer[missing] = None
    with pytest.raises(stmt.PointerError):
        stmt.validate_pointer(pointer)


def test_char_span_rejects_length_mismatch() -> None:
    # char_end - char_start must equal len(quoted_text) so the offsets select the
    # exact quote (the reproducibility invariant).
    pointer = _char_span_pointer("hello", 0, char_end=99)
    with pytest.raises(stmt.PointerError):
        stmt.validate_pointer(pointer)


def test_char_span_rejects_nonpositive_span() -> None:
    with pytest.raises(stmt.PointerError):
        stmt.validate_pointer(_char_span_pointer("x", 0, char_start=10, char_end=10, quoted_text=""))


def test_char_span_statement_is_not_orphan(tmp_path: Path) -> None:
    # A statement with NO segment but a valid char-span pointer is grounded.
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        quote = "reserve fund"
        start = UNTIMED_PROSE.find(quote)
        result = stmt.insert_statement(
            conn,
            {
                "statement_id": "alpine:ai:cs-1",
                "statement_text": "Staff noted the reserve fund issue.",
                "is_verbatim": 0,
                "produced_by": "ai",
            },
            [_char_span_pointer(quote, start)],
        )
        assert result["statement_id"] == "alpine:ai:cs-1"
        row = conn.execute(
            "SELECT locator_kind, char_start, char_end, quoted_text "
            "FROM evidence_links WHERE from_node_id='alpine:ai:cs-1'"
        ).fetchone()
    assert row["locator_kind"] == "char_span"
    assert UNTIMED_PROSE[row["char_start"]:row["char_end"]] == quote


def test_char_span_columns_not_web_projected() -> None:
    leaked = {"char_start", "char_end", "quoted_text"} & pub.WEB_SAFE_FIELD_ALLOWLIST
    assert leaked == set(), f"char-span pointer fields leak to web: {leaked}"
    safe = pub.to_web_safe(
        {"source_id": "s", "char_start": 1, "char_end": 9, "quoted_text": "verbatim raw"}
    )
    assert "char_start" not in safe and "char_end" not in safe and "quoted_text" not in safe


# ===========================================================================
# Phase 2 — production proposer (grounding, attribution, PII, fail-closed)
# ===========================================================================

def _raw(statement_text: str, quoted_text: str, **over) -> dict:
    raw = {"statement_text": statement_text, "quoted_text": quoted_text, "confidence": "medium"}
    raw.update(over)
    return raw


def test_load_source_text_reads_untimed_prose(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        assert pp.load_source_text(conn, SOURCE_ID) == UNTIMED_PROSE
        assert pp.load_source_text(conn, "nope") == ""


def test_proposer_grounds_quote_and_derives_char_span(tmp_path: Path) -> None:
    quote = "the reserve fund had fallen below the target level"
    model = StubModel([_raw("Staff said the reserve fund was below target.", quote)])
    proposer = pp.build_claude_proposer(model)
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        claims = proposer(conn, [SOURCE_ID], [])
    assert len(claims) == 1
    link = claims[0]["evidence_links"][0]
    assert link["locator_kind"] == "char_span"
    # The char-span is DERIVED and exactly selects the verbatim quote.
    assert UNTIMED_PROSE[link["char_start"]:link["char_end"]] == quote
    assert claims[0]["is_verbatim"] == 0      # the statement is a paraphrase
    assert link["is_verbatim"] == 1           # the quote is verbatim


def test_proposer_drops_ungrounded_quote_failclosed(tmp_path: Path) -> None:
    # A "quote" the model fabricated that is NOT a literal substring -> dropped.
    model = StubModel([_raw("The mayor admitted wrongdoing.",
                            "the mayor admitted to embezzling funds")])
    proposer = pp.build_claude_proposer(model)
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        claims = proposer(conn, [SOURCE_ID], [])
    assert claims == []


def test_proposer_drops_speaker_name_even_when_model_returns_one(tmp_path: Path) -> None:
    quote = "A motion was made to table the discussion"
    model = StubModel([_raw("Someone moved to table.", quote, speaker_name="Mayor Pat Maxwell")])
    proposer = pp.build_claude_proposer(model)
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        claims = proposer(conn, [SOURCE_ID], [])
    assert len(claims) == 1
    assert "speaker" not in claims[0], "proposer must never attach a speaker"


def test_end_to_end_run_extraction_writes_failclosed_ai_rows(tmp_path: Path) -> None:
    good = "the proposed water rate increase"
    ungrounded = "a secret backroom deal was struck"
    model = StubModel([
        _raw("The council reviewed a water rate increase.", good),
        _raw("There was a backroom deal.", ungrounded),  # dropped before write
    ])
    proposer = pp.build_claude_proposer(model)
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        result = ai.run_extraction(
            conn,
            run_id="gov137-run-1",
            input_source_ids=[SOURCE_ID],
            input_segment_ids=[],
            proposer=proposer,
            model_name="claude-opus-4-8",
            model_version="test",
            tool_version="gov137@test",
        )
        rows = conn.execute(
            "SELECT produced_by, verification_status, review_state, publication_state, "
            "ai_extraction_run_id FROM statements"
        ).fetchall()
        ev = conn.execute(
            "SELECT locator_kind, char_start, char_end, quoted_text FROM evidence_links"
        ).fetchall()
    # Only the grounded claim was written; the ungrounded one was dropped upstream.
    assert result["output_count"] == 1
    assert len(rows) == 1
    r = rows[0]
    assert r["produced_by"] == "ai"
    assert r["verification_status"] == "machine_extracted_unreviewed"
    assert r["review_state"] == "unreviewed"
    assert r["publication_state"] == "not_publishable"
    assert r["ai_extraction_run_id"] == "gov137-run-1"
    assert len(ev) == 1 and ev[0]["locator_kind"] == "char_span"
    assert UNTIMED_PROSE[ev[0]["char_start"]:ev[0]["char_end"]] == ev[0]["quoted_text"] == good
    # No speaker attribution row was created (conservative attribution).
    with db.open_db(tmp_path / "t.db") as conn2:
        n_attr = conn2.execute("SELECT COUNT(*) FROM speaker_attributions").fetchone()[0]
    assert n_attr == 0


def test_assert_no_pii_drops_pii_claim_at_write_boundary(tmp_path: Path) -> None:
    # Untimed prose that contains a street address; the model quotes it. The quote
    # is grounded (it IS in the source) but assert_no_pii rejects it at the write
    # boundary -> dropped fail-closed, counted, nothing written.
    prose = "A resident at 742 Evergreen Terrace objected to the rezoning request."
    quote = "742 Evergreen Terrace"
    model = StubModel([_raw("A resident objected to rezoning.", quote)])
    proposer = pp.build_claude_proposer(model)
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn, full_text=prose)
        result = ai.run_extraction(
            conn, run_id="gov137-pii", input_source_ids=[SOURCE_ID],
            input_segment_ids=[], proposer=proposer,
        )
        n_stmt = conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
        run = ai.get_run(conn, "gov137-pii")
    assert n_stmt == 0, "a PII-bearing claim must not be written"
    assert result["output_count"] == 0
    assert run["orphan_rejected_count"] == 1
    assert "PiiGuardError" in (run["error_detail"] or "")
    # error_detail names only the pattern KIND, never the matched PII value.
    assert "742 Evergreen Terrace" not in (run["error_detail"] or "")


def test_offline_disabled_failclosed_default_preserved(tmp_path: Path) -> None:
    # No proposer + offline config -> failed run, nothing written (no live call).
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        result = ai.run_extraction(
            conn, run_id="gov137-noprov", input_source_ids=[SOURCE_ID],
            proposer=None, provider_config={"provider": "offline-disabled"},
        )
        run = ai.get_run(conn, "gov137-noprov")
    assert result["error_status"] == "failed"
    assert "ProviderNotConfigured" in (run["error_detail"] or "")


def test_no_secret_reaches_the_ledger(tmp_path: Path) -> None:
    # Only model_name/model_version are recorded; no key/secret field exists.
    model = StubModel([_raw("The board approved the consent agenda.",
                            "approved the consent agenda")])
    proposer = pp.build_claude_proposer(model)
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        ai.run_extraction(
            conn, run_id="gov137-ledger", input_source_ids=[SOURCE_ID],
            input_segment_ids=[], proposer=proposer,
            model_name="claude-opus-4-8", model_version="2026-01",
        )
        run = ai.get_run(conn, "gov137-ledger")
    assert run["model_name"] == "claude-opus-4-8"
    assert run["model_version"] == "2026-01"
    blob = " ".join(str(v) for v in run.values()).lower()
    for secret_marker in ("sk-ant", "api_key", "apikey", "authorization", "bearer"):
        assert secret_marker not in blob, f"ledger leaked a secret marker: {secret_marker}"


# ===========================================================================
# Production AnthropicModelClient — exercised with a FAKE SDK client (no network)
# ===========================================================================

class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_kwargs: dict = {}

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self._response


class _FakeAnthropic:
    def __init__(self, response: _FakeResponse) -> None:
        self.messages = _FakeMessages(response)


def test_anthropic_client_parses_structured_claims() -> None:
    fake = _FakeAnthropic(_FakeResponse('{"claims": [{"statement_text": "x", '
                                        '"quoted_text": "y", "confidence": "high"}]}'))
    client = pp.AnthropicModelClient(client=fake)
    claims = client.extract("some source text", source_id="s")
    assert claims == [{"statement_text": "x", "quoted_text": "y", "confidence": "high"}]
    # Uses the default latest model + the source-grounded + char-span prompt.
    kwargs = fake.messages.last_kwargs
    assert kwargs["model"] == "claude-opus-4-8"
    assert "char_span" not in kwargs["system"].lower() or "quoted_text" in kwargs["system"]
    assert "EMPTY string" in kwargs["system"]  # the no-name instruction is present


def test_anthropic_client_refusal_is_failclosed() -> None:
    fake = _FakeAnthropic(_FakeResponse("", stop_reason="refusal"))
    client = pp.AnthropicModelClient(client=fake)
    assert client.extract("text", source_id="s") == []
