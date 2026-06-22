"""Stage 3.13 read-surface back-gap / coverage-regression auditor (GOV-411).

Owner: BackendCrawlerEngineer. Parent: GOV-405 (CEO Stage-3 sequencing mandate).
Read-only, deterministic, Alpine-only, reviewer-internal. This is the GOV-322 /
Stage 2.13 back-gap win — :mod:`stage2_backgap` — **one layer up**, over the live
**Stage-3** read surface (exactly as GOV-406 / Stage 3.12 was the GOV-306 traceability
analogue one layer up). It **reuses** the proven Stage-2 membership oracle and every
existing Stage-3 projection **by reference** — it never forks them.

Stage 3.12 (:mod:`stage3_traceability`, merged PR #73 -> ``5b6ab96``) proved the
**forward** direction: every value the Stage-3 surface *serves* traces back to a
canonical source — no orphan, no drift, no leak. But a forward auditor passes even if a
fail-closed overlay default **silently dropped a whole record class** — every
*remaining* served row is still grounded. That silent-shrinkage blind spot ("back-gap")
is the highest-severity trust failure for a first external reviewer: the surface looks
complete while showing *less* than reality. This module closes the **inverse-
completeness** direction over the Stage-3 read surface.

The Stage-3 surface this audits is the family of pure projections layered on the
already-web-safe Stage-2 read surface (``scripts/read_api.py``):

* :func:`stage3_card_feed.build_card_feed` (3.05, GOV-347) — the timeline card feed;
* :func:`stage3_verify_at_source.build_verify_at_source` (3.07, GOV-376) — the
  per-card verify-at-source drill-down;
* :func:`stage3_preservation_audit.build_preservation_overlay` (3.04, GOV-367) — the
  reviewer-internal raw-preservation overlay;
* :func:`stage3_source_inventory.build_inventory` (3.03, GOV-364) — the source/data
  inventory.

The six first-class checks (each a report key; CLI exit 1 if any non-clean) — the
Stage-3 analogs of :mod:`stage2_backgap`'s six, raised to the Stage-3 surface:

1. **card_feed_no_backgap** — INDEPENDENTLY recompute the set of canonical statements
   eligible for the Stage-3 reviewer card feed (reusing the GOV-322 membership oracle
   :func:`stage2_backgap.reviewer_eligible_ids`, which mirrors the read_api gate over
   canonical columns + SSOT leaf predicates — NOT the assembly loop) and assert the set
   the LIVE feed actually serves covers it: ``eligible - served == ∅``. The served set
   is read back from :func:`stage3_card_feed.build_card_feed` (the Stage-3 projection),
   so a regression that drops a record class from EITHER read_api OR the feed layer is
   caught. Recomputing independently is essential — calling the same assembly would
   compare it to itself and let a shrink pass silently.
2. **public_lane_no_backgap** — the same reconciliation for
   :func:`read_api.published_records` vs the independently-recomputed publish-eligible
   set (:func:`stage2_backgap.publish_eligible_ids`). Alpine is reviewer-internal, so
   the public lane stays empty by construction; this also asserts NO Stage-3 body
   silently *gains* a public lane (every assembled body stays ``access:
   reviewer_internal``).
3. **completeness_gap_coverage_parity** — every canonical ``completeness_gaps``
   (migration 0015) row still surfaces 1:1 by ``gap_id`` via
   :func:`read_api.completeness_gap_cards` AND rides through the Stage-3 feed's gap
   cards 1:1 by the derived handle; the ``no_primary_source`` headline count is
   reconciled too. The inverse complement of GOV-406's drift ``missing`` direction.
4. **stage3_overlay_presence_no_regression** — for every served reviewer card, all
   Stage-3 overlays still attach + are SSOT-bounded: the verify-at-source drill-down
   (1:1 bijection via :func:`stage3_verify_at_source.assert_covers_surface`, plus each
   ``resolvability_status`` / ``verify_at_source_status`` in its frozen vocab); the
   carried-up Stage-2 composition envelope keys (``provenance_status`` /
   ``confidence_label`` / ``speaker_label``); the source/data-inventory linkage (every
   canonical source a card traces to is present in the inventory); and — for every
   correction card — the traceability audit-trail row. The preservation overlay is
   asserted present body-level (it keys per stored object, not per statement): it
   composes, exposes ``manifest_digest``, and does not silently drop the whole unit
   class when stored objects exist. An overlay must not silently stop attaching to a
   record class.
5. **stageN_field_floor** — the Stage-1/Stage-2 base fields each card carried *before*
   Stage-3 layering remain present: the served record's identity (``statement_id``),
   the eligible ``ui_status`` (bounded to the 10-value :data:`_UI_STATUS_BASE`), and the
   ``evidence`` drawer; plus the projected feed card's structural floor (``handle`` /
   ``type`` / ``jurisdiction`` / ``status`` / ``evidence``). No Stage-3 overlay silently
   removed an earlier-stage field.
6. **determinism + read-only** — two independent snapshot passes over every Stage-3
   coverage set are byte-identical, and the module performs zero writes (no ``--apply``;
   SELECT-only by construction).

Hard constraints (GOV-411): NO migration, NO schema change, NO mutation, NO AI, NO
network, NO new envelope key, NO public-projection change. **0 production diff** to
``read_api.py`` / ``publication.py`` — additive module + test only. Premium-gate: N/A by
construction (serves no new field, changes no public projection; NON-unlock — subgoal
``8918271b`` flips ``achieved`` only at CTO merge via the goal-ledger auto-sync). Imports
and reuses :mod:`read_api` / :mod:`stage2_backgap` / the Stage-3 projection modules
verbatim — it never reimplements a serving/serialization gate; it only recomputes the
*eligible membership set* independently and reconciles it against what the live Stage-3
surface actually serves.

No ``--apply`` -> inherently dry-run; exit 1 on any back-gap so it doubles as a CI gate.
If a real back-gap is found that implies a defect in shipped ``main`` code (e.g. an
overlay's fail-closed default dropping a record class), STOP and escalate the failing
rows to CTO — the fix is a SEPARATE scoped issue, never a silent self-heal or a patch to
``read_api`` / ``publication`` from this ticket (GOV-230 ABSOLUTE drift rule; GOV-322
pass-up precedent). This ticket ships the net, not the patch.

Usage:
    python scripts/stage3_backgap.py [--db PATH] [--json]
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

import db  # noqa: E402
import publication as pub  # noqa: E402
import raw_preservation as rp  # noqa: E402  (REPO_ROOT default for the preservation replay)
import read_api  # noqa: E402  (read-only: lanes + predicates, no mutation)
import stage2_backgap as s2bg  # noqa: E402  (GOV-322 membership oracle, reused not forked)
import stage3_card_feed as feed  # noqa: E402  (3.05 projection, read not forked)
import stage3_preservation_audit as pres  # noqa: E402  (3.04 overlay, read not forked)
import stage3_source_inventory as inv  # noqa: E402  (3.03 inventory, read not forked)
import stage3_traceability as trace3  # noqa: E402  (3.12 audit-trail predicate, reused)
import stage3_verify_at_source as vas  # noqa: E402  (3.07 projection, read not forked)

# Imported from the owning SSOT modules — never re-declared, so the auditor cannot drift
# from the values the live surface actually projects.
_UI_STATUS_BASE = pub.ALLOWED_UI_STATUSES           # the 10-value Stage-1 ui_status floor
_PROVENANCE_VALUES = read_api.PROVENANCE_STATUS_VALUES  # GOV-311 provenance vocabulary
_CORRECTED_UI_STATUS = "corrected"                  # publication.compute_ui_status rule #5

# The feed status vocabulary (GOV-346 §2) — the only statuses a Stage-3 card may carry.
_FEED_STATUS_VALUES = frozenset({
    feed.STATUS_SOURCE_MISSING,
    feed.STATUS_CORRECTED,
    feed.STATUS_AI_PRESENTED,
    feed.STATUS_VERIFIED,
    feed.STATUS_UNVERIFIED,
})
# The Stage-1/Stage-2 floor keys a served reviewer record carries before Stage-3
# layering; a Stage-3 overlay that stripped one is a back-gap of its own.
_RECORD_FLOOR_KEYS = ("statement_id", "ui_status", "evidence")
# The structural floor keys every projected feed card must carry.
_CARD_FLOOR_KEYS = ("handle", "type", "jurisdiction", "status", "evidence")
# Reviewer-internal access marker every Stage-3 body must keep (never "public").
_REVIEWER_INTERNAL = "reviewer_internal"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _table_has_rows(conn: sqlite3.Connection, name: str) -> bool:
    return (
        _table_exists(conn, name)
        and conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is not None
    )


def _served_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every reviewer-internal served record the Stage-3 card surface projects.

    The Stage-3 card feed and verify-at-source drill-down are both 1:1 covers of
    :func:`read_api.reviewer_internal_records` (record cards) plus the gap lane; this is
    that record set — the exact set every Stage-3 record card derives from.
    """
    if not _table_exists(conn, "statements"):
        return []
    return list(read_api.reviewer_internal_records(conn))


