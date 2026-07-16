"""GOV-648 — reviewer-promotion batch CLI (Option-A pilot, leg 2 of 6).

GOV-710 (scale leg 2 of 6) parameterizes the slice selector: the pilot behaviour
is preserved verbatim when no selector is given, and a ``--meeting-date`` +
``--transcript-id`` pair scopes the slice to ONE transcript for the oldest→newest
scale-up across the local TOA corpus (GOV-709 §3). The frozen promotion gate is
unchanged; every batch is still `reviewer:isaac`-only and its own-card-gated.

A deterministic, fail-closed batch tool that drives the ONE sanctioned Lane-5
promotion path (:func:`ai_risk_gate.promote_statement`) over a transcript slice of
the local Town of Alpine corpus. It is an *orchestrator*, never a parallel write
path: it never issues a bare ``UPDATE statements`` for review fields and never
re-implements the reviewer gate. ``read_api`` / ``publication`` /
``ai_risk_gate`` / ``stage5_agenda_board`` stay byte-0-diff (the CLI imports them
as libraries only).

Two modes (GOV-647 spec §3, §7):

* ``propose`` — registry read-only. Emits a **local/vault-only manifest** (a
  human-reviewable packet) for a contiguous ``segment_index`` window of the pilot
  slice: statement id, ``timestamp_human``, verbatim text, and the source anchor
  (transcript ``local_path`` + ``sha256``). Never pushed to GitHub. This is the
  artifact Isaac reviews on a board-gated ``request_confirmation`` card.
* ``apply`` — reads an **accepted** manifest and applies its per-statement
  decisions to the ``reviewer_decisions`` ledger + statement statuses. Requires
  the accepting card id (``--card``); the card id is recorded into each decision's
  ``reason_category`` for audit. **Dry-run is the default** (company dry-run gate);
  ``--commit`` is required to actually write.

Hard invariants (GOV-647 §2, §4, §6):

* reviewer identity is ``reviewer:isaac`` ONLY; a forbidden automation/AI id is
  rejected before any DB call; an unregistered id fails closed inside the gate.
* a promoting decision lands ONLY on ``reviewed_source_linked`` (never
  ``human_verified``); ``publication_state`` is never touched.
* the scope gate refuses any statement outside the §1 date-scoped selector with
  exit code 2 (no bypass flag); a batch may not exceed 50 statements.
* re-applying an already-applied batch writes 0 new ledger rows and exits 0.
* anchoring (``statements.agenda_item_id`` + ``agenda_items`` rows) is written
  ONLY on ``apply --commit`` of an accepted batch, verbatim from the manifest; a
  statement with no proposed item stays unanchored (disclosed, never guessed).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402

# --- enforced pilot constants (GOV-647 §1, §2, §7.3) ------------------------

#: The single pilot meeting: 2026-06-23 Town of Alpine council. The slice is the
#: statements of the transcripts recorded that date (see :func:`pilot_slice`).
PILOT_MEETING_DATE = "2026-06-23"

#: The ONLY reviewer identity authorised for this pilot (owner decision, card
#: ``64a4c200`` / GOV-646). Automation/AI ids can never promote (defence-in-depth
#: below AND inside :func:`ai_risk_gate.is_registered_reviewer`).
REVIEWER_ID = "reviewer:isaac"

#: A promoting decision lands here and NOWHERE else. ``human_verified`` is
#: forbidden in this pilot (no human-verification protocol exists yet).
TO_VERIFICATION_STATUS = "reviewed_source_linked"

#: Human-review batch ceiling (GOV-647 §3): a sane window for one card.
MAX_BATCH = 50

#: The default decision for a proposed statement when the accepted manifest does
#: not name one — an accepted card is an approval.
DEFAULT_DECISION = "approved"

MANIFEST_VERSION = 1
MANIFEST_KIND = "reviewer_promotion_batch"

#: Exit codes.
EXIT_OK = 0
EXIT_REFUSED = 1        # fail-closed gate refusal (unregistered/forbidden/gate error)
EXIT_SCOPE = 2          # scope-gate / batch-size violation (no bypass)


class PromotionScopeError(RuntimeError):
    """A manifest referenced a statement outside the pilot slice, or oversized."""


# --- slice selector (GOV-647 §1 — the normative, date-scoped selector) ------

def pilot_slice(
    conn: sqlite3.Connection,
    *,
    meeting_date: str = PILOT_MEETING_DATE,
    transcript_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return the slice statements in ``segment_index`` order (packet rows).

    Two scoping modes (GOV-709 §3):

    * Pilot default (``transcript_id=None``): date-scoped through the transcript
      (NOT the meeting FK — see the §1 caveat), every statement whose segment
      belongs to a transcript recorded on ``meeting_date``.
    * Scale (``transcript_id`` given): scoped to that ONE transcript. This is the
      normative selector for the scale-up because a ``meeting_date`` can carry two
      timed transcripts whose ``segment_index`` sequences would otherwise interleave
      (2026-05-07, GOV-709 §1a). ``meeting_date`` is then a fail-closed cross-check
      against ``transcripts.meeting_date`` — a mismatch refuses rather than slices
      the wrong meeting.

    Ordering is deterministic (``segment_index`` then ``statement_id``) so batch
    windows are stable and re-proposals are byte-identical.
    """
    if transcript_id is not None:
        trow = conn.execute(
            "SELECT meeting_date FROM transcripts WHERE id = ?", (transcript_id,)
        ).fetchone()
        if trow is None:
            raise PromotionScopeError(f"transcript id {transcript_id} not in registry")
        if meeting_date is not None and trow["meeting_date"] != meeting_date:
            raise PromotionScopeError(
                f"transcript {transcript_id} is dated {trow['meeting_date']!r}, not the "
                f"requested --meeting-date {meeting_date!r} (fail-closed cross-check)"
            )
        where, param = "tr.id = ?", transcript_id
    else:
        where, param = "tr.meeting_date = ?", meeting_date
    rows = conn.execute(
        "SELECT s.statement_id, s.segment_id, ts.segment_index, ts.timestamp_human, "
        "       s.statement_text, tr.local_path AS transcript_local_path, tr.sha256 "
        "FROM statements s "
        "JOIN transcript_segments ts ON ts.segment_id = s.segment_id "
        "JOIN transcripts tr ON tr.id = ts.transcript_id "
        f"WHERE {where} "
        "ORDER BY ts.segment_index, s.statement_id",
        (param,),
    ).fetchall()
    return [dict(r) for r in rows]


