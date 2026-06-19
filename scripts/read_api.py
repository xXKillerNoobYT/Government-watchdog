"""Reviewer-internal read-API over the gated record store (GOV-98, Prereq-0).

Stage 1 Slice 4 Prereq-0. Contract: GOV-97 plan Part A + Part C; reuses the 1.05
publication SSOT (:mod:`publication`) and the GOV-98 concept-map registry
(:mod:`concept_map`). Source: Docs/stage1-slice4-prereq0-read-api-concept-map.md.

This is **not** an HTTP server. It is a local, read-only, stateless module (+ a
CLI) that projects the already-gated record store onto a web-safe response shape
the frontend A→E chain reads. No network listener, no public surface, Alpine-only.

Two gating principles, both reused (never re-typed):

* **Eligibility (fail-closed).** A statement is served only when BOTH gates agree
  — :func:`publication.compute_ui_status` resolves to a value in
  ``PUBLICATION_ELIGIBLE_UI_STATUSES`` AND the DB ``publication_state`` is
  ``publishable``. Default posture: not returned. ``do_not_publish`` / disputed /
  unreviewed / pending records never reach the render lane. The ui_status is
  RE-derived here (not trusted from the stored column) so a stale write cannot
  fail open. No orphan claim is served (a served statement resolves to ≥1
  evidence pointer or a segment edge). Labels travel with every record.
* **Web-safe boundary (two independent layers).** Every record crosses through
  :func:`publication.to_web_safe` (field allowlist, fail-closed), AND the whole
  assembled response is swept by :func:`assert_no_raw_paths` (a transport-level
  guard that rejects filesystem/absolute paths and raw markers while allowing
  public URLs). The second layer catches a leak even if a field were
  mis-allowlisted (GOV-34 transport-leak finding).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import completeness as comp  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import speakers as sp  # noqa: E402
import transcript_class as tc  # noqa: E402

# ---------------------------------------------------------------------------
# Transport-level raw/absolute-path guard (GOV-34). Independent of to_web_safe.
# ---------------------------------------------------------------------------

# Substrings that mark a raw/vault/private locator. A response body containing any
# of these has leaked something the field allowlist should have stripped.
RAW_PATH_MARKERS = (
    "/Users/",
    "/home/",
    "/var/",
    "/tmp/",
    "/private/",
    "/Volumes/",
    "\\Users\\",
    "Obsidian Vault",
    "Source-Data",
    "TownOfAlpine",
    "Raw-PDFs",
    ".sha256",
    "raw_local",
    "transcript_path",
    "local_note",
)

_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
# A *public* web URL — the only scheme family exempt from the raw-marker scan.
# Deliberately NOT any ``scheme://``: a ``file://`` URI is a local filesystem
# locator, not a citable web URL, so it must still be marker-scanned (a
# ``file:///Users/…TownOfAlpine/…`` provenance URI would otherwise ride across the
# boundary disguised as a "URL"). GOV-146 hardening of the GOV-34 transport guard.
_WEB_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

# URL-bearing fields the web-safe allowlist passes through by name. Their value
# must be a public web URL to cross the boundary; a non-web value (e.g. a
# ``file://`` vault URI on a locally-ingested source) is reviewer-internal
# provenance and is dropped at serialization (the source identity still travels
# via ``to_source_id``). See :func:`_strip_non_web_urls`.
_PUBLIC_URL_FIELDS = ("url", "original_url", "final_url", "archive_url")


class RawPathLeak(ValueError):
    """The response body contains a raw/absolute filesystem path (transport leak)."""


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def _is_web_url(value: str) -> bool:
    """True only for a public ``http(s)://`` URL (the marker-scan exemption)."""
    return bool(_WEB_URL_RE.match(value.strip()))


def _is_filesystem_path(value: str) -> bool:
    s = value.strip()
    if not s or _looks_like_url(s):
        return False
    # POSIX absolute path, or a Windows drive-absolute path.
    return s.startswith("/") or bool(_WIN_ABS_RE.match(s))


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def assert_no_raw_paths(body: Any) -> Any:
    """Raise :class:`RawPathLeak` if any string in ``body`` is a raw/absolute path.

    The transport-level assertion required by the acceptance criteria. Walks every
    string (keys + values, nested) and rejects a filesystem/absolute path or a
    known raw marker. Public URLs (``http(s)://…``) are allowed — only non-URL
    absolute/vault paths fail. Returns ``body`` unchanged on success so it can wrap
    a response inline.
    """
    for text in _iter_strings(body):
        if _is_filesystem_path(text):
            raise RawPathLeak(f"absolute/filesystem path in response body: {text!r}")
        # Only a public http(s) URL is exempt from the raw-marker scan. A non-web
        # scheme (notably file://) is scanned, so a vault URI can't cross disguised
        # as a URL (GOV-146 hardening).
        if not _is_web_url(text):
            for marker in RAW_PATH_MARKERS:
                if marker in text:
                    raise RawPathLeak(
                        f"raw marker {marker!r} in response body: {text!r}"
                    )
    return body