def _record_handle(record: dict[str, Any]) -> str:
    """The derived feed card handle for a served record — reused verbatim from the feed."""
    return feed._record_card(record)["handle"]


# ---------------------------------------------------------------------------
# Check 1 — Stage-3 card-feed back-gap (independent eligible vs live-feed served).
# ---------------------------------------------------------------------------


def card_feed_no_backgap(
    conn: sqlite3.Connection, feed_body: dict[str, Any], served: list[dict[str, Any]]
) -> dict[str, Any]:
    """Independently-recomputed reviewer-eligible set vs the records the LIVE feed serves.

    ``eligible`` is the GOV-322 membership oracle
    (:func:`stage2_backgap.reviewer_eligible_ids`) — recomputed over canonical
    ``statements`` rows from the stable SSOT leaf predicates, NOT by calling the read_api
    assembly. ``served_via_feed`` is reconstructed from the records whose card handle
    actually appears in the live :func:`stage3_card_feed.build_card_feed` output, so the
    reconciliation catches a record class dropped by EITHER the read_api lane OR the
    Stage-3 feed layer. ``backgap`` (eligible-but-not-served) is the silent-shrinkage
    failure this auditor exists to catch; ``over_served`` (served-but-not-eligible) is the
    forward-direction concern GOV-406 owns — surfaced for visibility but it does NOT, by
    itself, flip this verdict.
    """
    eligible = s2bg.reviewer_eligible_ids(conn)
    handle_to_sid = {_record_handle(rec): rec["statement_id"] for rec in served}
    feed_record_handles = {
        c.get("handle")
        for c in feed_body.get("cards", [])
        if c.get("type") != feed.TYPE_SOURCE_MISSING
    }
    served_via_feed = {
        sid for handle, sid in handle_to_sid.items() if handle in feed_record_handles
    }
    backgap = sorted(eligible - served_via_feed)
    over_served = sorted(served_via_feed - eligible)
    return {
        "eligible_count": len(eligible),
        "served_count": len(served_via_feed),
        "backgap": backgap,
        "over_served": over_served,
        "clean": not backgap,
    }


