"""GOV-698 — agenda-anchoring batch CLI (Option-A pilot, anchoring leg 2 of 6).

GOV-710 (scale leg 2 of 6) parameterizes the scope: ``--meeting-id`` /
``--agenda-doc-id`` / ``--transcript-id`` generalize the pinned pilot constants so
the anchoring lane scales across the local TOA corpus (GOV-709 §3). Defaults
reproduce the pilot verbatim; every fail-closed cross-check is kept and generalized
(doc_date == meeting date == transcript date), and the agenda-item id is keyed on
the meeting + document. The frozen surfaces are untouched.

A deterministic, fail-closed batch tool that (a) extracts top-level agenda items
from ONE meeting's own revised agenda document and (b) anchors the already
``reviewed_source_linked`` statements of that transcript to those agenda items using
a **reviewer-confirmed timestamp-range table**. Zero AI, zero network, zero credits.

Scope boundary (Isaac card ``7b606128`` / GOV-652, CTO spec GOV-697): the single
2026-06-23 Town of Alpine council meeting (``meetings.id = 129``), agenda source of
record ``documents.id = 137`` (the operative REVISED agenda), the closed batch of
50 statements ``stmt:localdoc-142:seg-0000 … seg-0049``. Nothing else is eligible;
scale-up beyond 50 is NOT authorised. Everything stays ``not_publishable``,
reviewer-internal; the registry + raw corpus stay local — only code + tests + the
additive migration ``0020_agenda_item_provenance.sql`` go to GitHub.

It is an *orchestrator over its own additive tables*, never a parallel write path
onto a frozen surface. ``read_api`` / ``ai_risk_gate`` / ``stage5_agenda_board`` are
NOT imported for mutation and stay byte-0-diff; anchoring changes no verification
status, so ``reviewer_decisions`` / ``ai_*`` are never written.

Two modes (GOV-697 spec §4):

* ``propose`` — registry read-only. Re-verifies the agenda document's ``sha256``
  against its raw file (source-changed = fail-closed abort), extracts the agenda
  items via the strict-increment marker grammar (§1), lists the 50 target
  statements with ``timestamp_seconds``, and emits a **local/gitignored manifest**
  (agenda items + provenance + a *null* range table, statement inventory, counts,
  a DB fingerprint). The manifest is the packet Isaac reviews and fills ranges into
  on the leg-4 board card. Never pushed to GitHub.

* ``apply`` — reads an **accepted** manifest (ranges filled by the reviewer) and
  anchors statements deterministically by pure timestamp containment. **Dry-run is
  the default**; ``--commit`` is required to write. Fail-closed + atomic.

Hard invariants (GOV-697 §2, §3):

* reviewer identity is ``reviewer:isaac`` ONLY; empty / automation / AI ids are
  rejected before any DB call.
* statements are touched by a WRITE-ONCE narrow UPDATE of ``agenda_item_id`` only
  (``… WHERE statement_id = ? AND agenda_item_id IS NULL``); every other column is
  asserted identical pre/post (drift => abort + rollback). Speaker fields are never
  touched — no name is better than a wrong name.
* the range table is validated half-open ``[start, end)``, non-overlapping,
  monotonic in ``item_order``; a violation is a fail-closed abort.
* a statement whose timestamp falls in no confirmed range stays ``NULL`` and is
  reported as ``unanchored_remaining`` — never nearest-neighboured, never guessed.
* re-run idempotent: already anchored to the SAME item => skipped; to a DIFFERENT
  item => hard error (re-anchoring is a new reviewer decision, out of scope).
* agenda_items inserts are idempotent on the deterministic ``agenda_item_id`` and
  re-verified against the manifest (source drift => abort).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import db  # noqa: E402

# --- enforced pilot constants (GOV-697 spec, verified read-only 2026-07-14) --

#: The single pilot meeting: 2026-06-23 Town of Alpine council.
PILOT_MEETING_DATE = "2026-06-23"
PILOT_MEETING_ID = 129

#: Agenda source of record: the operative REVISED agenda txt (GOV-697 §1). The
#: original agenda (135), the PDFs (134/136) and the revision-diff artifact (133)
#: are NOT extraction inputs. Pinned by id per the CTO spec; its doc_date/doc_type
#: are re-asserted at runtime so a mis-pinned id fails closed rather than silently
#: extracting the wrong document.
AGENDA_DOC_ID = 137

#: The single corpus ``sources`` row; keeps the existing agenda_doc_source_id FK
#: satisfied. Exact-document provenance lives in the additive 0020 columns.
AGENDA_DOC_SOURCE_ID = "alpine_local_corpus"

#: The ONLY reviewer identity authorised for this pilot (owner card 7b606128).
REVIEWER_ID = "reviewer:isaac"

#: Empty / automation / AI ids can never anchor (defence-in-depth).
FORBIDDEN_REVIEWER_IDS = frozenset(
    {"", "automation", "ai", "system", "reviewer:automation", "reviewer:ai"}
)

#: The closed target batch: statements already promoted to this status in GOV-650.
TARGET_VERIFICATION_STATUS = "reviewed_source_linked"

#: Human-review batch ceiling (no scale-up authorised).
MAX_BATCH = 50

MANIFEST_VERSION = 1
MANIFEST_KIND = "agenda_anchor_batch"

#: Immutable statement columns re-asserted identical pre/post an anchor UPDATE.
_IMMUTABLE_STATEMENT_COLUMNS = (
    "verification_status",
    "publication_state",
    "review_state",
    "statement_text",
    "speaker_attribution_id",
    "layer",
    "produced_by",
)

#: Exit codes.
EXIT_OK = 0
EXIT_REFUSED = 1   # fail-closed refusal (reviewer / source-changed / re-anchor / range)
EXIT_SCOPE = 2     # scope-gate / batch-size violation (no bypass)


class AnchorScopeError(RuntimeError):
    """A manifest referenced out-of-scope rows, an oversized batch, or bad ranges."""


class AnchorRefusedError(RuntimeError):
    """A fail-closed refusal: bad reviewer, source drift, or a re-anchor attempt."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --- 1) deterministic agenda-item extraction (GOV-697 §1) -------------------

