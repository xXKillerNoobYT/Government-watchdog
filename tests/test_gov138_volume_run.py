"""Tests for GOV-138 (GOV-126 Phase 3+4): the Lane 2->5 volume-run harness.

All offline, no network: pure sqlite + an injected deterministic ModelClient stub
over inline untimed-prose fixtures (same seam GOV-137 uses). These prove the
harness wiring + the Phase-4 evidence + the binding "nothing publishable by
default" invariant WITHOUT any live model call or spend.

The live Claude call (``--live`` / :class:`production_proposer.AnthropicModelClient`)
is intentionally NOT exercised here — it needs the anthropic SDK + an API key and
is the CTO-gated billable pass. Everything downstream of the model seam is
covered.

Invariants asserted:
- Lane 2 writes only source-grounded AI rows (produced_by='ai',
  machine_extracted_unreviewed, not_publishable); ungrounded claims dropped.
- Lanes 3 + 4 + the ledger produce evidence rows (ai_extraction_runs by lane,
  ai_verification_results, ai_risk_flags).
- The aggregate "nothing publishable" sweep HOLDS over real AI rows.
- An offline (null-model) run still runs all lanes, writes nothing, and the
  sweep holds (zero-spend wiring proof — what `--apply` without `--live` does).
- ``promote_statement`` (the only promotion path) NEVER flips publication_state,
  so even a human-APPROVED row is still not served by the web-safe read API.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_risk_gate as rg  # noqa: E402
import db  # noqa: E402
import gov138_volume_run as vr  # noqa: E402
import read_api  # noqa: E402

SOURCE_ID = "alpine:src:gov138"

# Untimed-ASR-style prose (no MM:SS). The grounding gate keys off exact substrings.
PROSE = (
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


def _seed_source(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (SOURCE_ID, "Alpine untimed transcript", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()


def _seed_transcript(conn: sqlite3.Connection, *, full_text: str = PROSE, video_id: str = "doc-1") -> None:
    conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, full_text, "
        "timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            video_id, f"gov-source://{SOURCE_ID}", "2024-10-09", full_text, full_text,
            f"Transcripts/2024/{video_id}.txt", "0" * 64, _now(), SOURCE_ID,
        ),
    )
    conn.commit()


def _seed_reviewer(conn: sqlite3.Connection) -> None:
    rg.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac", registered_by="test", commit=True,
    )


class StubModel:
    """Deterministic offline ModelClient: returns a fixed raw-claim list."""

    def __init__(self, claims: list[dict]) -> None:
        self._claims = claims

    def extract(self, source_text: str, *, source_id: str) -> list[dict]:
        return [dict(c) for c in self._claims]


def _raw(statement_text: str, quoted_text: str, **over) -> dict:
    raw = {"statement_text": statement_text, "quoted_text": quoted_text, "confidence": "medium"}
    raw.update(over)
    return raw


# ===========================================================================
# Volume run with a model that returns grounded claims
# ===========================================================================

def _run_with_grounded_claims(tmp_path: Path) -> tuple[dict, Path]:
    db_path = _migrated(tmp_path)
    grounded = "the reserve fund had fallen below the target level"
    ungrounded = "a secret backroom deal was struck"  # not in PROSE -> dropped
    model = StubModel([
        _raw("Staff said the reserve fund was below target.", grounded),
        _raw("There was a backroom deal.", ungrounded),
    ])
    with db.open_db(db_path) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        _seed_reviewer(conn)
        report = vr.run_volume(conn, model_client=model, source_id=SOURCE_ID, commit=True)
    return report, db_path


def test_volume_run_writes_grounded_ai_rows_and_drops_ungrounded(tmp_path: Path) -> None:
    report, db_path = _run_with_grounded_claims(tmp_path)
    assert report["lanes"]["lane2_extraction"]["output_count"] == 1  # ungrounded dropped
    with db.open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT produced_by, verification_status, publication_state FROM statements"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["produced_by"] == "ai"
    assert r["verification_status"] == "machine_extracted_unreviewed"
    assert r["publication_state"] == "not_publishable"


def test_volume_run_produces_phase4_ledger_rows(tmp_path: Path) -> None:
    report, db_path = _run_with_grounded_claims(tmp_path)
    ledger = report["evidence"]["ledger"]
    # one run row per lane 2/3/4 on the shared ledger
    assert ledger["ai_extraction_runs_by_lane"] == {
        "2_extraction": 1, "3_verification": 1, "4_risk": 1,
    }
    assert ledger["ai_verification_results"] >= 1  # lane 3 wrote a verdict
    assert ledger["ai_risk_flags"] >= 0            # lane 4 ran (flags >= 0)
    with db.open_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_verification_results").fetchone()[0] >= 1


def test_volume_run_nothing_publishable_holds_over_real_ai_rows(tmp_path: Path) -> None:
    report, db_path = _run_with_grounded_claims(tmp_path)
    sweep = report["evidence"]["nothing_publishable_sweep"]
    assert sweep["holds"] is True
    assert sweep["published_records_served"] == 0
    assert sweep["any_publishable_state"] == 0
    with db.open_db(db_path) as conn:
        assert read_api.published_records(conn) == []
    # the harness never attempts a promotion in a volume run
    assert report["lanes"]["lane5_reviewer_gate"]["promotions_attempted"] == 0


# ===========================================================================
# Offline null-model run (what `--apply` without `--live` does)
# ===========================================================================

def test_null_model_run_writes_nothing_but_runs_all_lanes(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    with db.open_db(db_path) as conn:
        _seed_source(conn)
        _seed_transcript(conn)
        _seed_reviewer(conn)
        report = vr.run_volume(
            conn, model_client=vr._NullModelClient(), source_id=SOURCE_ID, commit=True
        )
    assert report["lanes"]["lane2_extraction"]["output_count"] == 0
    assert report["evidence"]["corpus"]["statements_total"] == 0
    # all three lane runs still recorded (the chain executed)
    assert report["evidence"]["ledger"]["ai_extraction_runs_total"] == 3
    assert report["evidence"]["nothing_publishable_sweep"]["holds"] is True


# ===========================================================================
# promote_statement never flips publication_state (the only promotion path)
# ===========================================================================

def test_human_approved_row_is_still_not_published(tmp_path: Path) -> None:
    report, db_path = _run_with_grounded_claims(tmp_path)
    with db.open_db(db_path) as conn:
        sid = conn.execute("SELECT statement_id FROM statements").fetchone()[0]
        out = rg.promote_statement(
            conn, sid, reviewer_id="reviewer:isaac", decision="approved",
            to_verification_status="reviewed_source_linked",
            reason="grounded + cleared for test",
        )
        conn.commit()
        row = conn.execute(
            "SELECT verification_status, publication_state FROM statements WHERE statement_id = ?",
            (sid,),
        ).fetchone()
        served = read_api.published_records(conn)
        n_decisions = conn.execute("SELECT COUNT(*) FROM reviewer_decisions").fetchone()[0]
    # The reviewer decision WAS recorded and the verification status advanced ...
    assert out["to_verification_status"] == "reviewed_source_linked"
    assert row["verification_status"] == "reviewed_source_linked"
    assert n_decisions == 1
    # ... but publication_state was NOT flipped, so the web-safe API still serves
    # nothing. Promotion advances review; it never publishes.
    assert row["publication_state"] == "not_publishable"
    assert served == []


# ===========================================================================
# Agent-inline model seam (GOV-141 Option B): the executing agent IS the client
# ===========================================================================

# Wrapped prose: the same words as PROSE but line-wrapped + double-spaced, like
# the real PDF/ASR corpus. An agent authors a NORMALIZED quote; the client must
# recover the exact stored span across the irregular whitespace.
WRAPPED_PROSE = (
    "Staff explained that the reserve fund\nhad fallen below  the target level "
    "for the coming\nfiscal year."
)


def _write_claims(tmp_path: Path, claims: list[dict]) -> Path:
    p = tmp_path / "claims.json"
    p.write_text(json.dumps({"claims": claims}), encoding="utf-8")
    return p


def test_resolve_exact_span_recovers_whitespace_and_returns_literal_substring() -> None:
    # normalized phrase (single spaces) must match across newline + double space,
    # and the RETURNED span is a verbatim substring of the source (grounding precond).
    phrase = "the reserve fund had fallen below the target level"
    span = vr._resolve_exact_span(WRAPPED_PROSE, phrase)
    assert span is not None
    assert span in WRAPPED_PROSE          # literal substring -> str.find will anchor it
    assert WRAPPED_PROSE.find(span) >= 0
    # special-regex chars in a quote are escaped (no regex injection / crash).
    assert vr._resolve_exact_span("cost is $5 (approx).", "is $5 (approx).") == "is $5 (approx)."
    # a phrase absent from the source resolves to None (dropped, fail-closed).
    assert vr._resolve_exact_span(WRAPPED_PROSE, "a secret backroom deal") is None


def test_agent_inline_client_grounds_real_quote_drops_unresolvable_never_names(tmp_path: Path) -> None:
    claims = [
        {"statement_text": "The reserve fund was below target.",
         "quote_phrase": "the reserve fund had fallen below the target level",
         "confidence": "high", "speaker_name": "Mayor Green"},  # name must be ignored
        {"statement_text": "There was a backroom deal.",
         "quote_phrase": "a secret backroom deal was struck", "confidence": "low"},
    ]
    client = vr._AgentInlineModelClient(_write_claims(tmp_path, claims))
    raw = client.extract(WRAPPED_PROSE, source_id=SOURCE_ID)
    assert len(raw) == 1                                   # unresolvable claim dropped
    assert raw[0]["quoted_text"] in WRAPPED_PROSE          # exact stored span
    assert raw[0]["speaker_name"] == ""                    # client never carries a name
    assert raw[0]["confidence"] == "high"


def test_agent_inline_volume_run_records_mechanism_marker_in_ledger(tmp_path: Path) -> None:
    db_path = _migrated(tmp_path)
    claims_path = _write_claims(tmp_path, [
        {"statement_text": "Staff said the reserve fund was below target.",
         "quote_phrase": "the reserve fund had fallen below the target level",
         "confidence": "high"},
    ])
    client = vr._AgentInlineModelClient(claims_path)
    with db.open_db(db_path) as conn:
        _seed_source(conn)
        _seed_transcript(conn, full_text=WRAPPED_PROSE)
        _seed_reviewer(conn)
        report = vr.run_volume(
            conn, model_client=client, source_id=SOURCE_ID,
            extraction_mechanism="agent_inline", commit=True,
        )
        run = conn.execute(
            "SELECT model_name, tool_version FROM ai_extraction_runs WHERE lane='2_extraction'"
        ).fetchone()
    # B2: true runtime model_name + a visible, honest mechanism marker on the ledger.
    assert report["extraction_mechanism"] == "agent_inline"
    assert "agent_inline" in report["tool_version"]
    assert run["model_name"] == "claude-opus-4-8"
    assert "agent_inline(not-externally-re-derivable)" in run["tool_version"]
    # the real authored claim grounded into an AI row, fail-closed not_publishable.
    assert report["lanes"]["lane2_extraction"]["output_count"] == 1
    assert report["evidence"]["nothing_publishable_sweep"]["holds"] is True


def test_build_model_client_modes_and_mutual_exclusion(tmp_path: Path) -> None:
    claims_path = _write_claims(tmp_path, [])
    # offline default
    client, mech = vr._build_model_client(live=False, agent_inline_claims=None)
    assert mech == "offline" and isinstance(client, vr._NullModelClient)
    # agent-inline selected
    client, mech = vr._build_model_client(live=False, agent_inline_claims=claims_path)
    assert mech == "agent_inline" and isinstance(client, vr._AgentInlineModelClient)
    # both at once is rejected (cannot run two mechanisms)
    with pytest.raises(SystemExit):
        vr._build_model_client(live=True, agent_inline_claims=claims_path)
