"""GOV-406 Stage 3.12 — Stage-3 read-surface traceability + audit-trail auditor.

Proves :mod:`scripts.stage3_traceability` over a deterministic Alpine corpus: the
GOV-306 / Stage 2.12 traceability win one layer up over the live Stage-3 surface
(card feed 3.05 + verify-at-source 3.07). Each of the five checks is shown to be
**load-bearing** by a neuter probe: plant the specific defect -> the check goes RED;
neuter the check (or restore the clean projection) -> it goes green again. Mirrors the
GOV-393 / GOV-370 RED-neuter discipline.

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

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import db  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402
import stage2_traceability as trace  # noqa: E402
import stage3_card_feed as feed  # noqa: E402
import stage3_traceability as s3t  # noqa: E402
import stage3_verify_at_source as vas  # noqa: E402

REVIEWER = "reviewer:isaac"
VAULT_PATH = "/Users/IA/Documents/Obsidian Vault/Source-Data/TownOfAlpine/secret.md"


# ---------------------------------------------------------------------------
# Deterministic Alpine fixture corpus.
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
    """Sources + meeting + agenda + a grounded, raw-preserved transcript/segment."""
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
        note="GOV-406 Stage 3.12 traceability + audit-trail auditor",
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
    """Insert + promote one statement into the reviewer-internal serve (GOV-146).

    ``decision='corrected'`` routes through the sanctioned Lane-5 gate, which writes a
    ``reviewer_decisions(decision='corrected')`` audit row AND flips correction_status —
    the legitimately-corrected card the audit trail expects to be backed.
    """
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
    # (2) a sanctioned correction: routed through promote(decision='corrected') so the
    #     reviewer_decisions audit row exists -> the audit trail is intact.
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


# ===========================================================================
# Baseline — a clean Alpine corpus is fully traceable (AC-1: clean DB => exit 0).
# ===========================================================================


def test_clean_corpus_is_traceable(env) -> None:
    conn, _ = env
    report = s3t.audit_stage3_traceability(conn)
    assert report["clean"] is True, report
    for key in (
        "card_grounding", "verify_at_source_parity", "correction_audit_trail",
        "completeness_gap_parity", "transport",
    ):
        assert report[key]["clean"] is True, (key, report[key])
    # The corpus actually exercises the surfaces (not a vacuous green).
    assert report["served_count"] == 2
    assert report["correction_audit_trail"]["corrections"] == 1
    assert report["completeness_gap_parity"]["canonical_count"] == 2
    assert report["completeness_gap_parity"]["feed_gap_count"] == 2


def test_cli_clean_exits_zero_injected_exits_one(env, tmp_path) -> None:
    """AC-1: the CLI doubles as a CI gate — exit 0 clean, exit 1 on an injected break."""
    conn, _ = env
    db_path = tmp_path / "Database" / "test.db"
    assert s3t.main(["--db", str(db_path)]) == 0
    # Inject a dangling correction (bypass the audit path) and re-run -> exit 1.
    conn.execute(
        "UPDATE statements SET correction_status = 'corrected' WHERE statement_id = 's-healthy'"
    )
    conn.commit()
    assert s3t.main(["--db", str(db_path), "--json"]) == 1


# ===========================================================================
# Check 1 — card_grounding load-bearing (orphan card).
# ===========================================================================


def _plant_orphan(conn: sqlite3.Connection) -> None:
    """A served record grounded only by an evidence link whose source no longer resolves.

    Served via the link path (no segment), so read_api still serves it; but its
    ``to_source_id`` is deleted out from under it (FK off), so the FULL canonical chain
    dangles -> :func:`stage2_traceability.statement_grounded` returns False -> a Stage-3
    orphan card. (Mirrors the GOV-306 "served row whose evidence source was deleted".)
    """
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, jurisdiction, scan_date) VALUES "
        "('orphan_src', 'Orphan', 'alpine', 'website', 'county_relevant', 'secondary', "
        "'Alpine', '2026-05-02')"
    )
    conn.commit()
    _serve(conn, "s-orphan", segment_id=None, evidence=[_ev("orphan_src")])
    conn.commit()
    # Delete the backing source out from under the served link (FK off for the surgery).
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM sources WHERE source_id = 'orphan_src'")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def test_card_grounding_catches_orphan_and_is_load_bearing(env, monkeypatch) -> None:
    conn, _ = env
    _plant_orphan(conn)

    report = s3t.audit_stage3_traceability(conn)
    grounding = report["card_grounding"]
    assert grounding["clean"] is False
    assert "s-orphan" in {o["statement_id"] for o in grounding["orphans"]}
    assert report["clean"] is False

    # Neuter the grounding predicate -> the orphan is hidden -> the check goes green.
    monkeypatch.setattr(trace, "statement_grounded", lambda conn_, sid: True)
    neutered = s3t.audit_stage3_traceability(conn)
    assert neutered["card_grounding"]["clean"] is True


# ===========================================================================
# Check 2 — verify_at_source_parity load-bearing (stored/optimistic resolvability).
# ===========================================================================


def test_verify_parity_catches_optimistic_value_and_is_load_bearing(env, monkeypatch) -> None:
    conn, _ = env
    served = s3t._served_records(conn)
    verify_body = vas.build_verify_at_source(conn)

    # Clean projection -> the independent recompute agrees -> parity holds.
    assert s3t.verify_at_source_parity(conn, served, verify_body)["clean"] is True

    # Plant a fabricated/stored 'resolved' locator with NO canonical evidence-link
    # counterpart (the locator<->canonical-id bijection the check forbids breaking):
    # the independent recompute from the real GOV-306 predicates does not produce it,
    # so the projected list diverges from the recomputed list -> mismatch.
    corrupted = json.loads(json.dumps(verify_body))
    rec_card = next(
        c for c in corrupted["cards"] if c.get("type") != feed.TYPE_SOURCE_MISSING
    )
    rec_card["links"].append(
        {"locator": {}, "resolvability_status": vas.RESOLVABILITY_RESOLVED}
    )
    breach = s3t.verify_at_source_parity(conn, served, corrupted)
    assert breach["clean"] is False
    assert breach["mismatches"], breach

    # Neuter the independent recompute (force it to echo an all-'resolved' verdict):
    # the optimistic projected value is no longer contradicted -> were it not for the
    # length-bijection guard the breach would vanish, proving the recompute is the
    # load-bearing seam.
    monkeypatch.setattr(s3t.vas, "resolvability_status", lambda *a, **k: vas.RESOLVABILITY_RESOLVED)
    # The recompute now yields a single 'resolved' (one canonical link) while the body
    # still carries two -> the bijection-by-length guard keeps it RED (defense in depth).
    assert s3t.verify_at_source_parity(conn, served, corrupted)["clean"] is False


def test_verify_parity_catches_dropped_card_bijection(env) -> None:
    conn, _ = env
    served = s3t._served_records(conn)
    verify_body = vas.build_verify_at_source(conn)
    corrupted = json.loads(json.dumps(verify_body))
    corrupted["cards"] = corrupted["cards"][1:]  # drop one card -> bijection breaks
    breach = s3t.verify_at_source_parity(conn, served, corrupted)
    assert breach["clean"] is False
    assert breach["bijection_ok"] is False


# ===========================================================================
# Check 3 — correction_audit_trail load-bearing (dangling correction).
# ===========================================================================


def test_sanctioned_correction_is_backed(env) -> None:
    """A correction routed through the Lane-5 gate carries its audit row -> clean."""
    conn, _ = env
    report = s3t.audit_stage3_traceability(conn)
    x = report["correction_audit_trail"]
    assert x["clean"] is True
    assert x["corrections"] == 1
    # The audit row genuinely exists for the sanctioned correction.
    assert s3t._has_correction_decision(conn, "s-corrected") is True


def test_dangling_correction_is_caught_and_is_load_bearing(env, monkeypatch) -> None:
    conn, _ = env
    # Bypass the sanctioned gate: flip an already-served record to 'corrected' directly,
    # WITHOUT writing a reviewer_decisions(decision='corrected') audit row.
    conn.execute(
        "UPDATE statements SET correction_status = 'corrected' WHERE statement_id = 's-healthy'"
    )
    conn.commit()
    assert s3t._has_correction_decision(conn, "s-healthy") is False

    report = s3t.audit_stage3_traceability(conn)
    x = report["correction_audit_trail"]
    assert x["clean"] is False
    dangling_ids = {d["statement_id"] for d in x["dangling"]}
    assert "s-healthy" in dangling_ids
    assert next(d for d in x["dangling"] if d["statement_id"] == "s-healthy")["has_review_evidence"] is False
    assert report["clean"] is False

    # Neuter the review-evidence predicate -> the dangling correction is hidden.
    monkeypatch.setattr(s3t, "_has_correction_decision", lambda conn_, sid: True)
    neutered = s3t.audit_stage3_traceability(conn)
    assert neutered["correction_audit_trail"]["clean"] is True


# ===========================================================================
# Check 4 — completeness_gap_parity load-bearing (feed drops a gap).
# ===========================================================================


def test_gap_parity_clean_then_catches_feed_drop(env) -> None:
    conn, _ = env
    feed_body = feed.build_card_feed(conn)
    assert s3t.gap_parity_stage3(conn, feed_body)["clean"] is True

    corrupted = json.loads(json.dumps(feed_body))
    # Drop one gap card from the Stage-3 feed -> the Stage-3 cover leg goes RED.
    gap_idx = next(
        i for i, c in enumerate(corrupted["cards"]) if c.get("type") == feed.TYPE_SOURCE_MISSING
    )
    del corrupted["cards"][gap_idx]
    breach = s3t.gap_parity_stage3(conn, corrupted)
    assert breach["clean"] is False
    assert breach["feed_missing"], breach


def test_gap_parity_carries_forward_stage2_check(env, monkeypatch) -> None:
    """The Stage 2.12 parity leg is reused verbatim and is load-bearing."""
    conn, _ = env
    feed_body = feed.build_card_feed(conn)
    # Neuter the canonical-table read so trace.gap_parity sees a phantom -> RED.
    real = trace.gap_parity
    monkeypatch.setattr(
        s3t.trace, "gap_parity",
        lambda conn_, cards: {**real(conn_, cards), "clean": False, "missing": ["planted"]},
    )
    assert s3t.gap_parity_stage3(conn, feed_body)["clean"] is False


# ===========================================================================
# Check 5 — transport load-bearing (raw path / PII in the composed body).
# ===========================================================================


def test_transport_clean_then_catches_raw_path(env) -> None:
    conn, _ = env
    feed_body = feed.build_card_feed(conn)
    verify_body = vas.build_verify_at_source(conn)
    assert s3t.transport_clean(conn, feed_body, verify_body)["clean"] is True

    # Inject a reviewer-internal vault path into a verify-at-source locator.
    leaked = json.loads(json.dumps(verify_body))
    leaked["cards"][0].setdefault("links", []).append(
        {"locator": {"note": VAULT_PATH}, "resolvability_status": vas.RESOLVABILITY_UNRESOLVED}
    )
    breach = s3t.transport_clean(conn, feed_body, leaked)
    assert breach["clean"] is False
    assert breach["error"]


def test_transport_catches_structured_pii(env) -> None:
    conn, _ = env
    feed_body = feed.build_card_feed(conn)
    verify_body = vas.build_verify_at_source(conn)
    leaked = json.loads(json.dumps(feed_body))
    leaked["cards"][0]["reviewed_summary"] = "contact resident jane.doe@example.com"
    breach = s3t.transport_clean(conn, leaked, verify_body)
    assert breach["clean"] is False


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
