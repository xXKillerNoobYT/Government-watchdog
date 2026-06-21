"""GOV-376 Stage 3.07 — verify-at-source drill-down projection + read-time auditor.

Proves the GOV-375 contract (``Docs/stage3-07-verify-at-source-contract.md``)
against :mod:`stage3_verify_at_source` + :mod:`stage3_verify_at_source_audit`. Each
test maps to a contract §7 RED item; every guard is shown load-bearing via an
explicit neuter probe (mirror GOV-367 / GOV-350 / GOV-311):

- **R-1** resolvability resolves — a link whose ``to_source_id`` resolves -> ``resolved``;
  neuter ``_link_source_resolves`` -> the same link reads ``unresolved`` (load-bearing).
- **R-2** resolvability fail-closed — a dangling ``to_source_id`` + non-resolving
  segment + no raw -> ``unresolved`` (optimism is never the default).
- **R-3** unpreserved raw — a card grounded-by-chain but with no preserved raw is
  ``unverified`` (verify-at-source not claimed on an un-reproducible citation); the
  link may still be ``resolved`` via the segment leg, but the CARD is not verifiable.
- **R-4** verify-at-source requires BOTH legs — a resolved link + ``unverified``
  provenance -> ``unverified``; a grounded card with zero resolved links -> ``unverified``.
- **R-5** verifiable positive — ≥1 resolved link AND ``grounded`` -> ``verifiable``.
- **R-6** back-gap bijection — a planted drop -> RED; gap cards are ``source_missing``,
  never ``verifiable``.
- **R-7** no-leak / lane — a planted raw locator on a link is stripped from the body,
  ``assert_no_raw_paths`` stays green, and the public lane carries no verify key.

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402
import stage2_traceability as trace  # noqa: E402
import stage3_card_feed as feed  # noqa: E402
import stage3_verify_at_source as vas  # noqa: E402
import stage3_verify_at_source_audit as audit  # noqa: E402
import statements as st  # noqa: E402

REVIEWER = "reviewer:isaac"


# ---------------------------------------------------------------------------
# Fixture + seed helpers (mirror the GOV-311 served-corpus seed)
# ---------------------------------------------------------------------------


def _seed_base(conn: sqlite3.Connection) -> None:
    """A resolvable source + a hash-preserved transcript (seg-1) + an unpreserved one.

    * ``alpine_packet`` — a real ``sources`` row (so a link's ``to_source_id`` resolves).
    * ``seg-1`` -> transcript 1 with a recorded ``sha256`` -> grounded AND raw-preserved.
    * ``seg-unpreserved`` -> transcript 2 with a BLANK ``sha256`` -> grounded by chain
      but NOT raw-preserved (``raw_linked`` reads a falsy hash as unpreserved).
    """
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
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class) VALUES (2, 'vid-2', "
        "'https://youtu.be/vid-2', 'Unpreserved transcript text.', 'n/a', '', "
        "'2026-05-08T00:00:00Z', 'official_transcript')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-unpreserved', 2, 0, 0, '00:00', 'Unpreserved segment.')"
    )
    gate.register_reviewer(
        conn, REVIEWER, display_name="Isaac", registered_by="owner:isaac",
        note="GOV-376 verify-at-source tests",
    )
    conn.commit()


def _ev(to_source_id: str, **extra: object) -> dict[str, object]:
    """A valid evidence pointer (every ``_POINTER_REQUIRED`` field, valid enums).

    ``to_source_id`` must resolve to a real ``sources`` row at insert (the write gate
    validates it) — a dangling link is simulated post-insert by deleting the source.
    """
    base: dict[str, object] = {
        "to_source_id": to_source_id,
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
    }
    base.update(extra)
    return base


def _serve(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    segment_id: str | None = "seg-1",
    produced_by: str = "human",
    evidence: list[dict[str, object]] | None = None,
) -> None:
    """Insert + promote a statement into the reviewer-internal serve (GOV-146)."""
    record: dict[str, object] = {
        "statement_id": statement_id,
        "segment_id": segment_id,
        "agenda_item_id": "alpine:2026-05-08:item-7",
        "statement_text": f"Reviewed civic claim {statement_id}.",
        "produced_by": produced_by,
    }
    if evidence is None:
        st.insert_statement(conn, record)
    else:
        st.insert_statement(conn, record, evidence)
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_base(connection)
    yield connection
    connection.close()


def _card(conn: sqlite3.Connection, statement_id: str) -> dict:
    """The drill-down card for a served statement, by its feed handle."""
    handle = feed.card_handle(
        feed._resolve_record_type(
            next(r for r in read_api.reviewer_internal_records(conn)
                 if r["statement_id"] == statement_id)
        ),
        statement_id,
    )
    body = vas.build_verify_at_source(conn)
    card = next((c for c in body["cards"] if c["handle"] == handle), None)
    assert card is not None, f"{statement_id!r} missing from the drill-down"
    return card


# ---------------------------------------------------------------------------
# Frozen SSOT vocab (VS-5 / contract §2/§3)
# ---------------------------------------------------------------------------


def test_frozen_ssot_vocab() -> None:
    assert isinstance(vas.RESOLVABILITY_VALUES, frozenset)
    assert vas.RESOLVABILITY_VALUES == {"resolved", "unresolved"}
    assert vas.RESOLVABILITY_UNRESOLVED == "unresolved"  # the fail-closed default
    assert isinstance(vas.VERIFY_AT_SOURCE_VALUES, frozenset)
    assert vas.VERIFY_AT_SOURCE_VALUES == {"verifiable", "unverified"}
    assert vas.VERIFY_AT_SOURCE_UNVERIFIED == "unverified"  # the fail-closed default
    assert vas.VERIFY_AT_SOURCE_SOURCE_MISSING == "source_missing"


# ---------------------------------------------------------------------------
# R-1 — resolvability resolves (+ neuter probe proving it load-bearing)
# ---------------------------------------------------------------------------


def test_r1_link_resolves_when_source_resolves(conn: sqlite3.Connection) -> None:
    # segment_id=None isolates leg 2: the link resolves ONLY because alpine_packet
    # is a real sources row.
    _serve(conn, "r1", segment_id=None, evidence=[_ev("alpine_packet")])
    card = _card(conn, "r1")
    assert card["links"][0]["resolvability_status"] == vas.RESOLVABILITY_RESOLVED


def test_r1_neuter_link_source_predicate_goes_red(conn: sqlite3.Connection, monkeypatch) -> None:
    # Neuter the real leg-2 predicate -> the SAME link now reads unresolved, proving
    # resolvability is derived from the real predicate, not a stored/optimistic flag.
    _serve(conn, "r1n", segment_id=None, evidence=[_ev("alpine_packet")])
    assert _card(conn, "r1n")["links"][0]["resolvability_status"] == vas.RESOLVABILITY_RESOLVED
    monkeypatch.setattr(vas, "_link_source_resolves", lambda *a, **k: False)
    assert _card(conn, "r1n")["links"][0]["resolvability_status"] == vas.RESOLVABILITY_UNRESOLVED


# ---------------------------------------------------------------------------
# R-2 — resolvability fail-closed (dangling -> unresolved)
# ---------------------------------------------------------------------------


def test_r2_dangling_source_is_unresolved(conn: sqlite3.Connection) -> None:
    # A link whose source is deleted AFTER insert (FK-off) dangles: no segment, the
    # to_source_id no longer resolves, no preserved raw -> all three legs fail ->
    # unresolved (never optimistically resolved). This is the only way past the write
    # gate, which requires to_source_id to resolve at insert time.
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class) "
        "VALUES ('throwaway', 'Throwaway', 'alpine', 'document', 'official')"
    )
    conn.commit()
    _serve(conn, "r2", segment_id=None, evidence=[_ev("throwaway")])
    db_path = _db_path(conn)
    conn.close()
    raw = sqlite3.connect(db_path)  # FK enforcement OFF -> the link is left dangling
    raw.execute("DELETE FROM sources WHERE source_id = 'throwaway'")
    raw.commit()
    raw.close()
    with db.open_db(db_path) as conn2:
        card = _card(conn2, "r2")
        assert card["links"][0]["resolvability_status"] == vas.RESOLVABILITY_UNRESOLVED
        assert card["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_UNVERIFIED


def test_r2_unit_default_is_unresolved(conn: sqlite3.Connection) -> None:
    # The derivation unit, isolated: a dangling link with no resolving signal.
    status = vas.resolvability_status(
        conn, "missing-stmt", {"to_source_id": "ghost-source"},
    )
    assert status == vas.RESOLVABILITY_UNRESOLVED


# ---------------------------------------------------------------------------
# R-3 — unpreserved raw -> card unverified (un-reproducible citation)
# ---------------------------------------------------------------------------


def test_r3_unpreserved_raw_card_is_unverified(conn: sqlite3.Connection) -> None:
    _serve(conn, "r3", segment_id="seg-unpreserved", evidence=[_ev("alpine_packet")])
    card = _card(conn, "r3")
    # The link resolves via the segment leg ...
    assert card["links"][0]["resolvability_status"] == vas.RESOLVABILITY_RESOLVED
    # ... but the citation is not reproducible (raw not preserved) -> not verifiable.
    assert trace.raw_linked(conn, "r3") is False
    assert card["provenance_status"] == read_api.PROVENANCE_UNVERIFIED
    assert card["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_UNVERIFIED


def test_r3_neuter_raw_linked_flips_to_verifiable(conn: sqlite3.Connection, monkeypatch) -> None:
    # Proving the raw-preservation leg is load-bearing: force raw_linked True and the
    # un-reproducible card wrongly becomes verifiable.
    _serve(conn, "r3n", segment_id="seg-unpreserved", evidence=[_ev("alpine_packet")])
    assert _card(conn, "r3n")["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_UNVERIFIED
    monkeypatch.setattr(trace, "raw_linked", lambda *a, **k: True)
    assert _card(conn, "r3n")["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_VERIFIABLE


# ---------------------------------------------------------------------------
# R-4 — verify-at-source requires BOTH legs
# ---------------------------------------------------------------------------


def test_r4_grounded_but_zero_resolved_links_is_unverified(conn: sqlite3.Connection) -> None:
    # Grounded by chain+raw (seg-1) but NO evidence link -> zero resolved links ->
    # unverified by construction (still emitted, never dropped).
    _serve(conn, "r4", segment_id="seg-1", evidence=None)
    card = _card(conn, "r4")
    assert card["provenance_status"] == read_api.PROVENANCE_GROUNDED
    assert card["links"] == []
    assert card["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_UNVERIFIED


def test_r4_unit_requires_both_legs() -> None:
    assert vas.verify_at_source_status(True, True) == vas.VERIFY_AT_SOURCE_VERIFIABLE
    assert vas.verify_at_source_status(True, False) == vas.VERIFY_AT_SOURCE_UNVERIFIED
    assert vas.verify_at_source_status(False, True) == vas.VERIFY_AT_SOURCE_UNVERIFIED
    assert vas.verify_at_source_status(False, False) == vas.VERIFY_AT_SOURCE_UNVERIFIED


# ---------------------------------------------------------------------------
# R-5 — verifiable positive (both legs hold)
# ---------------------------------------------------------------------------


def test_r5_verifiable_when_resolved_and_grounded(conn: sqlite3.Connection) -> None:
    _serve(conn, "r5", segment_id="seg-1", evidence=[_ev("alpine_packet")])
    card = _card(conn, "r5")
    assert card["provenance_status"] == read_api.PROVENANCE_GROUNDED
    assert any(
        l["resolvability_status"] == vas.RESOLVABILITY_RESOLVED for l in card["links"]
    )
    assert card["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_VERIFIABLE


# ---------------------------------------------------------------------------
# R-6 — back-gap bijection (+ gap cards never verifiable)
# ---------------------------------------------------------------------------


def test_r6_covers_surface_clean(conn: sqlite3.Connection) -> None:
    _serve(conn, "r6a", segment_id="seg-1", evidence=[_ev("alpine_packet")])
    _serve(conn, "r6b", segment_id="seg-unpreserved")
    assert vas.assert_covers_surface(conn) is True


def test_r6_planted_drop_goes_red(conn: sqlite3.Connection) -> None:
    _serve(conn, "r6drop", segment_id="seg-1", evidence=[_ev("alpine_packet")])
    body = vas.build_verify_at_source(conn)
    body["cards"].pop()  # silently drop a card the feed mandates
    with pytest.raises(vas.VerifyCoverageError):
        vas.assert_covers_surface(conn, body)


def test_r6_gap_card_is_source_missing_never_verifiable() -> None:
    gap_card = vas._gap_drilldown({"gap_id": "gap-1"})
    assert gap_card["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_SOURCE_MISSING
    assert gap_card["verify_at_source_status"] != vas.VERIFY_AT_SOURCE_VERIFIABLE
    assert gap_card["links"] == []


# ---------------------------------------------------------------------------
# R-7 — no-leak / lane separation
# ---------------------------------------------------------------------------


def test_r7_planted_raw_locator_stripped_and_sweep_green(conn: sqlite3.Connection) -> None:
    # A raw transcript_path / deep_link planted on the link must never reach the body;
    # the web-safe drawer strips them and the transport sweep stays green.
    _serve(conn, "r7", segment_id="seg-1", evidence=[
        _ev("alpine_packet",
            transcript_path="/Users/IA/Obsidian Vault/Source-Data/raw.txt",
            deep_link="/Users/IA/Raw-PDFs/packet.pdf#page=1"),
    ])
    body = vas.build_verify_at_source(conn)  # runs assert_no_raw_paths internally
    blob = json.dumps(body)
    for marker in ("/Users/", "Source-Data", "Raw-PDFs", "transcript_path", "deep_link"):
        assert marker not in blob, f"leak: {marker!r} reached the drill-down"


def test_r7_reviewer_internal_lane_only(conn: sqlite3.Connection) -> None:
    _serve(conn, "r7lane", segment_id="seg-1", evidence=[_ev("alpine_packet")])
    body = vas.build_verify_at_source(conn)
    assert body["access"] == "reviewer_internal"
    assert body["scope"] == "alpine"
    # the public lane carries no verify-at-source key.
    public_blob = json.dumps(read_api.build_response(conn))
    assert "verify_at_source_status" not in public_blob
    assert "resolvability_status" not in public_blob


def test_r7_module_consumes_read_api_read_only() -> None:
    # The projection binds the real read surface (not a fork / stand-in); the real
    # 0-diff proof is the PR diff.
    assert vas.read_api is read_api
    assert vas.trace is trace
    assert vas.feed is feed


# ---------------------------------------------------------------------------
# Auditor (VS-1..5) — clean pass + each check proven load-bearing
# ---------------------------------------------------------------------------


def test_audit_clean_on_healthy_corpus(conn: sqlite3.Connection) -> None:
    _serve(conn, "ok-verifiable", segment_id="seg-1", evidence=[_ev("alpine_packet")])
    _serve(conn, "ok-unverified", segment_id="seg-unpreserved")
    _serve(conn, "ok-evidence-less", segment_id="seg-1")  # grounded, zero links -> unverified
    result = audit.run_audit(conn)
    assert result["clean"] is True, result
    assert all(c["ok"] for c in result["checks"].values())
    assert audit._main(["--db", str(_db_path(conn))]) == 0


def test_audit_vs2_catches_fabricated_verifiable() -> None:
    # A card claiming verifiable with no resolved link is exactly what VS-2 forbids.
    fabricated = {
        "cards": [{
            "handle": "c1_fake",
            "type": "statement",
            "verify_at_source_status": vas.VERIFY_AT_SOURCE_VERIFIABLE,
            "links": [{"locator": {}, "resolvability_status": vas.RESOLVABILITY_UNRESOLVED}],
        }]
    }
    verdict = audit.check_vs2_no_dangling_claim(fabricated)
    assert verdict["ok"] is False
    assert "c1_fake" in verdict["offenders"]


def test_audit_vs5_catches_off_vocab_status() -> None:
    poisoned = {
        "cards": [{
            "handle": "c1_bad",
            "type": "statement",
            "verify_at_source_status": "definitely-true",  # not in the frozen SSOT
            "links": [{"locator": {}, "resolvability_status": "maybe"}],
        }]
    }
    verdict = audit.check_vs5_frozen_vocab(poisoned)
    assert verdict["ok"] is False
    assert "c1_bad" in verdict["bad_cards"]
    assert "c1_bad" in verdict["bad_links"]


def _db_path(conn: sqlite3.Connection) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])
