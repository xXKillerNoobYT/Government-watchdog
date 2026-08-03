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


# --- GOV-1625: web-safe provenance_note -------------------------------------

class TestProvenanceNoteProjection:
    def test_note_present_when_set(self, conn):
        _make(conn, provenance_note="handed to me at the June council meeting")
        [card] = api.web_safe_files(conn)
        assert card["provenance_note"] == "handed to me at the June council meeting"

    def test_note_omitted_when_absent(self, conn):
        _make(conn)  # BASE has no provenance_note
        [card] = api.web_safe_files(conn)
        assert "provenance_note" not in card

    def test_note_omitted_when_blank(self, conn):
        _make(conn, provenance_note="   ")
        [card] = api.web_safe_files(conn)
        assert "provenance_note" not in card

    def test_non_url_note_survives_verbatim_not_dropped(self, conn):
        # Unlike origin_url (dropped unless a public URL), a non-URL note is FREE
        # TEXT and is emitted verbatim — it is never treated as / coerced into a
        # link. This is the backend half of the GOV-1609 §4.2 linkify guard: the
        # projection hands the frontend plain prose, not a locator.
        note = "call the clerk at 307-555-0100 for the original"
        _make(conn, provenance_note=note)
        [card] = api.web_safe_files(conn)
        assert card["provenance_note"] == note
        assert "origin_url" not in card  # nothing here is a URL

    def test_note_and_url_coexist_in_split_shape(self, conn):
        url = "https://web.archive.org/web/2026/packet.pdf"
        _make(conn, origin_url=url, provenance_note="scanned from the paper packet")
        [card] = api.web_safe_files(conn)
        assert card["origin_url"] == url
        assert card["provenance_note"] == "scanned from the paper packet"

    def test_note_is_swept_by_transport_backstop(self, conn):
        # Defense-in-depth: a note carrying a vault path still fails LOUDLY at the
        # boundary (the reviewer gate is primary; the sweep is the backstop).
        _make(conn, provenance_note="/Users/IA/Obsidian Vault/leak.pdf")
        with pytest.raises(read_api.RawPathLeak):
            api.build_files_response(conn)

    def test_note_within_frozen_allowlist(self):
        assert "provenance_note" in api.WEB_SAFE_FILE_FIELDS


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


# --- GOV-1700 (C1b): W-1's SECOND half — the re-check after the SQL ----------
#
# C1b broke each of the B6 contract's nine invariants in turn and ran the suite.
# Eight were caught. **W-1 was not**: deleting the post-SQL re-check entirely left
# all 65 tests green, because the WHERE clause still did the work and every
# existing state-gate test asserts the OUTCOME ("a held file is not served")
# rather than the MECHANISM.
#
# That is exactly the shape of thing a later reader deletes as redundant — and
# `CLAUDE.md` states "review gates re-check after SQL" as a house rule, so the
# claim was load-bearing in the docs and unenforced in the suite.
#
# The threat it defends against is NOT storage lying: `review_state` is TEXT with
# BINARY collation and a CHECK constraint over five values, so no stored row can
# satisfy `WHERE review_state = 'web_safe'` and then compare unequal in Python.
# The reachable threat is the one the module's own docstring names — **a mis-typed
# query** — and that is what this simulates: the projection is handed a connection
# whose SELECT has lost its filter, and must still refuse to serve anything
# unreviewed.


