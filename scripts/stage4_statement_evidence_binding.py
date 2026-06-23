"""Stage 4.07 statement->exact-source binding validator (GOV-467) — reviewer-internal, Alpine.

Implements the GOV-467 contract
(``docs/stage4-07-statement-evidence-binding-contract.md``): a deterministic **validator one
layer up over the assembled 4.05 digest** that re-proves the digest never *loses* the
statement->exact-source binding. The exact-source discipline already exists upstream
(:mod:`statements` enforces no-orphan-claims + complete-valid-pointer at write time;
:mod:`read_api` enforces it at serve time); this is the **defense-in-depth regression net**,
exactly as Stage 4.04 (``stage4_newsletter_preservation_audit``) sat one layer up over the feed.

For every **statement-bearing digest item** (an item in a digest's ``items[]``, projected from
one served reviewer-internal statement) the validator:

* **resolves it to its real statement record** (:func:`statement_index` — a FORWARD
  ``card_handle -> statement_id`` map, never a reverse of the one-way card hash) and asserts an
  **exact-source pointer**: a resolving ``segment_id`` segment edge OR >=1 ``evidence_link`` with
  a complete, valid pointer per :data:`statements.LOCATOR_REQUIRED_FIELDS`. A statement-bearing
  item with neither is an **orphan** — routed to VSR, never silently dropped
  (:func:`statement_link_validation` / :func:`assert_every_statement_bound` /
  :func:`assert_no_unrouted_orphans`). This is strictly stronger than the serve gate, which
  serves a statement whenever its ``evidence_links`` list is non-empty (never re-checking pointer
  *completeness*);
* asserts the item's claim/speaker label is a conservative member of
  :data:`stage4_newsletter_feed.STAGE3_CLAIM_VOCAB` and is **never silently upgraded to verified**
  — the ``claimStatus`` must equal the status independently recomputed from the live read surface
  (:func:`stage3_card_feed._compose_record_status`) (:func:`assert_labels_conservative`);
* asserts **paraphrase != verbatim** — a verbatim-styled statement (``is_verbatim``) must bind to
  a verbatim anchor (a resolving segment edge OR an ``evidence_link`` carrying ``quoted_text``)
  (:func:`assert_verbatim_anchored`).

Boundary rules (contract §6, restated as validator invariants):

* it imports :mod:`statements`, :mod:`read_api`, :mod:`stage3_card_feed`,
  :mod:`stage4_newsletter_feed`, and :mod:`stage4_newsletter_digest_assembler` **by reference**
  and re-declares none of their constants (``SCOPE`` / ``ACCESS`` / ``STAGE3_CLAIM_VOCAB`` /
  ``VSR`` / ``LOCATOR_REQUIRED_FIELDS`` / ``RAW_PATH_MARKERS``) — so the public contract surfaces
  stay byte-0-diff;
* it runs entirely at ``access: reviewer_internal`` and ``scope: alpine`` — Alpine-only, no public
  lane, no email/sender, no naming non-officials, and **no AI output / editorial prose** (4.08);
* it reads the raw exact-source columns reviewer-internally but **emits only** the §3 log/overlay
  (slugs + enums + a single envelope fingerprint), each transport-swept by
  :func:`read_api.assert_no_raw_paths` — a raw vault path / ``file://`` / ``.sha256`` leak fails
  LOUDLY at the boundary.

Pure function of the DB: same DB -> byte-identical validation log/overlay (idempotent). No
mutation, no AI, no network, no public publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import read_api  # noqa: E402
import stage3_card_feed as card_feed  # noqa: E402
import stage4_newsletter_digest_assembler as digest  # noqa: E402
import stage4_newsletter_feed as nl  # noqa: E402
import statements as st  # noqa: E402


class BindingContractError(AssertionError):
    """Raised when the assembled digest violates a GOV-467 binding invariant."""


class OrphanStatementError(BindingContractError):
    """A statement-bearing digest item has no resolving exact-source pointer (EG-2)."""


class UnroutedOrphanError(BindingContractError):
    """An orphan statement row was not routed to VSR — a silent drop (EG-4)."""


class LabelUpgradeError(BindingContractError):
    """An item's claim/speaker label left the Stage-3 vocab, or was upgraded to verified."""


class VerbatimAnchorError(BindingContractError):
    """A verbatim-styled statement lacks a segment / quoted_text exact verbatim anchor."""


class BindingReproducibilityError(BindingContractError):
    """Re-building the validation log did not produce byte-identical JSON."""


