"""Tests for the web-safe supplied-file read projection (GOV-1579 / B6).

Each acceptance criterion from the issue maps to a test class below:

  AC1 never returns pending/held/rejected (negative test)  -> TestWebSafeStateGate
  AC2 no absolute vault paths + no uploader PII (transport) -> TestNoLeak
  AC3 stripping is server-side (not renderer-only)          -> TestServerSideStripping

Plus the surfaces the blocked frontend legs consume:
  F2 web-safe linkage (source drawer)                       -> TestLinkageProjection
  F3 before/after supersede view, fail-closed                -> TestSupersedeViews
And the structural / envelope / determinism guarantees:
  frozen field allowlist                                     -> TestFieldAllowlist
  response envelope                                          -> TestEnvelope
  byte-deterministic projection                              -> TestDeterminism
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import file_linkage as fl  # noqa: E402
import file_read_api as api  # noqa: E402
import file_records as fr  # noqa: E402
import file_versioning as fv  # noqa: E402
import read_api  # noqa: E402

# A distinctive uploader email (uploader PII) + vault content-address (raw_sha256
# class). The no-leak tests assert NEITHER string ever appears in a response.
UPLOADER_PII = "uploader-secret@example.com"
SHA_V1 = hashlib.sha256(b"%PDF-1.4 Town of Alpine council packet v1").hexdigest()
SHA_V2 = hashlib.sha256(b"%PDF-1.4 Town of Alpine council packet v2 corrected").hexdigest()

# A file:// vault URI an operator might store in origin_url — reviewer-internal,
# must be dropped (never a public locator). Contains a raw-path marker on purpose.
VAULT_URI = "file:///Users/IA/Obsidian Vault/TownOfAlpine/2026-06-23-packet.pdf"

BASE = dict(
    area="alpine",
    source_type="agenda_packet",
    original_filename="2026-06-23-packet.pdf",
    sha256=SHA_V1,
    mime="application/pdf",
    byte_size=51234,
    supplied_by=UPLOADER_PII,
    captured_at="2026-06-23T00:00:00.000+00:00",
)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "b6.db"
    db.apply_migrations(db_path)
    c = db.open_db(db_path)
    yield c
    c.close()


def _make(conn: sqlite3.Connection, *, state: str = api.WEB_SAFE_STATE, **overrides) -> fr.FileRecord:
    """Insert a supplied file and advance it to ``state`` via the legal path."""
    rec = fr.insert_file_record(conn, **{**BASE, **overrides})
    if state == "pending":
        return rec
    fr.set_review_state(conn, rec.file_id, "reviewing")
    if state == "reviewing":
        return fr.get_file_record(conn, rec.file_id)
    if state == api.WEB_SAFE_STATE:
        return fr.set_review_state(conn, rec.file_id, api.WEB_SAFE_STATE)
    # held / rejected reached from reviewing
    return fr.set_review_state(conn, rec.file_id, state)


# --- AC1: state gate --------------------------------------------------------

class TestWebSafeStateGate:
    def test_web_safe_file_is_served(self, conn):
        rec = _make(conn)
        ids = {f["file_id"] for f in api.web_safe_files(conn)}
        assert rec.file_id in ids

    @pytest.mark.parametrize("state", ["pending", "reviewing", "held", "rejected"])
    def test_non_web_safe_states_never_served(self, conn, state):
        rec = _make(conn, state=state)
        ids = {f["file_id"] for f in api.web_safe_files(conn)}
        assert rec.file_id not in ids

    def test_every_served_row_is_web_safe(self, conn):
        _make(conn, original_filename="a.pdf")
        _make(conn, state="pending", original_filename="b.pdf")
        _make(conn, state="held", original_filename="c.pdf")
        assert all(f["review_state"] == api.WEB_SAFE_STATE for f in api.web_safe_files(conn))

    def test_held_after_web_safe_is_dropped(self, conn):
        """A file demoted web_safe -> held stops being served (re-checked, not cached)."""
        rec = _make(conn)
        assert rec.file_id in {f["file_id"] for f in api.web_safe_files(conn)}
        fr.set_review_state(conn, rec.file_id, "held")
        assert rec.file_id not in {f["file_id"] for f in api.web_safe_files(conn)}


# --- AC2: no raw-path / no uploader-PII leak (transport assertion) ----------

class TestNoLeak:
    def test_uploader_pii_never_in_body(self, conn):
        _make(conn)
        body = json.dumps(api.build_files_response(conn))
        assert UPLOADER_PII not in body

    def test_vault_content_address_never_in_body(self, conn):
        _make(conn)
        body = json.dumps(api.build_files_response(conn))
        assert SHA_V1 not in body

    def test_vault_origin_uri_is_dropped(self, conn):
        _make(conn, origin_url=VAULT_URI)
        [card] = api.web_safe_files(conn)
        assert "origin_url" not in card  # non-web URL -> stripped server-side
        assert VAULT_URI not in json.dumps(api.build_files_response(conn))

    def test_public_origin_url_survives(self, conn):
        url = "https://web.archive.org/web/2026/packet.pdf"
        _make(conn, origin_url=url)
        [card] = api.web_safe_files(conn)
        assert card["origin_url"] == url

    def test_transport_sweep_runs_on_whole_body(self, conn):
        """A raw path smuggled anywhere in the assembled body fails LOUDLY."""
        _make(conn)
        response = api.build_files_response(conn)
        response["files"][0]["poisoned"] = "/Users/IA/Obsidian Vault/leak.pdf"
        with pytest.raises(read_api.RawPathLeak):
            read_api.assert_no_raw_paths(response)


# --- AC3: server-side stripping ---------------------------------------------

class TestServerSideStripping:
    def test_projection_omits_sha256_key(self, conn):
        _make(conn)
        [card] = api.web_safe_files(conn)
        assert "sha256" not in card

    def test_projection_omits_supplied_by_key(self, conn):
        _make(conn)
        [card] = api.web_safe_files(conn)
        assert "supplied_by" not in card

    def test_db_row_still_has_the_stripped_fields(self, conn):
        """Stripping is a projection concern — the record keeps full provenance."""
        rec = _make(conn)
        stored = fr.get_file_record(conn, rec.file_id)
        assert stored.sha256 == SHA_V1
        assert stored.supplied_by == UPLOADER_PII


# --- F2: web-safe linkage ---------------------------------------------------

class TestLinkageProjection:
    def test_links_are_web_safe_shape(self, conn):
        rec = _make(conn)
        fl.link_file(
            conn, file_id=rec.file_id, subject_node_type="meeting",
            subject_node_id="2026-06-23", is_primary_source=True, linked_by="isaac",
        )
        [card] = api.web_safe_files(conn)
        assert card["links"] == [
            {"subject_node_type": "meeting", "subject_node_id": "2026-06-23",
             "is_primary_source": True}
        ]

    def test_linked_by_operator_identity_not_projected(self, conn):
        rec = _make(conn)
        fl.link_file(
            conn, file_id=rec.file_id, subject_node_type="area",
            subject_node_id="alpine", is_primary_source=False,
            linked_by="operator-secret@example.com",
        )
        body = json.dumps(api.build_files_response(conn))
        assert "operator-secret@example.com" not in body

    def test_no_links_is_empty_list(self, conn):
        _make(conn)
        [card] = api.web_safe_files(conn)
        assert card["links"] == []


# --- F3: before/after supersede views ---------------------------------------

def _supersede_both_web_safe(conn) -> tuple[fr.FileRecord, fr.FileRecord]:
    """v1 (web_safe) superseded by v2, with v2 also advanced to web_safe."""
    v1 = _make(conn)
    result = fv.supersede_file(
        conn, v1.file_id, area="alpine", source_type="agenda_packet",
        original_filename="2026-06-23-packet-corrected.pdf", sha256=SHA_V2,
        mime="application/pdf", byte_size=52999, supplied_by=UPLOADER_PII,
        captured_at="2026-06-24T09:30:00.000+00:00", superseded_by="isaac",
    )
    fr.set_review_state(conn, result.new.file_id, "reviewing")
    v2 = fr.set_review_state(conn, result.new.file_id, api.WEB_SAFE_STATE)
    return fr.get_file_record(conn, v1.file_id), v2


class TestSupersedeViews:
    def test_view_present_when_both_versions_web_safe(self, conn):
        v1, v2 = _supersede_both_web_safe(conn)
        [view] = api.supersede_views(conn)
        assert view["superseded_file_id"] == v1.file_id
        assert view["new_file_id"] == v2.file_id
        assert view["version_group_id"] == v1.version_group_id

    def test_diff_reports_content_changed_and_safe_fields(self, conn):
        _supersede_both_web_safe(conn)
        [view] = api.supersede_views(conn)
        diff = view["diff"]
        assert diff["content_changed"] is True
        assert "original_filename" in diff["changed"]
        assert diff["changed"]["byte_size"] == {"before": 51234, "after": 52999}

    def test_diff_never_carries_sha256_or_supplied_by(self, conn):
        _supersede_both_web_safe(conn)
        [view] = api.supersede_views(conn)
        assert "sha256" not in view["diff"]["changed"]
        assert "supplied_by" not in view["diff"]["changed"]
        assert "sha256" not in view["diff"]["unchanged"]
        assert "supplied_by" not in view["diff"]["unchanged"]

    def test_omitted_when_new_version_not_web_safe(self, conn):
        v1 = _make(conn)
        fv.supersede_file(
            conn, v1.file_id, area="alpine", source_type="agenda_packet",
            original_filename="v2.pdf", sha256=SHA_V2, mime="application/pdf",
            byte_size=52999, supplied_by=UPLOADER_PII,
            captured_at="2026-06-24T09:30:00.000+00:00", superseded_by="isaac",
        )
        # new stays pending -> fail-closed, no before/after crosses.
        assert api.supersede_views(conn) == []

    def test_omitted_when_prior_version_demoted(self, conn):
        v1, v2 = _supersede_both_web_safe(conn)
        fr.set_review_state(conn, v1.file_id, "held")  # prior no longer web_safe
        assert api.supersede_views(conn) == []


# --- structural allowlist ---------------------------------------------------

class TestFieldAllowlist:
    def test_card_keys_subset_of_allowlist(self, conn):
        rec = _make(conn)
        fl.link_file(
            conn, file_id=rec.file_id, subject_node_type="meeting",
            subject_node_id="2026-06-23", is_primary_source=True, linked_by="isaac",
        )
        [card] = api.web_safe_files(conn)
        assert set(card) <= api.WEB_SAFE_FILE_FIELDS

    def test_allowlist_excludes_raw_and_pii_fields(self):
        assert "sha256" not in api.WEB_SAFE_FILE_FIELDS
        assert "supplied_by" not in api.WEB_SAFE_FILE_FIELDS
        assert "sha256" not in api.WEB_SAFE_DIFF_FIELDS
        assert "supplied_by" not in api.WEB_SAFE_DIFF_FIELDS

    def test_assert_file_keys_rejects_extra_field(self):
        with pytest.raises(api.FieldLeak):
            api._assert_file_keys({"file_id": "x", "sha256": SHA_V1})


# --- envelope + determinism -------------------------------------------------

class TestEnvelope:
    def test_envelope_keys_present(self, conn):
        _make(conn)
        resp = api.build_files_response(conn)
        assert resp["scope"] == "alpine"
        assert resp["access"] == "web_safe"
        assert resp["dataOrigin"] == "reviewed_snapshot"
        assert isinstance(resp["files"], list)
        assert isinstance(resp["supersede_views"], list)

    def test_supersede_views_can_be_omitted(self, conn):
        _make(conn)
        resp = api.build_files_response(conn, include_supersede_views=False)
        assert "supersede_views" not in resp


class TestDeterminism:
    def test_projection_is_byte_identical_on_repeat(self, conn):
        _make(conn, original_filename="a.pdf")
        _make(conn, original_filename="b.pdf")
        first = json.dumps(api.build_files_response(conn), sort_keys=True)
        second = json.dumps(api.build_files_response(conn), sort_keys=True)
        assert first == second