# ---------------------------------------------------------------------------
# Check 2 — public-lane back-gap + no silent public-lane gain.
# ---------------------------------------------------------------------------


def public_lane_no_backgap(
    conn: sqlite3.Connection, bodies: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Independently-recomputed publish-eligible set vs the served public lane.

    Reuses the GOV-322 publish-eligible oracle
    (:func:`stage2_backgap.publish_eligible_ids`). On a pre-publish Alpine DB both sets
    are empty, but the reconciliation still catches a future regression that drops a
    published row. Additionally — the Stage-3 surface stays reviewer-internal by
    construction — every assembled body must keep ``access: reviewer_internal``; a body
    that silently flipped to ``public`` is its own (forward) back-gap of the access
    boundary, listed in ``public_lane_leaks``.
    """
    eligible = s2bg.publish_eligible_ids(conn)
    served = {r["statement_id"] for r in read_api.published_records(conn)}
    backgap = sorted(eligible - served)
    public_lane_leaks = sorted(
        name for name, body in bodies.items() if body.get("access") != _REVIEWER_INTERNAL
    )
    return {
        "eligible_count": len(eligible),
        "served_count": len(served),
        "backgap": backgap,
        "public_lane_leaks": public_lane_leaks,
        "clean": not backgap and not public_lane_leaks,
    }


# ---------------------------------------------------------------------------
# Check 3 — completeness-gap coverage parity (no gap silently dropped).
# ---------------------------------------------------------------------------


def completeness_gap_coverage_parity(
    conn: sqlite3.Connection, feed_body: dict[str, Any]
) -> dict[str, Any]:
    """Every canonical ``completeness_gaps`` row still surfaces 1:1 — through the feed.

    Two legs: (a) canonical-table vs :func:`read_api.completeness_gap_cards` parity by
    ``gap_id`` plus the ``no_primary_source`` headline-count parity (the Stage 2.13
    direction); (b) every projected gap card rides through the Stage-3 feed as a gap
    card, matched 1:1 by the derived handle (``card_handle(TYPE_SOURCE_MISSING, gap_id)``,
    an injective function of ``gap_id``). A gap dropped by EITHER layer makes a list
    non-empty.
    """
    if not _table_exists(conn, "completeness_gaps"):
        return {
            "canonical_count": 0, "projected_count": 0, "feed_gap_count": 0,
            "no_primary_source_canonical": 0, "no_primary_source_projected": 0,
            "dropped": [], "feed_dropped": [], "clean": True,
        }
    canonical_ids = {r[0] for r in conn.execute("SELECT gap_id FROM completeness_gaps")}
    canonical_nps = conn.execute(
        "SELECT count(*) FROM completeness_gaps WHERE gap_type = 'no_primary_source'"
    ).fetchone()[0]
    projected = read_api.completeness_gap_cards(conn)
    projected_ids = {c["gap_id"] for c in projected}
    projected_nps = sum(1 for c in projected if c.get("gap_type") == "no_primary_source")
    dropped = sorted(canonical_ids - projected_ids)

    expected_feed_handles = {
        feed.card_handle(feed.TYPE_SOURCE_MISSING, gid) for gid in projected_ids
    }
    feed_gap_handles = {
        c.get("handle")
        for c in feed_body.get("cards", [])
        if c.get("type") == feed.TYPE_SOURCE_MISSING
    }
    feed_dropped = sorted(expected_feed_handles - feed_gap_handles)
    return {
        "canonical_count": len(canonical_ids),
        "projected_count": len(projected_ids),
        "feed_gap_count": len(feed_gap_handles),
        "no_primary_source_canonical": canonical_nps,
        "no_primary_source_projected": projected_nps,
        "dropped": dropped,
        "feed_dropped": feed_dropped,
        "clean": not dropped and not feed_dropped and canonical_nps == projected_nps,
    }


# ---------------------------------------------------------------------------
# Check 4 — Stage-3 overlay presence (no overlay silently stops attaching).
# ---------------------------------------------------------------------------


def _card_overlay_violations(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    drill: dict[str, Any] | None,
    inventory_source_ids: set[str],
) -> list[str]:
    """Per-served-card overlay-presence violations (empty == every overlay attaches).

    Reconciles each Stage-3 overlay that keys per record against THIS served card:

    * **verify-at-source** — the drill-down for this handle exists, its
      ``verify_at_source_status`` is in the binary record-card SSOT, and every link's
      ``resolvability_status`` is in :data:`stage3_verify_at_source.RESOLVABILITY_VALUES`.
    * **composition envelope keys** (the carried-up Stage-2 overlays GOV-393 proved
      co-present) — ``provenance_status`` SSOT-bounded, ``confidence_label`` present,
      ``speaker_label`` a non-empty string.
    * **source/data-inventory linkage** — every canonical source this record traces to
      (:func:`stage3_source_inventory._record_source_ids`, reused) is present in the
      inventory's emitted source set; a sourced card whose source vanished from the
      inventory is a linkage regression.
    * **traceability audit-trail key** — a card rendered as a correction
      (``ui_status == 'corrected'``) has its Lane-5 ``reviewer_decisions`` audit row
      (:func:`stage3_traceability._has_correction_decision`, reused); the audit-trail
      overlay must not stop attaching to corrections.
    """
    out: list[str] = []
    if drill is None:
        out.append("verify-at-source drill-down absent")
    else:
        if drill.get("verify_at_source_status") not in vas.VERIFY_AT_SOURCE_VALUES:
            out.append(
                f"verify_at_source_status off-SSOT: {drill.get('verify_at_source_status')!r}"
            )
        for link in drill.get("links", []):
            if link.get("resolvability_status") not in vas.RESOLVABILITY_VALUES:
                out.append(
                    f"resolvability_status off-SSOT: {link.get('resolvability_status')!r}"
                )
    if record.get("provenance_status") not in _PROVENANCE_VALUES:
        out.append(f"provenance_status off-SSOT: {record.get('provenance_status')!r}")
    if "confidence_label" not in record:
        out.append("confidence_label overlay absent")
    speaker = record.get("speaker_label")
    if not isinstance(speaker, str) or not speaker.strip():
        out.append(f"speaker_label missing/empty: {speaker!r}")
    unlinked = sorted(inv._record_source_ids(conn, record) - inventory_source_ids)
    if unlinked:
        out.append(f"source-inventory linkage missing: {unlinked}")
    if record.get("ui_status") == _CORRECTED_UI_STATUS and not trace3._has_correction_decision(
        conn, record["statement_id"]
    ):
        out.append("correction audit-trail row absent")
    return out


def stage3_overlay_presence_no_regression(
    conn: sqlite3.Connection,
    served: list[dict[str, Any]],
    verify_body: dict[str, Any],
    pres_body: dict[str, Any],
    inventory_source_ids: set[str],
) -> dict[str, Any]:
    """Every Stage-3 overlay still attaches to every served record class (+ body-level).

    The verify-at-source drill-down must be a 1:1 bijection with the live feed
    (:func:`stage3_verify_at_source.assert_covers_surface`) — an overlay that stopped
    attaching to a record class makes ``bijection_ok`` False. Each served card's
    per-record overlays are then checked by :func:`_card_overlay_violations`. The
    preservation overlay keys per stored object (not per statement), so it is asserted
    present body-level: it composes, exposes ``manifest_digest``, every unit row exposes a
    boolean ``hash_ok`` + an SSOT ``preservation_state``, and the whole unit class is not
    silently empty when stored objects exist.
    """
    try:
        vas.assert_covers_surface(conn, verify_body)
        bijection_ok = True
        bijection_detail = ""
    except vas.VerifyCoverageError as exc:
        bijection_ok = False
        bijection_detail = str(exc)

    drill_by_handle = {c.get("handle"): c for c in verify_body.get("cards", [])}
    missing: list[dict[str, Any]] = []
    for record in served:
        violations = _card_overlay_violations(
            conn, record, drill_by_handle.get(_record_handle(record)), inventory_source_ids
        )
        if violations:
            missing.append(
                {"statement_id": record.get("statement_id"), "violations": violations}
            )

    units = pres_body.get("units")
    preservation_ok = (
        isinstance(units, list)
        and "manifest_digest" in pres_body
        and all(
            isinstance(u.get("hash_ok"), bool)
            and u.get("preservation_state") in pres.PRESERVATION_STATES
            for u in units
        )
    )
    objects_exist = _table_has_rows(conn, "documents") or _table_has_rows(conn, "transcripts")
    if objects_exist and isinstance(units, list) and not units:
        preservation_ok = False  # overlay silently dropped the whole unit class

    return {
        "checked": len(served),
        "bijection_ok": bijection_ok,
        "bijection_detail": bijection_detail,
        "missing": missing,
        "preservation_present": preservation_ok,
        "unit_count": len(units) if isinstance(units, list) else None,
        "clean": bijection_ok and not missing and preservation_ok,
    }


# ---------------------------------------------------------------------------
# Check 5 — Stage-1/Stage-2 field floor (no Stage-3 overlay removed an earlier field).
# ---------------------------------------------------------------------------


def stageN_field_floor(
    conn: sqlite3.Connection, served: list[dict[str, Any]], feed_body: dict[str, Any]
) -> dict[str, Any]:
    """Every served record + its projected feed card still carries the earlier-stage floor.

    The Stage-3 projections are additive; none may strip the floor. For the served
    record the floor is its identity (``statement_id``), the eligible ``ui_status``
    (bounded to the 10-value :data:`_UI_STATUS_BASE`), and the ``evidence`` drawer. For
    the projected feed card the floor is the structural :data:`_CARD_FLOOR_KEYS`
    (``handle`` / ``type`` / ``jurisdiction`` / ``status`` / ``evidence``). A record or
    card missing any of these has silently regressed below its earlier-stage shape.
    """
    card_by_handle = {c.get("handle"): c for c in feed_body.get("cards", [])}
    breaches: list[dict[str, Any]] = []
    for record in served:
        violations = [k for k in _RECORD_FLOOR_KEYS if k not in record]
        if record.get("ui_status") not in _UI_STATUS_BASE:
            violations.append(f"ui_status off 10-value base: {record.get('ui_status')!r}")
        handle = _record_handle(record)
        card = card_by_handle.get(handle)
        if card is None:
            violations.append("feed card absent")
        else:
            violations.extend(f"card missing {k!r}" for k in _CARD_FLOOR_KEYS if k not in card)
        if violations:
            breaches.append(
                {"statement_id": record.get("statement_id"), "violations": violations}
            )
    return {"checked": len(served), "breaches": breaches, "clean": not breaches}


# ---------------------------------------------------------------------------
# Check 6 — determinism + read-only.
# ---------------------------------------------------------------------------


def _snapshot(conn: sqlite3.Connection, repo_root: Path = rp.REPO_ROOT) -> str:
    """A byte-stable JSON snapshot of every Stage-3 coverage set this auditor reconciles.

    Two snapshots over the same DB must be identical — every Stage-3 projection is a pure
    function of stored fields. Sorting every set makes the comparison order-stable.
    """
    has_gaps = _table_exists(conn, "completeness_gaps")
    feed_body = feed.build_card_feed(conn)
    verify_body = vas.build_verify_at_source(conn)
    pres_body = pres.build_preservation_overlay(conn, repo_root)
    return json.dumps(
        {
            "reviewer_eligible": sorted(s2bg.reviewer_eligible_ids(conn)),
            "feed_handles": sorted(c.get("handle") for c in feed_body.get("cards", [])),
            "verify_handles": sorted(c.get("handle") for c in verify_body.get("cards", [])),
            "publish_eligible": sorted(s2bg.publish_eligible_ids(conn)),
            "publish_served": sorted(
                r["statement_id"] for r in read_api.published_records(conn)
            ),
            "inventory_sources": sorted(
                e.get("source_id") for e in inv.source_inventory(conn)
            ),
            "gap_projected": sorted(
                c["gap_id"] for c in (read_api.completeness_gap_cards(conn) if has_gaps else [])
            ),
            "preservation_manifest": pres_body.get("manifest_digest"),
        },
        sort_keys=True,
    )


def determinism_read_only(
    conn: sqlite3.Connection, repo_root: Path = rp.REPO_ROOT
) -> dict[str, Any]:
    """Two snapshot passes byte-identical, and the DB row counts are unchanged.

    The read-only leg is structural (the module issues no INSERT/UPDATE/DELETE), but
    asserting the ``statements`` + ``completeness_gaps`` + ``sources`` row counts are
    unchanged across the two passes is a cheap, positive proof that running the audit
    mutated nothing.
    """
    def _counts() -> tuple[int, int, int]:
        def _n(table: str) -> int:
            return (
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                if _table_exists(conn, table) else 0
            )
        return _n("statements"), _n("completeness_gaps"), _n("sources")

    before = _counts()
    first = _snapshot(conn, repo_root)
    second = _snapshot(conn, repo_root)
    after = _counts()
    return {
        "byte_identical": first == second,
        "row_counts_stable": before == after,
        "clean": first == second and before == after,
    }


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def audit_backgap(
    conn: sqlite3.Connection, repo_root: Path = rp.REPO_ROOT
) -> dict[str, Any]:
    """Run all six Stage-3 read-surface back-gap / coverage checks. Returns a JSON report.

    ``clean`` is the conjunction of every check. Read-only: builds the live Stage-3
    projections (feed + verify-at-source + preservation + inventory), recomputes each
    eligible set independently from canonical columns, reconciles against what the live
    surface serves, and reports any coverage regression — it never writes.
    """
    served = _served_records(conn)
    feed_body = feed.build_card_feed(conn)
    verify_body = vas.build_verify_at_source(conn)
    pres_body = pres.build_preservation_overlay(conn, repo_root)
    inv_body = inv.build_inventory(conn)
    inventory_source_ids = {
        e.get("source_id") for e in inv_body.get("sources", []) if e.get("source_id")
    }
    bodies = {
        "card_feed": feed_body,
        "verify_at_source": verify_body,
        "preservation": pres_body,
        "source_inventory": inv_body,
    }

    card_feed = card_feed_no_backgap(conn, feed_body, served)
    public = public_lane_no_backgap(conn, bodies)
    gaps = completeness_gap_coverage_parity(conn, feed_body)
    overlays = stage3_overlay_presence_no_regression(
        conn, served, verify_body, pres_body, inventory_source_ids
    )
    floor = stageN_field_floor(conn, served, feed_body)
    determinism = determinism_read_only(conn, repo_root)

    clean = all(
        c["clean"] for c in (card_feed, public, gaps, overlays, floor, determinism)
    )
    return {
        "served_count": len(served),
        "card_feed_no_backgap": card_feed,
        "public_lane_no_backgap": public,
        "completeness_gap_coverage_parity": gaps,
        "stage3_overlay_presence_no_regression": overlays,
        "stageN_field_floor": floor,
        "determinism_read_only": determinism,
        "clean": clean,
    }


def _format_report(report: dict[str, Any], db_path: Path) -> str:
    cf = report["card_feed_no_backgap"]
    pu = report["public_lane_no_backgap"]
    gp = report["completeness_gap_coverage_parity"]
    ov = report["stage3_overlay_presence_no_regression"]
    fl = report["stageN_field_floor"]
    dt = report["determinism_read_only"]
    lines = [
        f"Stage 3 read-surface back-gap / coverage audit (GOV-411) — {db_path}",
        f"  served record cards                   : {report['served_count']}",
        f"  1 card-feed back-gap                  : {'OK' if cf['clean'] else f'BACK-GAP ({len(cf['backgap'])})'} "
        f"(eligible={cf['eligible_count']} served={cf['served_count']})",
        f"  2 public-lane back-gap                : {'OK' if pu['clean'] else f'BACK-GAP ({len(pu['backgap'])})'} "
        f"(eligible={pu['eligible_count']} served={pu['served_count']} leaks={pu['public_lane_leaks']})",
        f"  3 completeness-gap coverage parity    : {'OK' if gp['clean'] else f'DROPPED ({len(gp['dropped']) + len(gp['feed_dropped'])})'} "
        f"(canonical={gp['canonical_count']} projected={gp['projected_count']} "
        f"feed_gaps={gp['feed_gap_count']} no_primary_source={gp['no_primary_source_canonical']})",
        f"  4 Stage-3 overlay presence            : {'OK' if ov['clean'] else f'MISSING ({len(ov['missing'])}, bijection={ov['bijection_ok']}, preservation={ov['preservation_present']})'} "
        f"(checked={ov['checked']} units={ov['unit_count']})",
        f"  5 Stage-1/2 field floor               : {'OK' if fl['clean'] else f'BREACH ({len(fl['breaches'])})'} "
        f"(checked={fl['checked']})",
        f"  6 determinism / read-only             : {'OK' if dt['clean'] else 'UNSTABLE'} "
        f"(byte_identical={dt['byte_identical']} row_counts_stable={dt['row_counts_stable']})",
        f"  NO BACK-GAP / CLEAN                    : {report['clean']}",
    ]
    for sid in cf["backgap"]:
        lines.append(f"    CARD-FEED BACK-GAP (eligible, not served): {sid}")
    for sid in pu["backgap"]:
        lines.append(f"    PUBLIC BACK-GAP (eligible, not served): {sid}")
    for name in pu["public_lane_leaks"]:
        lines.append(f"    PUBLIC-LANE LEAK (access not reviewer_internal): {name}")
    for gid in gp["dropped"]:
        lines.append(f"    GAP DROPPED (canonical, not projected): {gid}")
    for h in gp["feed_dropped"]:
        lines.append(f"    GAP DROPPED BY FEED: {h}")
    if not ov["bijection_ok"]:
        lines.append(f"    OVERLAY BIJECTION BREAK: {ov['bijection_detail']}")
    for m in ov["missing"]:
        lines.append(f"    OVERLAY MISSING: {m['statement_id']} -> {m['violations']}")
    if not ov["preservation_present"]:
        lines.append("    PRESERVATION OVERLAY ABSENT / malformed")
    for b in fl["breaches"]:
        lines.append(f"    FLOOR BREACH: {b['statement_id']} -> {b['violations']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage 3 read-surface back-gap / coverage-regression audit "
        "(read-only). GOV-411 Stage 3.13."
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
        print(f"stage3 back-gap audit: DB not found at {args.db}", file=sys.stderr)
        return 2

    with db.open_db(args.db) as conn:
        report = audit_backgap(conn)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report, args.db))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
