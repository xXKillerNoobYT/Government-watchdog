"""Stage 2 read-surface traceability + audit trail (GOV-306, Stage 2.12).

Owner: BackendCrawlerEngineer. Parent: GOV-305. Read-only, deterministic,
Alpine-only. Extends the proven read-only auditor pattern of
:mod:`ai_provenance` (GOV-278): open the DB, run SELECTs, never write — there is
no ``--apply``, so it is inherently dry-run and doubles as a CI gate (exit 1 on
any break).

For a watchdog product the trust backbone is **provenance**: every reviewer-
internal value the read surface would surface — a confidence label, a safe
speaker label, a completeness-gap card, an evidence citation — must trace back to
a canonical source with **no orphan, no drift, no leak**. This module proves that
end-to-end traceability invariant. Each check **independently recomputes** the
expected value from the canonical columns the served body itself no longer carries
(``to_web_safe`` strips ``segment_id`` / ``transcript_class`` /
``speaker_attribution_id`` / the raw ``to_source_id``) and compares it to the
:mod:`read_api` projection. A divergence flips ``clean=False``.

The seven first-class checks (each a report key):

1. **statement_grounding** — every served statement resolves through the FULL
   canonical chain to an existing source/segment (segment -> transcript, or an
   evidence link whose ``to_source_id`` still resolves to a ``sources`` row). A
   served-but-ungrounded row is an orphan. Independent of ``read_api``'s serving
   gate, which only checks the segment row OR a link *exists*, not that the chain
   resolves end-to-end.
2. **confidence_label** — every served ``confidence_label`` equals the
   deterministic GOV-283 ``CONFIDENCE_LABEL_BY_CLASS[transcript_class]`` mapping
   recomputed from the canonical ``transcript_class`` column (fail-closed to the
   conservative default through every break in the chain).
3. **speaker_label** — every served ``speaker_label`` is consistent with the
   GOV-290 proven-safe naming gate: a NAME (a label outside the SSOT safe set)
   surfaces only when the canonical attribution row is ``attributed`` AND
   ``speaker_class`` is in ``speakers.AUTO_NAMEABLE_CLASSES``.
4. **completeness_gap_parity** — the projected gap-card set matches canonical
   ``completeness_gaps`` (migration 0015) 1:1 by ``gap_id``; the projected
   ``no_primary_source`` count equals the canonical countable rows.
5. **ai_provenance** — every ``produced_by='ai'`` row carries a resolvable
   ``ai_extraction_runs`` row with ``error_status='ok'``. Reuses
   :func:`ai_provenance.audit_ai_provenance` verbatim — NOT re-invented; the
   full-DB scan is a (stronger) superset of "rows underlying a surfaced item".
6. **raw_preservation** — every served statement's grounding source unit links to
   a preserved raw predecessor (GOV-262): the transcript carries a recorded
   ``sha256`` text hash, or the evidence source is in
   ``raw_preservation.PRESERVED_STATES`` / has a hashed ``documents`` child — so
   the citation is reproducible.
7. **transport** — the assembled surfaced body has zero raw FS paths / structured
   PII (reuses :func:`read_api.assert_no_raw_paths` + the concept-map PII guard).

Hard constraints (GOV-306): NO migration, NO schema change, NO mutation, NO AI,
NO network. Does NOT touch ``publication.py`` (``to_web_safe`` /
``WEB_SAFE_FIELD_ALLOWLIST``) or any serving behavior; operates over the
reviewer-internal lane only. Reuses existing invariants — mirrors, never forks.

If a real traceability break is found that implies a defect in shipped code (e.g.
a drifting label on ``main``), STOP and escalate the failing row — the fix is a
separate scoped issue, never a silent self-heal (GOV-230 ABSOLUTE drift rule).

Usage:
    python scripts/stage2_traceability.py [--db PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ai_provenance as prov  # noqa: E402
import concept_map as cm  # noqa: E402
import db  # noqa: E402
import raw_preservation as rp  # noqa: E402
import read_api  # noqa: E402
import speakers as sp  # noqa: E402
import transcript_class as tc  # noqa: E402

# Imported from the SSOT — never re-declared, so the auditor cannot drift from the
# values read_api actually projects (GOV-306 SSOT-parity constraint).
_CONSERVATIVE_CONFIDENCE_LABEL = tc.CONFIDENCE_LABEL_BY_CLASS[tc.DEFAULT_TRANSCRIPT_CLASS]
# The exact set read_api may emit for a NON-named speaker. A served label outside
# this set is a name, and a name is permitted ONLY through the proven naming gate.
_SAFE_SPEAKER_LABELS = frozenset({sp.SAFE_GENERIC_LABEL, sp.SAFE_COMMUNITY_LABEL})


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------------------
# Served set — the integration corpus the audit runs over (also delivers the
# 2.10 integration-coverage byproduct: it must assemble + run read_api).
# ---------------------------------------------------------------------------


def _served_records(conn: sqlite3.Connection) -> list[tuple[str, dict[str, Any]]]:
    """Every reviewer-internal served statement, tagged with its serving lane.

    The published lane (owner-published) and the reviewer-internal lane (cleared,
    publish-pending) are disjoint by construction (a publishable row is never in
    the reviewer view), so the union is dedup-free. Returns ``[(lane, record), ...]``.
    """
    if not _table_exists(conn, "statements"):
        return []
    served: list[tuple[str, dict[str, Any]]] = []
    served.extend(("published", r) for r in read_api.published_records(conn))
    served.extend(("reviewer_internal", r) for r in read_api.reviewer_internal_records(conn))
    return served


def _canonical_statement(conn: sqlite3.Connection, statement_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT statement_id, segment_id, speaker_attribution_id "
        "FROM statements WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Check 1 — statement->source grounding (no orphan).
# ---------------------------------------------------------------------------


def statement_grounded(conn: sqlite3.Connection, statement_id: str) -> bool:
    """True iff ``statement_id`` resolves through the FULL canonical chain.

    Grounded means EITHER its ``segment_id`` resolves to a ``transcript_segments``
    row whose parent ``transcripts`` row exists, OR it carries >=1 ``evidence_link``
    whose ``to_source_id`` still resolves to a ``sources`` row. This is a strict
    superset of read_api's serving gate (which checks the segment row alone OR that
    a link merely *exists*), so a served row with a dangling transcript or a deleted
    evidence source — which read_api would still serve — is caught here as an orphan.
    """
    row = _canonical_statement(conn, statement_id)
    if row is None:
        return False  # served but no canonical statement row -> orphan
    segment_id = row["segment_id"]
    if segment_id:
        seg = conn.execute(
            "SELECT 1 FROM transcript_segments ts JOIN transcripts t ON t.id = ts.transcript_id "
            "WHERE ts.segment_id = ?",
            (segment_id,),
        ).fetchone()
        if seg is not None:
            return True
    if _table_exists(conn, "evidence_links"):
        for link in conn.execute(
            "SELECT to_source_id FROM evidence_links "
            "WHERE from_node_id = ? AND from_node_type = 'statement'",
            (statement_id,),
        ).fetchall():
            sid = link["to_source_id"]
            if sid and conn.execute(
                "SELECT 1 FROM sources WHERE source_id = ?", (sid,)
            ).fetchone() is not None:
                return True
    return False


# ---------------------------------------------------------------------------
# Check 2 — confidence_label provenance (no drift), recomputed from canonical.
# ---------------------------------------------------------------------------


def canonical_confidence_label(conn: sqlite3.Connection, statement_id: str) -> str:
    """Recompute the expected GOV-283 confidence label from canonical columns.

    Mirrors (does not call) ``read_api._confidence_label_for``: resolves
    ``statement -> segment_id -> transcript_segments.transcript_id ->
    transcripts.transcript_class`` and maps it through the SSOT
    ``transcript_class.CONFIDENCE_LABEL_BY_CLASS``. Fail-closed to
    :data:`_CONSERVATIVE_CONFIDENCE_LABEL` through every break in the chain. Re-runs
    the join independently against the canonical ``transcript_class`` column — which
    the served body no longer carries — so a divergence from the projected label is
    real drift, not a tautology.
    """
    row = _canonical_statement(conn, statement_id)
    if row is None:
        return _CONSERVATIVE_CONFIDENCE_LABEL
    segment_id = row["segment_id"]
    if not segment_id:
        return _CONSERVATIVE_CONFIDENCE_LABEL
    tr = conn.execute(
        "SELECT t.transcript_class AS transcript_class "
        "FROM transcript_segments ts JOIN transcripts t ON t.id = ts.transcript_id "
        "WHERE ts.segment_id = ?",
        (segment_id,),
    ).fetchone()
    if tr is None or tr["transcript_class"] is None:
        return _CONSERVATIVE_CONFIDENCE_LABEL
    return tc.CONFIDENCE_LABEL_BY_CLASS.get(
        tr["transcript_class"], _CONSERVATIVE_CONFIDENCE_LABEL
    )


# ---------------------------------------------------------------------------
# Check 3 — speaker_label provenance (no name leak).
# ---------------------------------------------------------------------------


def speaker_label_consistent(
    conn: sqlite3.Connection, statement_id: str, served_label: str
) -> bool:
    """True iff a served ``speaker_label`` is consistent with the GOV-290 gate.

    A label inside the SSOT safe set (:data:`_SAFE_SPEAKER_LABELS`) is always
    consistent — it names no one. Any OTHER label is a name, and a name is permitted
    ONLY when the canonical attribution row is ``attributed`` AND ``speaker_class``
    is in ``speakers.AUTO_NAMEABLE_CLASSES`` (the one proven-safe naming gate). A
    name on a row that fails the gate — or with no/unresolvable attribution — is a
    leak.
    """
    if served_label in _SAFE_SPEAKER_LABELS:
        return True
    row = _canonical_statement(conn, statement_id)
    if row is None:
        return False
    attribution_id = row["speaker_attribution_id"]
    if not attribution_id:
        return False  # a name with no backing attribution -> leak
    attr = conn.execute(
        "SELECT attribution_state, speaker_class "
        "FROM speaker_attributions WHERE speaker_attribution_id = ?",
        (attribution_id,),
    ).fetchone()
    if attr is None:
        return False
    return (
        attr["attribution_state"] == "attributed"
        and attr["speaker_class"] in sp.AUTO_NAMEABLE_CLASSES
    )


# ---------------------------------------------------------------------------
# Check 4 — completeness_gap parity (no phantom / no missing).
# ---------------------------------------------------------------------------


def gap_parity(
    conn: sqlite3.Connection, projected_cards: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare the projected gap-card set to canonical ``completeness_gaps`` 1:1.

    Keyed on the stable ``gap_id``. ``missing`` = a canonical gap absent from the
    projection (a gap silently dropped — forbidden by GOV-125); ``phantom`` = a
    projected card with no canonical row. The ``no_primary_source`` headline count
    is compared canonical-vs-projected so the surfaced "92 meetings" figure can
    never drift from the table.
    """
    if not _table_exists(conn, "completeness_gaps"):
        canonical_ids: set[str] = set()
        canonical_nps = 0
    else:
        canonical_ids = {
            r[0] for r in conn.execute("SELECT gap_id FROM completeness_gaps")
        }
        canonical_nps = conn.execute(
            "SELECT count(*) FROM completeness_gaps WHERE gap_type = 'no_primary_source'"
        ).fetchone()[0]
    projected_ids = {c["gap_id"] for c in projected_cards}
    projected_nps = sum(1 for c in projected_cards if c.get("gap_type") == "no_primary_source")
    missing = sorted(canonical_ids - projected_ids)
    phantom = sorted(projected_ids - canonical_ids)
    return {
        "canonical_count": len(canonical_ids),
        "projected_count": len(projected_ids),
        "no_primary_source_count": canonical_nps,
        "no_primary_source_projected": projected_nps,
        "missing": missing,
        "phantom": phantom,
        "clean": not missing and not phantom and canonical_nps == projected_nps,
    }


