"""GOV-364 Stage 3.03 — reviewer-internal source/data inventory over ``read_api``.

Proves the GOV-362 contract (``Docs/stage3-03-source-inventory-contract.md``)
against :mod:`stage3_source_inventory`. Each test maps to a contract §5 RED item:

- **T-1** — no-leak / allowlist subset: every flat field of every entry ∈
  ``SOURCE_INVENTORY_FIELDS`` ⊆ ``WEB_SAFE_FIELD_ALLOWLIST``; no reviewer-internal
  column (raw paths / sha256 / notes / owner_agent) reaches the body (INV-2/3).
- **T-2** — transport sweep: a planted ``raw_local_path`` is absent + ``build_inventory``
  does not raise (col never SELECTed); a vault marker on an allowlisted free field
  makes ``assert_no_raw_paths`` fire LOUDLY (INV-5 backstop).
- **T-3** — coverage correctness: reviewable / ingested / seeded states + exact counts (§2).
- **T-4** — lane gating: a seed-only source is carried (reviewer-internal shows the
  gap) AND no public / ``published_records`` path emits an inventory key (INV-1).
- **T-5** — determinism / never-hidden: order is ``(source_class, source_id)``, a
  second build is byte-identical, a seed-only source is never dropped (INV-7).
- **T-6** — 0-diff guard: the module imports (does not monkeypatch) ``publication`` /
  ``read_api`` (defensive; the PR diff is the real 0-diff proof, INV-6).

Pure sqlite + tmp files: no network, no AI, no real-corpus dependency.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import stage3_source_inventory as inv  # noqa: E402
import statements as st  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _add_source(conn: sqlite3.Connection, source_id: str, **cols: object) -> None:
    """Register one source row. Defaults give a minimal valid (alpine-scoped) seed."""
    row = {
        "source_id": source_id,
        "name": f"Source {source_id}",
        "scope": "alpine",
        "source_class": "municipal_primary",
        "source_authority_level": "primary",
        "jurisdiction": "Alpine",
        "source_type": "website",
        **cols,
    }
    keys = list(row)
    conn.execute(
        f"INSERT INTO sources ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
        [row[k] for k in keys],
    )


def _add_document(conn: sqlite3.Connection, source_id: str, n: int) -> None:
    """A crawled document resolving to ``source_id`` (its raw local_path is NEVER projected)."""
    conn.execute(
        "INSERT INTO documents (source_url, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            f"https://alpinewy.gov/{source_id}-doc-{n}.pdf",
            "/Users/IA/Obsidian Vault/Source-Data/raw.pdf",  # raw — must never appear
            "ab" * 32,
            "2026-06-01T00:00:00Z",
            source_id,
        ),
    )


def _promote(conn: sqlite3.Connection, statement_id: str, *, to_source_id: str) -> None:
    """Insert a statement + evidence link tracing to ``to_source_id``, then promote it.

    Mirrors the live reviewer-internal gate (GOV-146): reviewed + a promoting Lane-5
    decision + a resolvable evidence pointer + not-publishable. The evidence link
    carries a raw ``transcript_path`` that MUST be stripped upstream.
    """
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "agenda_item_id": None,
            "statement_text": f"Reviewed civic claim {statement_id}.",
            "verification_status": "machine_extracted_unreviewed",
            "produced_by": "human",
        },
        [
            {
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
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=1",
            }
        ],
    )
    gate.promote_statement(
        conn,
        statement_id,
        reviewer_id="reviewer:isaac",
        decision="approved",
        reason="reviewer-internal source-grounded civic claim (GOV-146)",
        to_verification_status="reviewed_source_linked",
    )


def _seed(conn: sqlite3.Connection) -> None:
    """Three sources spanning the three coverage states + a fully-poisoned row.

    * ``srcA`` — 2 documents + 1 reviewable statement -> ``reviewable`` (2/0/1).
      Also fully populated with raw_local_path / raw_sha256 / notes / owner_agent /
      local_note_path so T-1 proves none of those reviewer-internal cols leak.
    * ``srcB`` — 1 document, no reviewable statement -> ``ingested`` (1/0/0).
    * ``srcC`` — seed-only, no artifacts -> ``seeded`` (0/0/0); never hidden.
    """
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-364 inventory seed",
    )
    _add_source(
        conn, "srcA", name="Town of Alpine official website",
        source_class="municipal_primary", scan_date="2026-06-08",
        last_validated_utc="2026-06-14T03:25:00.000+00:00", archive_status="available",
        url="https://www.alpinewy.gov/", original_url="https://www.alpinewy.gov/",
        # reviewer-internal columns that MUST NEVER be projected (INV-3):
        raw_local_path="/Users/IA/Obsidian Vault/Source-Data/alpine.html",
        raw_sha256="deadbeef" * 8, local_note_path="Docs/Source-Data/registry/srcA.md",
        owner_agent="SourceArchivist", notes="internal reviewer note — do not publish",
        robots_policy="respect", raw_preservation_status="preserved",
    )
    _add_source(
        conn, "srcB", name="Lincoln County WY — Alpine-relevant",
        source_class="county_relevant", source_authority_level="secondary",
        jurisdiction="Lincoln County (Alpine-relevant)", scan_date="2026-06-09",
        url="https://www.lincolncountywy.gov/",
    )
    _add_source(
        conn, "srcC", name="Town of Alpine YouTube channel",
        source_class="meeting_video", source_type="video_channel",
        scan_date="2026-06-10",
    )
    conn.commit()
    _add_document(conn, "srcA", 1)
    _add_document(conn, "srcA", 2)
    _add_document(conn, "srcB", 1)
    conn.commit()
    _promote(conn, "stmt-A1", to_source_id="srcA")


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed(connection)
    yield connection
    connection.close()


def _by_id(entries: list[dict]) -> dict[str, dict]:
    return {e["source_id"]: e for e in entries}


# ---------------------------------------------------------------------------
# T-1 — no-leak / allowlist subset (INV-2/3)
# ---------------------------------------------------------------------------


def test_inventory_field_set_is_allowlist_subset() -> None:
    # The frozen field set is a strict subset of the publication SSOT allowlist.
    assert inv.SOURCE_INVENTORY_FIELDS <= pub.WEB_SAFE_FIELD_ALLOWLIST


def test_t1_every_flat_field_is_allowlisted_no_internal_column(conn: sqlite3.Connection) -> None:
    entries = inv.source_inventory(conn)
    assert entries  # not empty
    for entry in entries:
        flat_keys = set(entry) - {"coverage"}  # coverage is the one nested envelope
        assert flat_keys <= inv.SOURCE_INVENTORY_FIELDS
        assert flat_keys <= pub.WEB_SAFE_FIELD_ALLOWLIST
        # No reviewer-internal / WEB_UNSAFE column ever appears as a key.
        assert not (set(entry) & pub.WEB_UNSAFE_FIELDS)


def test_t1_no_raw_value_reaches_body(conn: sqlite3.Connection) -> None:
    # srcA carries raw_local_path / raw_sha256 / notes / owner_agent / local_note_path;
    # none of those VALUES may surface anywhere in the inventory body.
    blob = json.dumps(inv.build_inventory(conn))
    for marker in (
        "/Users/", "Source-Data", ".sha256", "deadbeef",
        "do not publish", "SourceArchivist", "Docs/Source-Data",
    ):
        assert marker not in blob, f"leak: {marker!r} reached the inventory"


# ---------------------------------------------------------------------------
# T-2 — transport sweep backstop (INV-5)
# ---------------------------------------------------------------------------


def test_t2_planted_raw_path_absent_and_build_does_not_raise(conn: sqlite3.Connection) -> None:
    # The raw_local_path planted on srcA is never SELECTed, so build_inventory is
    # clean and does not raise — the column simply can't reach the body.
    inventory = inv.build_inventory(conn)  # must not raise
    entry = _by_id(inventory["sources"])["srcA"]
    assert "raw_local_path" not in entry
    assert all("/Users/" not in str(v) for v in entry.values() if isinstance(v, str))


def test_t2_vault_marker_on_allowlisted_field_fires_backstop(tmp_path: Path) -> None:
    # Poison an ALLOWLISTED free field (name) with a vault path: the column-omission
    # guard cannot catch it (name is legitimately projected), so the transport sweep
    # MUST fire LOUDLY at the boundary (INV-5).
    db_path = tmp_path / "poison.db"
    db.apply_migrations(db_path)
    with db.open_db(db_path) as poison:
        _add_source(poison, "srcX", name="/Users/IA/Obsidian Vault/Source-Data/leak.html")
        poison.commit()
        with pytest.raises(read_api.RawPathLeak):
            inv.build_inventory(poison)


# ---------------------------------------------------------------------------
# T-3 — coverage correctness (§2)
# ---------------------------------------------------------------------------


def test_t3_coverage_states_and_counts(conn: sqlite3.Connection) -> None:
    entries = _by_id(inv.source_inventory(conn))

    cov_a = entries["srcA"]["coverage"]
    assert cov_a == {
        "state": "reviewable",
        "documents_total": 2,
        "transcripts_total": 0,
        "reviewable_statements": 1,
    }

    cov_b = entries["srcB"]["coverage"]
    assert cov_b["state"] == "ingested"
    assert cov_b["documents_total"] == 1
    assert cov_b["reviewable_statements"] == 0

    cov_c = entries["srcC"]["coverage"]
    assert cov_c == {
        "state": "seeded",
        "documents_total": 0,
        "transcripts_total": 0,
        "reviewable_statements": 0,
    }


def test_t3_coverage_state_in_frozen_ssot(conn: sqlite3.Connection) -> None:
    for entry in inv.source_inventory(conn):
        cov = entry["coverage"]
        assert cov["state"] in inv.SOURCE_COVERAGE_STATES
        assert set(cov) == inv.SOURCE_COVERAGE_KEYS


def test_t3_coverage_never_overstates_default_seeded() -> None:
    # A source with no artifacts and no reviewable statement collapses to the most
    # conservative state — never optimistically "ingested"/"reviewable".
    assert inv._coverage("x", {}, {}, {})["state"] == "seeded"
    assert inv._coverage("x", {"x": 1}, {}, {})["state"] == "ingested"
    assert inv._coverage("x", {"x": 1}, {}, {"x": 1})["state"] == "reviewable"


# ---------------------------------------------------------------------------
# T-4 — lane gating (INV-1)
# ---------------------------------------------------------------------------


def test_t4_inventory_is_reviewer_internal(conn: sqlite3.Connection) -> None:
    inventory = inv.build_inventory(conn)
    assert inventory["access"] == "reviewer_internal"
    assert inventory["scope"] == "alpine"


def test_t4_seed_only_source_carried_in_reviewer_internal(conn: sqlite3.Connection) -> None:
    # The seed-only source C (the gap) IS present in the reviewer-internal inventory.
    assert "srcC" in _by_id(inv.build_inventory(conn)["sources"])


def test_t4_public_lane_emits_no_inventory_key(conn: sqlite3.Connection) -> None:
    # The public lane (published_records / default build_response) never carries a
    # sources/inventory key — surfacing seed/registry rows publicly is impossible by
    # construction (the inventory lives in a separate reviewer-internal module).
    public = read_api.build_response(conn)  # no include_* opt-ins => public default
    assert "sources" not in public
    assert read_api.published_records(conn) == []  # nothing owner-published in fixture


# ---------------------------------------------------------------------------
# T-5 — determinism / never-hidden (INV-7)
# ---------------------------------------------------------------------------


def test_t5_deterministic_order_source_class_then_source_id(conn: sqlite3.Connection) -> None:
    entries = inv.source_inventory(conn)
    order_keys = [(e.get("source_class"), e["source_id"]) for e in entries]
    assert order_keys == sorted(order_keys)


def test_t5_second_build_is_byte_identical(conn: sqlite3.Connection) -> None:
    first = json.dumps(inv.build_inventory(conn), sort_keys=True)
    second = json.dumps(inv.build_inventory(conn), sort_keys=True)
    assert first == second


def test_t5_seed_only_source_never_dropped(conn: sqlite3.Connection) -> None:
    # All three registered sources are emitted — a 0/0/0 seed source is never hidden.
    ids = set(_by_id(inv.source_inventory(conn)))
    assert {"srcA", "srcB", "srcC"} <= ids


# ---------------------------------------------------------------------------
# T-6 — 0-diff guard (INV-6, defensive)
# ---------------------------------------------------------------------------


def test_t6_module_consumes_read_api_and_publication_read_only() -> None:
    # The projection imports (consumes) the read surface; it does not fork it. The
    # real 0-diff proof is the PR diff — this is a defensive guard that the module
    # binds the real modules (not a monkeypatched stand-in).
    assert inv.read_api is read_api
    assert inv.pub is pub
    # And it exposes the contract's public API (§5).
    assert callable(inv.source_inventory)
    assert callable(inv.build_inventory)
    assert callable(inv._main)
