-- ACCESS-2026 v0.1: server-authoritative account/feature/geography decision
-- substrate. This migration is deliberately policy-neutral: it stores explicit
-- owner-reviewed facts but does not infer plan benefits, border towns,
-- descendant geography, billing state, or a 90-day town-change rule.
--
-- Additive and inert. All rows are created by a later explicit owner action.
-- Applying this migration grants nobody access and changes no existing serving
-- route. Every table is append-only by service convention; current state is the
-- latest effective row ordered by (effective_utc, integer sequence). All time
-- values use one canonical UTC representation so text order equals time order.
--
-- Migration slot: 0027 follows the landed 0026 beta gate. If another 0027
-- lands first, the second-lander-renumbers rule applies at merge review.

-- Customer product identity. plan_code is an internal semantic code, not
-- customer-facing copy or a price. Multiple simultaneously active customer
-- plans are treated as an ambiguous fail-closed state by the evaluator.
CREATE TABLE IF NOT EXISTS access_plan_assignments (
    assignment_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id        TEXT NOT NULL UNIQUE,
    user_id              TEXT NOT NULL REFERENCES users(user_id),
    plan_code            TEXT NOT NULL
        CHECK (plan_code IN ('free', 'pro_town', 'pro_multi_home',
                             'pro_state', 'pro_global', 'contract')),
    catalog_version      TEXT NOT NULL,
    assignment_state     TEXT NOT NULL
        CHECK (assignment_state IN ('active', 'revoked')),
    owner_decision_ref   TEXT NOT NULL
        CHECK (length(trim(owner_decision_ref)) > 0),
    operation_id         TEXT NOT NULL
        CHECK (length(trim(operation_id)) > 0),
    actor                TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    recorded_utc         TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc) IS NOT NULL
               AND recorded_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc)),
    effective_utc        TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc) IS NOT NULL
               AND effective_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc)),
    expires_utc          TEXT,
    CHECK (expires_utc IS NULL OR
           (strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc) IS NOT NULL
            AND expires_utc =
                strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc))),
    CHECK (expires_utc IS NULL OR expires_utc > effective_utc)
);
CREATE INDEX IF NOT EXISTS idx_access_plan_user
    ON access_plan_assignments(user_id, plan_code, effective_utc, assignment_seq);

-- Internal/special programs are separate from customer plans. A beta tester or
-- developer can therefore receive explicit capabilities without pretending to
-- be a paid customer, and a paid customer does not inherit an internal role.
CREATE TABLE IF NOT EXISTS access_program_assignments (
    assignment_seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id        TEXT NOT NULL UNIQUE,
    user_id              TEXT NOT NULL REFERENCES users(user_id),
    program_code         TEXT NOT NULL
        CHECK (program_code IN ('developer', 'beta_tester',
                                'special_contract_team')),
    catalog_version      TEXT NOT NULL,
    assignment_state     TEXT NOT NULL
        CHECK (assignment_state IN ('active', 'revoked')),
    owner_decision_ref   TEXT NOT NULL
        CHECK (length(trim(owner_decision_ref)) > 0),
    operation_id         TEXT NOT NULL
        CHECK (length(trim(operation_id)) > 0),
    actor                TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    recorded_utc         TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc) IS NOT NULL
               AND recorded_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc)),
    effective_utc        TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc) IS NOT NULL
               AND effective_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc)),
    expires_utc          TEXT,
    CHECK (expires_utc IS NULL OR
           (strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc) IS NOT NULL
            AND expires_utc =
                strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc))),
    CHECK (expires_utc IS NULL OR expires_utc > effective_utc)
);
CREATE INDEX IF NOT EXISTS idx_access_program_user
    ON access_program_assignments(user_id, program_code, effective_utc, assignment_seq);

