"""GOV-290 Stage 2.07 — read-time, fail-closed SAFE speaker-label projection.

Deterministic ``speaker_attributions -> speaker_label`` projection in ``read_api``
(Alpine, no-AI), a structural mirror of GOV-283's confidence_label. Asserts the
2.07 contract bar:

- ``read_api`` attaches a derived ``speaker_label`` envelope key to every served
  statement in BOTH ``published_records`` and ``reviewer_internal_records``, via the
  join ``statement.speaker_attribution_id -> speaker_attributions`` and the SSOT
  constants in ``speakers`` — never re-declared here;
- name-leak guard: an ``attributed`` + ``on-record-official`` row surfaces its safe
  ``"Name, Role"`` label; an ``on-record-public`` / ``private-context`` /
  ``unidentified`` row NEVER surfaces a name;
- fail-closed, re-guarded (proven RED): no attribution id, an unresolvable id, a
  NULL ``display_label`` in the naming gate, and a deliberately NAME-POISONED
  ``display_label`` on a non-attributed row all collapse to the SSOT safe generic /
  community label — the poisoned name never reaches the response body;
- determinism: same DB -> byte-identical output across two runs;
- no-leak: the raw attribution columns never cross ``to_web_safe`` (absent from the
  allowlist, named in the unsafe set); only the derived label projects;
- SSOT parity: the safe-label strings + the nameable-class set are imported from
  ``speakers``, not copied — the projection's emittable range cannot drift.

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

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import speakers as sp  # noqa: E402
import statements as st  # noqa: E402

# A name that must NEVER appear in any served body — poisoned into display_label on
# rows that are not safely named.
POISON_NAME = "Confidential Witness Q"


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    _seed_base(connection)
    yield connection
    connection.close()


def _seed_base(conn: sqlite3.Connection) -> None:
    """A source + meeting + agenda item + one transcript segment to anchor on."""
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
    conn.commit()


def _add_attribution(
    conn: sqlite3.Connection,
    *,
    attribution_id: str,
    attribution_state: str,
    speaker_class: str,
    display_label: str | None,
    statement_id: str | None = None,
) -> str:
    """Insert one speaker_attributions row DIRECTLY (controls display_label exactly).

    Writing the row directly — rather than via :func:`speakers.attribute_speaker` —
    lets the test plant an adversarial (name-poisoned) ``display_label`` on a
    non-attributed row, which the safe write path would never produce. That is the
    point of the RED fail-closed proof: read_api must re-guard regardless of how the
    stored value got there. ``statement_id`` defaults to the caller's convention
    ``stmt-<attribution_id>`` but may be passed explicitly.
    """
    conn.execute(
        "INSERT INTO speaker_attributions (speaker_attribution_id, statement_id, "
        "attribution_state, speaker_class, display_label) VALUES (?, ?, ?, ?, ?)",
        (
            attribution_id,
            statement_id or _stmt_for(attribution_id),
            attribution_state,
            speaker_class,
            display_label,
        ),
    )
    conn.commit()
    return attribution_id


def _stmt_for(attribution_id: str) -> str:
    return f"stmt-{attribution_id}"


def _insert_statement(
    conn: sqlite3.Connection,
    *,
    statement_id: str,
    speaker_attribution_id: str | None,
) -> None:
    """An eligible, published statement anchored to the seeded segment (non-orphan)."""
    st.insert_statement(
        conn,
        {
            "statement_id": statement_id,
            "segment_id": "seg-1",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "speaker_attribution_id": speaker_attribution_id,
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
    )


def _attributed_statement(
    conn: sqlite3.Connection,
    *,
    attribution_id: str,
    attribution_state: str,
    speaker_class: str,
    display_label: str | None,
) -> str:
    """Insert statement + its attribution row; returns the statement_id."""
    statement_id = _stmt_for(attribution_id)
    _insert_statement(conn, statement_id=statement_id, speaker_attribution_id=attribution_id)
    _add_attribution(
        conn,
        attribution_id=attribution_id,
        attribution_state=attribution_state,
        speaker_class=speaker_class,
        display_label=display_label,
    )
    return statement_id


def _served(conn: sqlite3.Connection, statement_id: str) -> dict:
    record = next(
        (r for r in read_api.published_records(conn) if r["statement_id"] == statement_id),
        None,
    )
    assert record is not None, f"{statement_id!r} was expected to be served but was not"
    return record


# ---------------------------------------------------------------------------
# AC1 — every served statement carries a speaker_label envelope key (both lanes).
# ---------------------------------------------------------------------------


def test_every_published_statement_has_speaker_label_key(conn: sqlite3.Connection) -> None:
    _attributed_statement(
        conn, attribution_id="a1", attribution_state="attributed",
        speaker_class="on-record-official", display_label="Jane Doe, Mayor",
    )
    record = _served(conn, "stmt-a1")
    assert "speaker_label" in record
    assert record["speaker_label"] is not None


# ---------------------------------------------------------------------------
# AC4 — name-leak guard: only attributed + on-record-official surfaces a name.
# ---------------------------------------------------------------------------


def test_attributed_official_surfaces_safe_name_role(conn: sqlite3.Connection) -> None:
    """The one safe naming gate: attributed + on-record-official -> "Name, Role"."""
    _attributed_statement(
        conn, attribution_id="off", attribution_state="attributed",
        speaker_class="on-record-official", display_label="Jane Doe, Mayor",
    )
    assert _served(conn, "stmt-off")["speaker_label"] == "Jane Doe, Mayor"


def test_on_record_public_never_surfaces_name_even_if_attributed(conn: sqlite3.Connection) -> None:
    """on-record-public is NOT auto-nameable: a name in display_label is dropped."""
    _attributed_statement(
        conn, attribution_id="pub", attribution_state="attributed",
        speaker_class="on-record-public", display_label=POISON_NAME,
    )
    label = _served(conn, "stmt-pub")["speaker_label"]
    assert label == sp.SAFE_COMMUNITY_LABEL
    assert POISON_NAME not in label


@pytest.mark.parametrize("speaker_class", ["private-context", "unidentified"])
def test_non_nameable_class_never_surfaces_name(
    conn: sqlite3.Connection, speaker_class: str
) -> None:
    _attributed_statement(
        conn, attribution_id=f"nc-{speaker_class}", attribution_state="attributed",
        speaker_class=speaker_class, display_label=POISON_NAME,
    )
    label = _served(conn, f"stmt-nc-{speaker_class}")["speaker_label"]
    assert label == sp.SAFE_GENERIC_LABEL
    assert POISON_NAME not in label


# ---------------------------------------------------------------------------
# AC3 — fail-closed, re-guarded; ≥4 break modes, name-poison proven RED.
# ---------------------------------------------------------------------------


def test_fail_closed_no_attribution_id(conn: sqlite3.Connection) -> None:
    _insert_statement(conn, statement_id="stmt-none", speaker_attribution_id=None)
    assert _served(conn, "stmt-none")["speaker_label"] == sp.SAFE_GENERIC_LABEL


def test_fail_closed_unresolvable_attribution_id(conn: sqlite3.Connection) -> None:
    """A speaker_attribution_id pointing at no row (no FK on statements) -> generic."""
    _insert_statement(conn, statement_id="stmt-dangling", speaker_attribution_id="ghost-id")
    assert _served(conn, "stmt-dangling")["speaker_label"] == sp.SAFE_GENERIC_LABEL


def test_fail_closed_null_display_label_in_naming_gate(conn: sqlite3.Connection) -> None:
    """attributed + nameable but NULL display_label -> generic (never None)."""
    _attributed_statement(
        conn, attribution_id="nulllbl", attribution_state="attributed",
        speaker_class="on-record-official", display_label=None,
    )
    assert _served(conn, "stmt-nulllbl")["speaker_label"] == sp.SAFE_GENERIC_LABEL


def test_fail_closed_empty_display_label_in_naming_gate(conn: sqlite3.Connection) -> None:
    _attributed_statement(
        conn, attribution_id="emptylbl", attribution_state="attributed",
        speaker_class="on-record-official", display_label="   ",
    )
    assert _served(conn, "stmt-emptylbl")["speaker_label"] == sp.SAFE_GENERIC_LABEL


@pytest.mark.parametrize("bad_state", ["uncertain", "unattributed"])
def test_fail_closed_name_poison_on_non_attributed_row_is_not_leaked(
    conn: sqlite3.Connection, bad_state: str
) -> None:
    """RED proof: a name poisoned into display_label on a NON-attributed row, even
    with an otherwise-nameable speaker_class, must NOT leak — the label is derived
    from speaker_class alone and the poisoned column is never read."""
    statement_id = _attributed_statement(
        conn, attribution_id=f"poison-{bad_state}", attribution_state=bad_state,
        speaker_class="on-record-official", display_label=f"{POISON_NAME}, Mayor",
    )
    record = _served(conn, statement_id)
    assert record["speaker_label"] == sp.SAFE_GENERIC_LABEL
    # belt-and-suspenders: the poisoned name appears NOWHERE in the served record.
    assert POISON_NAME not in json.dumps(record)


def test_speaker_label_is_never_none_defensive(conn: sqlite3.Connection) -> None:
    """A fabricated record whose id resolves to no row still returns a string."""
    assert read_api._speaker_label_for(conn, {}) == sp.SAFE_GENERIC_LABEL
    assert read_api._speaker_label_for(conn, {"speaker_attribution_id": "nope"}) == sp.SAFE_GENERIC_LABEL


# ---------------------------------------------------------------------------
# AC2 — determinism: same DB -> byte-identical output across two runs.
# ---------------------------------------------------------------------------


def test_determinism_byte_identical_across_two_runs(conn: sqlite3.Connection) -> None:
    _attributed_statement(
        conn, attribution_id="d1", attribution_state="attributed",
        speaker_class="on-record-official", display_label="Jane Doe, Mayor",
    )
    _attributed_statement(
        conn, attribution_id="d2", attribution_state="uncertain",
        speaker_class="on-record-public", display_label=POISON_NAME,
    )
    first = json.dumps(read_api.build_response(conn, include_records=True), sort_keys=True)
    second = json.dumps(read_api.build_response(conn, include_records=True), sort_keys=True)
    assert first == second


# ---------------------------------------------------------------------------
# AC5 — web-safe boundary intact (raw attribution columns never cross).
# ---------------------------------------------------------------------------


def test_raw_attribution_columns_never_web_projected(conn: sqlite3.Connection) -> None:
    _attributed_statement(
        conn, attribution_id="ws", attribution_state="attributed",
        speaker_class="on-record-official", display_label="Jane Doe, Mayor",
    )
    record = _served(conn, "stmt-ws")
    for col in (
        "speaker_attribution_id", "display_label", "attribution_state",
        "speaker_class", "person_id", "candidate_person_id",
    ):
        assert col not in record
    # only the derived envelope key crosses.
    assert record["speaker_label"] == "Jane Doe, Mayor"


def test_allowlist_excludes_attribution_columns_and_to_web_safe_strips_them() -> None:
    for col in (
        "speaker_attribution_id", "display_label", "attribution_state",
        "speaker_class", "person_id", "candidate_person_id",
    ):
        assert col not in pub.WEB_SAFE_FIELD_ALLOWLIST
        assert col in pub.WEB_UNSAFE_FIELDS
    # the derived envelope key is NOT smuggled into the allowlist either.
    assert "speaker_label" not in pub.WEB_SAFE_FIELD_ALLOWLIST
    # transcript_class / segment_id stay unsafe (no GOV-283 / 1.07 regression).
    assert "transcript_class" in pub.WEB_UNSAFE_FIELDS
    assert "segment_id" in pub.WEB_UNSAFE_FIELDS
    stripped = pub.to_web_safe(
        {"statement_id": "s1", "display_label": POISON_NAME, "person_id": "p1"}
    )
    assert "display_label" not in stripped and "person_id" not in stripped


def test_full_response_with_labels_passes_transport_sweep(conn: sqlite3.Connection) -> None:
    _attributed_statement(
        conn, attribution_id="sweep", attribution_state="attributed",
        speaker_class="on-record-official", display_label="Jane Doe, Mayor",
    )
    body = read_api.build_response(conn, include_records=True)  # runs assert_no_raw_paths
    record = next(r for r in body["records"] if r["statement_id"] == "stmt-sweep")
    assert record["speaker_label"] == "Jane Doe, Mayor"
    # GOV-283 projection still rides alongside — no regression.
    assert record["confidence_label"] == "source_anchored_timed"


# ---------------------------------------------------------------------------
# AC6 — SSOT parity: imported, not copied; emittable range cannot drift.
# ---------------------------------------------------------------------------


def test_ssot_constants_are_imported_not_copied() -> None:
    """read_api derives every safe label from the speakers SSOT (identity, not copy)."""
    assert read_api._SAFE_SPEAKER_LABEL is sp.SAFE_GENERIC_LABEL
    assert read_api.sp.AUTO_NAMEABLE_CLASSES is sp.AUTO_NAMEABLE_CLASSES
    assert read_api.sp.SAFE_COMMUNITY_LABEL is sp.SAFE_COMMUNITY_LABEL


def test_emitted_label_range_cannot_drift(conn: sqlite3.Connection) -> None:
    """Across every (state x class) combo, the emitted non-named labels are exactly
    the SSOT safe set — only the proven naming gate adds the stored "Name, Role"."""
    states = sorted(sp.ALLOWED_ATTRIBUTION_STATES)
    classes = sorted(sp.ALLOWED_SPEAKER_CLASSES)
    named_label = "Jane Doe, Mayor"
    emitted: set[str] = set()
    i = 0
    for state in states:
        for cls in classes:
            aid = f"m{i}"
            i += 1
            _attributed_statement(
                conn, attribution_id=aid, attribution_state=state,
                speaker_class=cls, display_label=named_label,
            )
            emitted.add(_served(conn, _stmt_for(aid))["speaker_label"])
    # Only attributed + on-record-official surfaces the stored label; everything
    # else collapses to one of exactly two SSOT safe labels.
    assert emitted == {named_label, sp.SAFE_GENERIC_LABEL, sp.SAFE_COMMUNITY_LABEL}


# ---------------------------------------------------------------------------
# AC1 (reviewer-internal lane) — the reviewer view is labeled via the same path.
# ---------------------------------------------------------------------------


def test_reviewer_internal_record_also_labeled(conn: sqlite3.Connection) -> None:
    run_id = "gov290:ai-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-ri",
            "segment_id": "seg-1",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "speaker_attribution_id": "ri-attr",
            "statement_text": "A Town Council special meeting was convened.",
            "produced_by": "ai",
            "layer": "ai_thought_then",
            "ai_extraction_run_id": run_id,
        },
    )
    _add_attribution(
        conn, attribution_id="ri-attr", attribution_state="unattributed",
        speaker_class="on-record-public", display_label=POISON_NAME,
        statement_id="stmt-ri",
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac", registered_by="owner:isaac",
        note="GOV-290 reviewer-internal label test",
    )
    gate.promote_statement(
        conn, "stmt-ri", reviewer_id="reviewer:isaac", decision="approved",
        reason="reviewer-internal source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )
    served = read_api.reviewer_internal_records(conn)
    record = next(r for r in served if r["statement_id"] == "stmt-ri")
    assert record["speaker_label"] == sp.SAFE_COMMUNITY_LABEL
    assert POISON_NAME not in json.dumps(record)
    # the public lane never serves this pre-publish row.
    assert "stmt-ri" not in {r["statement_id"] for r in read_api.published_records(conn)}
