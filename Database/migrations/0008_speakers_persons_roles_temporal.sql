-- Stage 1 Slice 2, Issue D (GOV-83): speaker-attribution safety + person/role +
-- temporal layering. Contract 1.07 §3 (speaker attribution; persons/roles) + §4
-- (known-then vs later layers; outcome / outcome_updates). Sequenced in GOV-79
-- Part C-D; builds directly on the GOV-82 (0007) statements/evidence_links spine.
--
-- Additive + idempotent (CREATE ... IF NOT EXISTS; db.py ledger skips an
-- already-applied file, and IF NOT EXISTS makes a bare re-run safe regardless).
-- Extends — does NOT rebuild — the landed schema. Every table here is a NEW 1.07
-- node/edge with no landed equivalent.
--
-- ============================================================================
-- PRIVACY POSTURE (issue acceptance: "No private-identity fields present" —
-- SecurityPrivacyAgent MANDATORY gate; COMPANY.md non-negotiable; 1.07 §7):
-- These tables intentionally carry NO column for a home/personal address, voter
-- registration, personal phone/email, date of birth, SSN, or any private
-- identifier. Privacy is enforced by SCHEMA ABSENCE — data that has no column
-- cannot be stored or leaked, even by a buggy writer. A column-name scan test
-- (tests/test_speakers_temporal.py::test_no_private_identity_columns) asserts no
-- forbidden field name can ever be added without failing CI.
--
-- `persons.display_name` holds ONLY the public-record name of an official
-- (officials speaking in official capacity at a public meeting are on the
-- record). It is NOT on publication.WEB_SAFE_FIELD_ALLOWLIST, so to_web_safe()
-- drops it fail-closed; the only speaker datum that crosses to the frontend is
-- the COMPUTED safe label (scripts/speakers.safe_speaker_label), never a raw
-- name or a candidate. `candidate_person_id` (the uncertain-state reviewer hint,
-- 1.07 §3.2) is private/vault-only context and likewise never web-projected.
-- ============================================================================
--
-- ENUM REUSE (1.07 §5; gap analysis D-5): the 6-value RECORD verificationStatus
-- enum, the publication allowlist, and compute_ui_status are owned by
-- scripts/publication.py and IMPORTED by scripts/speakers.py — never re-typed.
-- The §3/§4 vocabularies this slice introduces (attribution_state, speaker_class,
-- reviewer_state, the §4 `layer` enum reused from 0007) are defined once in
-- scripts/speakers.py and mirrored by the CHECK literals below; a parity test
-- guards against drift.
--
-- SCOPE LOCK: Alpine-only, local/vault-only, NO AI. No bodies/government_body
-- table exists yet (meetings.body is plain TEXT), so roles.body_id is a nullable
-- forward-pointer TEXT with NO FK — mirroring how 0007 treated
-- statements.speaker_attribution_id (no FK against a not-yet-existing table).

PRAGMA foreign_keys = ON;

-- person (1.07 §1.1, §3): a real individual. `display_name` is gated public-
-- record identity (officials only); see the privacy posture header. NO private
-- identity / address / voter-registry columns exist by design.
CREATE TABLE IF NOT EXISTS persons (
    person_id    TEXT PRIMARY KEY,
    display_name TEXT,
    person_type  TEXT NOT NULL DEFAULT 'official'
        CHECK (person_type IN ('official', 'public', 'unknown')),
    created_utc  TEXT
);

-- role (1.07 §1.1): an office/role held during a date range. `body_id` is a
-- nullable forward-pointer (no bodies table yet); no FK is declared against a
-- table that does not exist.
CREATE TABLE IF NOT EXISTS roles (
    role_id     TEXT PRIMARY KEY,
    body_id     TEXT,
    title       TEXT NOT NULL,
    start_date  TEXT,
    end_date    TEXT,
    created_utc TEXT
);

-- served_in_role (1.07 §1.2): person served in role during a date range. A typed
-- edge node (Isaac concept-map directive: explicit relationship types).
CREATE TABLE IF NOT EXISTS served_in_role (
    served_in_role_id TEXT PRIMARY KEY,
    person_id         TEXT NOT NULL REFERENCES persons(person_id),
    role_id           TEXT NOT NULL REFERENCES roles(role_id),
    start_date        TEXT,
    end_date          TEXT,
    source_id         TEXT REFERENCES sources(source_id),
    created_utc       TEXT
);
CREATE INDEX IF NOT EXISTS idx_served_in_role_person ON served_in_role(person_id);
CREATE INDEX IF NOT EXISTS idx_served_in_role_role ON served_in_role(role_id);