# ---------------------------------------------------------------------------
# Eligibility (reused, fail-closed) + statement serving.
# ---------------------------------------------------------------------------


def _evidence_links_for(conn: sqlite3.Connection, statement_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement' "
        "ORDER BY evidence_link_id",
        (statement_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _segment_resolves(conn: sqlite3.Connection, segment_id: str | None) -> bool:
    if not segment_id:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM transcript_segments WHERE segment_id = ?", (segment_id,)
        ).fetchone()
        is not None
    )


def _eligible_ui_status(record: dict[str, Any], links: list[dict[str, Any]]) -> str:
    """Re-derive the record's ui_status from current signals (never trust storage)."""
    source_present = bool(record.get("segment_id")) or any(
        link.get("to_source_id") for link in links
    )
    archive_present = any(link.get("archive_status") == "available" for link in links)
    return pub.compute_ui_status(
        {
            "verificationStatus": record.get("verification_status"),
            "correctionStatus": record.get("correction_status"),
            "sourceChanged": bool(record.get("source_changed")),
            "sourcePresent": source_present,
            "archivePresent": archive_present,
            "rawPreserved": False,  # statements track no raw-preserved flag; conservative
        }
    )


# ---------------------------------------------------------------------------
# Derived confidence label (GOV-283, Stage 2.07) — read-time, fail-closed.
# ---------------------------------------------------------------------------

# The most conservative (lowest-confidence) label, derived from the SSOT
# fail-closed default class. Any break in the source-class resolution chain
# collapses to this value — a statement is NEVER projected at a higher
# confidence label than its resolvable source class permits (GOV-230 §default).
_CONSERVATIVE_CONFIDENCE_LABEL = tc.CONFIDENCE_LABEL_BY_CLASS[tc.DEFAULT_TRANSCRIPT_CLASS]


def _confidence_label_for(conn: sqlite3.Connection, record: dict[str, Any]) -> str:
    """Read-time, fail-closed confidence label derived from the source class.

    Activates the dormant :data:`transcript_class.CONFIDENCE_LABEL_BY_CLASS` SSOT
    (GOV-283): resolves a served statement's source transcript class via the
    documented join ``statement -> segment_id -> transcript_segments.transcript_id
    -> transcripts.transcript_class`` and maps it through the SSOT.

    Fail-closed (mirrors :func:`speakers.safe_speaker_label` / the GOV-275
    default): every break in the chain yields :data:`_CONSERVATIVE_CONFIDENCE_LABEL`
    (the ``auto_caption_untimed`` mapping, lowest confidence) —
    no ``segment_id``; a ``segment_id`` that resolves to no segment row; a segment
    with no parent transcript; a NULL/absent ``transcript_class``; or a class with
    no SSOT label (``no_transcript``). A statement that is anchored only via a
    non-transcript ``evidence_link`` (e.g. a PDF) has no resolvable transcript
    class and so stays conservative — never upgraded. Pure function of stored
    fields: same DB -> byte-identical label. No mutation, no AI, no network.

    The derived label is reviewer-internal-safe (an enum string, no raw locator)
    and is attached as an API-envelope key AFTER ``to_web_safe`` — the raw
    ``transcript_class`` itself never crosses the web-safe boundary (it is absent
    from ``WEB_SAFE_FIELD_ALLOWLIST`` and named in ``WEB_UNSAFE_FIELDS``).
    """
    segment_id = record.get("segment_id")
    if not segment_id:
        return _CONSERVATIVE_CONFIDENCE_LABEL
    row = conn.execute(
        "SELECT t.transcript_class AS transcript_class "
        "FROM transcript_segments ts "
        "JOIN transcripts t ON t.id = ts.transcript_id "
        "WHERE ts.segment_id = ?",
        (segment_id,),
    ).fetchone()
    if row is None:
        return _CONSERVATIVE_CONFIDENCE_LABEL
    transcript_class = row["transcript_class"]
    if transcript_class is None:
        return _CONSERVATIVE_CONFIDENCE_LABEL
    # `.get(..., conservative)` makes an off-map class (e.g. `no_transcript`, which
    # produces no statement and so has no label) fail closed rather than KeyError.
    return tc.CONFIDENCE_LABEL_BY_CLASS.get(
        transcript_class, _CONSERVATIVE_CONFIDENCE_LABEL
    )


