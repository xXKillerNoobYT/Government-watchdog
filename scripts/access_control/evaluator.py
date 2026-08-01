"""Fail-closed product/feature/geography decision core.

Every allow requires one coherent, server-clocked database snapshot containing:

1. an approved account;
2. exactly one active customer plan OR at least one active internal program;
3. a current feature + publication-lane grant;
4. a current exact-area grant tied to the *same* active assignment event; and
5. an explicitly served area state.

There is no plan-to-feature inference, ancestor/descendant inference,
border-town inference, browser-state input, or caller-selected evaluation time.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import features, geography
from .models import (
    CATALOG_VERSION,
    PUBLICATION_LANES,
    AccessDecision,
    AccessRequest,
)


class InvalidStoredAccessState(ValueError):
    """Stored authorization facts are malformed or internally inconsistent."""


def _utcnow() -> datetime:
    """Trusted clock seam; production callers cannot supply an evaluation time."""
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse(value: str, *, canonical: bool) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise InvalidStoredAccessState(f"invalid authorization timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidStoredAccessState(
            f"authorization timestamp lacks timezone: {value!r}"
        )
    utc = parsed.astimezone(timezone.utc)
    if canonical and _iso(utc) != value:
        raise InvalidStoredAccessState(
            f"authorization timestamp is not canonical UTC: {value!r}"
        )
    return utc


def _validate_row_times(row: sqlite3.Row, at: datetime) -> tuple[datetime, datetime | None]:
    recorded = _parse(row["recorded_utc"], canonical=True)
    effective = _parse(row["effective_utc"], canonical=True)
    expires_raw = row["expires_utc"]
    expires = _parse(expires_raw, canonical=True) if expires_raw is not None else None
    if recorded > at:
        raise InvalidStoredAccessState("authorization row is recorded in the future")
    if expires is not None and expires <= effective:
        raise InvalidStoredAccessState("authorization row has an invalid time window")
    return effective, expires


def _latest_by(
    rows: list[sqlite3.Row],
    *,
    key_fields: tuple[str, ...],
    sequence_field: str,
    at: datetime,
) -> dict[tuple[object, ...], sqlite3.Row]:
    """Latest started row per key, ordered by parsed instant then sequence.

    Rows are sorted in Python even though canonical storage also makes SQLite
    text ordering correct. This is defense in depth against a corrupted/imported
    row and makes the sequence tie-break explicit.
    """
    candidates: list[tuple[datetime, int, sqlite3.Row]] = []
    for row in rows:
        effective, _ = _validate_row_times(row, at)
        if effective <= at:
            candidates.append((effective, int(row[sequence_field]), row))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    out: dict[tuple[object, ...], sqlite3.Row] = {}
    for _, _, row in candidates:
        key = tuple(row[field] for field in key_fields)
        out.setdefault(key, row)
    return out


def _is_active(row: sqlite3.Row, *, state_field: str, at: datetime) -> bool:
    _, expires = _validate_row_times(row, at)
    return row[state_field] == "active" and (expires is None or expires > at)


def _current_account_tier(
    conn: sqlite3.Connection,
    user_id: str,
    at: datetime,
) -> str:
    """Chronological latest-row resolution for the pre-existing account log."""
    rows = conn.execute(
        "SELECT rowid, tier, granted_utc FROM access_grants WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    candidates: list[tuple[datetime, int, str]] = []
    for row in rows:
        granted = _parse(row["granted_utc"], canonical=False)
        if granted <= at:
            candidates.append((granted, int(row["rowid"]), row["tier"]))
    if not candidates:
        return "none"
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _active_profiles(
    conn: sqlite3.Connection,
    user_id: str,
    at: datetime,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    plan_rows = conn.execute(
        "SELECT * FROM access_plan_assignments WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    program_rows = conn.execute(
        "SELECT * FROM access_program_assignments WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    current_plans = _latest_by(
        plan_rows,
        key_fields=("plan_code",),
        sequence_field="assignment_seq",
        at=at,
    )
    current_programs = _latest_by(
        program_rows,
        key_fields=("program_code",),
        sequence_field="assignment_seq",
        at=at,
    )
    active_plans = [
        row
        for row in current_plans.values()
        if _is_active(row, state_field="assignment_state", at=at)
    ]
    active_programs = [
        row
        for row in current_programs.values()
        if _is_active(row, state_field="assignment_state", at=at)
    ]
    return active_plans, active_programs


def _basis_key(row: sqlite3.Row) -> tuple[str, str]:
    plan_id = row["plan_assignment_id"]
    program_id = row["program_assignment_id"]
    if (plan_id is None) == (program_id is None):
        raise InvalidStoredAccessState("grant does not have exactly one assignment basis")
    return ("plan", plan_id) if plan_id is not None else ("program", program_id)


def _active_bases(
    plans: list[sqlite3.Row],
    programs: list[sqlite3.Row],
) -> dict[tuple[str, str], sqlite3.Row]:
    out = {
        ("plan", row["assignment_id"]): row
        for row in plans
    }
    out.update(
        {
            ("program", row["assignment_id"]): row
            for row in programs
        }
    )
    return out


def _current_feature_grants(
    conn: sqlite3.Connection,
    request: AccessRequest,
    at: datetime,
) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM access_feature_grants"
        " WHERE user_id = ? AND feature_key = ? AND publication_lane = ?",
        (request.user_id, request.feature_key, request.publication_lane),
    ).fetchall()
    current = _latest_by(
        rows,
        key_fields=("plan_assignment_id", "program_assignment_id"),
        sequence_field="grant_seq",
        at=at,
    )
    return {
        _basis_key(row): row
        for row in current.values()
        if _is_active(row, state_field="grant_state", at=at)
    }


def _current_geography_grants(
    conn: sqlite3.Connection,
    request: AccessRequest,
    at: datetime,
) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM access_geography_grants"
        " WHERE user_id = ? AND area_id = ? AND scope_kind = 'exact'",
        (request.user_id, request.area_id),
    ).fetchall()
    current = _latest_by(
        rows,
        key_fields=("plan_assignment_id", "program_assignment_id"),
        sequence_field="grant_seq",
        at=at,
    )
    return {
        _basis_key(row): row
        for row in current.values()
        if _is_active(row, state_field="grant_state", at=at)
    }


def _evaluate_snapshot(
    conn: sqlite3.Connection,
    request: AccessRequest,
    at: datetime,
) -> AccessDecision:
    evaluated = _iso(at)

    if (
        not request.user_id
        or _current_account_tier(conn, request.user_id, at) != "approved"
    ):
        return AccessDecision.deny(
            request,
            "account_not_approved",
            evaluated_utc=evaluated,
        )
    if not features.is_known(request.feature_key):
        return AccessDecision.deny(
            request,
            "unknown_feature",
            evaluated_utc=evaluated,
        )
    if request.publication_lane not in PUBLICATION_LANES:
        return AccessDecision.deny(
            request,
            "unknown_publication_lane",
            evaluated_utc=evaluated,
        )

    plans, programs = _active_profiles(conn, request.user_id, at)
    if any(row["catalog_version"] != CATALOG_VERSION for row in plans + programs):
        return AccessDecision.deny(
            request,
            "unsupported_catalog_version",
            evaluated_utc=evaluated,
        )
    if len(plans) > 1:
        return AccessDecision.deny(
            request,
            "ambiguous_plan_state",
            evaluated_utc=evaluated,
        )
    if not plans and not programs:
        return AccessDecision.deny(
            request,
            "access_profile_missing",
            evaluated_utc=evaluated,
        )

    plan_code = plans[0]["plan_code"] if plans else None
    program_codes = tuple(sorted(row["program_code"] for row in programs))
    context = {
        "plan_code": plan_code,
        "program_codes": program_codes,
    }
    active_bases = _active_bases(plans, programs)

    feature_grants = _current_feature_grants(conn, request, at)
    feature_grants = {
        key: row
        for key, row in feature_grants.items()
        if key in active_bases and row["catalog_version"] == CATALOG_VERSION
    }
    if request.publication_lane == "reviewer_internal":
        feature_grants = {
            key: row
            for key, row in feature_grants.items()
            if key[0] == "program"
            and active_bases[key]["program_code"]
            in features.REVIEWER_INTERNAL_PROGRAMS
        }
    if not feature_grants:
        return AccessDecision.deny(
            request,
            "feature_not_granted",
            evaluated_utc=evaluated,
            **context,
        )

    geography_grants = _current_geography_grants(conn, request, at)
    geography_grants = {
        key: row
        for key, row in geography_grants.items()
        if key in active_bases and row["catalog_version"] == CATALOG_VERSION
    }
    if not geography_grants:
        return AccessDecision.deny(
            request,
            "geography_not_granted",
            evaluated_utc=evaluated,
            **context,
        )

    matching_bases = sorted(set(feature_grants) & set(geography_grants))
    if not matching_bases:
        return AccessDecision.deny(
            request,
            "entitlement_basis_mismatch",
            evaluated_utc=evaluated,
            **context,
        )
    basis = matching_bases[0]
    feature_row = feature_grants[basis]
    geography_row = geography_grants[basis]

    geography.validate_area(conn, request.area_id)
    area_row = conn.execute(
        "SELECT state FROM area_state WHERE area_id = ?",
        (request.area_id,),
    ).fetchone()
    area_state = area_row["state"] if area_row is not None else "locked"
    if area_state not in features.SERVED_AREA_STATES:
        return AccessDecision.deny(
            request,
            "area_not_served",
            evaluated_utc=evaluated,
            area_state=area_state,
            basis_kind=basis[0],
            basis_assignment_id=basis[1],
            feature_grant_id=feature_row["grant_id"],
            geography_grant_id=geography_row["grant_id"],
            **context,
        )

    return AccessDecision(
        allowed=True,
        reason_code="allow",
        feature_key=request.feature_key,
        area_id=request.area_id,
        publication_lane=request.publication_lane,
        evaluated_utc=evaluated,
        catalog_version=CATALOG_VERSION,
        plan_code=plan_code,
        program_codes=program_codes,
        area_state=area_state,
        basis_kind=basis[0],
        basis_assignment_id=basis[1],
        feature_grant_id=feature_row["grant_id"],
        geography_grant_id=geography_row["grant_id"],
    )


def evaluate(
    conn: sqlite3.Connection,
    request: AccessRequest,
) -> AccessDecision:
    """Evaluate one request in a coherent read transaction using server time."""
    at = _utcnow()
    evaluated = _iso(at)
    if conn.in_transaction:
        return AccessDecision.deny(
            request,
            "evaluation_transaction_active",
            evaluated_utc=evaluated,
        )

    try:
        conn.execute("BEGIN")
        return _evaluate_snapshot(conn, request, at)
    except InvalidStoredAccessState:
        return AccessDecision.deny(
            request,
            "invalid_access_state",
            evaluated_utc=evaluated,
        )
    except geography.InvalidArea:
        return AccessDecision.deny(
            request,
            "invalid_area_hierarchy",
            evaluated_utc=evaluated,
        )
    finally:
        if conn.in_transaction:
            conn.rollback()