class _QueryThatForgotToFilter:
    """A connection whose `SELECT … WHERE review_state = ?` lost its WHERE.

    Not a mock of the projection — a faithful stand-in for the one edit that
    would silently disarm the state gate. Everything else forwards untouched.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params=()):
        if "WHERE review_state = ?" in sql:
            return self._conn.execute(sql.replace("WHERE review_state = ? ", ""), ())
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestStateGateSurvivesAQueryThatStoppedFiltering:

    def test_the_unfiltered_query_really_would_return_everything(self, conn):
        """Non-vacuity: prove the stand-in defeats the SQL filter.

        Without this, a typo in the replacement string would make the test below
        pass for the wrong reason — the SQL would still be filtering and the
        re-check would never be exercised at all.
        """
        for state in ("pending", "reviewing", api.WEB_SAFE_STATE, "held", "rejected"):
            _make(conn, state=state, sha256=hashlib.sha256(state.encode()).hexdigest())
        leaky = _QueryThatForgotToFilter(conn)
        rows = leaky.execute(
            f"SELECT {', '.join(api._WEB_SAFE_FILE_COLUMNS)} FROM supplied_files "
            "WHERE review_state = ? ORDER BY version_group_id, created_at, file_id",
            (api.WEB_SAFE_STATE,),
        ).fetchall()
        assert len(rows) == 5, (
            f"the stand-in did not defeat the filter (got {len(rows)} rows, want 5) — "
            "the re-check test below would prove nothing")

    def test_only_web_safe_is_served_even_when_the_query_does_not_filter(self, conn):
        """W-1's defense in depth, isolated from the SQL that normally hides it."""
        for state in ("pending", "reviewing", api.WEB_SAFE_STATE, "held", "rejected"):
            _make(conn, state=state, sha256=hashlib.sha256(state.encode()).hexdigest())

        served = api.web_safe_files(_QueryThatForgotToFilter(conn))

        assert len(served) == 1, (
            "the post-SQL re-check did not hold: with an unfiltered query the "
            f"projection served {len(served)} files instead of 1. Deleting that "
            "re-check is invisible while the WHERE clause is intact — which is "
            "precisely why it needs its own guard.")
        assert served[0]["review_state"] == api.WEB_SAFE_STATE


# --- GOV-1703 (C8 hunt): TWO web-safe allowlists exist, and they disagree ------
#
# C8 for read-api compared every web-safe field set in the repo. There are two
# independent families:
#
#   * `publication.WEB_SAFE_FIELD_ALLOWLIST` / `WEB_UNSAFE_FIELDS` — the SSOT for
#     the statements/cards surface, and the set other projections check against
#     (`stage3_source_inventory` asserts a subset relation against it);
#   * this module's `WEB_SAFE_FILE_FIELDS` / `_WEB_SAFE_FILE_COLUMNS` / link and
#     diff sets, for supplied files.
#
# **`file_read_api` does not import `publication`, so nothing compared them.**
# Measured: the supplied-file allowlist contains **`review_state`**, which the SSOT
# names explicitly WEB-UNSAFE.
#
# It does not leak today, and the reason is precise rather than lucky: W-1 filters
# to `web_safe` and re-checks after the SQL, so **every projected card carries the
# same value** — measured across all five review states, 1 of 5 projected, one
# distinct value. A constant carries no information.
#
# But that makes the exemption's safety rest **entirely on W-1**. The structural
# allowlist — the second line of defence — has been opened for this field, so if
# the state gate regressed the allowlist would not object. Hence two tests: one
# pinning the divergence to its single reviewed exception, and one guarding the
# *justification* for that exception rather than just asserting it.


import publication as pub  # noqa: E402

#: The one field the supplied-file surface serves that the SSOT calls unsafe.
#: Reviewed and justified below; this set may only SHRINK.
_REVIEWED_UNSAFE_EXEMPTIONS = frozenset({"review_state"})


