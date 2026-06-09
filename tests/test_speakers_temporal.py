"""Tests for speaker-attribution safety + person/role + temporal layering.

GOV-83, Slice 2 D. Covers the Contract 1.07 §3/§4 acceptance criteria:
- migration 0008 (persons/roles/served_in_role/speaker_attributions/made_statement/
  outcomes/outcome_updates) is additive + idempotent;
- **safe-attribution default** — a low-confidence (or unconfirmed) `attributed`
  request fails closed to a safe, name-free label and binds no person edge (§3);
- **no private-identity fields** — a column-name scan proves no address/voter/
  personal-identifier column can exist; private fields stay off the web-safe
  allowlist (SecurityPrivacyAgent gate);
- **non-mutating outcome_updates** — linking a later outcome leaves the prior
  (known_then) row byte-for-byte unchanged (§4.2);
- **enum reuse** — the layer + verification enums are the same SSOT objects, not
  shadow copies.

No AI, no network: pure sqlite + the committed Alpine fixture.
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

import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import speakers as spk  # noqa: E402
import statements as stmt  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
SOURCE_ID = "alpine:video:2026-05-08-regular"

# Tables introduced by migration 0008 (the privacy-sensitive identity surface).
IDENTITY_TABLES = (
    "persons", "roles", "served_in_role", "speaker_attributions",
    "made_statement", "outcomes", "outcome_updates",
)

# Substrings that must NEVER appear in any identity-table column name. Private
# identity / address / voter-registry data is forbidden by SCHEMA ABSENCE
# (COMPANY.md non-negotiable; 1.07 §7; issue acceptance / SecurityPrivacyAgent).
FORBIDDEN_COLUMN_SUBSTRINGS = (
    "address", "street", "zip", "postal", "voter", "registration", "ssn",
    "social_security", "dob", "birth", "phone", "email", "home", "residence",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _check_clause(conn, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0]


def _migrated(tmp_path: Path) -> Path:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    return db_path


def _seed_source(conn, source_id: str = SOURCE_ID) -> str:
    conn.execute(
        "INSERT INTO sources (source_id, name, source_type, source_class, jurisdiction) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, "Alpine Council 2026-05-08 video", "meeting_video", "alpine-official", "alpine"),
    )
    conn.commit()
    return source_id


def _seed_segment(conn, *, source_id: str = SOURCE_ID) -> str:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, meeting_date, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], "2026-05-08", tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            "Transcripts/2026/alpine-sample-0001.json", "0" * 64, _now(), source_id,
        ),
    )
    tid = int(cur.lastrowid)
    seg.segment_transcript(conn, tid, source_id=source_id)
    return "alpine-sample-0001:seg-0000"


def _seed_statement(conn, statement_id: str = "alpine:2026-05-08:stmt-0001") -> str:
    seg_id = _seed_segment(conn)
    stmt.insert_statement(
        conn,
        {"statement_id": statement_id, "segment_id": seg_id,
         "statement_text": "Staff stated the WWTP gap was $X."},
    )
    return statement_id


def _seed_person(conn, person_id="person:alpine:doe", display_name="Jane Doe") -> str:
    conn.execute(
        "INSERT INTO persons (person_id, display_name, person_type, created_utc) "
        "VALUES (?, ?, 'official', ?)",
        (person_id, display_name, _now()),
    )
    conn.commit()
    return person_id


# --- migration 0008: additive + idempotent ---------------------------------

def test_migration_creates_identity_and_temporal_tables(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    for table in IDENTITY_TABLES:
        assert table in names, f"table {table} missing"


def test_migration_0008_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    db.apply_migrations(db_path)
    db.apply_migrations(db_path)  # second run must not raise
    with db.open_db(db_path) as conn:
        ledger = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        cols = [r[1] for r in conn.execute("PRAGMA table_info(speaker_attributions)")]
    assert "0008_speakers_persons_roles_temporal" in ledger
    assert cols.count("speaker_attribution_id") == 1


# --- ACCEPTANCE: no private-identity fields (SecurityPrivacyAgent gate) ------

def test_no_private_identity_columns(tmp_path: Path) -> None:
    """Schema-absence privacy guarantee: no identity table may carry a column
    whose name implies a private address / voter-registry / personal identifier."""
    with db.open_db(_migrated(tmp_path)) as conn:
        for table in IDENTITY_TABLES:
            for col in _columns(conn, table):
                low = col.lower()
                for bad in FORBIDDEN_COLUMN_SUBSTRINGS:
                    assert bad not in low, f"{table}.{col} looks like private data ({bad!r})"


def test_private_speaker_fields_not_web_safe() -> None:
    """Identity fields must never be on the fail-closed web-safe allowlist."""
    for private in ("display_name", "person_id", "candidate_person_id",
                    "basis", "minutes_source_id", "role_id"):
        assert private not in pub.WEB_SAFE_FIELD_ALLOWLIST, f"{private} leaked to web-safe"


def test_to_web_safe_drops_identity_fields() -> None:
    record = {
        "source_id": SOURCE_ID, "ui_status": "unverified",
        "display_name": "Jane Doe", "person_id": "person:alpine:doe",
        "candidate_person_id": "person:alpine:doe", "basis": "roll-call order",
    }
    safe = pub.to_web_safe(record)
    assert "display_name" not in safe and "person_id" not in safe
    assert "candidate_person_id" not in safe and "basis" not in safe
    assert safe["source_id"] == SOURCE_ID  # allowlisted survives


# --- enum reuse (no re-typed enums) ----------------------------------------

def test_layer_and_confidence_enums_are_reused_not_copied() -> None:
    assert spk.ALLOWED_LAYERS is stmt.ALLOWED_LAYERS
    assert spk.ALLOWED_CONFIDENCE is stmt.ALLOWED_CONFIDENCE


def test_outcome_verification_check_matches_python_enum(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        sql = _check_clause(conn, "outcomes")
    for value in pub.ALLOWED_VERIFICATION_STATUSES:
        assert f"'{value}'" in sql, f"outcomes CHECK missing {value!r}"


# --- ACCEPTANCE: safe-attribution default (1.07 §3) ------------------------

def test_low_confidence_attributed_defaults_to_safe_label(tmp_path: Path) -> None:
    """A named-official request with LOW confidence fails closed: state downgrades
    to `uncertain`, no person_id binds, the label is name-free, and there is NO
    made_statement edge. 'No name is better than wrong attribution.'"""
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        pid = _seed_person(conn, display_name="Jane Doe")
        result = spk.attribute_speaker(
            conn,
            {"speaker_attribution_id": "alpine:2026-05-08:seg-0000:spk",
             "statement_id": sid,
             "attribution_state": "attributed",      # requested a name...
             "speaker_class": "on-record-official",
             "person_id": pid,
             "person_confirmed": True,
             "role_title": "Council Member, Town of Alpine",
             "confidence": "low"},                    # ...but confidence is low
        )
        row = conn.execute(
            "SELECT attribution_state, person_id, candidate_person_id, display_label "
            "FROM speaker_attributions WHERE speaker_attribution_id = ?",
            ("alpine:2026-05-08:seg-0000:spk",),
        ).fetchone()
        edges = conn.execute("SELECT COUNT(*) FROM made_statement").fetchone()[0]

    assert result["attribution_state"] == "uncertain"      # downgraded
    assert row["attribution_state"] == "uncertain"
    assert row["person_id"] is None                        # no identity bound
    assert row["candidate_person_id"] == pid               # kept as reviewer hint
    assert "Jane Doe" not in row["display_label"]          # name suppressed
    assert "Jane Doe" not in result["speaker_label"]
    assert result["speaker_label"] == "Meeting Attendee"
    assert result["note"] == spk.UNCERTAIN_NOTE
    assert edges == 0                                       # no made_statement edge


def test_high_confidence_official_is_named_with_edge(tmp_path: Path) -> None:
    """The positive path: a confirmed, high-confidence official IS named and gets
    a made_statement person edge."""
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        pid = _seed_person(conn, display_name="Jane Doe")
        result = spk.attribute_speaker(
            conn,
            {"speaker_attribution_id": "alpine:2026-05-08:seg-0000:spk",
             "statement_id": sid,
             "attribution_state": "attributed",
             "speaker_class": "on-record-official",
             "person_id": pid,
             "person_confirmed": True,
             "role_title": "Council Member, Town of Alpine",
             "confidence": "high"},
        )
        edge = conn.execute(
            "SELECT person_id, statement_id FROM made_statement WHERE statement_id = ?",
            (sid,),
        ).fetchone()
        stmt_row = conn.execute(
            "SELECT speaker_attribution_id FROM statements WHERE statement_id = ?", (sid,)
        ).fetchone()

    assert result["attribution_state"] == "attributed"
    assert result["person_id"] == pid
    assert result["speaker_label"] == "Jane Doe, Council Member, Town of Alpine"
    assert edge["person_id"] == pid                         # made_statement edge present
    assert stmt_row["speaker_attribution_id"] == "alpine:2026-05-08:seg-0000:spk"


def test_unconfirmed_person_fails_closed(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        pid = _seed_person(conn, display_name="Jane Doe")
        result = spk.attribute_speaker(
            conn,
            {"speaker_attribution_id": "spk-unc", "statement_id": sid,
             "attribution_state": "attributed", "speaker_class": "on-record-official",
             "person_id": pid, "person_confirmed": False, "confidence": "high"},
        )
    assert result["attribution_state"] == "uncertain"
    assert result["person_id"] is None
    assert "Jane Doe" not in result["speaker_label"]


def test_unidentified_speaker_gets_generic_label(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        result = spk.attribute_speaker(
            conn,
            {"speaker_attribution_id": "spk-anon", "statement_id": sid,
             "attribution_state": "unattributed", "speaker_class": "unidentified",
             "confidence": "low"},
        )
    assert result["attribution_state"] == "unattributed"
    assert result["person_id"] is None
    assert result["speaker_label"] == "Meeting Attendee"


def test_naming_on_record_public_is_hard_stop(tmp_path: Path) -> None:
    """Naming a community member is a CEO hard stop — raises, writes nothing."""
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        pid = _seed_person(conn, person_id="person:alpine:resident", display_name="Pat Public")
        with pytest.raises(spk.SpeakerAttributionHardStop):
            spk.attribute_speaker(
                conn,
                {"speaker_attribution_id": "spk-pub", "statement_id": sid,
                 "attribution_state": "attributed", "speaker_class": "on-record-public",
                 "person_id": pid, "person_confirmed": True, "confidence": "high"},
            )
        assert conn.execute("SELECT COUNT(*) FROM speaker_attributions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM made_statement").fetchone()[0] == 0


def test_private_context_never_attributes(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        pid = _seed_person(conn)
        result = spk.attribute_speaker(
            conn,
            {"speaker_attribution_id": "spk-priv", "statement_id": sid,
             "attribution_state": "attributed", "speaker_class": "private-context",
             "person_id": pid, "person_confirmed": True, "confidence": "high"},
        )
    assert result["attribution_state"] == "unattributed"
    assert result["person_id"] is None


def test_attribution_requires_existing_statement(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        with pytest.raises(ValueError, match="does not resolve"):
            spk.attribute_speaker(
                conn,
                {"speaker_attribution_id": "spk-x", "statement_id": "no-such-stmt",
                 "attribution_state": "unattributed", "speaker_class": "unidentified"},
            )


def test_person_id_check_blocks_bound_identity_on_unattributed(tmp_path: Path) -> None:
    """The row-level CHECK is a backstop even against a raw INSERT bypassing speakers.py."""
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        pid = _seed_person(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO speaker_attributions ("
                "speaker_attribution_id, statement_id, attribution_state, speaker_class, person_id) "
                "VALUES ('x', ?, 'unattributed', 'unidentified', ?)",
                (sid, pid),
            )


# --- ACCEPTANCE: outcome_updates never mutates the prior node (1.07 §4.2) ---

def test_outcome_updates_does_not_mutate_prior_node(tmp_path: Path) -> None:
    """An outcome linked to a known_then statement must leave that statement
    byte-for-byte unchanged — append-only forward link, no rewrite."""
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        before = dict(conn.execute(
            "SELECT * FROM statements WHERE statement_id = ?", (sid,)
        ).fetchone())
        assert before["layer"] == "known_then"

        spk.insert_outcome(
            conn,
            {"outcome_id": "alpine:2026-09:outcome-bond",
             "outcome_text": "Bond closed at rate R in 2026-09.",
             "outcome_date": "2026-09-15", "layer": "actual_later"},
        )
        edge_id = spk.link_outcome_updates(
            conn, "alpine:2026-09:outcome-bond", sid, to_node_type="statement",
        )

        after = dict(conn.execute(
            "SELECT * FROM statements WHERE statement_id = ?", (sid,)
        ).fetchone())
        edge = conn.execute(
            "SELECT outcome_id, to_node_id, to_node_type, relation "
            "FROM outcome_updates WHERE outcome_update_id = ?", (edge_id,),
        ).fetchone()

    assert after == before, "prior known_then statement was mutated by an outcome update"
    assert edge["outcome_id"] == "alpine:2026-09:outcome-bond"
    assert edge["to_node_id"] == sid
    assert edge["relation"] == "updates"


def test_outcome_layer_defaults_actual_later(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        spk.insert_outcome(
            conn, {"outcome_id": "o1", "outcome_text": "Later result."},
        )
        row = conn.execute("SELECT layer, verification_status FROM outcomes WHERE outcome_id='o1'").fetchone()
    assert row["layer"] == "actual_later"
    assert row["verification_status"] == "machine_extracted_unreviewed"  # fail-closed


def test_link_outcome_updates_requires_existing_outcome(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        with pytest.raises(ValueError, match="does not resolve"):
            spk.link_outcome_updates(conn, "no-such-outcome", "stmt-x")


def test_outcome_update_is_insert_only_idempotent(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        _seed_source(conn)
        sid = _seed_statement(conn)
        spk.insert_outcome(conn, {"outcome_id": "o2", "outcome_text": "x"})
        spk.link_outcome_updates(conn, "o2", sid)
        spk.link_outcome_updates(conn, "o2", sid)  # re-link must not duplicate or raise
        n = conn.execute(
            "SELECT COUNT(*) FROM outcome_updates WHERE outcome_id='o2'"
        ).fetchone()[0]
    assert n == 1


# --- FK / served_in_role spine ---------------------------------------------

def test_served_in_role_fk_integrity(tmp_path: Path) -> None:
    with db.open_db(_migrated(tmp_path)) as conn:
        pid = _seed_person(conn)
        conn.execute(
            "INSERT INTO roles (role_id, title, start_date) VALUES ('role:seat-3', 'Council Member', '2025-01-01')"
        )
        conn.execute(
            "INSERT INTO served_in_role (served_in_role_id, person_id, role_id, start_date) "
            "VALUES ('svc-1', ?, 'role:seat-3', '2025-01-01')",
            (pid,),
        )
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        joined = conn.execute(
            "SELECT p.display_name, r.title FROM served_in_role sr "
            "JOIN persons p ON p.person_id = sr.person_id "
            "JOIN roles r ON r.role_id = sr.role_id"
        ).fetchall()
    assert violations == []
    assert joined[0]["title"] == "Council Member"
