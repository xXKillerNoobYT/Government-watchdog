"""Web-safe supplied-file read projection (GOV-1579 / B6, GOV-1566 chain).

Parent: GOV-1566 "New files". B1 (:mod:`raw_object_store`) stores the raw bytes,
B2 (:mod:`file_records`) the canonical record + provenance, B4
(:mod:`file_linkage`) the file->subject links, B5 (:mod:`file_versioning`) the
supersede/before-after history. **B6 (this module)** is the *sole Backend->Website
crossing* for supplied files: a read-only projection that returns ONLY files a
reviewer has cleared to ``review_state='web_safe'``, with every raw/private
locator and every uploader-identity field stripped **server-side** — never
renderer-only.

It is the supplied-file sibling of :mod:`read_api` and follows it exactly: a
local, read-only, stateless module (+ a CLI) whose output the website build bakes
into a web artifact (see :mod:`export_web_artifact`). No network listener, no
public runtime surface, Alpine-only.

Three fail-closed leak defenses, in depth:

* **State gate (fail-closed).** Only rows with ``review_state == 'web_safe'`` are
  projected — a re-checked SQL filter. A ``pending`` / ``reviewing`` / ``held`` /
  ``rejected`` file is NEVER served, and a before/after supersede view is emitted
  only when BOTH the prior and the new version are ``web_safe`` (an unreviewed
  version's metadata never crosses, even as the "before" side of a diff).
* **Structural allowlist (fail-closed).** The two raw/PII exposure fields on a
  supplied file — ``sha256`` (the B1 *vault content-address*: the key that
  decrypts the raw bytes, i.e. the ``raw_sha256`` class the rulebook forbids on
  any web surface) and ``supplied_by`` (the uploader's authenticated email, PII)
  — are **never SELECTed**, so they cannot appear in any projected body. Each
  projected card's key set is asserted to be a subset of the frozen
  :data:`WEB_SAFE_FILE_FIELDS`; a future column added to the SELECT would trip
  this rather than leak. The before/after diff is RECOMPUTED from the two records
  (never read from the stored ``diff_json``, which carries ``sha256`` /
  ``supplied_by``) and filtered to :data:`WEB_SAFE_DIFF_FIELDS`.
* **Transport sweep (backstop).** The whole assembled body is walked by
  :func:`read_api.assert_no_raw_paths` (the canonical GOV-34 transport guard,
  reused verbatim — never re-typed) before return, so any absolute/vault path or
  raw marker fails LOUDLY at the boundary even if a field were mis-allowlisted.
  Public ``http(s)://`` locators are the only exemption; a ``file://`` vault URI
  is scanned and dropped.

Pure function of stored fields: same DB -> byte-identical projection. No mutation,
no AI, no network.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import file_linkage as fl  # noqa: E402
import file_records as fr  # noqa: E402
import file_versioning as fv  # noqa: E402
# The canonical GOV-34 transport guard + web-URL test are REUSED from read_api
# (never re-typed — a second divergent copy of the marker list is the real risk).
import read_api  # noqa: E402

# ---------------------------------------------------------------------------
# Frozen web-safe field allowlists (structural, fail-closed).
# ---------------------------------------------------------------------------

#: The only review_state whose files cross to the website (fail-closed).
WEB_SAFE_STATE = "web_safe"

#: The exact web-safe key set of a projected file card. Pinned by a test so a
#: future field add is a conscious, reviewed change — never an accidental column
#: leak. Deliberately EXCLUDES ``sha256`` (B1 vault content-address / raw_sha256
#: class) and ``supplied_by`` (uploader email, PII) and ``created_at`` (internal
#: ingest time); the civic date travels as ``captured_at``. ``links`` is an
#: API-envelope key holding already-web-safe linkage.
WEB_SAFE_FILE_FIELDS = frozenset({
    "file_id",
    "area",
    "source_type",
    "original_filename",
    "captured_at",
    "origin_url",          # present ONLY when a public http(s) URL (else dropped)
    "mime",
    "byte_size",
    "review_state",
    "version_group_id",
    "supersedes_id",
    "links",
})

#: The web-safe columns SELECTed from ``supplied_files``. ``sha256`` and
#: ``supplied_by`` are POINTEDLY absent — the strongest no-leak posture: a value
#: never read can never be projected (mirrors read_api.completeness_gap_cards).
_WEB_SAFE_FILE_COLUMNS = (
    "file_id",
    "area",
    "source_type",
    "original_filename",
    "captured_at",
    "origin_url",
    "mime",
    "byte_size",
    "review_state",
    "version_group_id",
    "supersedes_id",
)

#: The web-safe key set of one linkage entry (F2 source drawer). ``linked_by``
#: (operator identity) and ``linked_at`` / ``link_id`` (internal provenance) are
#: excluded — the drawer needs only WHAT a reviewed file is a source for.
WEB_SAFE_LINK_FIELDS = frozenset({
    "subject_node_type",
    "subject_node_id",
    "is_primary_source",
})

#: The before/after diff fields safe to surface (F3). Derived from B5's
#: :data:`file_versioning.DIFF_FIELDS` MINUS the two raw/PII fields — so a
#: superseded file's changed size / filename / type is shown, but never its vault
#: hash or who supplied it. ``content_changed`` (a bool, no hash) still signals
#: that the bytes changed.
WEB_SAFE_DIFF_FIELDS = tuple(
    f for f in fv.DIFF_FIELDS if f not in {"sha256", "supplied_by"}
)

#: URL-typed diff fields that must be a public web URL to cross (else redacted).
_URL_DIFF_FIELDS = frozenset({"origin_url"})


class FieldLeak(ValueError):
    """A projected card carried a key outside its frozen web-safe allowlist."""


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _web_url_or_none(value: Any) -> str | None:
    """Return ``value`` only if it is a public ``http(s)://`` URL, else ``None``.

    A locally-ingested source can carry a ``file:///…vault…`` provenance URI in
    ``origin_url``; that is reviewer-internal and must never cross. Reuses
    :func:`read_api._is_web_url` (the same predicate the transport sweep exempts).
    """
    if isinstance(value, str) and read_api._is_web_url(value):
        return value
    return None


def _web_safe_links(conn: sqlite3.Connection, file_id: str) -> list[dict[str, Any]]:
    """Web-safe linkage for a file (F2): subject + primary-source flag only."""
    out: list[dict[str, Any]] = []
    for link in fl.links_for_file(conn, file_id):
        entry = {
            "subject_node_type": link.subject_node_type,
            "subject_node_id": link.subject_node_id,
            "is_primary_source": link.is_primary_source,
        }
        assert set(entry) <= WEB_SAFE_LINK_FIELDS  # structural, defense-in-depth
        out.append(entry)
    return out


def _assert_file_keys(card: dict[str, Any]) -> dict[str, Any]:
    """Fail-closed: a card's keys must be a subset of the web-safe allowlist."""
    extra = set(card) - WEB_SAFE_FILE_FIELDS
    if extra:
        raise FieldLeak(f"non-web-safe field(s) in supplied-file card: {sorted(extra)!r}")
    return card