# ---------------------------------------------------------------------------
# Derived safe speaker label (GOV-290, Stage 2.07) — read-time, fail-closed.
# ---------------------------------------------------------------------------

# The most conservative, provably name-free speaker label. Every break in the
# attribution-resolution chain — and every non-safely-named row — collapses to
# this SSOT value (imported, never re-declared). A statement is NEVER projected
# with a name unless its attribution row clears the exact write-time naming gate.
_SAFE_SPEAKER_LABEL = sp.SAFE_GENERIC_LABEL


def _speaker_label_for(conn: sqlite3.Connection, record: dict[str, Any]) -> str:
    """Read-time, fail-closed SAFE speaker label for a served statement (GOV-290).

    Joins ``statements.speaker_attribution_id -> speaker_attributions`` and derives
    a renderable label that is **provably name-free unless the row is safely
    named** — exactly mirroring the write-time gate in
    :func:`speakers.safe_speaker_label`: a name may surface ONLY when
    ``attribution_state == 'attributed'`` AND ``speaker_class`` is in
    :data:`speakers.AUTO_NAMEABLE_CLASSES`. In that one gate the write invariant
    guarantees the persisted ``display_label`` is the safe ``"Name, Role"`` string,
    so it is surfaced verbatim (re-deriving the name would require the ``persons``
    join, which is the named successor — out of scope here).

    **Re-guarded — never trusts storage to fail open.** For ANY other row
    (``uncertain`` / ``unattributed`` / ``private-context`` / a non-nameable
    ``speaker_class``) the stored ``display_label`` is **never consulted** — it
    could hold a name poisoned in past the write guard — and the label is derived
    purely from ``speaker_class``: :data:`speakers.SAFE_COMMUNITY_LABEL` for an
    ``on-record-public`` speaker, else :data:`speakers.SAFE_GENERIC_LABEL`. A
    poisoned name on a non-attributed row therefore cannot reach the envelope.

    **Fail-closed:** no ``speaker_attribution_id``; an id resolving to no row; or a
    NULL/empty ``display_label`` even inside the naming gate → the conservative
    :data:`_SAFE_SPEAKER_LABEL`. Never ``None``, never a candidate name.

    Pure function of stored fields: same DB -> byte-identical label. No mutation,
    no AI, no network. Attached as an API-envelope key AFTER ``to_web_safe`` — the
    raw attribution columns (``display_label`` / ``speaker_attribution_id`` /
    ``person_id`` / ``candidate_person_id`` / ``attribution_state`` /
    ``speaker_class``) never cross the web-safe boundary.
    """
    attribution_id = record.get("speaker_attribution_id")
    if not attribution_id:
        return _SAFE_SPEAKER_LABEL
    row = conn.execute(
        "SELECT attribution_state, speaker_class, display_label "
        "FROM speaker_attributions WHERE speaker_attribution_id = ?",
        (attribution_id,),
    ).fetchone()
    if row is None:
        return _SAFE_SPEAKER_LABEL

    state = row["attribution_state"]
    speaker_class = row["speaker_class"]

    # Proven-safe naming gate (identical to speakers.safe_speaker_label): only here
    # may the persisted, write-time-computed safe label ("Name, Role") surface.
    if state == "attributed" and speaker_class in sp.AUTO_NAMEABLE_CLASSES:
        label = row["display_label"]
        if label is not None and str(label).strip():
            return str(label)
        return _SAFE_SPEAKER_LABEL  # attributed but no stored label -> fail closed

    # Any non-safely-named row: derive from speaker_class ALONE — the free-text
    # display_label is never read, so a poisoned name cannot leak.
    if speaker_class == "on-record-public":
        return sp.SAFE_COMMUNITY_LABEL
    return _SAFE_SPEAKER_LABEL


def _strip_non_web_urls(safe: dict[str, Any]) -> dict[str, Any]:
    """Drop URL-typed fields whose value is not a public ``http(s)://`` URL.

    The web-safe allowlist passes ``original_url`` / ``archive_url`` / ``final_url``
    through by name, but a locally-ingested source carries a ``file:///…vault…``
    provenance URI that is reviewer-internal, never a citable web URL. Drop those
    here so only genuinely public locators cross the boundary; the source identity
    still travels via ``to_source_id``. The (file://-aware) transport sweep in
    :func:`assert_no_raw_paths` is the backstop that proves this ran.
    """
    for field in _PUBLIC_URL_FIELDS:
        value = safe.get(field)
        if isinstance(value, str) and not _is_web_url(value):
            safe.pop(field)
    return safe


