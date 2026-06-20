"""GOV-326 Stage 2.14 — deterministic doc/code drift guard for the read surface.

Pairs the reviewer-facing reference doc
``Docs/stage2-reviewer-internal-read-surface-reference.md`` with a test that fails
if the doc and the merged ``scripts/read_api.py`` ever disagree about **which
derived keys appear in which lane**. Same auditor philosophy as GOV-306/318/322:
docs rot, so the doc carries a machine-readable contract block that is checked
against the *live* ``read_api`` output on a fixture record.

What it pins (the issue's three required assertions):

1. Every overlay key the doc claims is reviewer-internal **is present** in the
   reviewer-internal envelope produced by ``read_api`` on a fixture record AND
   **absent** from the public lane (so the doc cannot silently mis-state the
   boundary) — see :func:`test_reviewer_internal_only_keys_present_ri_absent_public`.
2. The doc does **not** claim any derived key ``read_api`` does not emit, and does
   not omit a key it does emit — exact set parity per lane catches a phantom field
   AND a silently-added new field — see
   :func:`test_documented_envelope_keys_match_emitted_per_lane`.
3. The guard is genuinely RED when doc and code disagree — proven in-code by
   :func:`test_guard_is_red_on_phantom_key` /
   :func:`test_guard_is_red_on_wrong_lane` (and by the temp-edit RED proof in the
   PR / VSR leg).

``read_api.py`` and ``publication.py`` are imported read-only; this test touches no
production logic. Pure sqlite + tmp files: no network, no AI. Fixture mirrors the
GOV-311 served-corpus seed.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DOC_PATH = ROOT / "Docs" / "stage2-reviewer-internal-read-surface-reference.md"
sys.path.insert(0, str(SCRIPTS))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402

REVIEWER = "reviewer:isaac"

# A derived key is anything on a served record that is NOT a plain allowlist
# passthrough. ``ui_status`` is allowlisted (its *value* is re-derived), so it is
# documented as a ``rederived_allowlist_key`` and excluded from this set — exactly
# how the GOV-311 suite separates the two. set(record) - allowlist therefore
# isolates {confidence_label, speaker_label, evidence, provenance_status?}.
_ALLOWLIST = set(pub.WEB_SAFE_FIELD_ALLOWLIST)


# ---------------------------------------------------------------------------
# Parse the doc's machine-readable drift contract.
# ---------------------------------------------------------------------------

_BEGIN = "<!-- DRIFT-GUARD-CONTRACT:BEGIN -->"
_END = "<!-- DRIFT-GUARD-CONTRACT:END -->"
_LINE_RE = re.compile(
    r"^(?P<kind>envelope_key|rederived_allowlist_key):\s*"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\|\s*lanes:\s*(?P<lanes>[A-Za-z_,\s]+)$"
)
_VALID_LANES = {"public", "reviewer_internal"}


def _parse_doc_contract(text: str) -> dict[str, dict[str, set[str]]]:
    """Return ``{"envelope_key": {name: {lanes}}, "rederived_allowlist_key": {...}}``.

    Strict: the sentinels must exist exactly once, every non-blank line between them
    must match the grammar, and every lane token must be valid — a malformed doc is a
    test failure, never a silently-skipped line.
    """
    assert text.count(_BEGIN) == 1, "doc must contain exactly one contract BEGIN sentinel"
    assert text.count(_END) == 1, "doc must contain exactly one contract END sentinel"
    block = text.split(_BEGIN, 1)[1].split(_END, 1)[0]

    out: dict[str, dict[str, set[str]]] = {"envelope_key": {}, "rederived_allowlist_key": {}}
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        assert m, f"unparsable drift-contract line: {line!r}"
        lanes = {tok.strip() for tok in m["lanes"].split(",") if tok.strip()}
        assert lanes, f"no lanes on contract line: {line!r}"
        bad = lanes - _VALID_LANES
        assert not bad, f"invalid lane(s) {bad} on contract line: {line!r}"
        out[m["kind"]][m["name"]] = lanes
    return out


@pytest.fixture(scope="module")
def doc_contract() -> dict[str, dict[str, set[str]]]:
    assert DOC_PATH.exists(), f"reference doc missing: {DOC_PATH}"
    return _parse_doc_contract(DOC_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Live read_api fixture: one served PUBLIC record + one reviewer-internal record.
# Mirrors tests/test_gov311_provenance_status.py seed (grounded, raw-preserved).
# ---------------------------------------------------------------------------


@pytest.fixture()
def lane_records(tmp_path: Path) -> dict[str, dict]:
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    try:
        _seed_base(conn)
        # Public lane: an owner-published, grounded statement.
        st.insert_statement(
            conn,
            {
                "statement_id": "stmt-public",
                "segment_id": "seg-1",
                "agenda_item_id": "alpine:2026-05-08:item-7",
                "statement_text": "Published civic fact.",
                "verification_status": "human_verified",
                "produced_by": "human",
                "publication_state": "publishable",
            },
        )
        # Reviewer-internal lane: reviewer-cleared, not-yet-published, grounded.
        _serve_reviewer_internal(conn, statement_id="stmt-ri")

        public = read_api.published_records(conn)
        reviewer_internal = read_api.reviewer_internal_records(conn)
        pub_rec = next(r for r in public if r["statement_id"] == "stmt-public")
        ri_rec = next(r for r in reviewer_internal if r["statement_id"] == "stmt-ri")
        return {"public": pub_rec, "reviewer_internal": ri_rec}
    finally:
        conn.close()


def _seed_base(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "original_url) VALUES ('alpine_packet', 'Agenda Packet', 'alpine', "
        "'document', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-05-08:item-7', 1, 7, 'Fireworks ban — adoption')"
    )
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (1, 'vid-1', "
        "'https://youtu.be/vid-1', 'Alpine council transcript text.', 'n/a', ?, "
        "'2026-05-08T00:00:00Z', 'official_transcript')",
        ("0" * 64,),
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-1', 1, 0, 0, '00:00', 'Mayor calls the meeting to order.')"
    )
    gate.register_reviewer(
        conn, REVIEWER, display_name="Isaac", registered_by="owner:isaac",
        note="GOV-326 doc-drift guard fixture",
    )
    conn.commit()


def _serve_reviewer_internal(conn: sqlite3.Connection, *, statement_id: str) -> None:
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "segment_id": "seg-1",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "The council adopted the fireworks ban.",
            "produced_by": "human",
        },
    )
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _emitted_envelope_keys(record: dict) -> set[str]:
    """Derived keys on a served record: everything that is NOT a plain allowlist field."""
    return set(record) - _ALLOWLIST


def _documented_envelope_keys_for_lane(
    contract: dict[str, dict[str, set[str]]], lane: str
) -> set[str]:
    return {name for name, lanes in contract["envelope_key"].items() if lane in lanes}


# ---------------------------------------------------------------------------
# Contract parses + sanity.
# ---------------------------------------------------------------------------


def test_contract_block_parses_and_is_nonempty(doc_contract) -> None:
    assert doc_contract["envelope_key"], "doc documents no envelope keys"
    assert doc_contract["rederived_allowlist_key"], "doc documents no re-derived allowlist keys"


# ---------------------------------------------------------------------------
# Required assertion #2 — exact per-lane parity between doc and emitted keys.
# A phantom (documented-not-emitted) OR a silently-added (emitted-not-documented)
# key breaks set equality, in the correct lane.
# ---------------------------------------------------------------------------


def test_documented_envelope_keys_match_emitted_per_lane(doc_contract, lane_records) -> None:
    for lane in ("public", "reviewer_internal"):
        documented = _documented_envelope_keys_for_lane(doc_contract, lane)
        emitted = _emitted_envelope_keys(lane_records[lane])
        assert documented == emitted, (
            f"{lane} lane drift — documented {documented} != emitted {emitted}; "
            f"phantom (doc-only)={documented - emitted}, "
            f"undocumented (code-only)={emitted - documented}"
        )


def test_rederived_allowlist_keys_are_allowlisted_and_present(doc_contract, lane_records) -> None:
    for name, lanes in doc_contract["rederived_allowlist_key"].items():
        assert name in _ALLOWLIST, f"{name!r} documented as allowlist key but not in allowlist"
        for lane in lanes:
            assert name in lane_records[lane], f"{name!r} documented for {lane} but not emitted there"


# ---------------------------------------------------------------------------
# Required assertion #1 — lane boundary: reviewer-internal-only keys present in RI,
# absent from public. (provenance_status is the GOV-311 case.)
# ---------------------------------------------------------------------------


def test_reviewer_internal_only_keys_present_ri_absent_public(doc_contract, lane_records) -> None:
    ri_only = {
        name
        for name, lanes in doc_contract["envelope_key"].items()
        if lanes == {"reviewer_internal"}
    }
    assert "provenance_status" in ri_only, (
        "doc must document provenance_status as reviewer-internal-only (GOV-311 lane rule)"
    )
    for name in ri_only:
        assert name in lane_records["reviewer_internal"], f"{name!r} missing from reviewer-internal lane"
        assert name not in lane_records["public"], (
            f"{name!r} documented reviewer-internal-only but LEAKED into the public lane"
        )


def test_no_documented_key_is_phantom(doc_contract, lane_records) -> None:
    """Every documented key (either kind) is emitted in at least one claimed lane."""
    all_emitted = (
        _emitted_envelope_keys(lane_records["public"])
        | _emitted_envelope_keys(lane_records["reviewer_internal"])
        | (set(lane_records["public"]) & _ALLOWLIST)
        | (set(lane_records["reviewer_internal"]) & _ALLOWLIST)
    )
    documented = set(doc_contract["envelope_key"]) | set(doc_contract["rederived_allowlist_key"])
    phantom = documented - all_emitted
    assert not phantom, f"doc claims keys read_api never emits (phantom fields): {phantom}"


# ---------------------------------------------------------------------------
# Required assertion #3 — the guard is genuinely RED on doc/code disagreement.
# Proven in-code by mutating the parsed contract (no file edit needed): the same
# parity comparison that passes above must FAIL on a deliberately-wrong contract.
# ---------------------------------------------------------------------------


def test_guard_is_red_on_phantom_key(doc_contract, lane_records) -> None:
    drifted = {kind: {**m} for kind, m in doc_contract.items()}
    drifted["envelope_key"]["totally_made_up_field"] = {"public", "reviewer_internal"}
    failed = False
    for lane in ("public", "reviewer_internal"):
        documented = _documented_envelope_keys_for_lane(drifted, lane)
        if documented != _emitted_envelope_keys(lane_records[lane]):
            failed = True
    assert failed, "parity check failed to flag a phantom documented key (guard not RED)"


def test_guard_is_red_on_wrong_lane(doc_contract, lane_records) -> None:
    """If the doc mis-claimed provenance_status as public, the boundary check must fail."""
    drifted = {kind: {**m} for kind, m in doc_contract.items()}
    drifted["envelope_key"]["provenance_status"] = {"public", "reviewer_internal"}
    documented_public = _documented_envelope_keys_for_lane(drifted, "public")
    emitted_public = _emitted_envelope_keys(lane_records["public"])
    assert documented_public != emitted_public, (
        "mis-laning provenance_status to public should break public-lane parity (guard not RED)"
    )
