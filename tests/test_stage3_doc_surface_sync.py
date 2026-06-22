"""GOV-421 Stage 3.14 — deterministic doc/code drift guard for the FULL Stage-3 surface.

Pairs the reviewer-facing reference doc
``Docs/stage3-reviewer-internal-read-surface-reference.md`` with a test that fails if
the doc and the live code ever disagree about **which derived keys each served
envelope emits and in which lane**. Same auditor philosophy as the Stage 2.14 guard
(GOV-326, ``tests/test_stage2_doc_surface_sync.py``), raised to cover everything
Stage 3 newly surfaces to a reviewer / frontend.

Stage 3 added NO new envelope key to ``read_api`` — it added two reviewer-internal
re-projections on top of the already-web-safe read surface. So this guard pins the
three served envelopes a frontend actually consumes, each with its own contract block
in the doc, asserted against the *live* code on one fixture corpus:

1. ``read_api`` record envelope (public vs reviewer_internal lanes) — block 8.1.
   Exact per-lane set parity (catches a phantom field AND a silently-added one) plus
   the GOV-311 lane rule: ``provenance_status`` is present reviewer-internal, absent
   public. Identical machinery to the Stage 2.14 guard (the read_api contract is
   byte-identical to Stage 2's, by design — Stage 3 added no read_api key).
2. ``stage3_card_feed`` cards (reviewer-internal only) — block 8.2. Every documented
   ``always`` key is emitted; every emitted key is documented.
3. ``stage3_verify_at_source`` drill-down (reviewer-internal only) — block 8.3. Same
   coverage, plus the per-link ``locator`` / ``resolvability_status`` keys.

Required RED proofs are in-code per surface: a phantom documented key and a
removed/wrong-laned key flip the matching check RED on the parsed contract (no file
edit needed), then the unmutated contract passes — so the guard is genuinely
load-bearing, not a tautology.

``read_api`` / ``publication`` / the two re-projection modules are imported read-only;
this test touches no production logic. Pure sqlite + tmp files: no network, no AI.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DOC_PATH = ROOT / "Docs" / "stage3-reviewer-internal-read-surface-reference.md"
sys.path.insert(0, str(SCRIPTS))

import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import stage3_card_feed as feed  # noqa: E402
import stage3_verify_at_source as vas  # noqa: E402
import statements as st  # noqa: E402

REVIEWER = "reviewer:isaac"
_ALLOWLIST = set(pub.WEB_SAFE_FIELD_ALLOWLIST)


# ---------------------------------------------------------------------------
# Generic contract-block parser (one grammar, three blocks).
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(
    r"^(?P<kind>[a-z_]+):\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\|\s*"
    r"(?P<attr>lanes|presence):\s*(?P<vals>[A-Za-z_,\s]+)$"
)
_VALID_LANES = {"public", "reviewer_internal"}
_VALID_PRESENCE = {"always", "optional"}


def _parse_block(text: str, begin: str, end: str) -> dict[str, dict[str, set[str]]]:
    """Parse one ``BEGIN/END`` contract block into ``{kind: {name: {values}}}``.

    Strict: the sentinels must exist exactly once, every non-blank line must match the
    grammar, and every value token must be valid for its attribute — a malformed block
    is a test failure, never a silently-skipped line.
    """
    assert text.count(begin) == 1, f"doc must contain exactly one {begin!r}"
    assert text.count(end) == 1, f"doc must contain exactly one {end!r}"
    block = text.split(begin, 1)[1].split(end, 1)[0]

    out: dict[str, dict[str, set[str]]] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        assert m, f"unparsable contract line: {line!r}"
        vals = {tok.strip() for tok in m["vals"].split(",") if tok.strip()}
        assert vals, f"no values on contract line: {line!r}"
        if m["attr"] == "lanes":
            bad = vals - _VALID_LANES
            assert not bad, f"invalid lane(s) {bad} on: {line!r}"
        else:
            bad = vals - _VALID_PRESENCE
            assert not bad, f"invalid presence {bad} on: {line!r}"
            assert len(vals) == 1, f"presence must be a single token on: {line!r}"
        out.setdefault(m["kind"], {})[m["name"]] = vals
    return out


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"reference doc missing: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def read_api_contract(doc_text) -> dict[str, dict[str, set[str]]]:
    return _parse_block(
        doc_text,
        "<!-- STAGE3-READ-API-CONTRACT:BEGIN -->",
        "<!-- STAGE3-READ-API-CONTRACT:END -->",
    )


@pytest.fixture(scope="module")
def card_feed_contract(doc_text) -> dict[str, dict[str, set[str]]]:
    return _parse_block(
        doc_text,
        "<!-- STAGE3-CARD-FEED-CONTRACT:BEGIN -->",
        "<!-- STAGE3-CARD-FEED-CONTRACT:END -->",
    )


@pytest.fixture(scope="module")
def verify_contract(doc_text) -> dict[str, dict[str, set[str]]]:
    return _parse_block(
        doc_text,
        "<!-- STAGE3-VERIFY-AT-SOURCE-CONTRACT:BEGIN -->",
        "<!-- STAGE3-VERIFY-AT-SOURCE-CONTRACT:END -->",
    )


# ---------------------------------------------------------------------------
# Live fixture corpus: one PUBLIC record + one reviewer-internal record (with a
# resolvable evidence link) + one completeness gap. Combines the proven Stage 2.14
# (GOV-326) and card-feed (GOV-347) seeds so all three envelopes are exercised.
# ---------------------------------------------------------------------------


@pytest.fixture()
def surface(tmp_path: Path) -> dict:
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    conn = db.open_db(db_path)
    try:
        _seed_base(conn)
        # Public lane: an owner-published statement on the seeded segment.
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
        # Reviewer-internal lane: reviewer-cleared, not-published, with evidence.
        _promote_reviewer_internal(conn, "stmt-ri")
        # Gap lane: a known missing-source meeting (the only source of source_missing).
        comp.record_gap(
            conn,
            subject_node_id="2026-04-10",
            subject_node_type="meeting",
            gap_type="no_primary_source",
            commit=True,
        )

        public = next(
            r for r in read_api.published_records(conn) if r["statement_id"] == "stmt-public"
        )
        reviewer_internal = next(
            r
            for r in read_api.reviewer_internal_records(conn)
            if r["statement_id"] == "stmt-ri"
        )
        feed_body = feed.build_card_feed(conn)
        verify_body = vas.build_verify_at_source(conn)
        cards = feed_body["cards"]
        drilldowns = verify_body["cards"]
        return {
            "public": public,
            "reviewer_internal": reviewer_internal,
            "feed_body": feed_body,
            "verify_body": verify_body,
            "record_card": _one(cards, gap=False),
            "gap_card": _one(cards, gap=True),
            "record_drilldown": _one(drilldowns, gap=False),
            "gap_drilldown": _one(drilldowns, gap=True),
        }
    finally:
        conn.close()


def _one(cards: list[dict], *, gap: bool) -> dict:
    """The first (gap | record) card from a feed/drill-down card list."""
    pred = (lambda c: c.get("type") == feed.TYPE_SOURCE_MISSING) if gap else (
        lambda c: c.get("type") != feed.TYPE_SOURCE_MISSING
    )
    return next(c for c in cards if pred(c))


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
        note="GOV-421 doc-drift guard fixture",
    )
    conn.commit()


def _promote_reviewer_internal(conn: sqlite3.Connection, statement_id: str) -> None:
    """Insert a statement + resolvable evidence link, then promote it (GOV-146 gate).

    Mirrors the live reviewer-internal serve: reviewed + a promoting Lane-5 decision +
    a resolvable evidence pointer + not-publishable. The evidence link carries raw
    locators that MUST be stripped upstream — the transport sweep proves they never
    cross into the card / drill-down body.
    """
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "segment_id": "seg-1",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "machine_extracted_unreviewed",
            "produced_by": "human",
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "final_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": "2026-05-01",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 1,
                "verification_status": "human_verified",
                "confidence": "high",
                # raw/private locators that MUST be stripped at the boundary:
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=1",
            }
        ],
    )
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


# ---------------------------------------------------------------------------
# Helpers to isolate the documented / emitted key sets.
# ---------------------------------------------------------------------------


def _emitted_envelope_keys(record: dict) -> set[str]:
    """Derived keys on a served read_api record: everything NOT a plain allowlist field."""
    return set(record) - _ALLOWLIST


def _documented_envelope_keys_for_lane(
    contract: dict[str, dict[str, set[str]]], lane: str
) -> set[str]:
    return {name for name, lanes in contract["envelope_key"].items() if lane in lanes}


def _always_keys(contract: dict[str, dict[str, set[str]]], kind: str) -> set[str]:
    return {name for name, p in contract.get(kind, {}).items() if p == {"always"}}


def _all_keys(contract: dict[str, dict[str, set[str]]], kind: str) -> set[str]:
    return set(contract.get(kind, {}))


# ===========================================================================
# Blocks parse + sanity.
# ===========================================================================


def test_all_three_contract_blocks_parse_and_are_nonempty(
    read_api_contract, card_feed_contract, verify_contract
) -> None:
    assert read_api_contract["envelope_key"], "read_api block documents no envelope keys"
    assert read_api_contract["rederived_allowlist_key"], "no re-derived allowlist keys"
    assert card_feed_contract["record_card_key"], "card block documents no record-card keys"
    assert card_feed_contract["gap_card_key"], "card block documents no gap-card keys"
    assert verify_contract["record_drilldown_key"], "verify block documents no record keys"
    assert verify_contract["link_key"], "verify block documents no per-link keys"


# ===========================================================================
# 8.1 read_api record envelope — exact per-lane parity + GOV-311 lane rule.
# (Same machinery as the Stage 2.14 guard; the read_api contract is unchanged.)
# ===========================================================================


def test_read_api_documented_keys_match_emitted_per_lane(read_api_contract, surface) -> None:
    for lane in ("public", "reviewer_internal"):
        documented = _documented_envelope_keys_for_lane(read_api_contract, lane)
        emitted = _emitted_envelope_keys(surface[lane])
        assert documented == emitted, (
            f"{lane} lane drift — documented {documented} != emitted {emitted}; "
            f"phantom (doc-only)={documented - emitted}, "
            f"undocumented (code-only)={emitted - documented}"
        )


def test_read_api_rederived_allowlist_keys_are_allowlisted_and_present(
    read_api_contract, surface
) -> None:
    for name, lanes in read_api_contract["rederived_allowlist_key"].items():
        assert name in _ALLOWLIST, f"{name!r} documented as allowlist key but not allowlisted"
        for lane in lanes:
            assert name in surface[lane], f"{name!r} documented for {lane} but not emitted there"


def test_read_api_reviewer_internal_only_keys_present_ri_absent_public(
    read_api_contract, surface
) -> None:
    ri_only = {
        name
        for name, lanes in read_api_contract["envelope_key"].items()
        if lanes == {"reviewer_internal"}
    }
    assert "provenance_status" in ri_only, (
        "doc must document provenance_status as reviewer-internal-only (GOV-311 lane rule)"
    )
    for name in ri_only:
        assert name in surface["reviewer_internal"], f"{name!r} missing reviewer-internal"
        assert name not in surface["public"], (
            f"{name!r} documented reviewer-internal-only but LEAKED into the public lane"
        )


# ===========================================================================
# 8.2 card feed — every documented `always` key emitted; every emitted key documented.
# Reviewer-internal only: the feed envelope's access must be reviewer_internal.
# ===========================================================================


def test_stage3_reprojections_are_reviewer_internal_only(surface) -> None:
    # Both Stage-3 re-projections declare the reviewer-internal lane explicitly; there
    # is no public card feed and no public drill-down (§0/§2). A regression that
    # surfaced either at access "public" would be a lane-boundary leak.
    assert surface["feed_body"]["access"] == "reviewer_internal"
    assert surface["verify_body"]["access"] == "reviewer_internal"
    assert surface["feed_body"]["scope"] == "alpine"
    assert surface["verify_body"]["scope"] == "alpine"
    # And the fixture genuinely exercised both card kinds for the coverage checks.
    assert surface["record_card"] and surface["gap_card"]
    assert surface["record_drilldown"] and surface["gap_drilldown"]


def test_card_feed_always_keys_emitted_and_no_undocumented(card_feed_contract, surface) -> None:
    for kind, card_key in (("record_card_key", "record_card"), ("gap_card_key", "gap_card")):
        emitted = set(surface[card_key])
        always = _always_keys(card_feed_contract, kind)
        documented = _all_keys(card_feed_contract, kind)
        missing_always = always - emitted
        undocumented = emitted - documented
        assert not missing_always, f"{card_key}: documented always-keys not emitted: {missing_always}"
        assert not undocumented, f"{card_key}: emitted keys not documented: {undocumented}"


# ===========================================================================
# 8.3 verify-at-source drill-down — same coverage + per-link keys.
# ===========================================================================


def test_verify_drilldown_always_keys_emitted_and_no_undocumented(
    verify_contract, surface
) -> None:
    for kind, key in (
        ("record_drilldown_key", "record_drilldown"),
        ("gap_drilldown_key", "gap_drilldown"),
    ):
        emitted = set(surface[key])
        always = _always_keys(verify_contract, kind)
        documented = _all_keys(verify_contract, kind)
        assert not (always - emitted), f"{key}: documented always-keys not emitted: {always - emitted}"
        assert not (emitted - documented), f"{key}: emitted keys not documented: {emitted - documented}"


def test_verify_link_keys_match_emitted(verify_contract, surface) -> None:
    links = surface["record_drilldown"]["links"]
    assert links, "fixture record drill-down must carry at least one evidence link"
    documented = _all_keys(verify_contract, "link_key")
    always = _always_keys(verify_contract, "link_key")
    for link in links:
        emitted = set(link)
        assert not (always - emitted), f"link missing documented always-keys: {always - emitted}"
        assert not (emitted - documented), f"link emits undocumented keys: {emitted - documented}"


# ===========================================================================
# RED proofs — the guard is genuinely RED when doc and code disagree. Proven
# in-code by mutating the PARSED contract (no file edit) and showing the same
# comparison that passes above then FAILS.
# ===========================================================================


def test_red_on_read_api_phantom_key(read_api_contract, surface) -> None:
    drifted = {kind: {**m} for kind, m in read_api_contract.items()}
    drifted["envelope_key"] = {**drifted["envelope_key"], "totally_made_up": {"public", "reviewer_internal"}}
    failed = any(
        _documented_envelope_keys_for_lane(drifted, lane) != _emitted_envelope_keys(surface[lane])
        for lane in ("public", "reviewer_internal")
    )
    assert failed, "parity check failed to flag a phantom read_api key (guard not RED)"


def test_red_on_read_api_wrong_lane(read_api_contract, surface) -> None:
    drifted = {kind: {**m} for kind, m in read_api_contract.items()}
    # Mis-claim provenance_status as also public — must break public-lane parity.
    drifted["envelope_key"] = {**drifted["envelope_key"], "provenance_status": {"public", "reviewer_internal"}}
    documented_public = _documented_envelope_keys_for_lane(drifted, "public")
    assert documented_public != _emitted_envelope_keys(surface["public"]), (
        "mis-laning provenance_status to public should break public-lane parity (guard not RED)"
    )


def test_red_on_card_feed_phantom_always_key(card_feed_contract, surface) -> None:
    drifted = {kind: {**m} for kind, m in card_feed_contract.items()}
    drifted["record_card_key"] = {**drifted["record_card_key"], "phantom_card_field": {"always"}}
    always = _always_keys(drifted, "record_card_key")
    assert always - set(surface["record_card"]), (
        "a phantom always card key should be 'documented-but-not-emitted' (guard not RED)"
    )


def test_red_on_card_feed_undocumented_key(card_feed_contract, surface) -> None:
    # Drop a real documented key from a contract copy: the emitted key it covered is
    # now undocumented, so the no-undocumented check must fail.
    drifted = {kind: {**m} for kind, m in card_feed_contract.items()}
    drifted["record_card_key"] = {k: v for k, v in drifted["record_card_key"].items() if k != "status"}
    undocumented = set(surface["record_card"]) - _all_keys(drifted, "record_card_key")
    assert "status" in undocumented, (
        "dropping a documented card key should surface an undocumented emitted key (guard not RED)"
    )


def test_red_on_verify_undocumented_link_key(verify_contract, surface) -> None:
    drifted = {kind: {**m} for kind, m in verify_contract.items()}
    drifted["link_key"] = {k: v for k, v in drifted["link_key"].items() if k != "resolvability_status"}
    documented = _all_keys(drifted, "link_key")
    link = surface["record_drilldown"]["links"][0]
    assert set(link) - documented == {"resolvability_status"}, (
        "dropping resolvability_status should surface it as undocumented (guard not RED)"
    )
