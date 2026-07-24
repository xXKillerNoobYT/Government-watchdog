"""Stable internal vocabulary and decision DTOs for ACCESS-2026 v0.1.

Plan codes are semantic database identifiers, not public product names. The
owner can change customer-facing wording without rewriting historical grants.
No code in this module maps a plan to features or geography; that matrix is a
separate, still-owner-gated policy decision.
"""

from __future__ import annotations

from dataclasses import dataclass

CATALOG_VERSION = "ACCESS-2026/v0.1"

PLAN_CODES = frozenset(
    {
        "free",
        "pro_town",
        "pro_multi_home",
        "pro_state",
        "pro_global",
        "contract",
    }
)

PROGRAM_CODES = frozenset(
    {
        "developer",
        "beta_tester",
        "special_contract_team",
    }
)

PUBLICATION_LANES = frozenset({"public", "reviewer_internal"})


@dataclass(frozen=True)
class AccessRequest:
    """One exact authorization question.

    ``user_id`` must already come from an authenticated server-side session.
    Browser-selected plan/mode/location values are never accepted as identity
    or grant evidence.
    """

    user_id: str
    feature_key: str
    area_id: str
    publication_lane: str


@dataclass(frozen=True)
class AccessDecision:
    """Internal structured result; transports should expose bounded bodies.

    ``reason_code`` is stable for tests/audit. A public HTTP denial should map
    all deny reasons to one neutral response rather than returning this object
    verbatim.
    """

    allowed: bool
    reason_code: str
    feature_key: str
    area_id: str
    publication_lane: str
    evaluated_utc: str
    catalog_version: str
    plan_code: str | None = None
    program_codes: tuple[str, ...] = ()
    area_state: str | None = None
    basis_kind: str | None = None
    basis_assignment_id: str | None = None
    feature_grant_id: str | None = None
    geography_grant_id: str | None = None

    @classmethod
    def deny(
        cls,
        request: AccessRequest,
        reason_code: str,
        *,
        evaluated_utc: str,
        **context,
    ):
        return cls(
            allowed=False,
            reason_code=reason_code,
            feature_key=request.feature_key,
            area_id=request.area_id,
            publication_lane=request.publication_lane,
            evaluated_utc=evaluated_utc,
            catalog_version=CATALOG_VERSION,
            **context,
        )
