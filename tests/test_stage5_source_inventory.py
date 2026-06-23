"""GOV-484 Stage 5.03 — source/data inventory contract over the registry.

Proves the GOV-484 contract (``Docs/stage5-03-source-inventory-contract.md``)
against :mod:`stage5_source_inventory`. Each test maps to a contract §5 RED item:

- **R-1** lifecycle precedence: all four states derive; most-degraded-state-wins
  (disappeared beats a change flag; replaced beats a change flag).
- **R-2** fail-closed: a poisoned free-text status is never echoed in ``evidence.*``
  and never produces an out-of-vocab state.
- **R-3** archive availability: ``available_near_scan`` / ``not_available`` /
  ``not_checked`` derive correctly, key on the immutable ``scan_date``, and
  ``nearestSnapshotRef`` is a public URL or ``None`` — never a raw path.
- **R-4** no-leak (I1+I2): a fully-poisoned source row leaks no raw value; no raw
  locator column reaches the body; a vault marker in an emitted field fires loudly.
- **R-5** single envelope digest (I3): exactly one 64-hex string (the
  ``inventoryDigest``); no per-source hash.
- **R-6** determinism / never-hidden: order ``(source_class, source_id)``, a second
  build is byte-identical, a disappeared / seed-only source is never dropped.
- **R-7** 0-diff guard (I4+I7): the module imports (never monkeypatches)
  ``read_api`` / ``publication`` / ``stage3_source_inventory``; access is
  reviewer-internal, never public.

Plus a load-bearing **RED-proof** (I5): neutering ``derive_lifecycle_state`` to
always return ``unchanged`` makes the disappeared-source expectation flip — proving
the derivation is non-tautological.

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

import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import stage3_source_inventory as base  # noqa: E402
import stage5_source_inventory as inv  # noqa: E402


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


# A genuine Wayback web URL (the only kind that may become a nearestSnapshotRef).
_WAYBACK = "https://web.archive.org/web/20260601000000/https://www.alpinewy.gov/"


def _seed(conn: sqlite3.Connection) -> None:
    """Sources spanning every lifecycle state + precedence + a fully-poisoned row.

    * ``srcA-unchanged`` — stable + archived → ``unchanged`` / ``available_near_scan``.
    * ``srcB-changed``   — ``source_changed=1`` → ``changed``.
    * ``srcC-disappeared`` — ``source_missing`` + ``unavailable`` → ``disappeared``.
    * ``srcD-replaced``  — ``correction_status='replaced'`` → ``replaced``.
    * ``srcE-changed-but-gone`` — changed flag AND ``unavailable`` → ``disappeared``
      (most-degraded-state-wins precedence).
    * ``srcF-changed-but-replaced`` — changed flag AND ``superseded`` → ``replaced``.
    * ``srcZ-poison`` — fully populated reviewer-internal raw cols + a poisoned
      free-text ``verification_status`` (a vault path) + a ``file://`` archive url;
      proves no raw value leaks, the poison status is not echoed, and the lifecycle
      stays ``unchanged`` (poison status is not in any trigger vocab).
    """
    _add_source(
        conn, "srcA-unchanged", name="Town of Alpine official website",
        scan_date="2026-06-08", archive_status="available", archive_url=_WAYBACK,
        url="https://www.alpinewy.gov/", original_url="https://www.alpinewy.gov/",
        source_changed=0, verification_status="reviewed_source_linked",
        correction_status="none",
    )
    _add_source(
        conn, "srcB-changed", name="Alpine zoning page",
        scan_date="2026-06-09", archive_status="available", archive_url=_WAYBACK,
        source_changed=1, verification_status="reviewed_source_linked",
    )
    _add_source(
        conn, "srcC-disappeared", name="Removed Alpine notice",
        scan_date="2026-06-10", archive_status="unavailable",
        verification_status="source_missing",
    )
    _add_source(
        conn, "srcD-replaced", name="Superseded Alpine ordinance draft",
        scan_date="2026-06-11", archive_status="available", archive_url=_WAYBACK,
        correction_status="replaced",
    )
    _add_source(
        conn, "srcE-changed-but-gone", name="Alpine budget page (now gone)",
        scan_date="2026-06-12", archive_status="unavailable",
        source_changed=1,
    )
    _add_source(
        conn, "srcF-changed-but-replaced", name="Alpine resolution (superseded)",
        scan_date="2026-06-13", archive_status="available", archive_url=_WAYBACK,
        source_changed=1, correction_status="superseded",
    )
    _add_source(
        conn, "srcZ-poison", name="Alpine poison probe",
        scan_date="2026-06-14",
        # A poisoned free-text status carrying a vault path — must NOT be echoed and
        # must NOT leak; lifecycle must stay unchanged (status not in any trigger set).
        verification_status="/Users/IA/Obsidian Vault/Source-Data/leak.html",
        correction_status="../../etc/passwd",
        # file:// archive url -> dropped upstream; nearestSnapshotRef must be None.
        archive_url="file:///Users/IA/Obsidian%20Vault/Source-Data/leak.html",
        archive_status="available",
        # reviewer-internal raw columns that MUST NEVER be projected (I1/I2):
        raw_local_path="/Users/IA/Obsidian Vault/Source-Data/poison.html",
        raw_sha256="deadbeef" * 8, local_note_path="Docs/Source-Data/registry/poison.md",
        owner_agent="SourceArchivist", notes="internal reviewer note — do not publish",
        robots_policy="respect", raw_preservation_status="preserved",
    )
    conn.commit()


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
# R-1 — lifecycle precedence (unit + integration)
# ---------------------------------------------------------------------------


def test_r1_derive_all_four_states_unit() -> None:
    assert inv.derive_lifecycle_state({})["state"] == inv.LIFECYCLE_UNCHANGED
    assert (
        inv.derive_lifecycle_state({"source_changed": 1})["state"]
        == inv.LIFECYCLE_CHANGED
    )
    assert (
        inv.derive_lifecycle_state(
            {"verification_status": "source_changed"}
        )["state"]
        == inv.LIFECYCLE_CHANGED
    )
    assert (
        inv.derive_lifecycle_state(
            {"verification_status": "source_missing"}
        )["state"]
        == inv.LIFECYCLE_DISAPPEARED
    )
    assert (
        inv.derive_lifecycle_state({"archive_status": "unavailable"})["state"]
        == inv.LIFECYCLE_DISAPPEARED
    )
    assert (
        inv.derive_lifecycle_state(
            {"correction_status": "replaced"}
        )["state"]
        == inv.LIFECYCLE_REPLACED
    )
    assert (
        inv.derive_lifecycle_state(
            {"correction_status": "superseded"}
        )["state"]
        == inv.LIFECYCLE_REPLACED
    )


def test_r1_precedence_most_degraded_wins_unit() -> None:
    # disappeared beats a change flag.
    env = inv.derive_lifecycle_state(
        {"source_changed": 1, "archive_status": "unavailable"}
    )
    assert env["state"] == inv.LIFECYCLE_DISAPPEARED
    assert env["evidence"]["sourceChangedFlag"] is True  # evidence still recorded
    assert env["evidence"]["disappearanceSignal"] == "unavailable"
    # replaced beats a change flag.
    env2 = inv.derive_lifecycle_state(
        {"source_changed": 1, "correction_status": "superseded"}
    )
    assert env2["state"] == inv.LIFECYCLE_REPLACED
    assert env2["evidence"]["replacementSignal"] == "superseded"


def test_r1_states_in_inventory(conn: sqlite3.Connection) -> None:
    entries = _by_id(inv.source_inventory(conn))
    assert entries["srcA-unchanged"]["lifecycle"]["state"] == inv.LIFECYCLE_UNCHANGED
    assert entries["srcB-changed"]["lifecycle"]["state"] == inv.LIFECYCLE_CHANGED
    assert (
        entries["srcC-disappeared"]["lifecycle"]["state"] == inv.LIFECYCLE_DISAPPEARED
    )
    assert entries["srcD-replaced"]["lifecycle"]["state"] == inv.LIFECYCLE_REPLACED
    assert (
        entries["srcE-changed-but-gone"]["lifecycle"]["state"]
        == inv.LIFECYCLE_DISAPPEARED
    )
    assert (
        entries["srcF-changed-but-replaced"]["lifecycle"]["state"]
        == inv.LIFECYCLE_REPLACED
    )


# ---------------------------------------------------------------------------
# R-2 — fail-closed: poisoned free-text status never echoed / never out-of-vocab
# ---------------------------------------------------------------------------


def test_r2_poison_status_not_echoed_state_unchanged(conn: sqlite3.Connection) -> None:
    entry = _by_id(inv.source_inventory(conn))["srcZ-poison"]
    lifecycle = entry["lifecycle"]
    assert lifecycle["state"] == inv.LIFECYCLE_UNCHANGED  # poison status not a trigger
    evidence = lifecycle["evidence"]
    # None of the poisoned free-text values are echoed into evidence.
    assert evidence["changeSignal"] is None
    assert evidence["disappearanceSignal"] is None
    assert evidence["replacementSignal"] is None
    assert evidence["sourceChangedFlag"] is False


def test_r2_every_state_in_frozen_vocab(conn: sqlite3.Connection) -> None:
    body = inv.build_inventory(conn)
    for entry in body["sources"]:
        assert entry["lifecycle"]["state"] in inv.SOURCE_LIFECYCLE_STATES
    assert inv.assert_lifecycle_states_valid(body) is True


# ---------------------------------------------------------------------------
# R-3 — archive availability keyed to the immutable scan date
# ---------------------------------------------------------------------------


def test_r3_archive_availability_unit() -> None:
    avail = inv.archive_availability("2026-06-08", "available", _WAYBACK)
    assert avail == {
        "scanDate": "2026-06-08",
        "archiveStatus": "available",
        "snapshotAvailability": inv.SNAPSHOT_AVAILABLE,
        "nearestSnapshotRef": _WAYBACK,
    }
    # unavailable -> not_available, no ref.
    gone = inv.archive_availability("2026-06-10", "unavailable", None)
    assert gone["snapshotAvailability"] == inv.SNAPSHOT_NOT_AVAILABLE
    assert gone["nearestSnapshotRef"] is None
    # not_checked default + unknown status clamps to not_checked.
    unk = inv.archive_availability("2026-06-11", "weird-value", _WAYBACK)
    assert unk["archiveStatus"] == inv.ARCHIVE_STATUS_NOT_CHECKED
    assert unk["snapshotAvailability"] == inv.SNAPSHOT_NOT_CHECKED


def test_r3_available_status_without_web_ref_is_not_available_near_scan() -> None:
    # archive_status 'available' but no public ref -> cannot claim available_near_scan.
    avail = inv.archive_availability("2026-06-08", "available", None)
    assert avail["snapshotAvailability"] == inv.SNAPSHOT_NOT_CHECKED
    assert avail["nearestSnapshotRef"] is None


def test_r3_archive_envelope_in_inventory_keys_on_scan_date(
    conn: sqlite3.Connection,
) -> None:
    entries = _by_id(inv.source_inventory(conn))
    a = entries["srcA-unchanged"]["archiveAvailability"]
    assert a["scanDate"] == "2026-06-08"  # the immutable original scan date
    assert a["snapshotAvailability"] == inv.SNAPSHOT_AVAILABLE
    assert a["nearestSnapshotRef"] == _WAYBACK
    # disappeared source: archive unavailable -> not_available.
    c = entries["srcC-disappeared"]["archiveAvailability"]
    assert c["snapshotAvailability"] == inv.SNAPSHOT_NOT_AVAILABLE
    # poison source: file:// archive url is never a ref.
    z = entries["srcZ-poison"]["archiveAvailability"]
    assert z["nearestSnapshotRef"] is None


# ---------------------------------------------------------------------------
# R-4 — no-leak (I1+I2)
# ---------------------------------------------------------------------------


def test_r4_no_raw_value_reaches_body(conn: sqlite3.Connection) -> None:
    blob = json.dumps(inv.build_inventory(conn))
    for marker in (
        "/Users/", "Obsidian Vault", "Source-Data", ".sha256", "deadbeef",
        "do not publish", "SourceArchivist", "etc/passwd", "leak.html", "file://",
    ):
        assert marker not in blob, f"leak: {marker!r} reached the inventory"


def test_r4_no_reviewer_internal_column_key_in_body(conn: sqlite3.Connection) -> None:
    for entry in inv.source_inventory(conn):
        # Only the two derived envelopes are non-allowlisted keys; every flat field
        # is from the Stage-3.03 allowlisted set.
        flat_keys = set(entry) - {"coverage", "lifecycle", "archiveAvailability"}
        assert flat_keys <= base.SOURCE_INVENTORY_FIELDS
        assert flat_keys <= pub.WEB_SAFE_FIELD_ALLOWLIST
        assert not (set(entry) & pub.WEB_UNSAFE_FIELDS)


def test_r4_transport_sweep_fires_loudly_on_emitted_vault_path(
    tmp_path: Path,
) -> None:
    # A vault marker planted into an EMITTED allowlisted field (name) must fire the
    # transport sweep at the boundary — the I1 backstop, proven load-bearing.
    db_path = tmp_path / "leak.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _add_source(
        connection, "srcLeak",
        name="/Users/IA/Obsidian Vault/Source-Data/raw.html",  # raw path in a served field
        scan_date="2026-06-08",
    )
    connection.commit()
    with pytest.raises(read_api.RawPathLeak):
        inv.build_inventory(connection)
    connection.close()


# ---------------------------------------------------------------------------
# R-5 — single envelope digest (I3)
# ---------------------------------------------------------------------------


def test_r5_single_envelope_digest_no_per_source_hash(conn: sqlite3.Connection) -> None:
    body = inv.build_inventory(conn)
    digest = body["inventoryDigest"]
    assert isinstance(digest, str) and len(digest) == 64
    assert inv.assert_single_envelope_digest(body) is True
    # The only 64-hex string in the whole body is the envelope digest.
    hex64 = [t for t in read_api._iter_strings(body) if inv._is_hex64(t)]
    assert hex64 == [digest]


def test_r5_guard_red_when_per_source_hash_injected(conn: sqlite3.Connection) -> None:
    body = inv.build_inventory(conn)
    # Inject a per-source content hash -> the single-digest guard must go RED.
    body["sources"][0]["rawContentSha256"] = "ab" * 32
    with pytest.raises(inv.SourceInventoryContractError):
        inv.assert_single_envelope_digest(body)


# ---------------------------------------------------------------------------
# R-6 — determinism / never-hidden
# ---------------------------------------------------------------------------


def test_r6_deterministic_byte_identical(conn: sqlite3.Connection) -> None:
    first = json.dumps(inv.build_inventory(conn), sort_keys=True)
    second = json.dumps(inv.build_inventory(conn), sort_keys=True)
    assert first == second


def test_r6_order_is_source_class_then_id(conn: sqlite3.Connection) -> None:
    entries = inv.source_inventory(conn)
    keys = [(e.get("source_class"), e["source_id"]) for e in entries]
    assert keys == sorted(keys)


def test_r6_disappeared_and_all_sources_present_never_hidden(
    conn: sqlite3.Connection,
) -> None:
    ids = {e["source_id"] for e in inv.source_inventory(conn)}
    assert ids == {
        "srcA-unchanged", "srcB-changed", "srcC-disappeared", "srcD-replaced",
        "srcE-changed-but-gone", "srcF-changed-but-replaced", "srcZ-poison",
    }


# ---------------------------------------------------------------------------
# R-7 — 0-diff guard (I4+I7) + reviewer-internal access state (I6)
# ---------------------------------------------------------------------------


def test_r7_reviewer_internal_access_never_public(conn: sqlite3.Connection) -> None:
    body = inv.build_inventory(conn)
    assert body["access"] == "reviewer_internal"
    assert body["scope"] == "alpine"
    assert body["access"] != "public"


def test_r7_imports_real_modules_not_monkeypatched() -> None:
    # The module consumes the real read_api / publication / stage3 projection — it
    # never shadows them with a fake (the PR diff is the real 0-diff proof).
    assert inv.read_api is read_api
    assert base.read_api is read_api
    assert inv.base is base


# ---------------------------------------------------------------------------
# I5 — load-bearing RED-proof: the lifecycle derivation is non-tautological.
# ---------------------------------------------------------------------------


def test_i5_red_proof_neutered_derivation_flips_disappeared(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the real derivation, the disappeared source reads 'disappeared'.
    real = _by_id(inv.source_inventory(conn))["srcC-disappeared"]
    assert real["lifecycle"]["state"] == inv.LIFECYCLE_DISAPPEARED

    # Neuter the core check to always return 'unchanged'. If the disappeared
    # expectation were tautological it would still pass; instead it flips — proving
    # the real derivation is load-bearing.
    def _neutered(_signals: dict) -> dict:
        return {"state": inv.LIFECYCLE_UNCHANGED, "evidence": {}}

    monkeypatch.setattr(inv, "derive_lifecycle_state", _neutered)
    neutered = _by_id(inv.source_inventory(conn))["srcC-disappeared"]
    assert neutered["lifecycle"]["state"] == inv.LIFECYCLE_UNCHANGED
    assert neutered["lifecycle"]["state"] != real["lifecycle"]["state"]