# ---------------------------------------------------------------------------
# §2 — forward card_handle -> statement_id index (never a reverse of the hash)
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    """Canonical, key-sorted JSON — the byte-comparison form for fingerprints/reproducibility."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def statement_index(conn: sqlite3.Connection) -> dict[str, str]:
    """Forward ``card_handle -> statement_id`` map over the served reviewer-internal records (§2).

    The digest item carries ``cardIds[0]`` — a one-way ``card_handle(card_type, statement_id)``
    sha256 truncation, never the raw id. So the mapping is rebuilt FORWARD using the SAME
    derivation the feed used to assign ``cardIds`` (:func:`stage3_card_feed._resolve_record_type`
    + :func:`stage3_card_feed.card_handle`) — the handles match the digest by construction, with
    no parsing and no reverse. ``statement_id`` survives ``to_web_safe`` (it is web-safe), so it
    is read straight off the served record.
    """
    index: dict[str, str] = {}
    for record in read_api.reviewer_internal_records(conn):
        statement_id = record.get("statement_id")
        if not statement_id:
            continue
        card_type = card_feed._resolve_record_type(record)
        index[card_feed.card_handle(card_type, statement_id)] = statement_id
    return index


def _served_claim_status(conn: sqlite3.Connection) -> dict[str, str]:
    """``statement_id -> claimStatus`` recomputed INDEPENDENTLY from the live read surface.

    Re-derives the composed Stage-3 status from the re-served record exactly as the feed did
    (:func:`stage3_card_feed._compose_record_status`), so comparing it to the digest item's
    ``claimStatus`` catches a digest that silently upgraded a label (e.g. to ``verified``).
    """
    return {
        record["statement_id"]: card_feed._compose_record_status(record)
        for record in read_api.reviewer_internal_records(conn)
        if record.get("statement_id")
    }


# ---------------------------------------------------------------------------
# §1/§2 — resolve a statement-bearing item to its canonical exact-source pointer
# ---------------------------------------------------------------------------


def _statement_items(out: dict[str, Any]):
    """Yield every statement-bearing digest item (one per served record)."""
    for d in out.get("digests", []):
        yield from d.get("items", [])


def _item_card(item: dict[str, Any]) -> str | None:
    cards = item.get("cardIds")
    return cards[0] if cards else None


def _raw_statement(conn: sqlite3.Connection, statement_id: str | None) -> dict[str, Any] | None:
    """The raw ``statements`` row (canonical exact-source columns), or ``None``."""
    if not statement_id:
        return None
    row = conn.execute(
        "SELECT * FROM statements WHERE statement_id = ?", (statement_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def resolve_binding(conn: sqlite3.Connection, statement_id: str | None) -> dict[str, Any]:
    """Resolve a statement to its exact-source pointer (§1). ``{resolves, pointerKind}``.

    Reuses the Stage-2 model verbatim: a resolving ``segment_id`` segment edge
    (:func:`read_api._segment_resolves`) is ``pointerKind="segment"``; else the
    ``locator_kind`` of the FIRST ``evidence_link`` whose pointer passes
    :func:`statements.validate_pointer`; else an orphan (``resolves=False``,
    ``pointerKind=None``). Consistent with :func:`statements.is_orphan` by construction (same
    disjunction). An unresolvable ``statement_id`` (no index entry / no row) is an orphan.
    """
    stmt = _raw_statement(conn, statement_id)
    if stmt is None:
        return {"resolves": False, "pointerKind": None}
    if read_api._segment_resolves(conn, stmt.get("segment_id")):
        return {"resolves": True, "pointerKind": "segment"}
    for link in read_api._evidence_links_for(conn, statement_id):
        try:
            st.validate_pointer(link, conn=conn)
        except st.PointerError:
            continue
        return {"resolves": True, "pointerKind": link.get("locator_kind")}
    return {"resolves": False, "pointerKind": None}


def _has_verbatim_anchor(conn: sqlite3.Connection, statement_id: str, stmt: dict[str, Any]) -> bool:
    """True iff a verbatim-styled statement is anchored to an exact verbatim source.

    The contract's verbatim anchor: a resolving ``segment_id`` segment edge OR an
    ``evidence_link`` carrying non-empty ``quoted_text`` (the ``char_span`` exact-quote anchor).
    A bare page/section pointer is NOT a verbatim anchor — a verbatim statement bound only by one
    is a paraphrase presented as a quote.
    """
    if read_api._segment_resolves(conn, stmt.get("segment_id")):
        return True
    return any(
        link.get("quoted_text") for link in read_api._evidence_links_for(conn, statement_id)
    )


# ---------------------------------------------------------------------------
# §3 — statement-link validation log (orphans routed to VSR, never dropped)
# ---------------------------------------------------------------------------


def statement_link_validation(
    conn: sqlite3.Connection, out: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One row per statement-bearing digest item; orphans routed to VSR (§3). Transport-swept.

    Each row is ``{itemId, statementId, pointerKind, resolves, label, route}``. An item whose
    ``cardIds[0]`` resolves to no statement, or whose statement carries no complete exact-source
    pointer, is an orphan: it gets a ``routing`` entry (``routedTo: VSR`` / ``status: held``) and
    ``route`` is VSR — never silently dropped. ``passed`` is true iff there is no UNROUTED orphan.
    The whole body is swept by :func:`read_api.assert_no_raw_paths`, so a leak fails LOUDLY.
    """
    if out is None:
        out = digest.assemble_digests(conn)
    index = statement_index(conn)

    rows: list[dict[str, Any]] = []
    routing: list[dict[str, Any]] = []
    for item in _statement_items(out):
        statement_id = index.get(_item_card(item))
        binding = resolve_binding(conn, statement_id)
        resolves = binding["resolves"]
        rows.append({
            "itemId": item.get("id"),
            "statementId": statement_id,
            "pointerKind": binding["pointerKind"],
            "resolves": resolves,
            "label": item.get("labels", {}).get("claimStatus"),
            "route": None if resolves else nl.VSR,
        })
        if not resolves:
            routing.append({
                "itemId": item.get("id"),
                "statementId": statement_id,
                "reason": "no_exact_source_pointer",
                "routedTo": nl.VSR,
                "status": "held",
            })
    every_orphan_routed = all(
        row["route"] == nl.VSR for row in rows if not row["resolves"]
    )
    artifact = {
        "scope": nl.SCOPE,
        "access": nl.ACCESS,
        "rows": rows,
        "routing": routing,
        "passed": every_orphan_routed,
    }
    return read_api.assert_no_raw_paths(artifact)