def _project_file(conn: sqlite3.Connection, rec: dict[str, Any]) -> dict[str, Any]:
    """Project one ``supplied_files`` row (already SELECTed web-safe) to a card.

    ``origin_url`` is kept only when a public web URL; ``links`` is attached as an
    envelope key. The key-set assertion is the structural backstop.
    """
    card = {k: rec[k] for k in _WEB_SAFE_FILE_COLUMNS}
    origin_url = _web_url_or_none(card.get("origin_url"))
    if origin_url is None:
        card.pop("origin_url", None)
    else:
        card["origin_url"] = origin_url
    card["links"] = _web_safe_links(conn, rec["file_id"])
    return _assert_file_keys(card)


# ---------------------------------------------------------------------------
# Web-safe file list (F2 source drawer).
# ---------------------------------------------------------------------------

def web_safe_files(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every ``review_state='web_safe'`` supplied file, projected web-safe.

    Fail-closed: the SQL filters to ``web_safe`` AND each row is re-checked before
    projection, so a state flipped mid-read (or a mis-typed query) can never leak
    an unreviewed file. Ordered deterministically (version group, then ingest
    order, then id). ``sha256`` / ``supplied_by`` are never selected.
    """
    rows = conn.execute(
        f"SELECT {', '.join(_WEB_SAFE_FILE_COLUMNS)} FROM supplied_files "
        "WHERE review_state = ? ORDER BY version_group_id, created_at, file_id",
        (WEB_SAFE_STATE,),
    )
    served: list[dict[str, Any]] = []
    for row in rows:
        rec = dict(row)
        if rec.get("review_state") != WEB_SAFE_STATE:  # re-check, never trust storage
            continue
        served.append(_project_file(conn, rec))
    return served


# ---------------------------------------------------------------------------
# Before/after supersede views (F3).
# ---------------------------------------------------------------------------

def _web_safe_diff(prior: fr.FileRecord, new: fr.FileRecord) -> dict[str, Any]:
    """Recompute a before/after diff and strip it to the web-safe field subset.

    Recomputed from the two records via :func:`file_versioning.compute_before_after`
    (never read from the stored ``diff_json``, which carries ``sha256`` /
    ``supplied_by``). Only :data:`WEB_SAFE_DIFF_FIELDS` survive; a URL-typed field's
    before/after values are redacted to a public web URL (else ``None``).
    ``content_changed`` (a bool derived from the hash, not the hash) is preserved.
    """
    full = fv.compute_before_after(prior, new)
    changed: dict[str, Any] = {}
    for field, ba in full["changed"].items():
        if field not in WEB_SAFE_DIFF_FIELDS:
            continue
        if field in _URL_DIFF_FIELDS:
            changed[field] = {
                "before": _web_url_or_none(ba["before"]),
                "after": _web_url_or_none(ba["after"]),
            }
        else:
            changed[field] = {"before": ba["before"], "after": ba["after"]}
    unchanged = [f for f in full["unchanged"] if f in WEB_SAFE_DIFF_FIELDS]
    return {
        "changed": changed,
        "unchanged": unchanged,
        "content_changed": full["content_changed"],
    }


def supersede_views(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Web-safe before/after views for supersedes where BOTH versions are web_safe.

    Fail-closed: a supersede is projected only when the prior AND the new record
    both resolve and are ``review_state='web_safe'`` — so no unreviewed version's
    metadata crosses even as the "before" side. The diff is web-safe
    (:func:`_web_safe_diff`); only synthetic ids (file / version-group) travel.
    Ordered deterministically. Reads ``event_id`` / ``version_group_id`` /
    ``superseded_file_id`` / ``new_file_id`` only — never ``diff_json`` /
    ``superseded_by``.
    """
    views: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT version_group_id, superseded_file_id, new_file_id "
        "FROM supplied_file_supersede_events ORDER BY created_at, event_id"
    ):
        prior = fr.get_file_record(conn, row["superseded_file_id"])
        new = fr.get_file_record(conn, row["new_file_id"])
        if prior is None or new is None:
            continue
        if prior.review_state != WEB_SAFE_STATE or new.review_state != WEB_SAFE_STATE:
            continue  # fail-closed: never surface an unreviewed version's before/after
        views.append({
            "version_group_id": row["version_group_id"],
            "superseded_file_id": row["superseded_file_id"],
            "new_file_id": row["new_file_id"],
            "diff": _web_safe_diff(prior, new),
        })
    return views


