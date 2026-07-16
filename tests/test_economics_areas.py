"""AM-1: area state machine — legal transitions audited, illegal/ownerless refused.

Every legal transition writes exactly one ``area_transitions`` row; every illegal
edge and every ownerless call is refused BEFORE any write (zero rows left behind).
"""

from __future__ import annotations

import pytest

from economics import areas


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_legal_transition_writes_exactly_one_audit_row(econ_conn):
    before = _count(econ_conn, "area_transitions")
    tid = areas.transition(econ_conn, area_id="alpine", to_state="funded",
                           owner_decision_ref="card:GOV-999", rule="F-ELIG")
    assert isinstance(tid, int)
    assert _count(econ_conn, "area_transitions") == before + 1
    assert areas.get_state(econ_conn, "alpine") == "funded"
    row = econ_conn.execute(
        "SELECT from_state, to_state, owner_decision_ref, rule_evaluated"
        " FROM area_transitions WHERE transition_id = ?", (tid,)
    ).fetchone()
    assert (row["from_state"], row["to_state"]) == ("locked", "funded")
    assert row["owner_decision_ref"] == "card:GOV-999"
    assert row["rule_evaluated"] == "F-ELIG"


def test_illegal_edge_refused_with_zero_writes(econ_conn):
    # locked -> paid is illegal (paid requires a prior funded footing).
    before_state = _count(econ_conn, "area_state")
    before_audit = _count(econ_conn, "area_transitions")
    with pytest.raises(areas.IllegalTransition):
        areas.transition(econ_conn, area_id="alpine", to_state="paid",
                         owner_decision_ref="card:x")
    assert _count(econ_conn, "area_state") == before_state
    assert _count(econ_conn, "area_transitions") == before_audit
    assert areas.get_state(econ_conn, "alpine") == "locked"


def test_ownerless_transition_refused_with_zero_writes(econ_conn):
    before_audit = _count(econ_conn, "area_transitions")
    for bad_ref in ("", "   ", None):
        with pytest.raises(areas.OwnerlessTransition):
            areas.transition(econ_conn, area_id="alpine", to_state="funded",
                             owner_decision_ref=bad_ref)  # type: ignore[arg-type]
    assert _count(econ_conn, "area_transitions") == before_audit
    assert areas.get_state(econ_conn, "alpine") == "locked"


def test_noop_and_unknown_state_refused(econ_conn):
    with pytest.raises(areas.IllegalTransition):
        areas.transition(econ_conn, area_id="alpine", to_state="locked",
                         owner_decision_ref="card:x")  # locked -> locked no-op
    with pytest.raises(areas.IllegalTransition):
        areas.transition(econ_conn, area_id="alpine", to_state="galaxy",
                         owner_decision_ref="card:x")


def test_every_legal_edge_is_accepted_and_every_other_rejected(econ_conn):
    # Exhaustive AM-1 sweep: walk each state, try every target, assert exactly the
    # LEGAL_TRANSITIONS set is accepted. Uses a scratch area reset per attempt.
    econ_conn.execute("INSERT INTO areas (area_id, kind, name) VALUES ('t', 'town', 'T')")
    for frm in sorted(areas.STATES):
        for to in sorted(areas.STATES):
            # Force the from-state directly (bypassing the guard) to isolate the edge.
            econ_conn.execute(
                "INSERT INTO area_state (area_id, state, updated_utc) VALUES ('t', ?, 'x')"
                " ON CONFLICT(area_id) DO UPDATE SET state = excluded.state", (frm,))
            econ_conn.commit()
            legal = (frm, to) in areas.LEGAL_TRANSITIONS
            if legal:
                tid = areas.transition(econ_conn, area_id="t", to_state=to,
                                       owner_decision_ref="card:sweep")
                assert isinstance(tid, int)
            else:
                with pytest.raises(areas.IllegalTransition):
                    areas.transition(econ_conn, area_id="t", to_state=to,
                                     owner_decision_ref="card:sweep")


def test_rollup_spine_walk(econ_conn):
    assert areas.descendants(econ_conn, "wy") == ["alpine", "etna", "lincoln"]
    assert areas.descendants(econ_conn, "lincoln") == ["alpine", "etna"]
    assert areas.ancestors(econ_conn, "alpine") == ["lincoln", "wy"]