# ---------------------------------------------------------------------------
# §4 — the four binding guards (each load-bearing; see RED test list §5)
# ---------------------------------------------------------------------------


def assert_every_statement_bound(
    conn: sqlite3.Connection, out: dict[str, Any] | None = None
) -> bool:
    """RED if any statement-bearing digest item lacks an exact-source pointer (EG-2)."""
    log = statement_link_validation(conn, out)
    orphans = [row for row in log["rows"] if not row["resolves"]]
    if orphans:
        raise OrphanStatementError(
            f"{len(orphans)} statement-bearing digest item(s) carry no exact-source "
            f"pointer (orphan claims): {[o['itemId'] for o in orphans]}"
        )
    return True


def assert_no_unrouted_orphans(log: dict[str, Any]) -> bool:
    """RED if any orphan row in ``log`` is not routed to VSR (EG-4 — never a silent drop)."""
    unrouted = [
        row for row in log.get("rows", [])
        if not row.get("resolves") and row.get("route") != nl.VSR
    ]
    if unrouted:
        raise UnroutedOrphanError(
            f"{len(unrouted)} orphan statement row(s) not routed to VSR: "
            f"{[r.get('itemId') for r in unrouted]}"
        )
    return True


def assert_labels_conservative(
    conn: sqlite3.Connection, out: dict[str, Any] | None = None
) -> bool:
    """RED if an item's claim/speaker label leaves the vocab or is upgraded to verified.

    Two checks per statement-bearing item: (a) ``claimStatus`` and ``speakerStatus`` are members
    of the imported :data:`stage4_newsletter_feed.STAGE3_CLAIM_VOCAB` (the conservative
    vocabulary; zero new labels); (b) ``claimStatus`` equals the status INDEPENDENTLY recomputed
    from the live read surface — so a digest item silently upgraded to ``verified`` is caught.
    """
    if out is None:
        out = digest.assemble_digests(conn)
    index = statement_index(conn)
    served = _served_claim_status(conn)
    for item in _statement_items(out):
        labels = item.get("labels", {})
        for axis in ("claimStatus", "speakerStatus"):
            value = labels.get(axis)
            if value not in nl.STAGE3_CLAIM_VOCAB:
                raise LabelUpgradeError(
                    f"item {item.get('id')!r} {axis} {value!r} outside the Stage-3 vocabulary"
                )
        statement_id = index.get(_item_card(item))
        expected = served.get(statement_id)
        claim = labels.get("claimStatus")
        if expected is not None and claim != expected:
            raise LabelUpgradeError(
                f"item {item.get('id')!r} claimStatus {claim!r} != independently recomputed "
                f"{expected!r} (silent label upgrade)"
            )
    return True


