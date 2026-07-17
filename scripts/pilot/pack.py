"""Decision-pack builder (PILOT-2026 §4, GOV-781 leg 2).

Assembles the §4 pack by REUSING ``economics.report.build_pack`` /
``build_rollup`` for the canonical cost/capacity/eligibility surface and
wrapping the §2.4–§2.7 pilot extensions (safety / support / notification /
observed-capacity) around it — it does not fork the economics logic. The pack
is deterministic (no timestamp/id), so ``content_hash`` is reproducible and
``verify`` re-derives it (§4.3 / §5.3 test 5).

County/state rows are labeled rollup PROJECTIONS from observed Alpine data only,
never new-area ingest (INV-5). Export goes through ``economics.export`` so the
``assert_no_prices`` RED guard runs over every emitted cell (LED-6, no prices).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from economics import basis as _basis
from economics import export as _export
from economics import report as _report

from . import ALPINE_AREA_ID, DEFAULT_SEED, LINCOLN_COUNTY_ID, WYOMING_STATE_ID
from . import snapshot as _snapshot

SCHEMA = "PILOT-2026/pack/v1"

#: §4.2.5 activation conditions — STATED here, never executed by this chain.
_ACTIVATION_CONDITIONS = {
    "free": ("standing Alpine + owner-approved beta areas; condition = COHORT / "
             "GATE-B evidence healthy"),
    "donated_funded": ("F-ELIG true on MEASURED inputs (AREA-6) => GATE-F owner "
                       "card; see economics.eligibility (recommend-only)"),
    "paid": ("designed entitlement (0024 area_entitlements) + measured cost basis "
             "=> GATE-P owner card; pricing is a business-plan-lane decision, "
             "never this pack"),
    "locked_limited": ("default fallback; budget/capacity rule triggers stated on "
                       "the cohort card"),
    "note": ("activation is Isaac's separate decision via GATE-F / GATE-P / "
             "GATE-PUB on GOV-715; this pack only states conditions (§4.2.5)."),
}

_DISCLOSURES = [
    ("GOV-771: FE↔BE notification HTTP endpoint stays inert (feature-flag "
     "fail-closed); notification metrics are captured at the backend service "
     "layer only."),
    ("Small-N validity: 2–15 users => report ranges, not point estimates; any "
     "extrapolation is labeled ASSUMED (plan §9)."),
    ("Ollama-only cost realism: default operation is local, zero-credit; paid-"
     "provider scenarios are priced ASSUMED from measured token volumes unless a "
     "card authorized a paid batch."),
    ("Wave-0 synthetic baseline values carry the seed and appear only in the "
     "'synthetic baseline' column, never mixed into an 'observed' column (§0)."),
]


def _pilot_metrics(conn, area_id, period, seed, support_log_path) -> dict:
    """The §2.4–§2.7 pilot extensions, drawn from the snapshot extractor."""
    snap = _snapshot.extract(conn, area_id, period, seed=seed,
                             support_log_path=support_log_path)
    return {
        "quality": snap["quality"],
        "latency": snap["latency"],
        "safety": snap["safety"],
        "support": snap["support"],
        "notification": snap["notification"],
        "capacity_observed": snap["capacity"]["observed"],
    }


def build_area_row(conn: sqlite3.Connection, area_id: str, period: str, *,
                   active_users: int | None = None, seed: str = DEFAULT_SEED,
                   support_log_path=None) -> dict:
    """The MEASURED Alpine (town) row: economics pack + pilot extensions."""
    econ = _report.build_pack(conn, area_id, period, active_users=active_users,
                              include_capacity=True)
    return {
        "row_kind": "observed_town",
        "area_id": area_id,
        "economics": econ,
        "pilot_metrics": _pilot_metrics(conn, area_id, period, seed, support_log_path),
    }


def build_projection_row(conn: sqlite3.Connection, scope: str, area_id: str,
                         period: str) -> dict:
    """A labeled county/state rollup PROJECTION (pure aggregation; §4.1)."""
    rollup = _report.build_rollup(conn, scope, area_id, period)
    return {
        "row_kind": "projection",
        "scope": scope,
        "area_id": area_id,
        "header": "PROJECTION — rollup from observed Alpine data only (INV-5)",
        "economics": rollup,
    }


def build_pack(conn: sqlite3.Connection, *, area_id: str = ALPINE_AREA_ID,
               period: str, active_users: int | None = None,
               seed: str = DEFAULT_SEED, support_log_path=None,
               include_projections: bool = True) -> dict:
    """Assemble the full §4 decision pack (deterministic — no timestamp/id)."""
    rows = [build_area_row(conn, area_id, period, active_users=active_users,
                           seed=seed, support_log_path=support_log_path)]
    if include_projections:
        rows.append(build_projection_row(conn, "county_rollup", LINCOLN_COUNTY_ID, period))
        rows.append(build_projection_row(conn, "state_rollup", WYOMING_STATE_ID, period))

    pack = {
        "schema": SCHEMA,
        "area_id": area_id,
        "period": period,
        "scope": "pilot_pack",
        "seed": seed,
        "rows": rows,
        "activation_conditions": _ACTIVATION_CONDITIONS,
        "disclosures": _DISCLOSURES,
    }
    return pack


def content_hash(pack: dict) -> str:
    """Canonical SHA-256 of a pilot pack (sorted keys, UTF-8) — reproducibility."""
    canonical = json.dumps(pack, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def lint(pack: dict) -> list[str]:
    """AM-7 basis lint over every value cell in the pack (empty => clean)."""
    return _basis.lint_report(pack)


def assert_labeled(pack: dict) -> None:
    """RED guard: raise if any value cell is unlabeled (§4.3 / AM-7)."""
    violations = lint(pack)
    if violations:
        raise ValueError("pilot pack failed basis lint (AM-7): " + "; ".join(violations))


def export_rows(pack: dict) -> list[dict]:
    """LED-6 rows for the whole pilot pack; ``assert_no_prices`` runs inside (RED)."""
    rows = _export._rows_from_cells(pack, pack.get("area_id"), pack.get("period"))
    _export.assert_no_prices(rows)
    return rows


def build_and_record(conn: sqlite3.Connection, *, area_id: str = ALPINE_AREA_ID,
                     period: str, active_users: int | None = None,
                     seed: str = DEFAULT_SEED, support_log_path=None) -> dict:
    """Build the pack, lint it, record the economics sub-pack hash into
    ``ledger_report_runs`` (the DB reproducibility anchor), and return the pack
    plus its ``content_sha256``.
    """
    pack = build_pack(conn, area_id=area_id, period=period, active_users=active_users,
                      seed=seed, support_log_path=support_log_path)
    assert_labeled(pack)
    # Anchor the canonical economics sub-pack in the DB (INSERT OR REPLACE).
    econ = pack["rows"][0]["economics"]
    report_id, econ_hash = _report.record_run(conn, econ)
    return {
        "pack": pack,
        "content_sha256": content_hash(pack),
        "economics_report_id": report_id,
        "economics_content_sha256": econ_hash,
    }


def verify(conn: sqlite3.Connection, *, area_id: str = ALPINE_AREA_ID, period: str,
           expected_sha256: str, active_users: int | None = None,
           seed: str = DEFAULT_SEED, support_log_path=None) -> dict:
    """Rebuild the pack on the same substrate and confirm the hash is unchanged."""
    pack = build_pack(conn, area_id=area_id, period=period, active_users=active_users,
                      seed=seed, support_log_path=support_log_path)
    recomputed = content_hash(pack)
    return {"expected": expected_sha256, "recomputed": recomputed,
            "match": recomputed == expected_sha256}