class TestAllowlistsAgreeWithTheNamedUnsafeSet:

    @pytest.mark.parametrize("name", [
        "WEB_SAFE_FILE_FIELDS", "WEB_SAFE_LINK_FIELDS", "WEB_SAFE_DIFF_FIELDS",
        "_WEB_SAFE_FILE_COLUMNS",
    ])
    def test_no_unreviewed_named_unsafe_field_is_web_safe_here(self, name):
        """A field the SSOT calls unsafe may not appear here without review."""
        fields = frozenset(getattr(api, name))
        unreviewed = sorted((fields & pub.WEB_UNSAFE_FIELDS) - _REVIEWED_UNSAFE_EXEMPTIONS)
        assert not unreviewed, (
            f"{name} contains {unreviewed}, which `publication.WEB_UNSAFE_FIELDS` "
            "names as web-unsafe. Two allowlists govern what crosses to the website "
            "and this module does not import the SSOT, so nothing else compares them. "
            "Adding a field here is a publication-safety change: justify it and add "
            "it to _REVIEWED_UNSAFE_EXEMPTIONS, or take it out.")

    def test_the_exemption_set_is_not_carrying_a_field_that_left(self):
        """An exemption for a field no longer served is stale, not safe.

        Keeps the set shrinking rather than accumulating — the same rule as the
        migration and doc-citation ratchets.
        """
        everywhere = (frozenset(api.WEB_SAFE_FILE_FIELDS)
                      | frozenset(api._WEB_SAFE_FILE_COLUMNS)
                      | frozenset(api.WEB_SAFE_LINK_FIELDS)
                      | frozenset(api.WEB_SAFE_DIFF_FIELDS))
        stale = sorted(_REVIEWED_UNSAFE_EXEMPTIONS - everywhere)
        assert not stale, f"exemption(s) {stale} no longer appear in any allowlist — remove them"

    def test_the_review_state_exemption_is_justified_because_it_is_a_CONSTANT(self, conn):
        """Guard the REASON for the exemption, not just the exemption.

        `review_state` is safe here only because W-1 means every projected card
        carries `web_safe` — a constant carries no information. If the state gate
        ever regressed, this field would start reporting real reviewer posture to
        the website, and the structural allowlist would NOT object because the
        field is allowlisted. So the justification is what needs a test.
        """
        for state in ("pending", "reviewing", api.WEB_SAFE_STATE, "held", "rejected"):
            _make(conn, state=state, sha256=hashlib.sha256(state.encode()).hexdigest())

        served = api.web_safe_files(conn)
        values = {card.get("review_state") for card in served}
        assert served, "nothing projected — the test would be vacuous"
        assert values == {api.WEB_SAFE_STATE}, (
            f"review_state is no longer constant across the projection: {sorted(values)}. "
            "It is allowlisted ONLY because W-1 makes it carry no information; a "
            "varying value means the projection is reporting reviewer posture to the "
            "website.")


# --- GOV-1704 (C9 hunt): the projection is N+1, and that is FINE — but pin it --
#
# C9 measured query count against corpus size with a proxy that wraps BOTH
# `conn.execute` AND `conn.cursor()`. That detail is the finding behind the
# finding: the first proxy wrapped only `conn.execute` and reported a flat **2
# queries at every size**, because `file_linkage._rows` issues every one of its
# statements through `conn.cursor()`. A clean bill from a blind instrument.
#
# Corrected, the shape is unambiguous — one linkage SELECT per projected file:
#
#     files    queries      ms
#         1          3     0.2
#        10         12     0.5
#       100        102     4.4
#       200        202     8.3
#
# **N+1, and deliberately left alone.** This is a build-time projection baked into
# a web artifact (`dataOrigin: reviewed_snapshot`), not a per-request path, and the
# corpus is human-uploaded packets. 8.3 ms at 200 files extrapolates to ~0.8 s at
# 20,000. Rewriting a serving surface for a cost nobody is paying is the mistake
# C9 made in reverse at iteration 55, where two full scans turned out correct by
# design.
#
# What is NOT fine is the **cliff**: linear -> quadratic. A per-link lookup added
# inside the loop would take 200 files from 202 queries to 800+ and nothing would
# notice. So the guard bounds the MARGINAL cost per file rather than demanding a
# constant — it permits the design that exists and fails the degradation.


class _CountingCursor:
    def __init__(self, cur, tally):
        object.__setattr__(self, "_cur", cur)
        object.__setattr__(self, "_tally", tally)

    def execute(self, sql, params=()):
        self._tally(); return self._cur.execute(sql, params)

    def __getattr__(self, name): return getattr(self._cur, name)

    def __setattr__(self, name, value): setattr(self._cur, name, value)

    def __iter__(self): return iter(self._cur)


