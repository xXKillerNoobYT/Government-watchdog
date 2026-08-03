"""Owner-gated 2→3→15 cohort machine (0025 §4/§5, D4, INV-3/INV-6, AC-2/3).

Membership model (D4, ADDITIVE): joining a cohort never removes anyone from
an earlier one — beta-2 members remain members through beta-3 and beta-15.
The chain is ordered ``beta-2 < beta-3 < beta-15``, so the effective member
set of cohort C is every distinct user who ever transitioned INTO C or into
any earlier cohort in the chain. That makes each cap a CUMULATIVE program
size (2, then 3, then 15 total), which is exactly the product intent.

Cap enforcement (INV-6): :func:`advance` acquires SQLite's writer reservation
BEFORE reading capacity, then recomputes the member set from
``cohort_transitions`` inside the same transaction as the transition, cache
refresh, and notification. ``cohort_state.current_size`` is NEVER read for
enforcement, so a desynced counter cannot open the door (RED-proof: desync it
low, the recompute still rejects).

Every write is owner-gated: opening a cohort and every single transition
require a non-null ``owner_decision_ref`` (INV-3/AC-3) — also enforced
in-schema by 0025's NOT NULL (GOV-753 leg 1).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from notifications import service as notif

COHORT_CHAIN = ("beta-2", "beta-3", "beta-15")
DEFAULT_CAPS = {"beta-2": 2, "beta-3": 3, "beta-15": 15}


class UnknownCohort(ValueError):
    """Cohort id not in :data:`COHORT_CHAIN`."""


class CohortNotOpen(ValueError):
    """Transition into a cohort that is missing or not status='open'."""


class OwnerlessCohortAction(ValueError):
    """Open/advance attempted without an ``owner_decision_ref`` (INV-3)."""


class CohortCapExceeded(ValueError):
    """Recomputed membership would exceed ``max_size`` (AC-2); zero writes."""


class CohortCapBelowMembership(ValueError):
    """A cap change cannot be lower than committed cumulative membership."""


class CohortTransactionActive(RuntimeError):
    """A cohort writer requires an idle connection to own its transaction."""


class CohortWriteBusy(RuntimeError):
    """SQLite cohort writer lock was busy; retry the whole operation later."""


class CohortAdmissionBusy(CohortWriteBusy):
    """SQLite admission lock was busy; retry the complete operation later."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _rank(cohort_id: str) -> int:
    try:
        return COHORT_CHAIN.index(cohort_id)
    except ValueError:
        raise UnknownCohort(cohort_id) from None


