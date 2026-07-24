"""Behavioral and RED-proof tests for the fail-closed access decision core."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
from access_control import evaluator, grants
from access_control.geography import InvalidArea, validate_area
from access_control.models import CATALOG_VERSION, AccessRequest
from accounts import service as accounts
from economics import areas

NOW = datetime.now(timezone.utc)
OWNER = "owner:GOV-access-review"
TEST_OPERATION = "operation:access-decision-test"
TEST_ACTOR = "access-decision-test-suite"
_ORIGINAL_WRITERS = {
    name: getattr(grants, name)
    for name in (
        "record_plan",
        "record_program",
        "record_feature",
        "record_geography",
    )
}


@pytest.fixture(autouse=True)
def _explicit_audit_context(monkeypatch):
    """Keep existing behavioral tests concise while production stays explicit."""
    for name, writer in _ORIGINAL_WRITERS.items():

        def audited(*args, _writer=writer, **kwargs):
            kwargs.setdefault("operation_id", TEST_OPERATION)
            kwargs.setdefault("actor", TEST_ACTOR)
            return _writer(*args, **kwargs)

        monkeypatch.setattr(grants, name, audited)


@pytest.fixture()
def conn(tmp_path):
    db_path = tmp_path / "decision.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    areas.create_area(connection, area_id="wy", kind="state", name="Wyoming")
    areas.create_area(
        connection,
        area_id="lincoln",
        kind="county",
        name="Lincoln County",
        parent_area_id="wy",
    )
    areas.create_area(
        connection,
        area_id="alpine",
        kind="town",
        name="Alpine",
        parent_area_id="lincoln",
    )
    areas.create_area(
        connection,
        area_id="etna",
        kind="town",
        name="Etna",
        parent_area_id="lincoln",
    )
    connection.commit()
    yield connection
    connection.close()


def _user(conn, email="resident@example.test", *, approved=True):
    user_id = accounts.create_user(conn, email=email)
    if approved:
        accounts.approve(conn, user_id, owner_decision_ref=OWNER)
    return user_id


def _request(user_id, *, area_id="alpine", feature="timeline", lane="public"):
    return AccessRequest(
        user_id=user_id,
        feature_key=feature,
        area_id=area_id,
        publication_lane=lane,
    )


def _basis_kwargs(kind, assignment_id):
    return {
        "plan_assignment_id": assignment_id if kind == "plan" else None,
        "program_assignment_id": assignment_id if kind == "program" else None,
    }


def _profile(conn, user_id, profile):
    if profile == "plan":
        assignment_id = grants.record_plan(
            conn,
            user_id=user_id,
            plan_code="pro_town",
            state="active",
            owner_decision_ref=OWNER,
            effective_at=NOW - timedelta(minutes=10),
        )
        return "plan", assignment_id
    assignment_id = grants.record_program(
        conn,
        user_id=user_id,
        program_code=profile,
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
    )
    return "program", assignment_id


def _serve(conn, area_id="alpine"):
    if areas.get_state(conn, area_id) == "locked":
        areas.transition(
            conn,
            area_id=area_id,
            to_state="free_beta",
            owner_decision_ref=OWNER,
            rule="explicit-test-activation",
        )


def _grant_valid_path(
    conn,
    user_id,
    *,
    profile="plan",
    area_id="alpine",
    feature="timeline",
    lane="public",
    serve=True,
):
    basis_kind, assignment_id = _profile(conn, user_id, profile)
    basis = _basis_kwargs(basis_kind, assignment_id)
    feature_id = grants.record_feature(
        conn,
        user_id=user_id,
        feature_key=feature,
        publication_lane=lane,
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=9),
        **basis,
    )
    geography_id = grants.record_geography(
        conn,
        user_id=user_id,
        area_id=area_id,
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=8),
        **basis,
    )
    conn.commit()
    if serve:
        _serve(conn, area_id)
    return basis_kind, assignment_id, feature_id, geography_id


def test_exact_plan_feature_geography_and_served_area_allow(conn):
    user_id = _user(conn)
    basis_kind, assignment_id, feature_id, geography_id = _grant_valid_path(
        conn,
        user_id,
    )

    decision = evaluator.evaluate(conn, _request(user_id))

    assert decision.allowed is True
    assert decision.reason_code == "allow"
    assert decision.plan_code == "pro_town"
    assert decision.program_codes == ()
    assert decision.area_state == "free_beta"
    assert decision.catalog_version == CATALOG_VERSION
    assert decision.basis_kind == basis_kind
    assert decision.basis_assignment_id == assignment_id
    assert decision.feature_grant_id == feature_id
    assert decision.geography_grant_id == geography_id
    assert decision.evaluated_utc.endswith("+00:00")


def test_internal_program_can_authorize_without_fake_paid_plan(conn):
    user_id = _user(conn, "beta@example.test")
    _grant_valid_path(
        conn,
        user_id,
        profile="beta_tester",
        lane="reviewer_internal",
    )

    decision = evaluator.evaluate(
        conn,
        _request(user_id, lane="reviewer_internal"),
    )

    assert decision.allowed is True
    assert decision.plan_code is None
    assert decision.program_codes == ("beta_tester",)
    assert decision.basis_kind == "program"


@pytest.mark.parametrize(
    "setup,reason",
    [
        ("unapproved", "account_not_approved"),
        ("no_profile", "access_profile_missing"),
        ("no_feature", "feature_not_granted"),
        ("no_geography", "geography_not_granted"),
        ("locked_area", "area_not_served"),
    ],
)
def test_each_required_axis_fails_closed(conn, setup, reason):
    user_id = _user(
        conn,
        f"{setup}@example.test",
        approved=setup != "unapproved",
    )
    assignment_id = None
    if setup not in {"unapproved", "no_profile"}:
        _, assignment_id = _profile(conn, user_id, "plan")
    basis = _basis_kwargs("plan", assignment_id) if assignment_id else {}
    if setup not in {"unapproved", "no_profile", "no_feature"}:
        grants.record_feature(
            conn,
            user_id=user_id,
            feature_key="timeline",
            publication_lane="public",
            state="active",
            owner_decision_ref=OWNER,
            effective_at=NOW - timedelta(minutes=9),
            **basis,
        )
    if setup not in {
        "unapproved",
        "no_profile",
        "no_feature",
        "no_geography",
    }:
        grants.record_geography(
            conn,
            user_id=user_id,
            area_id="alpine",
            state="active",
            owner_decision_ref=OWNER,
            effective_at=NOW - timedelta(minutes=8),
            **basis,
        )
    conn.commit()
    if setup != "locked_area":
        _serve(conn)

    decision = evaluator.evaluate(conn, _request(user_id))
    assert decision.allowed is False
    assert decision.reason_code == reason


def test_plan_never_implies_feature_or_geography(conn):
    user_id = _user(conn)
    _profile(conn, user_id, "plan")
    conn.commit()

    decision = evaluator.evaluate(conn, _request(user_id))

    assert decision.allowed is False
    assert decision.reason_code == "feature_not_granted"


def test_exact_geography_does_not_expand_to_sibling_or_ancestor(conn):
    user_id = _user(conn)
    _grant_valid_path(conn, user_id, area_id="alpine")
    for area_id in ("etna", "lincoln", "wy"):
        decision = evaluator.evaluate(conn, _request(user_id, area_id=area_id))
        assert decision.allowed is False
        assert decision.reason_code == "geography_not_granted"


def test_latest_revocation_and_expiry_do_not_uncover_older_grants(conn):
    user_id = _user(conn)
    kind, assignment_id, _, _ = _grant_valid_path(conn, user_id)
    grants.record_feature(
        conn,
        user_id=user_id,
        feature_key="timeline",
        publication_lane="public",
        state="revoked",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=1),
        **_basis_kwargs(kind, assignment_id),
    )
    conn.commit()
    assert evaluator.evaluate(conn, _request(user_id)).reason_code == (
        "feature_not_granted"
    )

    second = _user(conn, "expired@example.test")
    plan_id = grants.record_plan(
        conn,
        user_id=second,
        plan_code="free",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(days=2),
    )
    basis = _basis_kwargs("plan", plan_id)
    grants.record_feature(
        conn,
        user_id=second,
        feature_key="timeline",
        publication_lane="public",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(days=1),
        **basis,
    )
    grants.record_geography(
        conn,
        user_id=second,
        area_id="alpine",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(days=2),
        **basis,
    )
    conn.commit()
    assert evaluator.evaluate(conn, _request(second)).reason_code == (
        "feature_not_granted"
    )


def test_future_revocation_does_not_revoke_early(conn):
    user_id = _user(conn)
    kind, assignment_id, _, _ = _grant_valid_path(conn, user_id)
    grants.record_geography(
        conn,
        user_id=user_id,
        area_id="alpine",
        state="revoked",
        owner_decision_ref=OWNER,
        effective_at=NOW + timedelta(hours=1),
        **_basis_kwargs(kind, assignment_id),
    )
    conn.commit()

    assert evaluator.evaluate(conn, _request(user_id)).allowed is True


def test_request_dto_cannot_select_an_authorization_time():
    with pytest.raises(TypeError):
        AccessRequest(
            user_id="u",
            feature_key="timeline",
            area_id="alpine",
            publication_lane="public",
            at=NOW,  # type: ignore[call-arg]
        )


def test_beta_revocation_invalidates_dependent_reviewer_grants(conn):
    user_id = _user(conn)
    beta_id = grants.record_program(
        conn,
        user_id=user_id,
        program_code="beta_tester",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
    )
    grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="free",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
    )
    beta_basis = _basis_kwargs("program", beta_id)
    grants.record_feature(
        conn,
        user_id=user_id,
        feature_key="timeline",
        publication_lane="reviewer_internal",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=9),
        **beta_basis,
    )
    grants.record_geography(
        conn,
        user_id=user_id,
        area_id="alpine",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=8),
        **beta_basis,
    )
    conn.commit()
    _serve(conn)
    request = _request(user_id, lane="reviewer_internal")
    assert evaluator.evaluate(conn, request).allowed is True

    grants.record_program(
        conn,
        user_id=user_id,
        program_code="beta_tester",
        state="revoked",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=1),
    )
    conn.commit()
    decision = evaluator.evaluate(conn, request)
    assert decision.allowed is False
    assert decision.reason_code == "feature_not_granted"
    assert decision.plan_code == "free"


def test_plan_downgrade_cannot_reuse_old_plan_grants(conn):
    user_id = _user(conn)
    global_id = grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="pro_global",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
    )
    basis = _basis_kwargs("plan", global_id)
    grants.record_feature(
        conn,
        user_id=user_id,
        feature_key="data_export",
        publication_lane="public",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=9),
        **basis,
    )
    grants.record_geography(
        conn,
        user_id=user_id,
        area_id="alpine",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=8),
        **basis,
    )
    conn.commit()
    _serve(conn)
    request = _request(user_id, feature="data_export")
    assert evaluator.evaluate(conn, request).allowed is True

    operation = "downgrade-operation"
    grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="pro_global",
        state="revoked",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=1),
        operation_id=operation,
    )
    grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="free",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=1),
        operation_id=operation,
    )
    conn.commit()
    assert evaluator.evaluate(conn, request).reason_code == "feature_not_granted"


def test_feature_and_geography_must_share_one_assignment_basis(conn):
    user_id = _user(conn)
    plan_id = grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="free",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
    )
    beta_id = grants.record_program(
        conn,
        user_id=user_id,
        program_code="beta_tester",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
    )
    grants.record_feature(
        conn,
        user_id=user_id,
        feature_key="timeline",
        publication_lane="public",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=9),
        **_basis_kwargs("program", beta_id),
    )
    grants.record_geography(
        conn,
        user_id=user_id,
        area_id="alpine",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=8),
        **_basis_kwargs("plan", plan_id),
    )
    conn.commit()
    _serve(conn)
    assert evaluator.evaluate(conn, _request(user_id)).reason_code == (
        "entitlement_basis_mismatch"
    )


def test_multiple_active_customer_plans_deny_as_ambiguous(conn):
    user_id = _user(conn)
    _grant_valid_path(conn, user_id)
    grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="pro_state",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=1),
    )
    conn.commit()

    decision = evaluator.evaluate(conn, _request(user_id))
    assert decision.allowed is False
    assert decision.reason_code == "ambiguous_plan_state"


def test_multiple_internal_programs_are_visible_but_basis_stays_exact(conn):
    user_id = _user(conn)
    _, beta_id, _, _ = _grant_valid_path(
        conn,
        user_id,
        profile="beta_tester",
        lane="reviewer_internal",
    )
    grants.record_program(
        conn,
        user_id=user_id,
        program_code="developer",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=1),
    )
    conn.commit()
    decision = evaluator.evaluate(
        conn,
        _request(user_id, lane="reviewer_internal"),
    )
    assert decision.allowed is True
    assert decision.program_codes == ("beta_tester", "developer")
    assert decision.basis_assignment_id == beta_id


def test_unknown_feature_and_lane_deny_before_grant_lookup(conn):
    user_id = _user(conn)
    _profile(conn, user_id, "developer")
    conn.commit()
    assert evaluator.evaluate(
        conn,
        _request(user_id, feature="browser_unlock"),
    ).reason_code == "unknown_feature"
    assert evaluator.evaluate(
        conn,
        _request(user_id, lane="private_everything"),
    ).reason_code == "unknown_publication_lane"


def test_reviewer_internal_cannot_be_bound_to_customer_plan(conn):
    user_id = _user(conn)
    _, plan_id = _profile(conn, user_id, "plan")
    with pytest.raises(grants.InvalidGrant, match="internal program"):
        grants.record_feature(
            conn,
            user_id=user_id,
            feature_key="timeline",
            publication_lane="reviewer_internal",
            state="active",
            owner_decision_ref=OWNER,
            **_basis_kwargs("plan", plan_id),
        )
    conn.rollback()


def test_geography_validator_rejects_malformed_expansion_chain(conn):
    assert validate_area(conn, "alpine")["name"] == "Alpine"
    areas.create_area(
        conn,
        area_id="bad-town",
        kind="town",
        name="Bad Town",
        parent_area_id="wy",
    )
    conn.commit()
    with pytest.raises(InvalidArea, match="requires parent kind"):
        validate_area(conn, "bad-town")


def test_writer_validation_rejects_ownerless_unknown_naive_and_missing_basis(conn):
    user_id = _user(conn)
    with pytest.raises(grants.InvalidGrant, match="owner_decision_ref"):
        grants.record_plan(
            conn,
            user_id=user_id,
            plan_code="free",
            state="active",
            owner_decision_ref=" ",
        )
    with pytest.raises(grants.InvalidGrant, match="unknown feature_key"):
        grants.record_feature(
            conn,
            user_id=user_id,
            feature_key="made_up",
            publication_lane="public",
            state="active",
            owner_decision_ref=OWNER,
        )
    with pytest.raises(grants.InvalidGrant, match="timezone-aware"):
        grants.record_program(
            conn,
            user_id=user_id,
            program_code="developer",
            state="active",
            owner_decision_ref=OWNER,
            effective_at=datetime(2026, 7, 24, 12, 0),
        )
    with pytest.raises(grants.InvalidGrant, match="exactly one"):
        grants.record_geography(
            conn,
            user_id=user_id,
            area_id="alpine",
            state="active",
            owner_decision_ref=OWNER,
        )
    conn.rollback()


def test_dependent_grant_cannot_outlive_assignment(conn):
    user_id = _user(conn)
    plan_id = grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="pro_town",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=NOW - timedelta(minutes=10),
        expires_at=NOW + timedelta(minutes=10),
    )
    with pytest.raises(grants.InvalidGrant, match="outlive"):
        grants.record_feature(
            conn,
            user_id=user_id,
            feature_key="timeline",
            publication_lane="public",
            state="active",
            owner_decision_ref=OWNER,
            effective_at=NOW - timedelta(minutes=9),
            **_basis_kwargs("plan", plan_id),
        )
    conn.rollback()


def test_writers_do_not_commit_unrelated_or_partial_changes(conn):
    user_id = _user(conn)
    conn.execute(
        "INSERT INTO consent_preferences (user_id, notification_consent)"
        " VALUES (?, 0)",
        (user_id,),
    )
    grants.record_plan(
        conn,
        user_id=user_id,
        plan_code="free",
        state="active",
        owner_decision_ref=OWNER,
    )
    with pytest.raises(grants.InvalidGrant):
        grants.record_feature(
            conn,
            user_id=user_id,
            feature_key="not-real",
            publication_lane="public",
            state="active",
            owner_decision_ref=OWNER,
        )
    conn.rollback()
    assert conn.execute(
        "SELECT COUNT(*) FROM consent_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM access_plan_assignments WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0] == 0


def test_complete_entitlement_bundle_has_one_explicit_operation_and_actor(conn):
    user_id = _user(conn)
    _, assignment_id, feature_id, geography_id = _grant_valid_path(conn, user_id)

    audit_contexts = {
        tuple(
            conn.execute(
                "SELECT operation_id, actor FROM access_plan_assignments"
                " WHERE assignment_id = ?",
                (assignment_id,),
            ).fetchone()
        ),
        tuple(
            conn.execute(
                "SELECT operation_id, actor FROM access_feature_grants"
                " WHERE grant_id = ?",
                (feature_id,),
            ).fetchone()
        ),
        tuple(
            conn.execute(
                "SELECT operation_id, actor FROM access_geography_grants"
                " WHERE grant_id = ?",
                (geography_id,),
            ).fetchone()
        ),
    }

    assert audit_contexts == {(TEST_OPERATION, TEST_ACTOR)}


def test_writer_api_requires_operation_and_actor(conn):
    user_id = _user(conn)
    writer = _ORIGINAL_WRITERS["record_plan"]
    common = {
        "user_id": user_id,
        "plan_code": "free",
        "state": "active",
        "owner_decision_ref": OWNER,
    }

    with pytest.raises(TypeError):
        writer(conn, **common)
    with pytest.raises(grants.InvalidGrant, match="operation_id"):
        writer(
            conn,
            operation_id="",
            actor=TEST_ACTOR,
            **common,
        )
    with pytest.raises(grants.InvalidGrant, match="actor"):
        writer(
            conn,
            operation_id=TEST_OPERATION,
            actor=" ",
            **common,
        )
    conn.rollback()


def test_mixed_awareness_and_submillisecond_windows_raise_invalid_grant(conn):
    user_id = _user(conn)
    writer = _ORIGINAL_WRITERS["record_plan"]
    common = {
        "user_id": user_id,
        "plan_code": "free",
        "state": "active",
        "owner_decision_ref": OWNER,
        "operation_id": TEST_OPERATION,
        "actor": TEST_ACTOR,
    }
    effective = NOW.replace(microsecond=100)

    with pytest.raises(grants.InvalidGrant, match="timezone-aware"):
        writer(
            conn,
            effective_at=effective,
            expires_at=(effective + timedelta(seconds=1)).replace(tzinfo=None),
            **common,
        )
    with pytest.raises(grants.InvalidGrant, match="millisecond precision"):
        writer(
            conn,
            effective_at=effective,
            expires_at=effective.replace(microsecond=900),
            **common,
        )
    conn.rollback()


def test_evaluator_refuses_caller_transaction_without_rolling_it_back(conn):
    user_id = _user(conn)
    conn.execute(
        "INSERT INTO consent_preferences (user_id, notification_consent)"
        " VALUES (?, 0)",
        (user_id,),
    )
    decision = evaluator.evaluate(conn, _request(user_id))
    assert decision.allowed is False
    assert decision.reason_code == "evaluation_transaction_active"
    assert conn.in_transaction is True
    conn.rollback()


def test_equal_effective_time_uses_sequence_tie_break(conn):
    user_id = _user(conn)
    _, plan_id = _profile(conn, user_id, "plan")
    basis = _basis_kwargs("plan", plan_id)
    same = NOW - timedelta(minutes=5)
    grants.record_feature(
        conn,
        user_id=user_id,
        feature_key="timeline",
        publication_lane="public",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=same,
        **basis,
    )
    grants.record_feature(
        conn,
        user_id=user_id,
        feature_key="timeline",
        publication_lane="public",
        state="revoked",
        owner_decision_ref=OWNER,
        effective_at=same,
        **basis,
    )
    grants.record_geography(
        conn,
        user_id=user_id,
        area_id="alpine",
        state="active",
        owner_decision_ref=OWNER,
        effective_at=same,
        **basis,
    )
    conn.commit()
    _serve(conn)
    assert evaluator.evaluate(conn, _request(user_id)).reason_code == (
        "feature_not_granted"
    )


def test_noncanonical_and_malformed_stored_times_fail_closed(conn):
    user_id = _user(conn)
    _grant_valid_path(conn, user_id)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE access_feature_grants SET effective_utc = 'not-a-time'"
        " WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    decision = evaluator.evaluate(conn, _request(user_id))
    assert decision.allowed is False
    assert decision.reason_code == "invalid_access_state"


def test_mixed_offset_revocation_cannot_be_misordered(conn):
    user_id = _user(conn)
    kind, assignment_id, _, _ = _grant_valid_path(conn, user_id)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "INSERT INTO access_feature_grants"
        " (grant_id,user_id,feature_key,publication_lane,catalog_version,"
        " plan_assignment_id,program_assignment_id,grant_state,"
        " owner_decision_ref,operation_id,actor,recorded_utc,effective_utc)"
        " VALUES ('offset-revoke',?,'timeline','public',?,?,?,?,"
        " 'owner:x','op:x','test-suite',?,"
        " '2026-07-24T09:00:00.000-04:00')",
        (
            user_id,
            CATALOG_VERSION,
            assignment_id if kind == "plan" else None,
            assignment_id if kind == "program" else None,
            "revoked",
            NOW.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        ),
    )
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    assert evaluator.evaluate(conn, _request(user_id)).reason_code == (
        "invalid_access_state"
    )


def test_account_pause_takes_effect_on_next_snapshot(conn):
    user_id = _user(conn)
    _grant_valid_path(conn, user_id)
    assert evaluator.evaluate(conn, _request(user_id)).allowed is True
    accounts.pause(conn, user_id, owner_decision_ref=OWNER)
    assert evaluator.evaluate(conn, _request(user_id)).reason_code == (
        "account_not_approved"
    )


def test_publication_lane_is_exact(conn):
    user_id = _user(conn)
    _grant_valid_path(conn, user_id, lane="public")
    assert evaluator.evaluate(
        conn,
        _request(user_id, lane="reviewer_internal"),
    ).reason_code == "feature_not_granted"


def test_area_state_is_allowlisted_not_merely_not_locked(conn):
    user_id = _user(conn)
    _grant_valid_path(conn, user_id)
    for state in ("free_home", "free_beta", "funded", "paid", "limited"):
        conn.execute(
            "UPDATE area_state SET state = ? WHERE area_id = 'alpine'",
            (state,),
        )
        conn.commit()
        assert evaluator.evaluate(conn, _request(user_id)).allowed is True

    conn.execute("PRAGMA ignore_check_constraints = ON")
    conn.execute(
        "UPDATE area_state SET state = 'future_auto_served'"
        " WHERE area_id = 'alpine'"
    )
    conn.commit()
    conn.execute("PRAGMA ignore_check_constraints = OFF")
    decision = evaluator.evaluate(conn, _request(user_id))
    assert decision.allowed is False
    assert decision.reason_code == "area_not_served"
