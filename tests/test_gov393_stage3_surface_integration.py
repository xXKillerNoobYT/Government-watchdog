"""GOV-393 Stage 3.10 — Stage-3 composition integration safety net.

The reviewable Alpine MVP now layers FOUR Stage-3 projections on top of the
already-web-safe Stage-2 read surface (``scripts/read_api.py``):

* ``stage3_card_feed`` (3.05, GOV-347) — the timeline card feed;
* ``stage3_verify_at_source`` + ``stage3_verify_at_source_audit`` (3.07, GOV-376)
  — the per-card verify-at-source drill-down + read-time auditor;
* ``stage3_source_inventory`` (3.03, GOV-364) — the per-source coverage inventory;
* ``stage3_preservation_audit`` (3.04, GOV-367) — the per-unit raw-preservation overlay.

Each shipped with its OWN isolated unit test + poison driver + VSR/SecPriv audit.
None compose all four over ONE deterministic Alpine corpus and assert the
cross-projection invariants *together*. This is that end-to-end safety net — the
Stage-3 analogue of the Stage-2.10 win ``tests/test_gov317_read_surface_integration.py``
(GOV-318), one layer up. Isolation-clean is not composition-clean: an interaction
across projections could drop/duplicate a record or leak a raw path / PII /
reviewer-internal key that no isolated test can see.

It builds ONE deterministic multi-record Alpine fixture spanning the projection
dimensions (a healthy human record, a healthy AI record, a multi-dimension poison
record, a reviewable source, a seed-only source, and clean / FS-path / PII gap
rows), drives the WHOLE composed body through all four Stage-3 projections +
``read_api`` once, and asserts:

* **INV1 — same-record-set agreement:** every card the feed (3.05) surfaces is
  drill-down-resolvable via verify-at-source (3.07) — a handle-level *bijection*;
  every source a surfaced record traces to is counted ``reviewable`` in the
  inventory (3.03) and covered as a preservation unit (3.04); completeness-gap rows
  reconcile across feed + verify, never vanish. No projection silently includes a
  record another drops.
* **INV2 — combined multi-dimension poison fails every projection CLOSED on the
  SAME composed pass** (proven RED: flip one projection fail-open -> the assertion
  fails; mirror GOV-318 INV2 RED probes).
* **INV3 — cross-lane no-leak under composition:** zero reviewer-internal ids /
  envelope keys / raw locators reach the public/published lane; the published lane
  stays byte-identical to its projection-free shape across the whole composition
  (proven RED: a lane-blind serialize leaks publicly).
* **INV4 — transport guard holds over the whole composed body** even with poisoned
  upstream rows (``assert_no_raw_paths`` over every projection + the combined blob).
* **INV5 — determinism:** two composed passes over the same fixture are
  byte-identical, projection by projection.

Test-only / read-only: imports EXISTING projection functions only, adds NO
production projection, envelope key, schema, migration, AI, or network. Pure sqlite
+ tmp files, Alpine-only, reviewer-internal. ``scripts/read_api.py`` and
``scripts/publication.py`` stay byte-0-diff (an explicit git-diff assertion pins
it). If a composed assertion surfaces a real fail-open / leak in production, that is
a SEPARATE CTO-routed defect — this ticket ships the net, it does not patch what the
net catches.
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
import publication as pub  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402
import speakers as sp  # noqa: E402
import statements as st  # noqa: E402
import transcript_class as tc  # noqa: E402

# The four Stage-3 projections under composition (imported, never forked).
import stage3_card_feed as feed  # noqa: E402
import stage3_preservation_audit as presv  # noqa: E402
import stage3_source_inventory as inv  # noqa: E402
import stage3_verify_at_source as vas  # noqa: E402
import stage3_verify_at_source_audit as vaudit  # noqa: E402

REVIEWER = "reviewer:isaac"

# A candidate-identity name planted on the display_label of a NON-attributed
# speaker row. The GOV-290 read-time re-guard must derive the label from
# speaker_class alone, so this string must surface NOWHERE in any composed body.
POISON_NAME = "Confidential Witness Q"

# A reviewer-internal vault path planted as a source raw locator + a gap detail.
# The web-safe layers strip it and the transport sweep is the LOUD backstop.
VAULT_PATH = "/Users/IA/Documents/Obsidian Vault/Source-Data/TownOfAlpine/secret.md"

# The conservative confidence floor an off-map transcript class collapses to.
_FLOOR_CONFIDENCE = tc.CONFIDENCE_LABEL_BY_CLASS[tc.DEFAULT_TRANSCRIPT_CLASS]


# ---------------------------------------------------------------------------
# Deterministic Alpine fixture corpus (spans every projection dimension).
# ---------------------------------------------------------------------------


def _ev(to_source_id: str, **extra: object) -> dict[str, object]:
    """A valid evidence pointer; ``to_source_id`` must resolve at insert (write gate)."""
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


def _add_attribution(
    conn: sqlite3.Connection,
    *,
    attribution_id: str,
    statement_id: str,
    attribution_state: str,
    speaker_class: str,
    display_label: str | None,
) -> None:
    """INSERT one speaker_attributions row DIRECTLY (controls display_label exactly).

    Writing the row directly — not via the safe ``speakers.attribute_speaker`` path —
    lets the corpus plant an adversarial name on a NON-attributed row, which the write
    path would never produce. That is the point of the INV2 fail-closed proof: the read
    surface must re-guard regardless of how the value landed.
    """
    conn.execute(
        "INSERT INTO speaker_attributions (speaker_attribution_id, statement_id, "
        "attribution_state, speaker_class, display_label) VALUES (?, ?, ?, ?, ?)",
        (attribution_id, statement_id, attribution_state, speaker_class, display_label),
    )
    conn.commit()


def _serve(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    segment_id: str | None,
    produced_by: str = "human",
    ai_run_id: str | None = None,
    evidence: list[dict[str, object]] | None = None,
    speaker_attribution_id: str | None = None,
) -> None:
    """Insert + promote one statement into the reviewer-internal serve (GOV-146)."""
    record: dict[str, object] = {
        "statement_id": statement_id,
        "segment_id": segment_id,
        "agenda_item_id": "alpine:2026-05-08:item-7",
        "statement_text": f"The council adopted the fireworks ban ({statement_id}).",
        "produced_by": produced_by,
    }
    if speaker_attribution_id is not None:
        record["speaker_attribution_id"] = speaker_attribution_id
    if produced_by == "ai":
        record["layer"] = "ai_thought_then"
        record["ai_extraction_run_id"] = ai_run_id
    if evidence is None:
        st.insert_statement(conn, record)
    else:
        st.insert_statement(conn, record, evidence)
    gate.promote_statement(
        conn, statement_id, reviewer_id=REVIEWER, decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


def _seed_anchors(conn: sqlite3.Connection, repo_root: Path) -> None:
    """Sources + meeting + agenda item + transcripts/segments + the on-disk raw doc.

    * ``alpine_packet`` — a real official source: backs the evidence links, is
      preserved-by-children (a document whose raw bytes re-hash on disk), and carries
      an archive ref. It also carries a reviewer-internal ``raw_local_path`` vault
      marker that must NEVER reach any projected body.
    * ``alpine_seed`` — a registered seed-only source with NO artifacts: the honest
      ``0/0/0`` inventory ``seeded`` gap (shown, never hidden) and a preservation
      ``defect`` (no preserved raw) — proves a projection never optimistically pads.
    * ``seg-grounded`` -> transcript 1 (sha256 = text hash -> re-hashes; grounded AND
      raw-preserved); ``seg-poison`` -> transcript 3 (``no_transcript`` class -> off the
      confidence map -> FLOOR; BLANK sha256 -> raw leg fails + a preservation defect).
    """
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
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "source_authority_level, jurisdiction, scan_date) VALUES "
        "('alpine_seed', 'Seed-Only Registry Row', 'alpine', 'website', 'county_relevant', "
        "'secondary', 'Lincoln County', '2026-05-02')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-05-08:item-7', 1, 7, 'Fireworks ban — adoption')"
    )
    # transcript 1 — hash-preserved official transcript (grounded + raw-linked).
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
    # transcript 3 — no_transcript class (off confidence map) AND blank sha256
    # (raw leg fails -> a preservation MISMATCH defect: text present, hash empty).
    conn.execute(
        "INSERT INTO transcripts (id, video_id, video_url, full_text, local_path, "
        "sha256, fetch_time_utc, transcript_class, source_id) VALUES (3, 'vid-3', "
        "'https://youtu.be/vid-3', 'Class-poisoned transcript text.', 'n/a', '', "
        "'2026-05-08T00:00:00Z', 'no_transcript', 'alpine_packet')"
    )
    conn.execute(
        "INSERT INTO transcript_segments (segment_id, transcript_id, segment_index, "
        "timestamp_seconds, timestamp_human, segment_text) VALUES "
        "('seg-poison', 3, 0, 0, '00:00', 'Class-poisoned segment.')"
    )
    # A document under alpine_packet whose raw bytes are on disk and re-hash: makes the
    # source preserved-by-children AND a preserved document preservation unit.
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
        note="GOV-393 Stage-3 composition integration safety net",
    )
    conn.commit()


def _seed_corpus(conn: sqlite3.Connection, repo_root: Path) -> None:
    """One deterministic multi-record Alpine corpus spanning every projection dimension."""
    _seed_anchors(conn, repo_root)
    ok_run = "gov393:ok-run"
    ai.create_run(conn, run_id=ok_run, input_source_ids=[])  # defaults error_status='ok'

    # (1) HEALTHY human: grounded + raw-preserved + attributed official speaker ->
    #     confidence source_anchored_timed, speaker "Jane Doe, Mayor", verifiable.
    _serve(conn, "s-healthy", segment_id="seg-grounded",
           evidence=[_ev("alpine_packet")], speaker_attribution_id="attr-healthy")
    _add_attribution(conn, attribution_id="attr-healthy", statement_id="s-healthy",
                     attribution_state="attributed", speaker_class="on-record-official",
                     display_label="Jane Doe, Mayor")

    # (2) HEALTHY AI: grounded + ok producing run + on-record-public speaker (NOT
    #     auto-nameable: the planted name collapses to the safe community label).
    _serve(conn, "s-ai", segment_id="seg-grounded", produced_by="ai", ai_run_id=ok_run,
           evidence=[_ev("alpine_packet")], speaker_attribution_id="attr-ai")
    _add_attribution(conn, attribution_id="attr-ai", statement_id="s-ai",
                     attribution_state="attributed", speaker_class="on-record-public",
                     display_label=POISON_NAME)

    # (3) COMBINED MULTI-DIMENSION POISON on one served row:
    #     - no_transcript class            -> confidence FLOOR
    #     - blank sha256 + no evidence link -> raw leg fails -> provenance UNVERIFIED,
    #       zero resolved links -> verify-at-source UNVERIFIED, transcript a defect
    #     - non-attributed candidate name  -> speaker collapses to SAFE_GENERIC_LABEL
    _serve(conn, "s-poison", segment_id="seg-poison", speaker_attribution_id="attr-poison")
    _add_attribution(conn, attribution_id="attr-poison", statement_id="s-poison",
                     attribution_state="uncertain", speaker_class="on-record-official",
                     display_label=f"{POISON_NAME}, Mayor")

    # (4) Completeness-gap dimension: clean + FS-path + PII detail rows. The poisoned
    #     details must be OMITTED while the gap ROWS stay countable across projections.
    comp.record_gap(
        conn, subject_node_id="2026-04-10", subject_node_type="meeting",
        gap_type="no_primary_source",
        detail="meeting folder 2026-04-10 has only derived (.md) material", commit=True,
    )
    comp.record_gap(  # record_gap's guard is PII-only, not path-aware: read-time must catch.
        conn, subject_node_id="2026-04-11", subject_node_type="meeting",
        gap_type="no_primary_source", detail=f"see {VAULT_PATH}", commit=True,
    )
    _plant_gap_pii(conn, gap_id="no_primary_source:meeting:m-pii", subject_node_id="m-pii",
                   detail="reported by resident jane.doe@example.com")
    conn.commit()


def _plant_gap_pii(conn: sqlite3.Connection, *, gap_id: str, subject_node_id: str, detail: str) -> None:
    """INSERT a PII-detail gap row directly (record_gap's write guard would block it)."""
    conn.execute(
        "INSERT INTO completeness_gaps (gap_id, subject_node_id, subject_node_type, "
        "gap_type, severity, detail, source_id, detected_run_id, detected_utc, "
        "resolved_status, produced_by) VALUES (?, ?, 'meeting', 'no_primary_source', "
        "'warn', ?, NULL, NULL, '2026-06-19T00:00:00.000+00:00', 'open', 'deterministic')",
        (gap_id, subject_node_id, detail),
    )
    conn.commit()


@pytest.fixture()
def env(tmp_path: Path):
    """The composed Alpine corpus: an open connection + the repo_root for preservation."""
    db_path = tmp_path / "Database" / "test.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_corpus(connection, tmp_path)
    yield connection, tmp_path
    connection.close()


# ---------------------------------------------------------------------------
# Composed-pass helper: the WHOLE Stage-3 surface in ONE pass.
# ---------------------------------------------------------------------------


def _compose(conn: sqlite3.Connection, repo_root: Path) -> dict[str, dict]:
    """Drive the composed body through all four Stage-3 projections + the public lane once."""
    return {
        "feed": feed.build_card_feed(conn),                                  # 3.05
        "verify": vas.build_verify_at_source(conn),                          # 3.07
        "inventory": inv.build_inventory(conn),                              # 3.03
        "preservation": presv.build_preservation_overlay(conn, repo_root),   # 3.04
        "public": read_api.build_response(conn),                             # public lane
    }


def _records(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["statement_id"]: r for r in read_api.reviewer_internal_records(conn)}


def _feed_card(body: dict, conn: sqlite3.Connection, statement_id: str) -> dict:
    """The feed card for a served statement, by its derived handle."""
    record = _records(conn)[statement_id]
    handle = feed.card_handle(feed._resolve_record_type(record), statement_id)
    return next(c for c in body["cards"] if c["handle"] == handle)


def _verify_card(body: dict, conn: sqlite3.Connection, statement_id: str) -> dict:
    record = _records(conn)[statement_id]
    handle = feed.card_handle(feed._resolve_record_type(record), statement_id)
    return next(c for c in body["cards"] if c["handle"] == handle)


def _inventory_by_id(body: dict) -> dict[str, dict]:
    return {s["source_id"]: s for s in body["sources"]}


def _preservation_unit_refs(body: dict) -> set[tuple[str, object]]:
    refs: set[tuple[str, object]] = set()
    for unit in body["units"]:
        ref = unit["unit_ref"]
        key = "source_id" if ref["object_type"] == "source" else "id"
        refs.add((ref["object_type"], ref[key]))
    return refs


# ===========================================================================
# INV1 — same-record-set agreement across the four projections.
# ===========================================================================


def test_inv1_feed_and_verify_are_a_handle_bijection(env) -> None:
    """3.05 <-> 3.07: the feed and the drill-down cover EXACTLY the same card set."""
    conn, repo_root = env
    bodies = _compose(conn, repo_root)
    feed_handles = {c["handle"] for c in bodies["feed"]["cards"]}
    verify_handles = {c["handle"] for c in bodies["verify"]["cards"]}

    # Independently-recomputed mandated set (feed.expected_handles is a separate
    # derivation, not the feed body) — so this is a genuine cross-check.
    expected = feed.expected_handles(conn)
    assert feed_handles == expected, feed_handles ^ expected
    assert verify_handles == expected, verify_handles ^ expected
    # The live back-gap guards both pass over the same corpus (no silent drop).
    assert feed.assert_feed_covers_surface(conn, bodies["feed"]) is True
    assert vas.assert_covers_surface(conn, bodies["verify"]) is True
    # 3 record cards (healthy/ai/poison) + 3 gap cards = 6 cards on every card lane.
    assert len(feed_handles) == 6


def test_inv1_every_record_source_is_counted_reviewable_and_preserved(env) -> None:
    """3.03 + 3.04: a source a surfaced record traces to is counted AND covered."""
    conn, repo_root = env
    bodies = _compose(conn, repo_root)
    inventory = _inventory_by_id(bodies["inventory"])
    preserved_refs = _preservation_unit_refs(bodies["preservation"])

    counted_sources: set[str] = set()
    for record in read_api.reviewer_internal_records(conn):
        traced = inv._record_source_ids(conn, record)
        assert traced, f"{record['statement_id']}: surfaced card traces to no source"
        for sid in traced:
            # 3.03 counts it as reviewable (not silently dropped / never seeded-only).
            assert sid in inventory, f"{sid} surfaced by a record but absent from inventory"
            assert inventory[sid]["coverage"]["state"] == inv.COVERAGE_REVIEWABLE
            assert inventory[sid]["coverage"]["reviewable_statements"] >= 1
            # 3.04 covers it as a preservation source unit.
            assert ("source", sid) in preserved_refs
            counted_sources.add(sid)

    # The reviewable-counted source set is exactly the sources records trace to —
    # the inventory neither invents nor drops a backing source.
    reviewable_in_inventory = {
        sid for sid, s in inventory.items()
        if s["coverage"]["state"] == inv.COVERAGE_REVIEWABLE
    }
    assert reviewable_in_inventory == counted_sources


def test_inv1_seed_only_source_shown_not_hidden_or_padded(env) -> None:
    """3.03 honesty: a registered seed-only source is emitted as 0/0/0 ``seeded``."""
    conn, repo_root = env
    inventory = _inventory_by_id(inv.build_inventory(conn))
    assert "alpine_seed" in inventory, "a registered source must never be dropped"
    coverage = inventory["alpine_seed"]["coverage"]
    assert coverage["state"] == inv.COVERAGE_SEEDED
    assert coverage["documents_total"] == 0
    assert coverage["transcripts_total"] == 0
    assert coverage["reviewable_statements"] == 0


def test_inv1_gap_rows_reconcile_across_feed_and_verify(env) -> None:
    """Completeness-gap rows reconcile (same count, never vanish) across 3.05 + 3.07."""
    conn, repo_root = env
    bodies = _compose(conn, repo_root)
    gap_count = len(read_api.completeness_gap_cards(conn))
    assert gap_count == 3

    feed_gaps = [c for c in bodies["feed"]["cards"] if c["type"] == feed.TYPE_SOURCE_MISSING]
    verify_gaps = [
        c for c in bodies["verify"]["cards"] if c["type"] == vas.feed.TYPE_SOURCE_MISSING
    ]
    assert len(feed_gaps) == gap_count
    assert len(verify_gaps) == gap_count
    # Same gap handles on both card lanes (reconcile, not re-derive).
    assert {c["handle"] for c in feed_gaps} == {c["handle"] for c in verify_gaps}
    # Every gap card is honestly source_missing on the feed and never verifiable on
    # the drill-down.
    assert all(c["status"] == feed.STATUS_SOURCE_MISSING for c in feed_gaps)
    assert all(
        c["verify_at_source_status"] == vas.VERIFY_AT_SOURCE_SOURCE_MISSING
        for c in verify_gaps
    )


def test_inv1_healthy_rows_carry_expected_composed_values(env) -> None:
    """Pin the healthy-row composed values so a silent cross-projection regression trips."""
    conn, repo_root = env
    bodies = _compose(conn, repo_root)

    healthy = _feed_card(bodies["feed"], conn, "s-healthy")
    assert healthy["status"] == feed.STATUS_VERIFIED
    assert healthy["confidence_label"] == "source_anchored_timed"
    assert healthy["speaker_label"] == "Jane Doe, Mayor"
    assert healthy["provenance_status"] == read_api.PROVENANCE_GROUNDED
    assert _verify_card(bodies["verify"], conn, "s-healthy")["verify_at_source_status"] == (
        vas.VERIFY_AT_SOURCE_VERIFIABLE
    )

    ai_card = _feed_card(bodies["feed"], conn, "s-ai")
    assert ai_card["status"] == feed.STATUS_AI_PRESENTED
    assert ai_card["speaker_label"] == sp.SAFE_COMMUNITY_LABEL  # name dropped
    assert ai_card["provenance_status"] == read_api.PROVENANCE_GROUNDED

    # The 3.07 read-time auditor is clean over the composed corpus' record cards.
    verdict = vaudit.run_audit(conn)
    assert verdict["checks"]["VS-2"]["ok"] is True, verdict  # no dangling verifiable claim
    assert verdict["checks"]["VS-5"]["ok"] is True, verdict  # frozen SSOT vocab


# ===========================================================================
# INV2 — combined poison fails every projection CLOSED on the SAME pass.
# ===========================================================================


def _composition_violations(conn: sqlite3.Connection, repo_root: Path) -> list[str]:
    """The composed-pass fail-closed checks as a list of violations (empty == green).

    Returns rather than asserts so the RED proofs can flip ONE projection fail-open and
    observe the SAME checks start failing (load-bearing, mirror GOV-318 INV2).
    """
    bodies = _compose(conn, repo_root)
    out: list[str] = []

    # 3.05 feed — the poison record collapses to a non-reassuring status, the planted
    # name is dropped to the safe label, and the off-map class falls to the floor.
    poison_feed = _feed_card(bodies["feed"], conn, "s-poison")
    if poison_feed["status"] != feed.STATUS_UNVERIFIED:
        out.append(f"feed poison status not unverified: {poison_feed['status']!r}")
    if poison_feed["speaker_label"] != sp.SAFE_GENERIC_LABEL:
        out.append(f"feed poison speaker not safe: {poison_feed['speaker_label']!r}")
    if poison_feed["confidence_label"] != _FLOOR_CONFIDENCE:
        out.append(f"feed poison confidence not floor: {poison_feed['confidence_label']!r}")
    if poison_feed["provenance_status"] != read_api.PROVENANCE_UNVERIFIED:
        out.append("feed poison provenance not unverified")

    # 3.07 verify-at-source — the poison card is not verify-at-source-claimable.
    poison_verify = _verify_card(bodies["verify"], conn, "s-poison")
    if poison_verify["verify_at_source_status"] != vas.VERIFY_AT_SOURCE_UNVERIFIED:
        out.append("verify poison status not unverified")

    # 3.04 preservation — the poison record's transcript (blank sha256) is a defect.
    poison_units = [
        u for u in bodies["preservation"]["units"]
        if u["unit_ref"]["object_type"] == "transcript" and u["unit_ref"]["id"] == 3
    ]
    if not poison_units:
        out.append("preservation dropped the poison transcript unit")
    elif poison_units[0]["hash_ok"] is not False:
        out.append("preservation poison transcript not flagged hash defect")
    elif poison_units[0]["preservation_state"] != presv.DEFECT:
        out.append("preservation poison transcript not a defect")

    # 3.03 inventory — the seed-only source is not optimistically counted reviewable.
    seed = _inventory_by_id(bodies["inventory"]).get("alpine_seed")
    if seed is None:
        out.append("inventory dropped the seed-only source")
    elif seed["coverage"]["state"] != inv.COVERAGE_SEEDED:
        out.append(f"inventory seed source not seeded: {seed['coverage']['state']!r}")

    # Cross-lane transport — the candidate name / vault path appear NOWHERE composed.
    blob = json.dumps(bodies)
    if POISON_NAME in blob:
        out.append("poison name leaked into a composed body")
    for marker in ("/Users/", "Obsidian Vault", "secret.md", "jane.doe@example.com"):
        if marker in blob:
            out.append(f"transport leak in composed body: {marker!r}")
    return out


def test_inv2_combined_poison_fails_every_projection_closed(env) -> None:
    conn, repo_root = env
    assert _composition_violations(conn, repo_root) == []


def test_inv2_red_provenance_fail_open_breaks_the_net(env, monkeypatch) -> None:
    """RED: force the provenance overlay grounded -> the feed poison card reads verified."""
    conn, repo_root = env
    monkeypatch.setattr(
        read_api, "_provenance_status_for", lambda conn_, record: read_api.PROVENANCE_GROUNDED
    )
    violations = _composition_violations(conn, repo_root)
    assert any("provenance" in v or "status not unverified" in v for v in violations), violations


def test_inv2_red_speaker_fail_open_leaks_candidate_name(env, monkeypatch) -> None:
    """RED: force the speaker overlay to trust the raw display_label -> name leaks."""
    def _fail_open(conn_, record):
        row = conn_.execute(
            "SELECT display_label FROM speaker_attributions WHERE speaker_attribution_id = ?",
            (record.get("speaker_attribution_id"),),
        ).fetchone()
        return row["display_label"] if row and row["display_label"] else sp.SAFE_GENERIC_LABEL

    conn, repo_root = env
    monkeypatch.setattr(read_api, "_speaker_label_for", _fail_open)
    violations = _composition_violations(conn, repo_root)
    assert any("speaker" in v or "poison name leaked" in v for v in violations), violations


def test_inv2_red_preservation_fail_open_hides_transcript_defect(env, monkeypatch) -> None:
    """RED: force the transcript reconcile to report all-OK -> the defect vanishes."""
    conn, repo_root = env
    real = rp.reconcile_transcript_text

    def _fail_open(conn_, repo_root=rp.REPO_ROOT):
        result = real(conn_, repo_root=repo_root)
        # Drop every mismatch/missing_text: the engine now claims a clean corpus.
        return {"checked": result["checked"], "ok": result["checked"],
                "mismatch": [], "missing_text": []}

    monkeypatch.setattr(rp, "reconcile_transcript_text", _fail_open)
    violations = _composition_violations(conn, repo_root)
    assert any("preservation" in v for v in violations), violations


def test_inv2_red_inventory_fail_open_overstates_coverage(env, monkeypatch) -> None:
    """RED: force every source to count a reviewable statement -> the seed gap is padded."""
    conn, repo_root = env
    monkeypatch.setattr(
        inv, "_reviewable_counts", lambda conn_: {"alpine_packet": 9, "alpine_seed": 9}
    )
    violations = _composition_violations(conn, repo_root)
    assert any("seed source not seeded" in v for v in violations), violations


# ===========================================================================
# INV3 — cross-lane no-leak: nothing reviewer-internal reaches the public lane.
# ===========================================================================

# The reviewer-internal-ONLY envelope keys the four projections attach. None may
# appear on a public/published record or in the public build_response body.
# (``confidence_label`` / ``speaker_label`` are deliberately ABSENT: GOV-283/GOV-290
# are Stage-1/2.07 overlays that DO ride the public lane — only the Stage-2.12
# ``provenance_status`` and the Stage-3 keys are reviewer-internal.)
_REVIEWER_INTERNAL_KEYS = (
    "provenance_status", "verify_at_source_status", "resolvability_status",
    "coverage", "preservation_state", "manifest_digest", "reviewed_summary",
)


def test_inv3_public_lane_carries_no_reviewer_internal_ids_or_keys(env) -> None:
    conn, repo_root = env
    reviewer_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    assert reviewer_ids, "fixture must serve a non-empty reviewer-internal set"

    # The pre-publish corpus is entirely reviewer-internal: the public lane is empty.
    assert read_api.published_records(conn) == []

    # Publish one owner-approved row so the public lane is genuinely NON-empty — the
    # gate then has something to gate, and the no-leak check is load-bearing (a public
    # row carries the public overlays but never a reviewer-internal key).
    st.insert_statement(
        conn,
        {
            "statement_id": "s-public", "segment_id": "seg-grounded",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Published civic fact.",
            "verification_status": "human_verified", "produced_by": "human",
            "publication_state": "publishable",
        },
    )
    published = read_api.published_records(conn)
    assert published, "expected a non-empty public lane after publishing a row"
    assert {r["statement_id"] for r in published}.isdisjoint(reviewer_ids)

    # The public build_response body carries NO reviewer-internal envelope key, no
    # reviewer-internal id, and none of the composed poison.
    public_blob = json.dumps(read_api.build_response(conn))
    for key in _REVIEWER_INTERNAL_KEYS:
        assert key not in public_blob, f"public lane leaked reviewer-internal key {key!r}"
    for sid in reviewer_ids:
        assert sid not in public_blob, f"public lane leaked reviewer-internal id {sid!r}"
    assert POISON_NAME not in public_blob


def test_inv3_published_row_byte_identical_to_projection_free_shape(env) -> None:
    """An owner-published row gets the byte-identical pre-Stage-3 shape (no overlay key)."""
    conn, repo_root = env
    st.insert_statement(
        conn,
        {
            "statement_id": "s-published",
            "segment_id": "seg-grounded",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Published civic fact.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
    )
    record = {r["statement_id"]: r for r in read_api.published_records(conn)}["s-published"]
    for key in _REVIEWER_INTERNAL_KEYS:
        assert key not in record, f"published row carries reviewer-internal key {key!r}"
    # ...and is not duplicated into the reviewer-internal serve.
    assert "s-published" not in {
        r["statement_id"] for r in read_api.reviewer_internal_records(conn)
    }


def test_inv3_red_lane_blind_serialize_leaks_provenance_publicly(env, monkeypatch) -> None:
    """RED: a lane-blind serialize (always attaching provenance) leaks publicly."""
    conn, repo_root = env
    st.insert_statement(
        conn,
        {
            "statement_id": "s-published",
            "segment_id": "seg-grounded",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Published civic fact.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
    )
    real_serialize = read_api._serialize_statement

    def _lane_blind(conn_, record, ui_status, *, include_provenance_status=False):
        return real_serialize(conn_, record, ui_status, include_provenance_status=True)

    monkeypatch.setattr(read_api, "_serialize_statement", _lane_blind)
    record = {r["statement_id"]: r for r in read_api.published_records(conn)}["s-published"]
    assert "provenance_status" in record  # the leak the real lane gate prevents


# ===========================================================================
# INV4 — transport guard holds over the whole composed body (poisoned upstream).
# ===========================================================================


def test_inv4_transport_sweep_holds_over_whole_composed_body(env) -> None:
    conn, repo_root = env
    bodies = _compose(conn, repo_root)
    # Each projection runs assert_no_raw_paths internally; re-sweep each here to pin
    # the transport contract verbatim over the composed surface.
    for name in ("feed", "verify", "inventory", "preservation"):
        assert read_api.assert_no_raw_paths(bodies[name]) is bodies[name]
    # No vault path / candidate name / PII / raw locator survives anywhere composed.
    blob = json.dumps(bodies)
    for marker in (
        "/Users/", "/Volumes/", "Obsidian Vault", "Source-Data", "secret.md",
        "jane.doe@example.com", POISON_NAME, ".sha256",
    ):
        assert marker not in blob, f"transport leak in composed body: {marker!r}"


def test_inv4_raw_path_in_composed_body_is_caught_loudly(env) -> None:
    """Sanity: the shared transport guard is wired across the composition, not a no-op."""
    with pytest.raises(read_api.RawPathLeak):
        read_api.assert_no_raw_paths(
            {"cards": [{"locator": {"note": VAULT_PATH}}]}
        )


def test_inv4_no_per_unit_raw_hash_leaks_under_composition(env) -> None:
    """The preservation overlay carries verdicts only — never a 64-hex raw sha256."""
    conn, repo_root = env
    overlay = presv.build_preservation_overlay(conn, repo_root)
    for unit in overlay["units"]:
        assert "raw_sha256" not in unit and "sha256" not in unit
        for value in _iter_values(unit):
            if isinstance(value, str):
                assert not _is_hex64(value), f"raw hash leaked into unit: {value!r}"


def _iter_values(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _iter_values(v)
    else:
        yield obj


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


# ===========================================================================
# INV5 — determinism: two composed passes are byte-identical, projection by projection.
# ===========================================================================


def test_inv5_two_composed_passes_are_byte_identical(env) -> None:
    conn, repo_root = env
    first = _compose(conn, repo_root)
    second = _compose(conn, repo_root)
    for name in ("feed", "verify", "inventory", "preservation", "public"):
        assert json.dumps(first[name], sort_keys=True) == json.dumps(
            second[name], sort_keys=True
        ), f"{name} projection is non-deterministic across composed passes"


# ===========================================================================
# 0-diff guard — the safety net is additive (read_api.py / publication.py unchanged).
# ===========================================================================


def test_read_api_and_publication_zero_diff_vs_main() -> None:
    """git-diff evidence: the SSOT modules are byte-for-byte unchanged vs origin/main."""
    diff = subprocess.run(
        ["git", "diff", "origin/main", "--", "scripts/read_api.py", "scripts/publication.py"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert diff.returncode == 0, diff.stderr
    assert diff.stdout == "", f"read_api.py/publication.py drifted from origin/main:\n{diff.stdout}"