#: A top-level marker: a number + '.' either alone on its line (dominant layout)
#: or inline with a title (e.g. ``10. EXECUTIVE SESSION``). ``rest`` is the inline
#: remainder when present. Nested budget clauses (``1. - The use of …``) also match
#: this shape; the strict-increment rule below is what excludes them.
_MARKER_RE = re.compile(r"^\s*(?P<num>\d+)\.(?:\s+(?P<rest>\S.*?))?\s*$")

_WS_RE = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def extract_agenda_items(text: str) -> list[dict[str, Any]]:
    """Extract top-level agenda items by the strict-increment marker grammar.

    A marker line is accepted **only** if its number equals the previously accepted
    number + 1, starting at 1. This mechanically excludes nested enumerations (which
    restart at 1 mid-document) without any AI, fuzzy matching, or inference.

    * Inline marker (``10. EXECUTIVE SESSION``): title = the inline remainder;
      citation span = the marker line alone.
    * Marker-alone (``5.`` then a blank then ``PUBLIC HEARING``): title = the FIRST
      non-empty line after the marker (whitespace-collapsed); citation span = the
      marker line through that title line. Wrapped ``a./b./c.`` sub-item detail is
      NOT consumed — top-level items only.

    Returns a list of dicts with 1-indexed ``line_start`` / ``line_end`` (inclusive)
    and ``citation_target`` = ``"lines:START-END"``. Raises AnchorScopeError if an
    accepted marker has no derivable title (malformed source).
    """
    lines = text.split("\n")
    n = len(lines)
    items: list[dict[str, Any]] = []
    expected = 1
    i = 0
    while i < n:
        m = _MARKER_RE.match(lines[i])
        if not m or int(m.group("num")) != expected:
            i += 1
            continue
        marker_line = i + 1  # 1-indexed
        rest = m.group("rest")
        if rest and rest.strip():
            title = _collapse_ws(rest)
            line_end = marker_line
            i += 1
        else:
            # Title = first non-empty line after the marker.
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n:
                raise AnchorScopeError(
                    f"agenda item {expected} (line {marker_line}) has no title line"
                )
            title = _collapse_ws(lines[j])
            line_end = j + 1  # 1-indexed
            i = j + 1
        items.append(
            {
                "item_order": expected,
                "title": title,
                "line_start": marker_line,
                "line_end": line_end,
                "citation_target": f"lines:{marker_line}-{line_end}",
            }
        )
        expected += 1
    return items