def _owner_ref(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise OwnerlessCohortAction("owner_decision_ref is required")
    return normalized


def open_cohort(conn: sqlite3.Connection, cohort_id: str, *,
                owner_decision_ref: str,
                max_size: int | None = None) -> None:
    """Create/open a cohort step. Owner-gated like any transition."""
    rank = _rank(cohort_id)  # validates the id
    owner_ref = _owner_ref(owner_decision_ref)
    cap = DEFAULT_CAPS[cohort_id] if max_size is None else max_size
    if rank > 0 and cap < DEFAULT_CAPS[COHORT_CHAIN[rank - 1]]:
        raise ValueError("cap below the previous step's cap breaks additive membership")
    if conn.in_transaction:
        raise CohortTransactionActive(
            "open_cohort requires an idle connection; it owns the transaction"
        )
    try:
        conn.execute("BEGIN IMMEDIATE")
        member_count = _member_count(conn, cohort_id)
        if cap < member_count:
            raise CohortCapBelowMembership(
                f"{cohort_id}: cap {cap} < current membership {member_count}"
            )
        conn.execute(
            "INSERT INTO cohort_state (cohort_id, max_size, status, opened_utc,"
            " owner_decision_ref) VALUES (?, ?, 'open', ?, ?)"
            " ON CONFLICT(cohort_id) DO UPDATE SET max_size = excluded.max_size,"
            " status = 'open', opened_utc = excluded.opened_utc,"
            " owner_decision_ref = excluded.owner_decision_ref",
            (cohort_id, cap, _utcnow(), owner_ref),
        )
        _refresh_cumulative_caches(conn, cohort_id)
        conn.commit()
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        if _is_busy_error(exc):
            raise CohortWriteBusy(
                f"{cohort_id}: cohort writer is busy; retry later"
            ) from exc
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def latest_cohort(conn: sqlite3.Connection, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT to_cohort FROM cohort_transitions WHERE user_id = ?"
        " ORDER BY at_utc DESC, transition_id DESC LIMIT 1", (user_id,)
    ).fetchone()
    return row[0] if row else None


def _member_count(conn: sqlite3.Connection, cohort_id: str) -> int:
    """Effective ADDITIVE membership of ``cohort_id`` (see module docstring)."""
    chain_upto = COHORT_CHAIN[: _rank(cohort_id) + 1]
    placeholders = ",".join("?" for _ in chain_upto)
    return conn.execute(
        f"SELECT COUNT(DISTINCT user_id) FROM cohort_transitions"
        f" WHERE to_cohort IN ({placeholders})", chain_upto
    ).fetchone()[0]


def is_member(conn: sqlite3.Connection, user_id: str, cohort_id: str) -> bool:
    chain_upto = COHORT_CHAIN[: _rank(cohort_id) + 1]
    placeholders = ",".join("?" for _ in chain_upto)
    row = conn.execute(
        f"SELECT 1 FROM cohort_transitions WHERE user_id = ?"
        f" AND to_cohort IN ({placeholders}) LIMIT 1",
        (user_id, *chain_upto)).fetchone()
    return row is not None


def _exact_transition_id(
    conn: sqlite3.Connection,
    user_id: str,
    cohort_id: str,
) -> int | None:
    row = conn.execute(
        "SELECT transition_id FROM cohort_transitions"
        " WHERE user_id = ? AND to_cohort = ?"
        " ORDER BY transition_id LIMIT 1",
        (user_id, cohort_id),
    ).fetchone()
    return row[0] if row else None


def _refresh_cumulative_caches(
    conn: sqlite3.Connection,
    changed_cohort: str,
) -> None:
    """Refresh the changed cohort and every existing cumulative successor."""
    for cohort_id in COHORT_CHAIN[_rank(changed_cohort):]:
        if conn.execute(
            "SELECT 1 FROM cohort_state WHERE cohort_id = ?",
            (cohort_id,),
        ).fetchone() is None:
            continue
        size = _member_count(conn, cohort_id)
        conn.execute(
            "UPDATE cohort_state SET current_size = ?,"
            " status = CASE"
            " WHEN status = 'closed' THEN 'closed'"
            " WHEN ? >= max_size THEN 'full'"
            " ELSE 'open' END"
            " WHERE cohort_id = ?",
            (size, size, cohort_id),
        )


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        primary = code & 0xFF
        return primary in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    return str(exc).lower() in {
        "database is locked",
        "database table is locked",
    }


def advance(conn: sqlite3.Connection, user_id: str, *, to_cohort: str,
            owner_decision_ref: str) -> int:
    """Move a user into ``to_cohort``; returns the transition rowid.

    Rejections (all BEFORE any row is written, leaving zero rows behind):
      * missing/empty ``owner_decision_ref``   → OwnerlessCohortAction (AC-3)
      * unknown cohort id                      → UnknownCohort
      * cohort missing or not open             → CohortNotOpen
      * recomputed membership would exceed cap → CohortCapExceeded (AC-2/INV-6)
      * caller transaction already active      → CohortTransactionActive
      * SQLite writer reservation unavailable  → CohortAdmissionBusy (retryable)
    """
    owner_ref = _owner_ref(owner_decision_ref)
    _rank(to_cohort)
    if conn.in_transaction:
        raise CohortTransactionActive(
            "advance requires an idle connection; it owns the transaction"
        )

    try:
        # Reserve the sole SQLite writer before every state/cap read. A second
        # connection waits here, then recomputes from the winner's commit.
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT max_size, status FROM cohort_state WHERE cohort_id = ?",
            (to_cohort,)).fetchone()
        # 'full' does NOT short-circuit here: status is derived cache, and the
        # recompute below is the only authority on capacity (INV-6). A 'full'
        # cohort still admits an already-carried member (no size change).
        if state is None or state[1] == "closed":
            raise CohortNotOpen(to_cohort)
        max_size = state[0]
        transition_id = _exact_transition_id(conn, user_id, to_cohort)
        if transition_id is None:
            already_member = is_member(conn, user_id, to_cohort)
            new_size = (
                _member_count(conn, to_cohort)
                + (0 if already_member else 1)
            )
            if new_size > max_size:
                raise CohortCapExceeded(
                    f"{to_cohort}: recomputed size {new_size} > cap {max_size}")
            cur = conn.execute(
                "INSERT INTO cohort_transitions (user_id, from_cohort, to_cohort,"
                " owner_decision_ref, at_utc) VALUES (?, ?, ?, ?, ?)",
                (user_id, latest_cohort(conn, user_id), to_cohort,
                 owner_ref, _utcnow()),
            )
            transition_id = cur.lastrowid
            # Stage the in-app event in this transaction. Other connections
            # cannot observe it until the admission itself commits.
            notif.notify_cohort_advanced(
                conn,
                user_id,
                to_cohort,
                commit=False,
            )
        _refresh_cumulative_caches(conn, to_cohort)
        conn.commit()
    except sqlite3.OperationalError as exc:
        if conn.in_transaction:
            conn.rollback()
        if _is_busy_error(exc):
            raise CohortAdmissionBusy(
                f"{to_cohort}: admission writer is busy; retry later"
            ) from exc
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return transition_id
