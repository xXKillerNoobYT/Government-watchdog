"""Transactional writers for explicit ACCESS-2026 authorization facts.

These low-level append helpers never commit. Callers group a complete plan or
program lifecycle change in one ``with conn:`` block (or explicit
commit/rollback) and reuse one ``operation_id`` across every row. This avoids
partially visible upgrades, downgrades, and revocations.

Feature and geography grants must name the exact *active assignment event* that
authorizes them. A later plan/program revocation therefore invalidates every
dependent grant without relying on a fragile cleanup fan-out.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from . import features, geography
from .models import CATALOG_VERSION, PLAN_CODES, PROGRAM_CODES, PUBLICATION_LANES


class InvalidGrant(ValueError):
    """A grant request failed input, provenance, or window validation."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise InvalidGrant("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidGrant(f"invalid stored assignment timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise InvalidGrant(f"stored assignment timestamp lacks timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _window(
    effective_at: datetime | None,
    expires_at: datetime | None,
) -> tuple[str, str | None]:
    effective = effective_at or _now()
    effective_iso = _iso(effective)
    expires_iso = _iso(expires_at) if expires_at is not None else None
    if expires_iso is not None and _parse(expires_iso) <= _parse(effective_iso):
        raise InvalidGrant(
            "expires_at must remain later than effective_at at millisecond precision"
        )
    return effective_iso, expires_iso


def _owner_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidGrant("owner_decision_ref is required")
    return normalized


def _operation(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidGrant("operation_id is required")
    return normalized


def _actor(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise InvalidGrant("actor is required")
    return normalized


def _require_user(conn: sqlite3.Connection, user_id: str) -> None:
    if conn.execute(
        "SELECT 1 FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone() is None:
        raise InvalidGrant(f"unknown user {user_id!r}")


def _assignment_basis(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    plan_assignment_id: str | None,
    program_assignment_id: str | None,
) -> tuple[str, sqlite3.Row]:
    if (plan_assignment_id is None) == (program_assignment_id is None):
        raise InvalidGrant(
            "exactly one plan_assignment_id or program_assignment_id is required"
        )
    if plan_assignment_id is not None:
        kind = "plan"
        table = "access_plan_assignments"
        id_column = "assignment_id"
        row_id = plan_assignment_id
    else:
        kind = "program"
        table = "access_program_assignments"
        id_column = "assignment_id"
        row_id = str(program_assignment_id)
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {id_column} = ?",
        (row_id,),
    ).fetchone()
    if row is None or row["user_id"] != user_id:
        raise InvalidGrant(f"{kind} assignment does not belong to the user")
    if row["assignment_state"] != "active":
        raise InvalidGrant(f"{kind} grant basis must be an active assignment event")
    if row["catalog_version"] != CATALOG_VERSION:
        raise InvalidGrant(f"unsupported {kind} catalog_version")
    return kind, row


def _validate_dependent_window(
    basis: sqlite3.Row,
    *,
    effective_utc: str,
    expires_utc: str | None,
) -> None:
    effective = _parse(effective_utc)
    basis_effective = _parse(basis["effective_utc"])
    if effective < basis_effective:
        raise InvalidGrant("dependent grant cannot predate its assignment basis")
    basis_expires_raw = basis["expires_utc"]
    if basis_expires_raw is None:
        return
    basis_expires = _parse(basis_expires_raw)
    if expires_utc is None or _parse(expires_utc) > basis_expires:
        raise InvalidGrant("dependent grant cannot outlive its assignment basis")


def record_plan(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    plan_code: str,
    state: str,
    owner_decision_ref: str,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    assignment_id: str | None = None,
    catalog_version: str = CATALOG_VERSION,
    operation_id: str,
    actor: str,
) -> str:
    """Append a customer-plan event; never grants a feature or commits."""
    _require_user(conn, user_id)
    if plan_code not in PLAN_CODES:
        raise InvalidGrant(f"unknown plan_code {plan_code!r}")
    if state not in {"active", "revoked"}:
        raise InvalidGrant(f"unknown assignment state {state!r}")
    if catalog_version != CATALOG_VERSION:
        raise InvalidGrant(f"unsupported catalog_version {catalog_version!r}")
    effective, expires = _window(effective_at, expires_at)
    row_id = assignment_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO access_plan_assignments"
        " (assignment_id, user_id, plan_code, catalog_version,"
        " assignment_state, owner_decision_ref, operation_id, actor,"
        " recorded_utc, effective_utc, expires_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            user_id,
            plan_code,
            catalog_version,
            state,
            _owner_ref(owner_decision_ref),
            _operation(operation_id),
            _actor(actor),
            _iso(_now()),
            effective,
            expires,
        ),
    )
    return row_id


def record_program(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    program_code: str,
    state: str,
    owner_decision_ref: str,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    assignment_id: str | None = None,
    catalog_version: str = CATALOG_VERSION,
    operation_id: str,
    actor: str,
) -> str:
    """Append an internal/special-program event; never commits."""
    _require_user(conn, user_id)
    if program_code not in PROGRAM_CODES:
        raise InvalidGrant(f"unknown program_code {program_code!r}")
    if state not in {"active", "revoked"}:
        raise InvalidGrant(f"unknown assignment state {state!r}")
    if catalog_version != CATALOG_VERSION:
        raise InvalidGrant(f"unsupported catalog_version {catalog_version!r}")
    effective, expires = _window(effective_at, expires_at)
    row_id = assignment_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO access_program_assignments"
        " (assignment_id, user_id, program_code, catalog_version,"
        " assignment_state, owner_decision_ref, operation_id, actor,"
        " recorded_utc, effective_utc, expires_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            user_id,
            program_code,
            catalog_version,
            state,
            _owner_ref(owner_decision_ref),
            _operation(operation_id),
            _actor(actor),
            _iso(_now()),
            effective,
            expires,
        ),
    )
    return row_id