def agenda_item_id_for(
    order: int,
    *,
    meeting_id: int = PILOT_MEETING_ID,
    agenda_doc_id: int = AGENDA_DOC_ID,
) -> str:
    """Deterministic, re-run-stable agenda_item_id (the natural idempotency key).

    Keyed on ``meeting_id`` + ``agenda_doc_id`` so it stays collision-free per
    meeting/document as the chain scales beyond the pilot (GOV-709 §3). Defaults
    reproduce the pilot ids ``agi:m129:doc-137:item-NN`` verbatim.
    """
    return f"agi:m{meeting_id}:doc-{agenda_doc_id}:item-{order:02d}"


def _resolve_anchor_scope(
    conn: sqlite3.Connection,
    *,
    meeting_id: int,
    transcript_id: int | None,
) -> dict[str, Any]:
    """Resolve the meeting date and fail-closed cross-check the transcript.

    The meeting identity is taken from an explicit ``meeting_id`` (the timed
    transcripts do not back-reference their meeting row — GOV-709 §1a). Its
    ``meetings.meeting_date`` becomes the single date every other source must agree
    with: a supplied ``transcript_id`` must share it (else a mis-paired transcript
    is refused rather than anchored to the wrong meeting).
    """
    mrow = conn.execute(
        "SELECT meeting_date FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if mrow is None:
        raise AnchorRefusedError(f"meeting id {meeting_id} not in registry")
    meeting_date = mrow["meeting_date"]
    if transcript_id is not None:
        trow = conn.execute(
            "SELECT meeting_date FROM transcripts WHERE id = ?", (transcript_id,)
        ).fetchone()
        if trow is None:
            raise AnchorRefusedError(f"transcript id {transcript_id} not in registry")
        if trow["meeting_date"] != meeting_date:
            raise AnchorRefusedError(
                f"transcript {transcript_id} is dated {trow['meeting_date']!r} but "
                f"meeting {meeting_id} is dated {meeting_date!r} (fail-closed cross-check)"
            )
    return {"meeting_date": meeting_date}


# --- source-of-record loading + integrity (GOV-697 §1) ----------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_agenda_document(
    conn: sqlite3.Connection,
    *,
    corpus_root: Path,
    agenda_doc_id: int = AGENDA_DOC_ID,
    meeting_date: str = PILOT_MEETING_DATE,
) -> dict[str, Any]:
    """Read the agenda document row and re-verify its raw file sha256.

    Fail-closed: a wrong doc_date/doc_type on the id, a missing raw file, or a
    sha256 mismatch (source_changed) all abort — the extractor never parses an
    unverified or drifted source. ``doc_date`` must equal ``meeting_date`` (which
    the caller has already reconciled against the meeting + transcript rows, so a
    doc from a different meeting refuses here). ``corpus_root`` resolves
    ``local_path`` (relative to the ops-clone repo root, not ``Database/``).
    """
    row = conn.execute(
        "SELECT id, title, doc_type, doc_date, local_path, sha256 "
        "FROM documents WHERE id = ?",
        (agenda_doc_id,),
    ).fetchone()
    if row is None:
        raise AnchorRefusedError(f"agenda document id {agenda_doc_id} not in registry")
    doc = dict(row)
    if doc["doc_date"] != meeting_date or doc["doc_type"] != "agenda":
        raise AnchorRefusedError(
            f"agenda doc {agenda_doc_id} is not the {meeting_date} agenda "
            f"(doc_date={doc['doc_date']!r}, doc_type={doc['doc_type']!r})"
        )
    raw_path = (corpus_root / doc["local_path"]).resolve()
    if not raw_path.is_file():
        raise AnchorRefusedError(f"agenda raw file missing: {raw_path}")
    actual = _sha256_file(raw_path)
    if actual != doc["sha256"]:
        raise AnchorRefusedError(
            f"agenda doc {AGENDA_DOC_ID} sha256 mismatch (source_changed): "
            f"stored {doc['sha256'][:16]}… != file {actual[:16]}…"
        )
    doc["_raw_path"] = raw_path
    doc["_text"] = raw_path.read_text(encoding="utf-8")
    return doc