# ---------------------------------------------------------------------------
# Check 6 — raw-preservation linkage (reproducible citation).
# ---------------------------------------------------------------------------


def raw_linked(conn: sqlite3.Connection, statement_id: str) -> bool:
    """True iff the statement's grounding source unit has a preserved raw predecessor.

    Reproducibility (GOV-262): a citation is reproducible only if the source unit it
    rests on was preserved. Satisfied when the grounding transcript carries a
    recorded ``sha256`` (the preserved text hash), OR an evidence-source is in
    ``raw_preservation.PRESERVED_STATES`` / has a ``documents`` child with a
    non-NULL ``sha256``. A served, grounded statement whose unit has no preserved
    raw is an un-reproducible citation.
    """
    row = _canonical_statement(conn, statement_id)
    if row is None:
        return False
    segment_id = row["segment_id"]
    if segment_id:
        tr = conn.execute(
            "SELECT t.sha256 AS sha256 "
            "FROM transcript_segments ts JOIN transcripts t ON t.id = ts.transcript_id "
            "WHERE ts.segment_id = ?",
            (segment_id,),
        ).fetchone()
        if tr is not None and tr["sha256"]:
            return True
    if _table_exists(conn, "evidence_links"):
        for link in conn.execute(
            "SELECT to_source_id FROM evidence_links "
            "WHERE from_node_id = ? AND from_node_type = 'statement'",
            (statement_id,),
        ).fetchall():
            sid = link["to_source_id"]
            if not sid:
                continue
            src = conn.execute(
                "SELECT raw_preservation_status FROM sources WHERE source_id = ?", (sid,)
            ).fetchone()
            if src is not None and src["raw_preservation_status"] in rp.PRESERVED_STATES:
                return True
            child = conn.execute(
                "SELECT 1 FROM documents WHERE source_id = ? AND sha256 IS NOT NULL LIMIT 1",
                (sid,),
            ).fetchone()
            if child is not None:
                return True
    return False