def pilot_slice_ids(
    conn: sqlite3.Connection,
    *,
    meeting_date: str = PILOT_MEETING_DATE,
    transcript_id: int | None = None,
) -> set[str]:
    """The set of statement ids in the slice — the scope-gate allowlist."""
    return {
        r["statement_id"]
        for r in pilot_slice(conn, meeting_date=meeting_date, transcript_id=transcript_id)
    }


# --- propose (registry read-only; emits a local/vault-only manifest) --------

def build_manifest(
    conn: sqlite3.Connection,
    *,
    offset: int = 0,
    limit: int = MAX_BATCH,
    meeting_date: str = PILOT_MEETING_DATE,
    transcript_id: int | None = None,
) -> dict[str, Any]:
    """Build a review packet for a contiguous window of the slice.

    Read-only: touches no write path. The window is ``[offset, offset+limit)`` in
    the deterministic slice order. ``limit`` is clamped to :data:`MAX_BATCH`. Each
    entry carries the verbatim statement text, its transcript timestamp, and the
    source anchor (transcript ``local_path`` + ``sha256``). ``proposed_agenda_item``
    is copied from any anchor a statement already carries; it is otherwise absent
    (never guessed — anchoring is a human-approved apply-time write).

    ``meeting_date`` / ``transcript_id`` select the slice (see :func:`pilot_slice`)
    and are recorded verbatim in the manifest so :func:`apply_manifest` re-derives
    the exact same scope-gate allowlist from the accepted packet (GOV-709 §3).
    """
    if offset < 0:
        raise PromotionScopeError("offset must be >= 0")
    if not (0 < limit <= MAX_BATCH):
        raise PromotionScopeError(f"limit must be in 1..{MAX_BATCH} (got {limit})")

    window = pilot_slice(
        conn, meeting_date=meeting_date, transcript_id=transcript_id
    )[offset:offset + limit]
    statements: list[dict[str, Any]] = []
    for row in window:
        statements.append(
            {
                "statement_id": row["statement_id"],
                "segment_id": row["segment_id"],
                "segment_index": row["segment_index"],
                "timestamp_human": row["timestamp_human"],
                "text": row["statement_text"],
                "source": {
                    "transcript_local_path": row["transcript_local_path"],
                    "sha256": row["sha256"],
                },
                # Reviewer fills a real decision on the card; default is approve.
                "decision": DEFAULT_DECISION,
                # Anchoring is opt-in + human-approved; absent => stays unanchored.
                "agenda_item_id": None,
            }
        )
    last = offset + len(statements)
    scope_tag = f"tx{transcript_id}" if transcript_id is not None else meeting_date
    return {
        "manifest_version": MANIFEST_VERSION,
        "kind": MANIFEST_KIND,
        "meeting_date": meeting_date,
        "transcript_id": transcript_id,
        "batch_id": f"promotion-batch:{scope_tag}:{offset:04d}-{last:04d}",
        "reviewer_id": REVIEWER_ID,
        "to_verification_status": TO_VERIFICATION_STATUS,
        "agenda_items": [],
        "statements": statements,
    }