def _web_safe_evidence(link: dict[str, Any]) -> dict[str, Any]:
    """One evidence-drawer entry: web-safe allowlist + non-web URL strip."""
    return _strip_non_web_urls(pub.to_web_safe(link))


def _serialize_statement(
    conn: sqlite3.Connection, record: dict[str, Any], ui_status: str
) -> dict[str, Any]:
    """Project a served statement + its evidence drawer onto the web-safe shape.

    The flat record fields go through ``to_web_safe``; the ``evidence`` list is an
    API-envelope key holding already-web-safe drawer entries. ``ui_status`` is the
    re-derived eligible value (the label the frontend consumes verbatim).
    ``confidence_label`` (GOV-283) is the fail-closed, read-time label derived from
    the source transcript class — attached as an envelope key (not via the
    allowlist) so the raw ``transcript_class`` never crosses the boundary.
    ``speaker_label`` (GOV-290) is the fail-closed, read-time SAFE speaker label
    derived from the joined ``speaker_attributions`` row — also an envelope key, so
    the raw attribution columns never cross the boundary.
    """
    flat = dict(record)
    flat["ui_status"] = ui_status
    safe = _strip_non_web_urls(pub.to_web_safe(flat))
    safe["evidence"] = [
        _web_safe_evidence(link) for link in _evidence_links_for(conn, record["statement_id"])
    ]
    safe["confidence_label"] = _confidence_label_for(conn, record)
    safe["speaker_label"] = _speaker_label_for(conn, record)
    return safe


def published_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Web-safe served statements: eligibility-gated, orphan-dropped, labels attached.

    Both gates must agree (uiStatus eligible AND publication_state publishable),
    and the statement must not be an orphan. Everything else is silently not
    served (the fail-closed default), never served unlabeled.
    """
    served: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM statements ORDER BY statement_id"):
        record = dict(row)
        links = _evidence_links_for(conn, record["statement_id"])
        ui_status = _eligible_ui_status(record, links)
        if ui_status not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES:
            continue
        if record.get("publication_state") != "publishable":
            continue
        # No orphan claim served (1.07 §2.3): segment edge OR ≥1 evidence pointer.
        if not (_segment_resolves(conn, record.get("segment_id")) or links):
            continue
        served.append(_serialize_statement(conn, record, ui_status))
    return served


# ---------------------------------------------------------------------------
# Reviewer-internal serve (GOV-146) — reviewer-cleared, owner-publish-pending.
# ---------------------------------------------------------------------------


def _producing_run_ok(conn: sqlite3.Connection, record: dict[str, Any]) -> bool:
    """True iff the statement's producing gateway run is healthy (or none exists).

    Fail-closed mirror of the Lane-5 gate (``ai_risk_gate``): a failed/partial
    producing run blocks the row from the reviewer-internal serve, and a missing
    run row is treated as a block (never silently served). A row with no AI run
    (human/automation origin) has nothing to block on.
    """
    run_id = record.get("ai_extraction_run_id")
    if not run_id:
        return True
    try:
        run = ai.get_run(conn, run_id)
    except ValueError:
        return False  # run id present but unresolved => fail closed.
    return run.get("error_status") == "ok"


def reviewer_internal_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Web-safe reviewer-internal served statements: reviewer-cleared, NOT published.

    The vault-only reviewer view GOV-146 adds so the reviewer-internal read serve
    returns a non-empty real reviewed set even though nothing is owner-published.
    A row is served only when EVERY clause holds (fail-closed, default not served)
    — the same gate as the public lane minus the owner ``publishable`` flip:

    * ``verification_status`` is a reviewed value (a human moved it off the
      machine-extracted default);
    * a *promoting* reviewer decision exists in the Lane-5 audit ledger
      (:func:`ai_risk_gate.latest_decision` — never trust a bare status column);
    * no unresolved no-go Lane-4 risk flag remains
      (:func:`ai_risk_gate.open_risk_flags`);
    * the producing gateway run (if any) is ``error_status='ok'``;
    * the re-derived ``ui_status`` is publication-eligible (source-backed); and
    * the row is **not** orphaned (a segment edge OR ≥1 evidence pointer).

    Crucially it serves ONLY rows whose ``publication_state`` is still
    ``not_publishable`` — a publishable row belongs to :func:`published_records`,
    never here — so this view can never become a back-door public surface, and the
    public lane stays 0 until the separate owner publish gate (1.11 P8) is flipped.
    Every record is web-safe (``to_web_safe`` + non-web-URL strip); the whole body
    is transport-swept by :func:`build_response`.
    """
    served: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM statements ORDER BY statement_id"):
        record = dict(row)
        statement_id = record["statement_id"]
        # Reviewer-internal is strictly the not-yet-published set; a publishable row
        # is the public lane's, never duplicated into the reviewer view.
        if record.get("publication_state") == "publishable":
            continue
        if record.get("verification_status") not in pub.REVIEWED_VERIFICATION_STATUSES:
            continue
        decision = gate.latest_decision(conn, statement_id)
        if not decision or not decision.get("promoted"):
            continue
        if gate.open_risk_flags(conn, statement_id):
            continue
        if not _producing_run_ok(conn, record):
            continue
        links = _evidence_links_for(conn, statement_id)
        ui_status = _eligible_ui_status(record, links)
        if ui_status not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES:
            continue
        # No orphan claim served (1.07 §2.3): segment edge OR ≥1 evidence pointer.
        if not (_segment_resolves(conn, record.get("segment_id")) or links):
            continue
        served.append(_serialize_statement(conn, record, ui_status))
    return served