-- Explicit per-user feature + publication-lane grants. Plans and programs do
-- not imply these rows: the owner-approved benefit matrix will create them in
-- a later policy layer. Unknown feature keys are denied by the evaluator.
CREATE TABLE IF NOT EXISTS access_feature_grants (
    grant_seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id             TEXT NOT NULL UNIQUE,
    user_id              TEXT NOT NULL REFERENCES users(user_id),
    feature_key          TEXT NOT NULL CHECK (length(trim(feature_key)) > 0),
    publication_lane     TEXT NOT NULL
        CHECK (publication_lane IN ('public', 'reviewer_internal')),
    catalog_version      TEXT NOT NULL,
    plan_assignment_id   TEXT
        REFERENCES access_plan_assignments(assignment_id),
    program_assignment_id TEXT
        REFERENCES access_program_assignments(assignment_id),
    grant_state          TEXT NOT NULL
        CHECK (grant_state IN ('active', 'revoked')),
    owner_decision_ref   TEXT NOT NULL
        CHECK (length(trim(owner_decision_ref)) > 0),
    operation_id         TEXT NOT NULL
        CHECK (length(trim(operation_id)) > 0),
    actor                TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    recorded_utc         TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc) IS NOT NULL
               AND recorded_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc)),
    effective_utc        TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc) IS NOT NULL
               AND effective_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc)),
    expires_utc          TEXT,
    CHECK ((plan_assignment_id IS NOT NULL) +
           (program_assignment_id IS NOT NULL) = 1),
    CHECK (expires_utc IS NULL OR
           (strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc) IS NOT NULL
            AND expires_utc =
                strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc))),
    CHECK (expires_utc IS NULL OR expires_utc > effective_utc)
);
CREATE INDEX IF NOT EXISTS idx_access_feature_user
    ON access_feature_grants(
        user_id, feature_key, publication_lane,
        plan_assignment_id, program_assignment_id, effective_utc, grant_seq
    );

-- Exact-area grants only. A county/state grant does NOT imply descendant towns
-- in v0.1, and no border-town adjacency is inferred. Those policies require an
-- authoritative, versioned geography decision before implementation.
CREATE TABLE IF NOT EXISTS access_geography_grants (
    grant_seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id             TEXT NOT NULL UNIQUE,
    user_id              TEXT NOT NULL REFERENCES users(user_id),
    area_id              TEXT NOT NULL REFERENCES areas(area_id),
    scope_kind           TEXT NOT NULL DEFAULT 'exact'
        CHECK (scope_kind = 'exact'),
    catalog_version      TEXT NOT NULL,
    plan_assignment_id   TEXT
        REFERENCES access_plan_assignments(assignment_id),
    program_assignment_id TEXT
        REFERENCES access_program_assignments(assignment_id),
    grant_state          TEXT NOT NULL
        CHECK (grant_state IN ('active', 'revoked')),
    owner_decision_ref   TEXT NOT NULL
        CHECK (length(trim(owner_decision_ref)) > 0),
    operation_id         TEXT NOT NULL
        CHECK (length(trim(operation_id)) > 0),
    actor                TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    recorded_utc         TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc) IS NOT NULL
               AND recorded_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', recorded_utc)),
    effective_utc        TEXT NOT NULL
        CHECK (strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc) IS NOT NULL
               AND effective_utc =
               strftime('%Y-%m-%dT%H:%M:%f+00:00', effective_utc)),
    expires_utc          TEXT,
    CHECK ((plan_assignment_id IS NOT NULL) +
           (program_assignment_id IS NOT NULL) = 1),
    CHECK (expires_utc IS NULL OR
           (strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc) IS NOT NULL
            AND expires_utc =
                strftime('%Y-%m-%dT%H:%M:%f+00:00', expires_utc))),
    CHECK (expires_utc IS NULL OR expires_utc > effective_utc)
);
CREATE INDEX IF NOT EXISTS idx_access_geography_user
    ON access_geography_grants(
        user_id, area_id, plan_assignment_id, program_assignment_id,
        effective_utc, grant_seq
    );
