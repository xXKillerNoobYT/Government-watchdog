"""Known capability keys.

This registry names independently grantable product capabilities. It is not a
plan-benefit matrix: every allow still requires an explicit
``access_feature_grants`` row for the authenticated user and publication lane.
"""

from __future__ import annotations

FEATURE_KEYS = frozenset(
    {
        "civic_overview",
        "timeline",
        "agenda_board",
        "issue_research",
        "evidence_vault",
        "newsletter_builder",
        "power_map",
        "watchlist",
        "alerts",
        "location_switching",
        "team_workspace",
        "data_export",
    }
)

# Reviewer-internal records are operational review material, not a paid-plan
# benefit. Contract/customer plans stay on public-safe publication lanes.
REVIEWER_INTERNAL_PROGRAMS = frozenset({"developer", "beta_tester"})

# Explicit allowlist: a future area state does not silently become served.
SERVED_AREA_STATES = frozenset(
    {"free_home", "free_beta", "funded", "paid", "limited"}
)


def is_known(feature_key: str) -> bool:
    return feature_key in FEATURE_KEYS