-- speaker_attribution (1.07 §3.1): the separately-typed attribution record joined
-- to a statement, so identity can be uncertain/withheld WITHOUT weakening the
-- statement's source pointer (§3.4). `attribution_state` + `speaker_class` are the
-- two safety dimensions. `person_id` is set ONLY when attributed (enforced by
-- scripts/speakers.py — never bound on uncertain/unattributed). `candidate_person_id`
-- is the §3.2 reviewer-only hint for an `uncertain` record; it is private and
-- never web-projected. `display_label` is the SAFE renderable label (role/generic),
-- never a raw name on its own for an unconfirmed speaker.
CREATE TABLE IF NOT EXISTS speaker_attributions (
    speaker_attribution_id TEXT PRIMARY KEY,
    statement_id           TEXT NOT NULL REFERENCES statements(statement_id),
    attribution_state      TEXT NOT NULL DEFAULT 'unattributed'
        CHECK (attribution_state IN ('attributed', 'uncertain', 'unattributed')),
    speaker_class          TEXT NOT NULL DEFAULT 'unidentified'
        CHECK (speaker_class IN ('on-record-official', 'on-record-public', 'unidentified', 'private-context')),
    person_id              TEXT REFERENCES persons(person_id),
    role_id                TEXT REFERENCES roles(role_id),
    candidate_person_id    TEXT REFERENCES persons(person_id),
    display_label          TEXT,
    basis                  TEXT,
    minutes_source_id      TEXT REFERENCES sources(source_id),
    reviewer_state         TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (reviewer_state IN ('unreviewed', 'approved', 'rejected')),
    confidence             TEXT NOT NULL DEFAULT 'low' CHECK (confidence IN ('high', 'medium', 'low')),
    created_utc            TEXT,
    -- §3.4 invariant, enforced at the row level: a bound person_id (a resolved
    -- identity) is allowed ONLY in the `attributed` state. uncertain/unattributed
    -- fail closed to NULL person_id (the candidate, if any, lives in
    -- candidate_person_id). speakers.py enforces the speaker_class half.
    CHECK (person_id IS NULL OR attribution_state = 'attributed')
);
CREATE INDEX IF NOT EXISTS idx_speaker_attr_statement ON speaker_attributions(statement_id);
CREATE INDEX IF NOT EXISTS idx_speaker_attr_person ON speaker_attributions(person_id);

-- made_statement (1.07 §1.2, §3.4): person made statement at a timestamp. This
-- edge may exist ONLY when the attribution is `attributed` AND the speaker_class
-- permits naming (on-record-official, or on-record-public WITH recorded CEO
-- approval — the latter a hard stop that automation never creates). Enforced by
-- scripts/speakers.py; the FK guarantees the person exists.
CREATE TABLE IF NOT EXISTS made_statement (
    made_statement_id TEXT PRIMARY KEY,
    person_id         TEXT NOT NULL REFERENCES persons(person_id),
    statement_id      TEXT NOT NULL REFERENCES statements(statement_id),
    role_id           TEXT REFERENCES roles(role_id),
    created_utc       TEXT,
    UNIQUE (person_id, statement_id)
);
CREATE INDEX IF NOT EXISTS idx_made_statement_statement ON made_statement(statement_id);

-- outcome (1.07 §1.1, §4): a later real-world result that UPDATES an earlier
-- event without rewriting it. Carries its own date and the §4 `layer` (default
-- `actual_later`). Record-level SSOT columns reuse the 6-value enum (D-5).
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id          TEXT PRIMARY KEY,
    outcome_date        TEXT,
    outcome_text        TEXT NOT NULL,
    layer               TEXT NOT NULL DEFAULT 'actual_later'
        CHECK (layer IN ('known_then', 'presented_then', 'ai_thought_then', 'corrected_later', 'actual_later')),
    source_id           TEXT REFERENCES sources(source_id),
    verification_status TEXT NOT NULL DEFAULT 'machine_extracted_unreviewed'
        CHECK (verification_status IN ('source_recorded', 'machine_extracted_unreviewed', 'reviewed_source_linked', 'human_verified', 'disputed', 'do_not_publish')),
    correction_status   TEXT NOT NULL DEFAULT 'none',
    confidence          TEXT NOT NULL DEFAULT 'medium' CHECK (confidence IN ('high', 'medium', 'low')),
    created_utc         TEXT
);

-- outcome_updates (1.07 §1.2, §4.2): later outcome updates a prior event WITHOUT
-- rewriting known-then context. Forward-only: the edge points FROM the new
-- outcome TO an earlier node (statement/decision/...); it NEVER mutates the
-- target row. `to_node_id` is a polymorphic target (the prior node may live in
-- several tables), so it is a plain TEXT pointer with `to_node_type` as the tag —
-- no single FK can span tables. The non-mutation guarantee is enforced by
-- scripts/speakers.link_outcome_updates (insert-only) and unit-tested.
CREATE TABLE IF NOT EXISTS outcome_updates (
    outcome_update_id TEXT PRIMARY KEY,
    outcome_id        TEXT NOT NULL REFERENCES outcomes(outcome_id),
    to_node_id        TEXT NOT NULL,
    to_node_type      TEXT NOT NULL DEFAULT 'statement'
        CHECK (to_node_type IN ('statement', 'decision', 'agenda_item', 'evidence_link', 'outcome')),
    relation          TEXT NOT NULL DEFAULT 'updates'
        CHECK (relation IN ('updates', 'corrects')),
    created_utc       TEXT,
    UNIQUE (outcome_id, to_node_id, to_node_type)
);
CREATE INDEX IF NOT EXISTS idx_outcome_updates_outcome ON outcome_updates(outcome_id);
CREATE INDEX IF NOT EXISTS idx_outcome_updates_target ON outcome_updates(to_node_id, to_node_type);