# --- target-statement selector (GOV-697 §2 — the closed 50-row batch) -------

def target_statements(
    conn: sqlite3.Connection, *, transcript_id: int | None = None
) -> list[dict[str, Any]]:
    """The ``reviewed_source_linked`` statements, in ``timestamp_seconds`` order.

    Joined to ``transcript_segments`` for the timestamp used by the containment
    rule. ``transcript_id`` scopes the batch to ONE transcript's promoted rows (the
    scale selector — without it, several promoted meetings would collapse into one
    anchor batch, GOV-709 §3); the pilot default (``None``) is the whole reviewed
    set, exactly as before. Deterministic ordering (timestamp then statement_id)
    keeps the manifest byte-stable across re-proposals.
    """
    where = "s.verification_status = ?"
    params: list[Any] = [TARGET_VERIFICATION_STATUS]
    if transcript_id is not None:
        where += " AND ts.transcript_id = ?"
        params.append(transcript_id)
    rows = conn.execute(
        "SELECT s.statement_id, s.segment_id, ts.timestamp_seconds, ts.timestamp_human "
        "FROM statements s "
        "JOIN transcript_segments ts ON ts.segment_id = s.segment_id "
        f"WHERE {where} "
        "ORDER BY ts.timestamp_seconds, s.statement_id",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


# --- propose (registry read-only; emits a local/gitignored manifest) --------

def build_manifest(
    conn: sqlite3.Connection,
    *,
    corpus_root: Path,
    meeting_id: int = PILOT_MEETING_ID,
    agenda_doc_id: int = AGENDA_DOC_ID,
    transcript_id: int | None = None,
) -> dict[str, Any]:
    """Build the anchoring review packet. Read-only: touches no write path.

    Emits the extracted agenda items (each with a *null* ``range_start_s`` /
    ``range_end_s`` for the reviewer to fill), optional ``hint_only`` substring
    matches (never applied), the statement inventory, counts, and a DB fingerprint
    the apply step re-checks for drift.

    ``meeting_id`` / ``agenda_doc_id`` / ``transcript_id`` select the scope and are
    recorded in the manifest; the meeting date they reconcile to gates the agenda
    doc, and the agenda-item ids are keyed on the meeting + document (GOV-709 §3).
    Defaults reproduce the pilot packet verbatim.
    """
    scope = _resolve_anchor_scope(
        conn, meeting_id=meeting_id, transcript_id=transcript_id
    )
    meeting_date = scope["meeting_date"]
    doc = load_agenda_document(
        conn, corpus_root=corpus_root,
        agenda_doc_id=agenda_doc_id, meeting_date=meeting_date,
    )
    raw_items = extract_agenda_items(doc["_text"])
    statements = target_statements(conn, transcript_id=transcript_id)

    # Optional aid: exact case/whitespace-folded substring hits of an item title in
    # a segment's text. Hints are advisory only and are NEVER applied by `apply`.
    seg_text = {
        r["segment_id"]: (r["segment_text"] or "")
        for r in conn.execute(
            "SELECT segment_id, segment_text FROM transcript_segments"
        ).fetchall()
    }
    folded = {sid: _collapse_ws(t).casefold() for sid, t in seg_text.items()}

    agenda_items: list[dict[str, Any]] = []
    for it in raw_items:
        needle = _collapse_ws(it["title"]).casefold()
        hints = [
            {"segment_id": s["segment_id"], "timestamp_seconds": s["timestamp_seconds"]}
            for s in statements
            if needle and needle in folded.get(s["segment_id"], "")
        ]
        agenda_items.append(
            {
                "agenda_item_id": agenda_item_id_for(
                    it["item_order"], meeting_id=meeting_id, agenda_doc_id=agenda_doc_id
                ),
                "item_order": it["item_order"],
                "title": it["title"],
                "source_document_id": agenda_doc_id,
                "citation_target": it["citation_target"],
                # Reviewer fills these on the leg-4 card; propose proposes NO ranges.
                "range_start_s": None,
                "range_end_s": None,
                "hint_only": hints,
            }
        )

    stmt_inventory = [
        {
            "statement_id": s["statement_id"],
            "segment_id": s["segment_id"],
            "timestamp_seconds": s["timestamp_seconds"],
            "timestamp_human": s["timestamp_human"],
        }
        for s in statements
    ]

    return {
        "manifest_version": MANIFEST_VERSION,
        "kind": MANIFEST_KIND,
        "meeting_id": meeting_id,
        "meeting_date": meeting_date,
        "transcript_id": transcript_id,
        "reviewer_id": REVIEWER_ID,
        "to_verification_status": TARGET_VERIFICATION_STATUS,
        "agenda_doc": {
            "document_id": doc["id"],
            "sha256": doc["sha256"],
            "source_id": AGENDA_DOC_SOURCE_ID,
            "local_path": doc["local_path"],
        },
        "agenda_items": agenda_items,
        "statements": stmt_inventory,
        "counts": {"agenda_items": len(agenda_items), "statements": len(stmt_inventory)},
        "db_fingerprint": {
            "reviewed_statement_count": len(stmt_inventory),
            "agenda_doc_sha256": doc["sha256"],
            "statement_ids_sha256": _statement_ids_fingerprint(stmt_inventory),
        },
    }


def _statement_ids_fingerprint(stmt_inventory: list[dict[str, Any]]) -> str:
    ids = sorted(s["statement_id"] for s in stmt_inventory)
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def canonical_manifest_sha256(manifest: dict[str, Any]) -> str:
    """Stable sha256 over the canonical JSON of the manifest (the card-bound hash).

    The reviewer-filled ranges are part of the hash, so the accepted-with-ranges
    manifest is what leg 4 binds to the confirmation card.
    """
    canonical = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_manifest(manifest: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out_path


# --- range-table validation (GOV-697 §2) ------------------------------------

def validated_ranges(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the reviewer-filled ranges, validated. Items with a null range are
    skipped (they simply anchor nothing).

    Enforced: each provided range is half-open ``[start, end)`` with
    ``start < end``; ranges are non-overlapping and strictly monotonic in
    ``item_order``. Any violation is a fail-closed AnchorScopeError.
    """
    ranged: list[dict[str, Any]] = []
    for it in manifest.get("agenda_items", []):
        start = it.get("range_start_s")
        end = it.get("range_end_s")
        if start is None and end is None:
            continue
        if start is None or end is None:
            raise AnchorScopeError(
                f"agenda item {it.get('agenda_item_id')} has a half-filled range "
                f"({start!r}, {end!r}); fill both or neither"
            )
        if not isinstance(start, int) or not isinstance(end, int):
            raise AnchorScopeError(
                f"agenda item {it.get('agenda_item_id')} range must be integer seconds"
            )
        if start >= end:
            raise AnchorScopeError(
                f"agenda item {it.get('agenda_item_id')} range [{start}, {end}) is empty"
            )
        ranged.append(
            {
                "agenda_item_id": it["agenda_item_id"],
                "item_order": it["item_order"],
                "start": start,
                "end": end,
            }
        )
    ranged.sort(key=lambda r: r["item_order"])
    for prev, cur in zip(ranged, ranged[1:]):
        if cur["item_order"] <= prev["item_order"]:
            raise AnchorScopeError("duplicate item_order in range table")
        if cur["start"] < prev["end"]:
            raise AnchorScopeError(
                f"range for {cur['agenda_item_id']} overlaps {prev['agenda_item_id']} "
                f"([{prev['start']},{prev['end']}) vs [{cur['start']},{cur['end']}))"
            )
    return ranged


def _item_for_timestamp(ranged: list[dict[str, Any]], ts: int) -> str | None:
    """The agenda_item_id whose half-open range contains ``ts``; None if in none.

    Non-overlap (enforced above) guarantees at most one match.
    """
    for r in ranged:
        if r["start"] <= ts < r["end"]:
            return r["agenda_item_id"]
    return None


# --- apply (accepted manifest -> agenda_items + narrow anchor UPDATE) --------

def _assert_anchoring_reviewer(reviewer_id: str) -> None:
    rid = (reviewer_id or "").strip()
    if not rid or rid.lower() in FORBIDDEN_REVIEWER_IDS:
        raise AnchorRefusedError(
            f"reviewer_id {reviewer_id!r} is empty or a forbidden automation/AI actor"
        )
    if rid != REVIEWER_ID:
        raise AnchorRefusedError(
            f"reviewer_id {reviewer_id!r} is not the authorised pilot reviewer "
            f"{REVIEWER_ID!r}"
        )


def _assert_source_unchanged(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    *,
    transcript_id: int | None = None,
) -> None:
    """Re-check the agenda doc sha256 and the exact target statement set vs the DB."""
    doc = manifest.get("agenda_doc", {})
    row = conn.execute(
        "SELECT sha256 FROM documents WHERE id = ?", (doc.get("document_id"),)
    ).fetchone()
    if row is None or row["sha256"] != doc.get("sha256"):
        raise AnchorRefusedError(
            "agenda document sha256 changed since the manifest was built (source_changed)"
        )
    live = target_statements(conn, transcript_id=transcript_id)
    live_ids = sorted(s["statement_id"] for s in live)
    manifest_ids = sorted(s["statement_id"] for s in manifest.get("statements", []))
    if live_ids != manifest_ids:
        raise AnchorRefusedError(
            "the reviewed-statement set changed since the manifest was built "
            f"(manifest {len(manifest_ids)} vs live {len(live_ids)})"
        )


def _upsert_agenda_item(
    conn: sqlite3.Connection, item: dict[str, Any], *, meeting_id: int
) -> bool:
    """Idempotent insert of one agenda_items row VERBATIM from the manifest.

    Returns True if a row was inserted, False if an identical row already existed.
    A present row whose identity fields differ from the manifest is a fail-closed
    source-drift abort (never an UPDATE/DELETE). ``meeting_id`` is the manifest's
    meeting (not a module constant) so scale batches file items under their own
    meeting row.
    """
    aid = item["agenda_item_id"]
    existing = conn.execute(
        "SELECT item_order, title, source_document_id, citation_target "
        "FROM agenda_items WHERE agenda_item_id = ?",
        (aid,),
    ).fetchone()
    if existing is not None:
        drift = (
            existing["item_order"] != item["item_order"]
            or existing["title"] != item["title"]
            or existing["source_document_id"] != item["source_document_id"]
            or existing["citation_target"] != item["citation_target"]
        )
        if drift:
            raise AnchorRefusedError(
                f"agenda item {aid} already exists with different identity "
                "(source drift); refusing to overwrite"
            )
        return False
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title, "
        "agenda_doc_source_id, created_utc, source_document_id, citation_target) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            aid,
            meeting_id,
            item["item_order"],
            item["title"],
            AGENDA_DOC_SOURCE_ID,
            _utcnow(),
            item["source_document_id"],
            item["citation_target"],
        ),
    )
    return True


def _statement_immutable_snapshot(
    conn: sqlite3.Connection, statement_id: str
) -> dict[str, Any]:
    cols = ", ".join(_IMMUTABLE_STATEMENT_COLUMNS)
    row = conn.execute(
        f"SELECT {cols} FROM statements WHERE statement_id = ?", (statement_id,)
    ).fetchone()
    return dict(row) if row is not None else {}


def apply_manifest(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    *,
    card_id: str,
    commit: bool,
    reviewer_id: str = REVIEWER_ID,
    expect_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Anchor the accepted manifest's statements. Dry-run unless ``commit`` is True.

    Fail-closed + atomic: every write runs in ONE transaction on a
    ``commit=False`` handle and is committed only if the whole batch succeeds; any
    refusal rolls back so nothing partial is written.
    """
    if not (card_id or "").strip():
        raise AnchorRefusedError("apply requires an accepting --card id")
    _assert_anchoring_reviewer(reviewer_id)

    manifest_sha = canonical_manifest_sha256(manifest)
    if expect_manifest_sha256 is not None and expect_manifest_sha256 != manifest_sha:
        raise AnchorRefusedError(
            "manifest sha256 does not match the card-accepted version "
            f"(expected {expect_manifest_sha256[:16]}… got {manifest_sha[:16]}…)"
        )

    entries = manifest.get("statements", [])
    if len(entries) > MAX_BATCH:
        raise AnchorScopeError(
            f"batch of {len(entries)} exceeds the {MAX_BATCH}-statement ceiling"
        )

    # Scope (meeting + transcript) is taken from the card-bound manifest, not module
    # constants, so scale batches file items under their own meeting and re-check
    # their own transcript's reviewed set (GOV-709 §3). Defaults keep the pilot.
    meeting_id = manifest.get("meeting_id", PILOT_MEETING_ID)
    transcript_id = manifest.get("transcript_id")

    _assert_source_unchanged(conn, manifest, transcript_id=transcript_id)
    ranged = validated_ranges(manifest)
    manifest_items = {it["agenda_item_id"]: it for it in manifest.get("agenda_items", [])}

    inserted = anchored = already = 0
    per_item: dict[str, int] = {}
    unanchored: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        # 1) materialise every extracted agenda item (idempotent, provenance-bound).
        for it in sorted(manifest.get("agenda_items", []), key=lambda x: x["item_order"]):
            if _upsert_agenda_item(conn, it, meeting_id=meeting_id):
                inserted += 1

        # 2) anchor each statement by pure timestamp containment.
        for entry in entries:
            sid = entry["statement_id"]
            ts = entry["timestamp_seconds"]
            target = _item_for_timestamp(ranged, ts)
            if target is None:
                unanchored.append(sid)
                results.append({"statement_id": sid, "action": "unanchored", "ts": ts})
                continue
            if target not in manifest_items:
                raise AnchorScopeError(
                    f"range names agenda item {target} absent from manifest agenda_items"
                )

            current = conn.execute(
                "SELECT agenda_item_id FROM statements WHERE statement_id = ?", (sid,)
            ).fetchone()
            if current is None:
                raise AnchorScopeError(f"statement {sid} not found in registry")
            existing_anchor = current["agenda_item_id"]
            if existing_anchor == target:
                already += 1
                per_item[target] = per_item.get(target, 0) + 1
                results.append(
                    {"statement_id": sid, "action": "already-anchored", "agenda_item_id": target}
                )
                continue
            if existing_anchor is not None:
                raise AnchorRefusedError(
                    f"statement {sid} already anchored to {existing_anchor!r}; "
                    f"re-anchoring to {target!r} is a new reviewer decision (out of scope)"
                )

            before = _statement_immutable_snapshot(conn, sid)
            # WRITE-ONCE narrow UPDATE: agenda_item_id only, and only if still NULL.
            cur = conn.execute(
                "UPDATE statements SET agenda_item_id = ? "
                "WHERE statement_id = ? AND agenda_item_id IS NULL",
                (target, sid),
            )
            if cur.rowcount != 1:
                raise AnchorRefusedError(
                    f"write-once UPDATE for {sid} affected {cur.rowcount} rows"
                )
            after = _statement_immutable_snapshot(conn, sid)
            if before != after:
                raise AnchorRefusedError(
                    f"immutable statement columns drifted while anchoring {sid}"
                )
            anchored += 1
            per_item[target] = per_item.get(target, 0) + 1
            results.append(
                {"statement_id": sid, "action": "anchored", "agenda_item_id": target}
            )

        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise

    per_item_full = {
        it["agenda_item_id"]: per_item.get(it["agenda_item_id"], 0)
        for it in sorted(manifest.get("agenda_items", []), key=lambda x: x["item_order"])
    }
    return {
        "kind": MANIFEST_KIND,
        "card_id": card_id,
        "reviewer_id": reviewer_id,
        "manifest_sha256": manifest_sha,
        "committed": commit,
        "dry_run": not commit,
        "counts": {
            "total_statements": len(entries),
            "agenda_items_inserted": inserted,
            "anchored": anchored,
            "already_anchored": already,
            "unanchored_remaining": len(unanchored),
        },
        "anchored_per_item": per_item_full,
        "unanchored_statement_ids": unanchored,
        "results": results,
        "generated_utc": _utcnow(),
    }


def write_report(report: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out_path


# --- CLI --------------------------------------------------------------------

def _default_logs_dir() -> Path:
    return db.REPO_ROOT / "Logs" / "agenda-anchoring"


def _cmd_propose(args: argparse.Namespace) -> int:
    conn = db.open_db(args.db)
    try:
        manifest = build_manifest(
            conn, corpus_root=args.corpus_root,
            meeting_id=args.meeting_id, agenda_doc_id=args.agenda_doc_id,
            transcript_id=args.transcript_id,
        )
    except AnchorRefusedError as exc:
        print(f"refused (fail-closed): {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AnchorScopeError as exc:
        print(f"scope error: {exc}", file=sys.stderr)
        return EXIT_SCOPE
    finally:
        conn.close()

    if args.out is not None:
        path = write_manifest(manifest, args.out)
        print(
            f"wrote manifest: {path}\n"
            f"  agenda_items={manifest['counts']['agenda_items']} "
            f"statements={manifest['counts']['statements']} "
            f"sha256={canonical_manifest_sha256(manifest)[:16]}…"
        )
    else:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    conn = db.open_db(args.db)
    try:
        report = apply_manifest(
            conn,
            manifest,
            card_id=args.card,
            commit=args.commit,
            expect_manifest_sha256=args.manifest_sha256,
        )
    except AnchorScopeError as exc:
        print(f"scope error: {exc}", file=sys.stderr)
        return EXIT_SCOPE
    except AnchorRefusedError as exc:
        print(f"refused (fail-closed): {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()

    if args.report is not None:
        write_report(report, args.report)

    banner = "COMMIT" if report["committed"] else "DRY-RUN (no writes; pass --commit to apply)"
    c = report["counts"]
    print(
        f"[{banner}] card {report['card_id']} manifest {report['manifest_sha256'][:16]}…\n"
        f"  agenda_items_inserted={c['agenda_items_inserted']} "
        f"anchored={c['anchored']} already_anchored={c['already_anchored']} "
        f"unanchored_remaining={c['unanchored_remaining']} / {c['total_statements']}"
    )
    for aid, n in report["anchored_per_item"].items():
        print(f"    {aid}: {n} statement(s)")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GOV-698 agenda-anchoring batch CLI (2026-06-23 TOA pilot)."
    )
    parser.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help="registry DB path (default: the local registry)",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_prop = sub.add_parser("propose", help="emit a local anchoring manifest (read-only)")
    p_prop.add_argument(
        "--meeting-id", type=int, default=PILOT_MEETING_ID,
        help="meeting whose date reconciles doc + transcript (default: pilot 129)",
    )
    p_prop.add_argument(
        "--agenda-doc-id", type=int, default=AGENDA_DOC_ID,
        help="agenda document id to extract items from (default: pilot 137)",
    )
    p_prop.add_argument(
        "--transcript-id", type=int, default=None,
        help="scope the reviewed target statements to ONE transcript (scale selector)",
    )
    p_prop.add_argument(
        "--corpus-root", type=Path, default=db.REPO_ROOT,
        help="root the agenda doc local_path resolves against (default: repo root)",
    )
    p_prop.add_argument(
        "--out", type=Path, default=None,
        help="write manifest to this local/gitignored path (else stdout)",
    )
    p_prop.set_defaults(func=_cmd_propose)

    p_app = sub.add_parser("apply", help="anchor an accepted manifest (dry-run by default)")
    p_app.add_argument("--manifest", required=True, help="path to the accepted manifest JSON")
    p_app.add_argument("--card", required=True, help="accepting board interaction/card id")
    p_app.add_argument(
        "--manifest-sha256", default=None,
        help="the card-bound manifest sha256 to enforce (fail-closed on mismatch)",
    )
    p_app.add_argument(
        "--report", type=Path, default=None,
        help="write the JSON run report to this local/gitignored path",
    )
    p_app.add_argument(
        "--commit", action="store_true",
        help="actually write (default is dry-run per the company gate)",
    )
    p_app.set_defaults(func=_cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