# ---------------------------------------------------------------------------
# Completeness-gap cards (GOV-298, Stage 2) — read-time, web-safe, never hidden.
# ---------------------------------------------------------------------------

# Conservative fallbacks for an off-SSOT gap row (drift / value poisoned in past
# the 0015 CHECK). The row is ALWAYS surfaced (a gap is never hidden — GOV-125's
# "never silently dropped" rule), but a value that is not in the frozen SSOT
# vocabulary is never trusted: it collapses to a safe placeholder instead.
_UNKNOWN_GAP_TYPE = "unknown"          # not a real gap_type — clearly flags drift
_CONSERVATIVE_GAP_SEVERITY = "warn"    # completeness.default_severity fail-closed value
_CONSERVATIVE_RESOLVED_STATUS = "open"  # never silently present a drifted row as resolved

# The exact web-safe gap-card key set. Named so the no-leak test can assert the
# projected body is a SUBSET of this — and so a future field add is a conscious,
# reviewed change, not an accidental column leak.
GAP_CARD_FIELDS = frozenset({
    "gap_id",
    "subject_id",
    "subject_node_type",
    "gap_type",
    "severity",
    "resolved_status",
    "detail",  # optional — present ONLY when it clears the read-time leak guards
})


def _safe_gap_detail(detail: str | None) -> str | None:
    """Return ``detail`` only if it clears the raw-path + structured-PII guards.

    The free-text ``detail`` is the one PII / raw-path risk on a gap row. It is
    RE-guarded at read time (defense-in-depth over the write-time
    ``completeness.record_gap`` PII guard): a leak-prone detail is OMITTED — the
    ``detail`` field is simply absent — but the gap ROW itself is still emitted
    (the gap is never hidden). Returns ``None`` to signal "omit the field".
    """
    if not detail:
        return None
    try:
        assert_no_raw_paths(detail)
    except RawPathLeak:
        return None
    try:
        cm.assert_no_pii(detail, "completeness_gap_card.detail")
    except cm.PiiGuardError:
        return None
    return detail