def write_manifest(manifest: dict[str, Any], out_path: Path) -> Path:
    """Write the manifest JSON to a local (gitignored) path. Never pushed."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out_path


# --- apply (accepted manifest -> ledger + statuses; dry-run by default) ------

def _assert_promotable_reviewer(reviewer_id: str) -> None:
    """Fast-reject an empty / automation / AI reviewer id before any DB call.

    Defence-in-depth over :func:`ai_risk_gate.is_registered_reviewer` (which also
    folds :data:`ai_risk_gate.FORBIDDEN_REVIEWER_IDS`): an automation/AI actor can
    never promote in this pilot even if it were mis-seeded into the registry.
    """
    rid = (reviewer_id or "").strip()
    if not rid or rid.lower() in gate.FORBIDDEN_REVIEWER_IDS:
        raise gate.ReviewerGateError(
            f"reviewer_id {reviewer_id!r} is empty or a forbidden automation/AI "
            "actor; only reviewer:isaac may promote in this pilot"
        )
    if rid != REVIEWER_ID:
        raise gate.ReviewerGateError(
            f"reviewer_id {reviewer_id!r} is not the authorised pilot reviewer "
            f"{REVIEWER_ID!r}"
        )


def _intended_target(decision: str, current_status: str) -> str:
    """The verification_status this decision would land on (for idempotency)."""
    if decision in gate.PROMOTING_DECISIONS:
        return TO_VERIFICATION_STATUS
    if decision in gate._TERMINAL_STATUS:
        return gate._TERMINAL_STATUS[decision]
    return current_status  # hold: unchanged


def _already_applied(
    conn: sqlite3.Connection, statement_id: str, decision: str, reviewer_id: str
) -> bool:
    """True iff the latest ledger decision already equals the intended one.

    Idempotency pre-filter (GOV-647 §4): re-applying an already-applied batch must
    write 0 new rows. We compare on ``(decision, reviewer_id)`` and — for a
    promotion — the reviewed target status.
    """
    latest = gate.latest_decision(conn, statement_id)
    if latest is None:
        return False
    if latest.get("reviewer_id") != reviewer_id or latest.get("decision") != decision:
        return False
    if decision in gate.PROMOTING_DECISIONS:
        return latest.get("to_verification_status") == TO_VERIFICATION_STATUS
    return True


def _materialize_agenda_item(
    conn: sqlite3.Connection,
    agenda_item_id: str,
    agenda_items_by_id: dict[str, dict[str, Any]],
) -> None:
    """Create the ``agenda_items`` row VERBATIM from the manifest, idempotently.

    Anchoring identity (title + order + meeting) is copied exactly from the
    accepted manifest — never paraphrased (GOV-647 §5). A dangling anchor (an
    ``agenda_item_id`` with no manifest definition) is refused fail-closed rather
    than fabricated.
    """
    if conn.execute(
        "SELECT 1 FROM agenda_items WHERE agenda_item_id = ?", (agenda_item_id,)
    ).fetchone() is not None:
        return
    spec = agenda_items_by_id.get(agenda_item_id)
    if spec is None:
        raise PromotionScopeError(
            f"agenda_item_id {agenda_item_id!r} is anchored by a statement but not "
            "defined in the manifest's agenda_items; refusing to fabricate identity"
        )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES (?, ?, ?, ?)",
        (agenda_item_id, spec.get("meeting_id"), spec.get("item_order"), spec["title"]),
    )


def apply_manifest(
    conn: sqlite3.Connection,
    manifest: dict[str, Any],
    *,
    card_id: str,
    commit: bool,
    reviewer_id: str = REVIEWER_ID,
) -> dict[str, Any]:
    """Apply an accepted manifest's decisions. Dry-run unless ``commit`` is True.

    Fail-closed + atomic: every write runs in ONE transaction on a shared
    ``commit=False`` handle and is committed only if the whole batch succeeds; any
    refusal rolls back so nothing partial is written. Returns a summary with a
    per-statement result list. ``card_id`` is required (an accepted card is what
    authorises the write) and is recorded into each decision's ``reason_category``.
    """
    if not (card_id or "").strip():
        raise gate.ReviewerGateError("apply requires an accepting --card id")
    _assert_promotable_reviewer(reviewer_id)

    entries = manifest.get("statements", [])
    if len(entries) > MAX_BATCH:
        raise PromotionScopeError(
            f"batch of {len(entries)} exceeds the {MAX_BATCH}-statement ceiling"
        )

    # The scope-gate allowlist is re-derived from the SELECTOR the manifest was
    # built with (card-bound), not a module constant, so scale batches gate against
    # their own transcript slice while pilot manifests stay date-scoped (GOV-709 §3).
    meeting_date = manifest.get("meeting_date", PILOT_MEETING_DATE)
    transcript_id = manifest.get("transcript_id")
    slice_scope = (
        f"transcript {transcript_id}" if transcript_id is not None
        else f"{meeting_date} slice"
    )
    slice_ids = pilot_slice_ids(
        conn, meeting_date=meeting_date, transcript_id=transcript_id
    )
    offenders = [e["statement_id"] for e in entries if e["statement_id"] not in slice_ids]
    if offenders:
        raise PromotionScopeError(
            f"{len(offenders)} statement(s) outside the {slice_scope}: "
            f"{offenders[:5]}{'…' if len(offenders) > 5 else ''}"
        )

    agenda_items_by_id = {a["agenda_item_id"]: a for a in manifest.get("agenda_items", [])}
    reason_category = f"promotion-card:{card_id}"

    results: list[dict[str, Any]] = []
    promoted = held = rejected = skipped = 0
    try:
        for entry in entries:
            statement_id = entry["statement_id"]
            decision = entry.get("decision", DEFAULT_DECISION)
            reason = entry.get("reason") or (
                f"reviewer:isaac accepted batch card {card_id} "
                f"({meeting_date} TOA council {slice_scope}, GOV-648/GOV-710)"
            )

            if _already_applied(conn, statement_id, decision, reviewer_id):
                skipped += 1
                results.append(
                    {"statement_id": statement_id, "action": "skip-already-applied",
                     "decision": decision}
                )
                continue

            anchor_id = entry.get("agenda_item_id")
            if anchor_id and decision in gate.PROMOTING_DECISIONS:
                _materialize_agenda_item(conn, anchor_id, agenda_items_by_id)
                conn.execute(
                    "UPDATE statements SET agenda_item_id = ? WHERE statement_id = ?",
                    (anchor_id, statement_id),
                )

            outcome = gate.promote_statement(
                conn,
                statement_id,
                reviewer_id=reviewer_id,
                decision=decision,
                reason=reason,
                to_verification_status=(
                    TO_VERIFICATION_STATUS if decision in gate.PROMOTING_DECISIONS else None
                ),
                reason_category=reason_category,
                commit=False,
            )
            if outcome["promoted"]:
                promoted += 1
            elif decision == "hold":
                held += 1
            else:
                rejected += 1
            results.append(
                {"statement_id": statement_id, "action": "apply", **outcome}
            )

        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise

    return {
        "batch_id": manifest.get("batch_id"),
        "card_id": card_id,
        "reviewer_id": reviewer_id,
        "committed": commit,
        "dry_run": not commit,
        "counts": {
            "total": len(entries),
            "promoted": promoted,
            "held": held,
            "rejected": rejected,
            "skipped_idempotent": skipped,
        },
        "results": results,
    }


# --- CLI --------------------------------------------------------------------

def _cmd_propose(args: argparse.Namespace) -> int:
    conn = db.open_db(args.db)
    try:
        manifest = build_manifest(
            conn, offset=args.offset, limit=args.limit,
            meeting_date=args.meeting_date, transcript_id=args.transcript_id,
        )
    except PromotionScopeError as exc:
        print(f"scope error: {exc}", file=sys.stderr)
        return EXIT_SCOPE
    finally:
        conn.close()

    if args.out is not None:
        path = write_manifest(manifest, args.out)
        print(f"wrote manifest ({len(manifest['statements'])} statements): {path}")
    else:
        print(json.dumps(manifest, indent=2, sort_keys=True))
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    conn = db.open_db(args.db)
    try:
        summary = apply_manifest(
            conn, manifest, card_id=args.card, commit=args.commit
        )
    except PromotionScopeError as exc:
        print(f"scope error: {exc}", file=sys.stderr)
        return EXIT_SCOPE
    except gate.ReviewerGateError as exc:
        print(f"refused (fail-closed): {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()

    banner = "COMMIT" if summary["committed"] else "DRY-RUN (no writes; pass --commit to apply)"
    print(f"[{banner}] batch {summary['batch_id']} card {summary['card_id']}")
    for r in summary["results"]:
        if r["action"] == "skip-already-applied":
            print(f"  = {r['statement_id']}: already applied ({r['decision']}) — skipped")
        else:
            print(
                f"  {'✓' if r.get('promoted') else '·'} {r['statement_id']}: "
                f"{r['decision']} -> {r['to_verification_status']} "
                f"(promoted={r.get('promoted')})"
            )
    c = summary["counts"]
    print(
        f"totals: {c['total']} in batch | promoted {c['promoted']} | held {c['held']} "
        f"| rejected {c['rejected']} | skipped {c['skipped_idempotent']}"
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="GOV-648 reviewer-promotion batch CLI (2026-06-23 TOA pilot)."
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH,
                        help="registry DB path (default: the local registry)")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_prop = sub.add_parser("propose", help="emit a local review-packet manifest (read-only)")
    p_prop.add_argument("--meeting-date", default=PILOT_MEETING_DATE,
                        help="meeting date to slice (default: the pilot 2026-06-23); "
                             "with --transcript-id it becomes a fail-closed cross-check")
    p_prop.add_argument("--transcript-id", type=int, default=None,
                        help="scope the slice to ONE transcript (the scale selector; "
                             "required to disambiguate a date with two timed transcripts)")
    p_prop.add_argument("--offset", type=int, default=0,
                        help="start index into the slice (segment_index order)")
    p_prop.add_argument("--limit", type=int, default=MAX_BATCH,
                        help=f"batch size, 1..{MAX_BATCH} (default {MAX_BATCH})")
    p_prop.add_argument("--out", type=Path, default=None,
                        help="write manifest to this local/gitignored path (else stdout)")
    p_prop.set_defaults(func=_cmd_propose)

    p_app = sub.add_parser("apply", help="apply an accepted manifest (dry-run by default)")
    p_app.add_argument("--manifest", required=True, help="path to the accepted manifest JSON")
    p_app.add_argument("--card", required=True, help="accepting board interaction/card id")
    p_app.add_argument("--commit", action="store_true",
                       help="actually write (default is dry-run per the company gate)")
    p_app.set_defaults(func=_cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
