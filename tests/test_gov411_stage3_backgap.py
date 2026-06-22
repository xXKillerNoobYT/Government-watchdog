"""GOV-411 Stage 3.13 — Stage-3 read-surface back-gap / coverage-regression auditor.

Proves :mod:`scripts.stage3_backgap` over a deterministic Alpine corpus: the GOV-322 /
Stage 2.13 back-gap win one layer up over the live Stage-3 surface (card feed 3.05 +
verify-at-source 3.07 + preservation 3.04 + source-inventory 3.03). Each of the six
checks is shown to be **load-bearing** by a neuter/poison probe: plant the specific
silent-shrinkage defect -> the check goes RED; neuter the check (or restore the clean
projection) -> it goes green again. Mirrors the GOV-406 / GOV-322 RED-neuter discipline.

The independent-recompute seam is proven *genuine* (not a tautology against the same
assembly gate): for the card-feed back-gap, neutering the independent eligibility oracle
(:func:`stage2_backgap.reviewer_eligible_ids`) to echo the feed's served set hides a real
feed drop — proving the oracle, not the projection, is what catches the shrinkage.

Test-only / read-only: imports EXISTING projection + auditor functions, adds NO
production projection, envelope key, schema, migration, AI, or network. Pure sqlite +
tmp files, Alpine-only, reviewer-internal. ``scripts/read_api.py`` /
``scripts/publication.py`` stay byte-0-diff (an explicit git-diff assertion pins it).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402
import stage2_backgap as s2bg  # noqa: E402
import stage3_backgap as bg  # noqa: E402
import stage3_card_feed as feed  # noqa: E402
import stage3_verify_at_source as vas  # noqa: E402
import statements as st  # noqa: E402

REVIEWER = "reviewer:isaac"
VAULT_PATH = "/Users/IA/Documents/Obsidian Vault/Source-Data/TownOfAlpine/secret.md"


# ---------------------------------------------------------------------------
# Deterministic Alpine fixture corpus (mirrors the Stage 3.12 corpus shape).
# ---------------------------------------------------------------------------


def _ev(to_source_id: str, **extra: object) -> dict[str, object]:
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


def _seed_anchors(conn: sqlite3.Connection, repo_root: Path) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, jurisdiction, original_url, scan_date, last_validated_utc, "
        "archive_url, archive_status, raw_local_path) VALUES "
        "('alpine_packet', 'Agenda Packet', 'alpine', 'document', 'official', 'primary', "
        "'Alpine', 'https://alpinewy.gov/packet.pdf', '2026-05-01', '2026-05-09T00:00:00Z', "
        "'https://web.archive.org/web/2026/x', 'available', ?)",
        (VAULT_PATH,),
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-05-08:item-7', 1, 7, 'Fireworks ban — adoption')"
    )
    grounded_text = "Alpine council transcript text."
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class, source_id) VALUES (1, 'vid-1', "
        "'https://youtu.be/vid-1', ?, 'n/a', ?, '2026-05-08T00:00:00Z', "
        "'official_transcript', 'alpine_packet')",
        (grounded_text, rp.sha256_text(grounded_text)),
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-grounded', 1, 0, 0, '00:00', 'Mayor calls the meeting to order.')"
    )
    raw_path = repo_root / "Raw-Corpus" / "packet.pdf"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"%PDF intact alpine packet bytes")
    conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES ('https://alpine/packet', 'Raw-Corpus/packet.pdf', ?, "
        "'2026-05-08T00:00:00Z', 'alpine_packet')",
        (rp.sha256_file(raw_path),),
    )
    gate.register_reviewer(
        conn, REVIEWER, display_name="Isaac", registered_by="owner:isaac",
        note="GOV-411 Stage 3.13 back-gap auditor",
    )
    conn.commit()


def _serve(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    segment_id: str | None,
    evidence: list[dict[str, object]] | None,
    decision: str = "approved",
) -> None:
    record: dict[str, object] = {
        "statement_id": statement_id,
        "segment_id": segment_id,
        "agenda_item_id": "alpine:2026-05-08:item-7",
        "statement_text": f"The council adopted the fireworks ban ({statement_id}).",
        "produced_by": "human",
    }
    if evidence is None:
        st.insert_statement(conn, record)
    else:
        st.insert_statement(conn, record, evidence)
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision=decision,
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _seed_corpus(conn: sqlite3.Connection, repo_root: Path) -> None:
    _seed_anchors(conn, repo_root)
    # (1) healthy grounded approved record.
    _serve(conn, "s-healthy", segment_id="seg-grounded", evidence=[_ev("alpine_packet")])
    # (2) a sanctioned correction routed through promote(decision='corrected') — the
    #     reviewer_decisions audit row exists, so the audit-trail overlay is intact.
    _serve(conn, "s-corrected", segment_id="seg-grounded", evidence=[_ev("alpine_packet")],
           decision="corrected")
    # (3) completeness-gap rows (one clean, one with a leak-prone detail).
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="meeting folder 2026-04-10 has only derived (.md) material", commit=True,
    )
    comp.record_gap(
        conn, subject_node_id="2026-04-11", subject_node_type="meeting",
        gap_type="no_primary_source", detail=f"see {VAULT_PATH}", commit=True,
    )
    conn.commit()


@pytest.fixture()
def env(tmp_path: Path):
    db_path = tmp_path / "Database" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_corpus(connection, tmp_path)
    yield connection, tmp_path
    connection.close()


def _audit(env):
    conn, root = env
    return bg.audit_backgap(conn, repo_root=root)


# ===========================================================================
# Baseline — a clean Alpine corpus has NO back-gap (clean DB => exit 0).
# ===========================================================================


def test_clean_corpus_has_no_backgap(env) -> None:
    report = _audit(env)
    assert report["clean"] is True, report
    for key in (
        "card_feed_no_backgap", "public_lane_no_backgap",
        "completeness_gap_coverage_parity", "stage3_overlay_presence_no_regression",
        "stageN_field_floor", "determinism_read_only",
    ):
        assert report[key]["clean"] is True, (key, report[key])
    # The corpus actually exercises the surfaces (not a vacuous green).
    assert report["served_count"] == 2
    assert report["card_feed_no_backgap"]["eligible_count"] == 2
    assert report["card_feed_no_backgap"]["served_count"] == 2
    assert report["completeness_gap_coverage_parity"]["canonical_count"] == 2
    assert report["completeness_gap_coverage_parity"]["feed_gap_count"] == 2
    assert report["stage3_overlay_presence_no_regression"]["bijection_ok"] is True
    assert report["stage3_overlay_presence_no_regression"]["preservation_present"] is True


def test_cli_clean_exits_zero_injected_exits_one(env, monkeypatch) -> None:
    """AC: the CLI doubles as a CI gate — exit 0 clean, exit 1 on an injected back-gap."""
    conn, root = env
    db_path = root / "Database" / "test.db"
    # The CLI defaults preservation repo_root to rp.REPO_ROOT; point it at the tmp
    # corpus so the run is hermetic and the clean assertion is real.
    monkeypatch.setattr(rp, "REPO_ROOT", root)
    assert bg.main(["--db", str(db_path)]) == 0
    # Inject a back-gap: render an already-served record as a correction WITHOUT its
    # Lane-5 audit row -> the audit-trail overlay stops attaching -> exit 1.
    conn.execute(
        "UPDATE statements SET correction_status = 'corrected' WHERE statement_id = 's-healthy'"
    )
    conn.commit()
    assert bg.main(["--db", str(db_path), "--json"]) == 1


# ===========================================================================
# Check 1 — card_feed_no_backgap load-bearing (a record class dropped by the feed).
# ===========================================================================


def test_card_feed_backgap_catches_feed_drop_and_oracle_is_load_bearing(env, monkeypatch) -> None:
    conn, _ = env
    served = bg._served_records(conn)
    feed_body = feed.build_card_feed(conn)

    # Clean: the independent eligible set is fully covered by the live feed.
    assert bg.card_feed_no_backgap(conn, feed_body, served)["clean"] is True

    # Plant the silent shrinkage: drop one record card from the live feed (a fail-closed
    # overlay default dropping a record class). The independent oracle still counts it
    # eligible -> eligible-but-not-served -> RED.
    corrupted = json.loads(json.dumps(feed_body))
    rec_idx = next(
        i for i, c in enumerate(corrupted["cards"]) if c.get("type") != feed.TYPE_SOURCE_MISSING
    )
    del corrupted["cards"][rec_idx]
    breach = bg.card_feed_no_backgap(conn, corrupted, served)
    assert breach["clean"] is False
    assert breach["backgap"], breach

    # Prove the independent recompute is the genuine seam (NOT a tautology): neuter the
    # oracle so it no longer counts the dropped record eligible -> the drop is hidden ->
    # green. The independent oracle, not the projection, is what catches the shrinkage.
    surviving = {
        sid for h, sid in {bg._record_handle(r): r["statement_id"] for r in served}.items()
        if h in {c.get("handle") for c in corrupted["cards"]}
    }
    monkeypatch.setattr(s2bg, "reviewer_eligible_ids", lambda conn_: set(surviving))
    assert bg.card_feed_no_backgap(conn, corrupted, served)["clean"] is True


# ===========================================================================
# Check 2 — public_lane_no_backgap load-bearing (dropped publish + silent public gain).
# ===========================================================================


def test_public_lane_backgap_catches_dropped_publish(env, monkeypatch) -> None:
    conn, _ = env
    bodies = {"card_feed": {"access": "reviewer_internal"}}

    # Clean: nothing publishable on Alpine -> both sets empty -> no back-gap.
    assert bg.public_lane_no_backgap(conn, bodies)["clean"] is True

    # Plant a publish-eligible id the served public lane does not carry (a published row
    # silently dropped) -> RED. Restore -> green (load-bearing).
    monkeypatch.setattr(s2bg, "publish_eligible_ids", lambda conn_: {"s-phantom-published"})
    breach = bg.public_lane_no_backgap(conn, bodies)
    assert breach["clean"] is False
    assert "s-phantom-published" in breach["backgap"]


def test_public_lane_catches_silent_public_gain(env) -> None:
    conn, _ = env
    # A Stage-3 body that silently flipped its access to public -> listed as a leak -> RED.
    bodies = {"verify_at_source": {"access": "public"}}
    breach = bg.public_lane_no_backgap(conn, bodies)
    assert breach["clean"] is False
    assert "verify_at_source" in breach["public_lane_leaks"]


# ===========================================================================
# Check 3 — completeness_gap_coverage_parity load-bearing (gap dropped by feed/projection).
# ===========================================================================


def test_gap_parity_catches_feed_drop(env) -> None:
    conn, _ = env
    feed_body = feed.build_card_feed(conn)
    assert bg.completeness_gap_coverage_parity(conn, feed_body)["clean"] is True

    corrupted = json.loads(json.dumps(feed_body))
    gap_idx = next(
        i for i, c in enumerate(corrupted["cards"]) if c.get("type") == feed.TYPE_SOURCE_MISSING
    )
    del corrupted["cards"][gap_idx]
    breach = bg.completeness_gap_coverage_parity(conn, corrupted)
    assert breach["clean"] is False
    assert breach["feed_dropped"], breach


def test_gap_parity_catches_projection_drop(env, monkeypatch) -> None:
    conn, _ = env
    feed_body = feed.build_card_feed(conn)
    # Neuter the read-surface gap projection to drop every gap while the canonical table
    # still holds them -> canonical-but-not-projected -> RED (load-bearing).
    monkeypatch.setattr(read_api, "completeness_gap_cards", lambda conn_: [])
    breach = bg.completeness_gap_coverage_parity(conn, feed_body)
    assert breach["clean"] is False
    assert len(breach["dropped"]) == 2


# ===========================================================================
# Check 4 — stage3_overlay_presence_no_regression load-bearing.
# ===========================================================================


def test_overlay_presence_catches_bijection_break(env) -> None:
    conn, _ = env
    served = bg._served_records(conn)
    verify_body = vas.build_verify_at_source(conn)
    pres_body = __import__("stage3_preservation_audit").build_preservation_overlay(conn, env[1])
    inv_ids = {e.get("source_id") for e in __import__("stage3_source_inventory").source_inventory(conn)}

    assert bg.stage3_overlay_presence_no_regression(
        conn, served, verify_body, pres_body, inv_ids
    )["clean"] is True

    # Drop a drill-down card -> the verify overlay stopped covering a card -> bijection RED.
    corrupted = json.loads(json.dumps(verify_body))
    corrupted["cards"] = corrupted["cards"][1:]
    breach = bg.stage3_overlay_presence_no_regression(
        conn, served, corrupted, pres_body, inv_ids
    )
    assert breach["clean"] is False
    assert breach["bijection_ok"] is False


def test_overlay_presence_catches_inventory_linkage_loss(env) -> None:
    conn, _ = env
    served = bg._served_records(conn)
    verify_body = vas.build_verify_at_source(conn)
    pres_body = __import__("stage3_preservation_audit").build_preservation_overlay(conn, env[1])

    # The source-inventory silently dropped the source class -> a sourced card's canonical
    # source is no longer present in the inventory -> linkage missing -> RED.
    breach = bg.stage3_overlay_presence_no_regression(
        conn, served, verify_body, pres_body, set()
    )
    assert breach["clean"] is False
    assert any(
        "source-inventory linkage missing" in v
        for m in breach["missing"] for v in m["violations"]
    ), breach


def test_overlay_presence_catches_dangling_correction_audit_trail(env, monkeypatch) -> None:
    conn, _ = env
    # Render an already-served record as a correction WITHOUT its Lane-5 audit row -> the
    # audit-trail overlay stops attaching to that correction class -> RED.
    conn.execute(
        "UPDATE statements SET correction_status = 'corrected' WHERE statement_id = 's-healthy'"
    )
    conn.commit()
    served = bg._served_records(conn)
    verify_body = vas.build_verify_at_source(conn)
    pres_body = __import__("stage3_preservation_audit").build_preservation_overlay(conn, env[1])
    inv_ids = {e.get("source_id") for e in __import__("stage3_source_inventory").source_inventory(conn)}

    breach = bg.stage3_overlay_presence_no_regression(
        conn, served, verify_body, pres_body, inv_ids
    )
    assert breach["clean"] is False
    assert any(
        "correction audit-trail row absent" in v
        for m in breach["missing"] for v in m["violations"]
    ), breach

    # Neuter the audit-trail predicate -> the dangling correction is hidden -> green.
    monkeypatch.setattr(bg.trace3, "_has_correction_decision", lambda conn_, sid: True)
    neutered = bg.stage3_overlay_presence_no_regression(
        conn, served, verify_body, pres_body, inv_ids
    )
    assert neutered["clean"] is True


def test_overlay_presence_catches_preservation_overlay_dropout(env) -> None:
    conn, _ = env
    served = bg._served_records(conn)
    verify_body = vas.build_verify_at_source(conn)
    inv_ids = {e.get("source_id") for e in __import__("stage3_source_inventory").source_inventory(conn)}

    # Stored objects exist, but the preservation overlay silently emits zero units -> the
    # whole unit class was dropped -> RED.
    empty_overlay = {"access": "reviewer_internal", "manifest_digest": "x", "units": []}
    breach = bg.stage3_overlay_presence_no_regression(
        conn, served, verify_body, empty_overlay, inv_ids
    )
    assert breach["clean"] is False
    assert breach["preservation_present"] is False


# ===========================================================================
# Check 5 — stageN_field_floor load-bearing (Stage-3 overlay strips an earlier field).
# ===========================================================================


def test_field_floor_catches_card_floor_strip(env) -> None:
    conn, _ = env
    served = bg._served_records(conn)
    feed_body = feed.build_card_feed(conn)
    assert bg.stageN_field_floor(conn, served, feed_body)["clean"] is True

    # A Stage-3 overlay stripped the card's status field -> card floor breach -> RED.
    corrupted = json.loads(json.dumps(feed_body))
    rec_card = next(
        c for c in corrupted["cards"] if c.get("type") != feed.TYPE_SOURCE_MISSING
    )
    del rec_card["status"]
    breach = bg.stageN_field_floor(conn, served, corrupted)
    assert breach["clean"] is False
    assert any("card missing 'status'" in v for b in breach["breaches"] for v in b["violations"])


def test_field_floor_catches_record_floor_strip(env) -> None:
    conn, _ = env
    served = bg._served_records(conn)
    feed_body = feed.build_card_feed(conn)
    # A served record that lost its Stage-1 evidence drawer -> record floor breach -> RED.
    served[0] = {k: v for k, v in served[0].items() if k != "evidence"}
    breach = bg.stageN_field_floor(conn, served, feed_body)
    assert breach["clean"] is False


# ===========================================================================
# Check 6 — determinism_read_only load-bearing (non-deterministic recompute).
# ===========================================================================


def test_determinism_clean_then_catches_unstable_recompute(env, monkeypatch) -> None:
    conn, root = env
    assert bg.determinism_read_only(conn, root)["clean"] is True

    # Force the eligibility oracle to return a different value on each call -> the two
    # snapshot passes diverge -> byte_identical False -> RED (the check is load-bearing).
    counter = {"n": 0}

    def _unstable(conn_):
        counter["n"] += 1
        return {f"phantom-{counter['n']}"}

    monkeypatch.setattr(s2bg, "reviewer_eligible_ids", _unstable)
    breach = bg.determinism_read_only(conn, root)
    assert breach["clean"] is False
    assert breach["byte_identical"] is False


# ===========================================================================
# 0-diff guard — the auditor is additive (read_api.py / publication.py unchanged).
# ===========================================================================


def test_read_api_and_publication_zero_diff_vs_main() -> None:
    """git-diff evidence: the SSOT modules are byte-for-byte unchanged vs origin/main."""
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--", "scripts/read_api.py", "scripts/publication.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"read_api.py/publication.py drifted from origin/main:\n{diff.stdout}"