def record_feature(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    feature_key: str,
    publication_lane: str,
    state: str,
    owner_decision_ref: str,
    plan_assignment_id: str | None = None,
    program_assignment_id: str | None = None,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    grant_id: str | None = None,
    catalog_version: str = CATALOG_VERSION,
    operation_id: str,
    actor: str,
) -> str:
    """Append a feature/lane event tied to one exact assignment; never commits."""
    _require_user(conn, user_id)
    if not features.is_known(feature_key):
        raise InvalidGrant(f"unknown feature_key {feature_key!r}")
    if publication_lane not in PUBLICATION_LANES:
        raise InvalidGrant(f"unknown publication_lane {publication_lane!r}")
    if state not in {"active", "revoked"}:
        raise InvalidGrant(f"unknown grant state {state!r}")
    if catalog_version != CATALOG_VERSION:
        raise InvalidGrant(f"unsupported catalog_version {catalog_version!r}")
    basis_kind, basis = _assignment_basis(
        conn,
        user_id=user_id,
        plan_assignment_id=plan_assignment_id,
        program_assignment_id=program_assignment_id,
    )
    if publication_lane == "reviewer_internal":
        if basis_kind != "program":
            raise InvalidGrant("reviewer_internal requires an internal program")
        if basis["program_code"] not in features.REVIEWER_INTERNAL_PROGRAMS:
            raise InvalidGrant(
                "program is not eligible for reviewer_internal publication"
            )
    effective, expires = _window(effective_at, expires_at)
    _validate_dependent_window(
        basis,
        effective_utc=effective,
        expires_utc=expires,
    )
    row_id = grant_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO access_feature_grants"
        " (grant_id, user_id, feature_key, publication_lane, catalog_version,"
        " plan_assignment_id, program_assignment_id, grant_state,"
        " owner_decision_ref, operation_id, actor, recorded_utc,"
        " effective_utc, expires_utc)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            user_id,
            feature_key,
            publication_lane,
            catalog_version,
            plan_assignment_id,
            program_assignment_id,
            state,
            _owner_ref(owner_decision_ref),
            _operation(operation_id),
            _actor(actor),
            _iso(_now()),
            effective,
            expires,
        ),
    )
    return row_id


def record_geography(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    area_id: str,
    state: str,
    owner_decision_ref: str,
    plan_assignment_id: str | None = None,
    program_assignment_id: str | None = None,
    effective_at: datetime | None = None,
    expires_at: datetime | None = None,
    grant_id: str | None = None,
    catalog_version: str = CATALOG_VERSION,
    operation_id: str,
    actor: str,
) -> str:
    """Append an exact-area event tied to one assignment; never commits."""
    _require_user(conn, user_id)
    geography.validate_area(conn, area_id)
    if state not in {"active", "revoked"}:
        raise InvalidGrant(f"unknown grant state {state!r}")
    if catalog_version != CATALOG_VERSION:
        raise InvalidGrant(f"unsupported catalog_version {catalog_version!r}")
    _, basis = _assignment_basis(
        conn,
        user_id=user_id,
        plan_assignment_id=plan_assignment_id,
        program_assignment_id=program_assignment_id,
    )
    effective, expires = _window(effective_at, expires_at)
    _validate_dependent_window(
        basis,
        effective_utc=effective,
        expires_utc=expires,
    )
    row_id = grant_id or str(uuid.uuid4())
    conn.execute(
        "INSERT INTO access_geography_grants"
        " (grant_id, user_id, area_id, scope_kind, catalog_version,"
        " plan_assignment_id, program_assignment_id, grant_state,"
        " owner_decision_ref, operation_id, actor, recorded_utc,"
        " effective_utc, expires_utc)"
        " VALUES (?, ?, ?, 'exact', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row_id,
            user_id,
            area_id,
            catalog_version,
            plan_assignment_id,
            program_assignment_id,
            state,
            _owner_ref(owner_decision_ref),
            _operation(operation_id),
            _actor(actor),
            _iso(_now()),
            effective,
            expires,
        ),
    )
    return row_id