# ---------------------------------------------------------------------------
# Response assembly (projects + transport-asserts the whole body).
# ---------------------------------------------------------------------------

def build_files_response(
    conn: sqlite3.Connection,
    *,
    include_supersede_views: bool = True,
) -> dict[str, Any]:
    """Assemble the web-safe supplied-file projection and transport-assert it.

    Every leaf is already web-safe (structural allowlist); the whole body is then
    swept by :func:`read_api.assert_no_raw_paths` before return so any leak fails
    LOUDLY at the boundary. ``dataOrigin`` is the static ``reviewed_snapshot`` —
    this projection, by construction, only ever serves reviewer-cleared files.
    (``asOf`` / ``generatedAt`` are stamped by the build/export layer, kept OUT
    here so the projection stays byte-deterministic for the round-trip tests.)
    """
    response: dict[str, Any] = {
        "scope": "alpine",
        "access": "web_safe",
        "dataOrigin": "reviewed_snapshot",
        "files": web_safe_files(conn),
    }
    if include_supersede_views:
        response["supersede_views"] = supersede_views(conn)
    return read_api.assert_no_raw_paths(response)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Web-safe supplied-file read projection (GOV-1579 / B6)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--no-supersede-views",
        dest="include_supersede_views",
        action="store_false",
        help="omit the before/after supersede views (F3)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        response = build_files_response(
            conn, include_supersede_views=args.include_supersede_views
        )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