def assert_verbatim_anchored(
    conn: sqlite3.Connection, out: dict[str, Any] | None = None
) -> bool:
    """RED if any verbatim-styled statement lacks a segment / quoted_text anchor (paraphrase!=verbatim)."""
    if out is None:
        out = digest.assemble_digests(conn)
    index = statement_index(conn)
    for item in _statement_items(out):
        statement_id = index.get(_item_card(item))
        stmt = _raw_statement(conn, statement_id)
        if stmt is None or not stmt.get("is_verbatim"):
            continue
        if not _has_verbatim_anchor(conn, statement_id, stmt):
            raise VerbatimAnchorError(
                f"item {item.get('id')!r} (statement {statement_id!r}) is verbatim-styled but "
                f"lacks a verbatim anchor (no resolving segment edge, no quoted_text pointer) — "
                f"a paraphrase must never be presented as a verbatim quote"
            )
    return True


# ---------------------------------------------------------------------------
# §4 — reproducibility (idempotent) + opaque envelope fingerprint
# ---------------------------------------------------------------------------


def binding_fingerprint(log: dict[str, Any]) -> str:
    """sha256 of the canonical validation log — single opaque envelope-level fingerprint."""
    return hashlib.sha256(_canonical(log).encode("utf-8")).hexdigest()


def assert_reproducible(conn: sqlite3.Connection) -> str:
    """RED if re-building the validation log is not byte-identical. Returns the binding_digest.

    Builds the log twice and compares the canonical JSON; a tautological self-compare would still
    pass, so the guard re-runs the full build each time.
    """
    first = _canonical(statement_link_validation(conn))
    second = _canonical(statement_link_validation(conn))
    if first != second:
        raise BindingReproducibilityError("validation log is not byte-identical across builds")
    return hashlib.sha256(first.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# §3 — reviewer-internal audit overlay (GOV-453 / GOV-457 precedent)
# ---------------------------------------------------------------------------


def build_binding_overlay(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run the four guards (fail-closed, collect rather than raise), assemble the overlay, sweep it."""
    out = digest.assemble_digests(conn)
    log = statement_link_validation(conn, out)

    violations: dict[str, list[str]] = {
        "orphans": [], "routing": [], "labels": [], "verbatim": [],
    }
    try:
        assert_every_statement_bound(conn, out)
    except OrphanStatementError as exc:  # pragma: no cover - clean corpus path tested
        violations["orphans"].append(str(exc))
    try:
        assert_no_unrouted_orphans(log)
    except UnroutedOrphanError as exc:  # pragma: no cover
        violations["routing"].append(str(exc))
    try:
        assert_labels_conservative(conn, out)
    except LabelUpgradeError as exc:  # pragma: no cover
        violations["labels"].append(str(exc))
    try:
        assert_verbatim_anchored(conn, out)
    except VerbatimAnchorError as exc:  # pragma: no cover
        violations["verbatim"].append(str(exc))

    overlay = {
        "scope": nl.SCOPE,
        "access": nl.ACCESS,
        "statement_item_count": len(log["rows"]),
        "bound_count": sum(1 for row in log["rows"] if row["resolves"]),
        "orphan_count": sum(1 for row in log["rows"] if not row["resolves"]),
        "all_bound": not violations["orphans"],
        "no_unrouted_orphans": not violations["routing"],
        "labels_conservative": not violations["labels"],
        "verbatim_anchored": not violations["verbatim"],
        "binding_digest": binding_fingerprint(log),
        "violations": violations,
    }
    return read_api.assert_no_raw_paths(overlay)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 4.07 reviewer-internal digest statement->exact-source binding validator (GOV-467)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--artifact",
        choices=("log", "overlay"),
        default="log",
        help="which artifact to emit (default: the statement-link validation log)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the binding + routing + label + verbatim guards (non-zero exit on a violation)",
    )
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        if args.artifact == "log":
            out: dict[str, Any] = statement_link_validation(conn)
            if args.check:
                assert_every_statement_bound(conn)
                assert_no_unrouted_orphans(out)
                assert_labels_conservative(conn)
                assert_verbatim_anchored(conn)
                assert_reproducible(conn)
        else:
            out = build_binding_overlay(conn)
            if args.check and any(out["violations"].values()):
                raise BindingContractError(f"overlay violations: {out['violations']}")
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