def completeness_gap_cards(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Web-safe completeness-gap cards (GOV-298): fail-closed, but never hidden.

    Projects the first-class ``completeness_gaps`` table (migration ``0015``,
    GOV-125) — populated with the ~90 ``no_primary_source`` Alpine meetings — onto
    the web-safe gap-card shape the Stage 2.06 frontend "completeness gap card"
    surface consumes. Today ``scripts/completeness.py`` only offers raw reads
    (``gaps_for`` / ``gap_report``) that never cross a web-safe boundary; this is
    the read surface that closes that gap.

    **Only web-safe fields are projected** — ``gap_type``, ``severity``,
    ``subject_node_type``, ``resolved_status``, and a web-safe ``subject_id`` (the
    stable node id). These are NOT routed through ``pub.to_web_safe``: that
    serializer is a *name* allowlist that would both strip the controlled-vocab
    gap fields AND (because ``source_id`` is allowlisted for ``sources`` records)
    pass the gap's internal provenance ``source_id`` straight through. Instead the
    card is built explicitly, and the internal/provenance columns
    (``source_id`` / ``detected_run_id`` / ``detected_utc``) are **never SELECTed**
    — so they cannot appear in any projected body (the strongest no-leak posture).

    **Fail-closed, but gaps are never hidden** (reconciles GOV-125 "always
    serveable" with web-safety): a ``gap_type`` / ``severity`` / ``resolved_status``
    not in the frozen SSOT vocabulary collapses to a conservative placeholder
    (:data:`_UNKNOWN_GAP_TYPE` / :data:`_CONSERVATIVE_GAP_SEVERITY` /
    :data:`_CONSERVATIVE_RESOLVED_STATUS`) rather than being trusted, and a
    leak-prone ``detail`` is omitted — but the gap ROW is ALWAYS emitted, so the
    ~90 ``no_primary_source`` rows stay countable in the projected output.

    **SSOT parity**: the accepted vocabularies are consumed from the
    :mod:`completeness` frozensets (``GAP_TYPES`` / ``SEVERITIES`` /
    ``RESOLVED_STATUSES``), never re-hardcoded — mirroring the existing parity
    discipline vs the ``0015`` CHECK. Pure function of stored fields: same DB ->
    byte-identical cards. No mutation, no AI, no network.

    The whole assembled body is transport-swept by :func:`assert_no_raw_paths` in
    :func:`build_response` (the GOV-34 backstop) — same as every other surface.
    """
    cards: list[dict[str, Any]] = []
    # Deliberately SELECT only web-safe columns: source_id / detected_run_id /
    # detected_utc are never read, so they can never reach a projected body.
    for row in conn.execute(
        "SELECT gap_id, subject_node_id, subject_node_type, gap_type, severity, "
        "detail, resolved_status FROM completeness_gaps "
        "ORDER BY gap_type, subject_node_id"
    ):
        record = dict(row)
        gap_type = (
            record["gap_type"]
            if record["gap_type"] in comp.GAP_TYPES
            else _UNKNOWN_GAP_TYPE
        )
        severity = (
            record["severity"]
            if record["severity"] in comp.SEVERITIES
            else _CONSERVATIVE_GAP_SEVERITY
        )
        resolved_status = (
            record["resolved_status"]
            if record["resolved_status"] in comp.RESOLVED_STATUSES
            else _CONSERVATIVE_RESOLVED_STATUS
        )
        card: dict[str, Any] = {
            "gap_id": record["gap_id"],
            "subject_id": record["subject_node_id"],
            "subject_node_type": record["subject_node_type"],
            "gap_type": gap_type,
            "severity": severity,
            "resolved_status": resolved_status,
        }
        detail = _safe_gap_detail(record["detail"])
        if detail is not None:
            card["detail"] = detail
        cards.append(card)
    return cards


# ---------------------------------------------------------------------------
# Plain-language label layer (owner addendum / §A.7) — web-safe projection.
# ---------------------------------------------------------------------------


def _safe_alias(row: dict[str, Any]) -> dict[str, Any]:
    """Project one alias row onto the web-safe shape (sourceRef WITHOUT local_ref).

    The MANDATORY provenance is preserved as public-citable fields — source id,
    original/archive URL, and the locator. The vault/local ref
    (``source_ref_local_ref``) is reviewer-internal and is deliberately NOT
    projected (the transport sweep re-proves no local path survives).
    """
    ref: dict[str, Any] = {"sourceId": row.get("source_ref_source_id")}
    if row.get("source_ref_original_url"):
        ref["originalUrl"] = row["source_ref_original_url"]
    if row.get("source_ref_archive_url"):
        ref["archiveUrl"] = row["source_ref_archive_url"]
    locator: dict[str, Any] = {}
    for db_key, out_key in (
        ("source_ref_timestamp_human", "timestampHuman"),
        ("source_ref_page", "page"),
        ("source_ref_section", "section"),
        ("source_ref_paragraph", "paragraph"),
        # char-span anchor (GOV-149/0017): integer offsets into the preserved
        # source text — positionally like `page`, web-safe (no path).
        ("source_ref_char_start", "charStart"),
        ("source_ref_char_end", "charEnd"),
    ):
        value = row.get(db_key)
        if value not in (None, ""):
            locator[out_key] = value
    if locator:
        ref["locator"] = locator

    alias: dict[str, Any] = {
        "term": row.get("term"),
        "aliasType": row.get("alias_type"),
        "sourceRef": ref,
    }
    if row.get("first_seen_meeting_id") is not None:
        alias["firstSeenMeetingId"] = row["first_seen_meeting_id"]
    if row.get("first_seen_date"):
        alias["firstSeenDate"] = row["first_seen_date"]
    return alias


def _with_label_layer(
    conn: sqlite3.Connection, safe_node: dict[str, Any], node_id: str, node_type: str
) -> dict[str, Any]:
    """Attach `canonicalHumanLabel` + web-safe `sourceAliases` to a node dict.

    The government/source string is never the primary label — it travels in
    `sourceAliases`, each carrying its mandatory sourceRef provenance (owner
    addendum). `canonicalHumanLabel` is the plain-English primary display.
    """
    row = conn.execute(
        f"SELECT canonical_human_label FROM {'topics' if node_type == 'topic' else 'agenda_threads'} "
        f"WHERE {'topic_id' if node_type == 'topic' else 'agenda_thread_id'} = ?",
        (node_id,),
    ).fetchone()
    safe_node["canonicalHumanLabel"] = row["canonical_human_label"] if row is not None else None
    safe_node["sourceAliases"] = [
        _safe_alias(a) for a in cm.aliases_for_node(conn, node_id, node_type)
    ]
    return safe_node


# ---------------------------------------------------------------------------
# Agenda thread (GOV-98 A.4): node + chronological members + typed lifecycle.
# ---------------------------------------------------------------------------


def agenda_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any] | None:
    """Web-safe agenda_thread: node + its members (chronological) + lifecycle edges.

    Members are the ``agenda_item``s linked to the thread via
    ``agenda_item_in_thread``, ordered by meeting date then item order (known-then
    chronology). Lifecycle edges are the typed ``Supersedes``/``Amends``/
    ``Revisits`` relations among members — never an untyped "related" (BEH-AGENDA-2).
    Returns ``None`` if the thread does not exist.
    """
    thread_row = conn.execute(
        "SELECT * FROM agenda_threads WHERE agenda_thread_id = ?", (thread_id,)
    ).fetchone()
    if thread_row is None:
        return None

    member_rows = conn.execute(
        "SELECT ai.* FROM agenda_items ai "
        "JOIN concept_edges ce ON ce.from_node_id = ai.agenda_item_id "
        "LEFT JOIN meetings m ON m.id = ai.meeting_id "
        "WHERE ce.edge_type = 'agenda_item_in_thread' AND ce.to_node_id = ? "
        "ORDER BY m.meeting_date, ai.item_order, ai.agenda_item_id",
        (thread_id,),
    ).fetchall()
    member_ids = {row["agenda_item_id"] for row in member_rows}

    lifecycle: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM concept_edges WHERE edge_type IN "
        "('agenda_item_supersedes', 'agenda_item_amends', 'agenda_item_revisits') "
        "ORDER BY edge_id"
    ):
        edge = dict(row)
        # Only edges whose endpoints are both members of this thread.
        if edge["from_node_id"] in member_ids and edge["to_node_id"] in member_ids:
            lifecycle.append(pub.to_web_safe(edge))

    return {
        "thread": _with_label_layer(
            conn, pub.to_web_safe(dict(thread_row)), thread_id, "agenda_thread"
        ),
        "members": [pub.to_web_safe(dict(row)) for row in member_rows],
        "lifecycle_edges": lifecycle,
    }


# ---------------------------------------------------------------------------
# Topic tree (GOV-98 A.3): acyclicity-validated rollup subtree + breadcrumb.
# ---------------------------------------------------------------------------


def _topic_children_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """parent_topic_id -> [child_topic_id, ...] over topic_rollup (child→parent)."""
    children: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT from_node_id, to_node_id FROM concept_edges WHERE edge_type = 'topic_rollup'"
    ):
        children.setdefault(row[1], []).append(row[0])
    return children


def _topic_row(conn: sqlite3.Connection, topic_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
    return dict(row) if row is not None else None


def _safe_topic(conn: sqlite3.Connection, topic_id: str) -> dict[str, Any]:
    """Web-safe topic node with its label layer (canonicalHumanLabel + aliases)."""
    safe = pub.to_web_safe(_topic_row(conn, topic_id) or {"topic_id": topic_id})
    return _with_label_layer(conn, safe, topic_id, "topic")


def topic_descendants(conn: sqlite3.Connection, topic_id: str) -> set[str]:
    """All descendant topic ids (inclusive of ``topic_id``) via topic_rollup.

    Powers BEH-TOPICTREE-2 rollup filtering: filtering to a parent returns items
    in that topic AND all descendants. Acyclicity is validated first so this can't
    loop forever.
    """
    cm.assert_acyclic(conn)
    children = _topic_children_map(conn)
    out: set[str] = set()
    frontier = [topic_id]
    while frontier:
        node = frontier.pop()
        if node in out:
            continue
        out.add(node)
        frontier.extend(children.get(node, ()))
    return out


def _breadcrumb(conn: sqlite3.Connection, topic_id: str) -> list[dict[str, Any]]:
    """Path from the top ancestor down to ``topic_id`` (where it sits, BEH-TOPICTREE-3)."""
    parents = cm._topic_rollup_parent_map(conn)
    chain = [topic_id]
    seen = {topic_id}
    node = topic_id
    while parents.get(node):
        parent = parents[node][0]  # tree: a single parent; first is canonical
        if parent in seen:
            break  # defensive: assert_acyclic should already have rejected this
        chain.append(parent)
        seen.add(parent)
        node = parent
    chain.reverse()  # top ancestor first
    return [_safe_topic(conn, tid) for tid in chain]


def _subtree(conn: sqlite3.Connection, topic_id: str, children: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "topic": _safe_topic(conn, topic_id),
        "children": [
            _subtree(conn, child, children) for child in sorted(children.get(topic_id, ()))
        ],
    }


def topic_tree(conn: sqlite3.Connection, root_topic_id: str) -> dict[str, Any]:
    """Web-safe topic_rollup subtree rooted at ``root_topic_id`` + breadcrumb.

    Validates acyclicity BEFORE building (BEH-TOPICTREE-4): raises
    :class:`concept_map.TopicTreeCycleError` rather than serving a broken tree.
    """
    cm.assert_acyclic(conn)
    children = _topic_children_map(conn)
    return {
        "root": _safe_topic(conn, root_topic_id),
        "breadcrumb": _breadcrumb(conn, root_topic_id),
        "tree": _subtree(conn, root_topic_id, children),
    }


# ---------------------------------------------------------------------------
# Response assembly (projects + transport-asserts the whole body).
# ---------------------------------------------------------------------------


def build_response(
    conn: sqlite3.Connection,
    *,
    thread_id: str | None = None,
    topic_root: str | None = None,
    include_records: bool = True,
    include_reviewer_internal: bool = False,
    include_completeness_gaps: bool = False,
) -> dict[str, Any]:
    """Assemble the reviewer-internal read response and transport-assert it.

    Every leaf record is already web-safe (projected via ``to_web_safe``); the
    whole assembled body is then swept by :func:`assert_no_raw_paths` before
    return so a leak fails LOUDLY at the boundary, not silently downstream.

    ``records`` carries the owner-published set (:func:`published_records`).
    ``reviewer_internal_records`` (opt-in via ``include_reviewer_internal``) carries
    the reviewer-cleared-but-owner-publish-pending set (GOV-146) the frontend
    renders behind the beta gate — kept under a distinct key so the two states can
    never be conflated.

    ``completeness_gaps`` (opt-in via ``include_completeness_gaps``) carries the
    web-safe gap cards (GOV-298) the Stage 2.06 "completeness gap card" surface
    renders — the ~90 ``no_primary_source`` Alpine meetings, fail-closed but never
    hidden — again under a distinct key, with no internal-provenance columns.
    """
    response: dict[str, Any] = {"scope": "alpine", "access": "reviewer_internal"}
    if include_records:
        response["records"] = published_records(conn)
    if include_reviewer_internal:
        response["reviewer_internal_records"] = reviewer_internal_records(conn)
    if include_completeness_gaps:
        response["completeness_gaps"] = completeness_gap_cards(conn)
    if thread_id is not None:
        response["agenda_thread"] = agenda_thread(conn, thread_id)
    if topic_root is not None:
        response["topic_tree"] = topic_tree(conn, topic_root)
    return assert_no_raw_paths(response)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reviewer-internal read-API (GOV-98).")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--thread", dest="thread_id", default=None)
    parser.add_argument("--topic-root", dest="topic_root", default=None)
    parser.add_argument("--no-records", dest="include_records", action="store_false")
    parser.add_argument(
        "--reviewer-internal",
        dest="include_reviewer_internal",
        action="store_true",
        help="include the reviewer-cleared, owner-publish-pending set (GOV-146)",
    )
    parser.add_argument(
        "--completeness-gaps",
        dest="include_completeness_gaps",
        action="store_true",
        help="include the web-safe completeness-gap cards (GOV-298)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        response = build_response(
            conn,
            thread_id=args.thread_id,
            topic_root=args.topic_root,
            include_records=args.include_records,
            include_reviewer_internal=args.include_reviewer_internal,
            include_completeness_gaps=args.include_completeness_gaps,
        )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