# ---------------------------------------------------------------------------
# Check 7 — transport guard (no raw FS path / PII leak in the surfaced body).
# ---------------------------------------------------------------------------


def transport_clean(conn: sqlite3.Connection) -> dict[str, Any]:
    """Sweep the assembled surfaced body for raw paths / structured PII.

    Assembles the surfaced lanes directly (NOT via ``build_response``, whose own
    sweep would mask a regression) and runs the two independent guards: the GOV-34
    ``read_api.assert_no_raw_paths`` transport sweep and the concept-map
    ``assert_no_pii`` structured-PII sweep over the serialized body.
    """
    body: dict[str, Any] = {
        "records": read_api.published_records(conn) if _table_exists(conn, "statements") else [],
        "reviewer_internal_records": (
            read_api.reviewer_internal_records(conn) if _table_exists(conn, "statements") else []
        ),
        "completeness_gaps": (
            read_api.completeness_gap_cards(conn)
            if _table_exists(conn, "completeness_gaps")
            else []
        ),
    }
    try:
        read_api.assert_no_raw_paths(body)
        cm.assert_no_pii(json.dumps(body, sort_keys=True), "stage2_traceability.transport")
    except (read_api.RawPathLeak, cm.PiiGuardError) as exc:
        return {"clean": False, "error": str(exc)}
    return {"clean": True, "error": None}


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def audit_stage2_traceability(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run all seven read-surface traceability checks. Returns a JSON-able report.

    ``clean`` is the conjunction of every check. Read-only: assembles + runs
    read_api, recomputes each expected value from canonical columns, compares, and
    reports drift — it never writes.
    """
    served = _served_records(conn)

    # 1 — grounding.
    orphans = [
        {"statement_id": rec["statement_id"], "lane": lane}
        for lane, rec in served
        if not statement_grounded(conn, rec["statement_id"])
    ]
    grounding = {"checked": len(served), "orphans": orphans, "clean": not orphans}

    # 2 — confidence_label drift.
    conf_drift = []
    for lane, rec in served:
        expected = canonical_confidence_label(conn, rec["statement_id"])
        observed = rec.get("confidence_label")
        if observed != expected:
            conf_drift.append(
                {"statement_id": rec["statement_id"], "lane": lane,
                 "expected": expected, "observed": observed}
            )
    confidence = {"checked": len(served), "drift": conf_drift, "clean": not conf_drift}

    # 3 — speaker_label name leak.
    leaks = []
    for lane, rec in served:
        label = rec.get("speaker_label")
        if not speaker_label_consistent(conn, rec["statement_id"], label):
            leaks.append(
                {"statement_id": rec["statement_id"], "lane": lane, "speaker_label": label}
            )
    speaker = {"checked": len(served), "leaks": leaks, "clean": not leaks}

    # 4 — completeness-gap parity.
    cards = read_api.completeness_gap_cards(conn) if _table_exists(conn, "completeness_gaps") else []
    gaps = gap_parity(conn, cards)

    # 5 — AI provenance (reuse GOV-278 auditor verbatim).
    ai_report = prov.audit_ai_provenance(conn)
    ai_check = {
        "ai_statement_count": ai_report["ai_statement_count"],
        "orphan_count": ai_report["orphan_count"],
        "non_ok_run": ai_report["non_ok_run"],
        "clean": ai_report["clean"],
    }

    # 6 — raw-preservation linkage.
    unlinked = [
        {"statement_id": rec["statement_id"], "lane": lane}
        for lane, rec in served
        if statement_grounded(conn, rec["statement_id"])  # only meaningful for grounded rows
        and not raw_linked(conn, rec["statement_id"])
    ]
    raw_check = {"checked": len(served), "unlinked": unlinked, "clean": not unlinked}

    # 7 — transport.
    transport = transport_clean(conn)

    clean = all(
        c["clean"]
        for c in (grounding, confidence, speaker, gaps, ai_check, raw_check, transport)
    )
    return {
        "served_count": len(served),
        "statement_grounding": grounding,
        "confidence_label": confidence,
        "speaker_label": speaker,
        "completeness_gap_parity": gaps,
        "ai_provenance": ai_check,
        "raw_preservation": raw_check,
        "transport": transport,
        "clean": clean,
    }


def _format_report(report: dict[str, Any], db_path: Path) -> str:
    g = report["statement_grounding"]
    c = report["confidence_label"]
    s = report["speaker_label"]
    p = report["completeness_gap_parity"]
    a = report["ai_provenance"]
    r = report["raw_preservation"]
    t = report["transport"]
    lines = [
        f"Stage 2 read-surface traceability audit (GOV-306) — {db_path}",
        f"  served statements                 : {report['served_count']}",
        f"  1 statement->source grounding     : {'OK' if g['clean'] else f'BREAK ({len(g['orphans'])} orphan)'}",
        f"  2 confidence_label provenance     : {'OK' if c['clean'] else f'DRIFT ({len(c['drift'])})'}",
        f"  3 speaker_label provenance        : {'OK' if s['clean'] else f'NAME LEAK ({len(s['leaks'])})'}",
        f"  4 completeness_gap parity         : {'OK' if p['clean'] else 'BREAK'} "
        f"(canonical={p['canonical_count']} projected={p['projected_count']} "
        f"no_primary_source={p['no_primary_source_count']})",
        f"  5 AI provenance chain             : {'OK' if a['clean'] else f'ORPHAN ({a['orphan_count']})'} "
        f"(ai_rows={a['ai_statement_count']})",
        f"  6 raw-preservation linkage        : {'OK' if r['clean'] else f'UNLINKED ({len(r['unlinked'])})'}",
        f"  7 transport (no raw path / PII)   : {'OK' if t['clean'] else 'LEAK'}",
        f"  TRACEABLE / CLEAN                  : {report['clean']}",
    ]
    for o in g["orphans"]:
        lines.append(f"    ORPHAN: {o['statement_id']} ({o['lane']})")
    for d in c["drift"]:
        lines.append(
            f"    DRIFT confidence_label: {d['statement_id']} "
            f"served={d['observed']!r} expected={d['expected']!r}"
        )
    for leak in s["leaks"]:
        lines.append(f"    NAME LEAK speaker_label: {leak['statement_id']} -> {leak['speaker_label']!r}")
    for gid in p["missing"]:
        lines.append(f"    GAP MISSING (not projected): {gid}")
    for gid in p["phantom"]:
        lines.append(f"    GAP PHANTOM (no canonical row): {gid}")
    for u in r["unlinked"]:
        lines.append(f"    UNLINKED raw: {u['statement_id']} ({u['lane']})")
    if not t["clean"]:
        lines.append(f"    TRANSPORT LEAK: {t['error']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 2 read-surface traceability audit (read-only). GOV-306 Stage 2.12."
    )
    parser.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help=f"path to the sqlite DB (default: {db.DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable JSON report"
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"stage2 traceability audit: DB not found at {args.db}", file=sys.stderr)
        return 2

    with db.open_db(args.db) as conn:
        report = audit_stage2_traceability(conn)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report, args.db))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