class _CountingConn:
    """Counts statements via `execute` AND `cursor()`.

    Wrapping only `execute` is the trap this guard was born from: it misses every
    query a module issues through a cursor, and reports a flat count that looks
    like an absence of N+1.
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "count", 0)

    def _tally(self): object.__setattr__(self, "count", self.count + 1)

    def execute(self, sql, params=()):
        self._tally(); return self._conn.execute(sql, params)

    def cursor(self): return _CountingCursor(self._conn.cursor(), self._tally)

    def __getattr__(self, name): return getattr(self._conn, name)


def _seed_web_safe_files(conn, n, links_each=3):
    for i in range(n):
        rec = _make(conn, sha256=hashlib.sha256(f"perf-{i}".encode()).hexdigest())
        for j in range(links_each):
            fl.link_file(conn, file_id=rec.file_id, subject_node_type="area",
                         subject_node_id=f"alpine-{j}", is_primary_source=(j == 0),
                         linked_by="operator")


class TestProjectionQueryCostStaysLinear:
    """Not "no N+1" — the marginal cost per file, which is what can cliff."""

    #: One linkage SELECT per file today. The bound leaves room for a second
    #: per-file query without failing, and fails a per-LINK query (3 links each).
    _MAX_QUERIES_PER_FILE = 2.0

    def _measure(self, conn, n):
        _seed_web_safe_files(conn, n)
        counting = _CountingConn(conn)
        response = api.build_files_response(counting)
        assert len(response["files"]) == n, "seed did not project as expected"
        assert all(card["links"] for card in response["files"]), (
            "no links projected — the linkage path would not be exercised and this "
            "measurement would be hollow")
        return counting.count

    def test_marginal_query_cost_per_file_is_bounded(self, conn, tmp_path):
        """Measure at two sizes; the SLOPE is the property, not the intercept."""
        small = self._measure(conn, 8)

        big_path = tmp_path / "perf-big.db"
        db.apply_migrations(big_path)
        big_conn = db.open_db(big_path)
        try:
            big = self._measure(big_conn, 40)
        finally:
            big_conn.close()

        marginal = (big - small) / (40 - 8)
        assert marginal <= self._MAX_QUERIES_PER_FILE, (
            f"the projection now issues {marginal:.2f} queries per additional file "
            f"({small} at 8 files, {big} at 40). It has always been N+1 and that is "
            "accepted for a build-time projection — but the cost per file has grown, "
            "which is the linear->quadratic cliff this guard exists to catch. A "
            "per-LINK query inside the per-FILE loop is the usual cause.")

    def test_the_counter_sees_cursor_issued_queries(self, conn):
        """Non-vacuity, and it is the whole reason this class exists.

        `file_linkage` runs every statement through `conn.cursor()`. A proxy that
        wraps only `conn.execute` counts none of them and reports a flat cost at
        any corpus size — a clean bill from a blind instrument. This asserts the
        counter actually grows with the corpus.
        """
        counting = _CountingConn(conn)
        before = counting.count
        _seed_web_safe_files(conn, 5)
        api.build_files_response(counting)
        assert counting.count - before >= 5, (
            f"only {counting.count - before} statements counted for 5 files — the "
            "counter is not seeing cursor-issued queries, so any cost measured "
            "with it is meaningless")


# --- GOV-1705 (C12): the one fail-OPEN surface, held shut by tripwire ---------
#
# Every other allowlist in this module is fail-closed: an unknown key is denied.
# `WEB_SAFE_DIFF_FIELDS` inverts that. It is `file_versioning.DIFF_FIELDS` MINUS
# a two-field denylist, so **a field added to B5's diff becomes web-safe with no
# review at all** — on the sole Backend->Website crossing for supplied files.
#
# The B6 contract named this gap on 2026-08-01 and deliberately did not close it,
# because flipping the derivation to an allowlist is a BEHAVIOUR change: a new
# diff field would then vanish from the projection silently instead of appearing
# in it. Both of those are silent. The useful third option is to make the change
# LOUD, which is what this class does — the derivation is untouched, and the
# membership it derives from is pinned so a person has to make the call.


class TestDiffFieldsAdditionsForceAWebSafetyDecision:
    """A fail-open derivation is acceptable only while additions are impossible.

    So this makes additions impossible-in-silence rather than impossible: change
    `DIFF_FIELDS` and the suite stops, naming the decision you now owe.
    """

    #: Pinned 2026-08-01, and NOT a golden file for its own sake — each of these
    #: nine has been judged web-safe or not (the two unsafe ones live in
    #: `api._WEB_UNSAFE_DIFF_FIELDS`), and it is that JUDGEMENT the pin protects.
    #: Update this in the same PR that changes `DIFF_FIELDS`, and say in the PR
    #: body which side the new field landed on and why.
    _REVIEWED_DIFF_FIELDS = frozenset({
        "sha256", "byte_size", "original_filename", "mime", "area",
        "source_type", "origin_url", "supplied_by", "captured_at",
    })

    def test_no_diff_field_appeared_without_a_web_safety_decision(self):
        appeared = sorted(set(fv.DIFF_FIELDS) - self._REVIEWED_DIFF_FIELDS)
        assert not appeared, (
            f"{appeared} was added to file_versioning.DIFF_FIELDS and is now "
            "WEB-SAFE BY DEFAULT — it will cross to the website in every "
            "supersede diff. Decide: if it carries raw content addresses, "
            "identity, or anything vault-shaped, add it to "
            "file_read_api._WEB_UNSAFE_DIFF_FIELDS. Then add it here to record "
            "that the call was made.")

    def test_no_reviewed_diff_field_vanished(self):
        """The other direction: a removal silently shrinks what the pin covers."""
        vanished = sorted(self._REVIEWED_DIFF_FIELDS - set(fv.DIFF_FIELDS))
        assert not vanished, (
            f"{vanished} left DIFF_FIELDS but is still pinned here. Drop it from "
            "the pin in the same PR — a pin listing fields that no longer exist "
            "stops describing the real surface.")

    def test_the_unsafe_denylist_still_matches_real_fields(self):
        """Non-vacuity — the failure mode that would leak without any edit here.

        `_WEB_UNSAFE_DIFF_FIELDS` denies BY NAME. Rename the `sha256` column in
        B5 and the denylist matches nothing: the vault content-address flows
        straight into the projection, and every allowlist test above still
        passes because the *key set* is what they check. Nothing else in this
        file would notice.
        """
        stale = sorted(api._WEB_UNSAFE_DIFF_FIELDS - set(fv.DIFF_FIELDS))
        assert not stale, (
            f"file_read_api._WEB_UNSAFE_DIFF_FIELDS names {stale}, which are no "
            "longer in DIFF_FIELDS. The denylist is now denying nothing for "
            "those names — if they were renamed, whatever replaced them is "
            "crossing to the web RIGHT NOW. Re-point the denylist.")

    def test_the_derivation_is_still_a_denylist_over_the_full_diff(self):
        """Pins the RELATIONSHIP, so the two sets cannot drift apart quietly."""
        expected = tuple(
            f for f in fv.DIFF_FIELDS if f not in api._WEB_UNSAFE_DIFF_FIELDS)
        assert api.WEB_SAFE_DIFF_FIELDS == expected, (
            "WEB_SAFE_DIFF_FIELDS is no longer DIFF_FIELDS minus the denylist. "
            "If it was deliberately flipped to an explicit allowlist that is an "
            "improvement, but it is a behaviour change (new diff fields will now "
            "vanish rather than appear) — update this test and the B6 contract's "
            "known-gaps section together.")
