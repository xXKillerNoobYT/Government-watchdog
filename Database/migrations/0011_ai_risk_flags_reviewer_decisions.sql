-- Stage 1 Slice 3 D (GOV-91): Lane 4 risk layer + Lane 5 runtime reviewer-gate.
--
-- Consumes the GOV-88 (Slice 3 A) gap analysis & interface design
-- (Docs/stage3-ai-gateway-gap-analysis.md) §4.3 (Lane 4 L4-5) + §4.4 (Lane 5
-- L5-1/L5-5), against contracts 1.09 (automation-vs-AI boundary, step 11 / G2)
-- and 1.11 (publication/privacy/legal/moderation gates §1/§2/§4/§5/§6.5) and
-- AI_GATEWAY_PROCESSING_WORKFLOW.md lanes 4 ("identify privacy/legal/publication/
-- moderation risks and no-go conditions") and 5 ("approve, correct, dispute,
-- hold, or reject output before beta/public presentation").
--
-- DESIGN — same fail-closed shape as 0010 (Lane 3): both tables are NEW,
-- append-only SIDE tables keyed to the statement. Neither this migration nor the
-- Lane-4 scanner mutates `statements` / `evidence_links` — an AI claim stays
-- `machine_extracted_unreviewed` + `not_publishable` by construction. The ONE
-- field that ever flips a claim to a reviewed status is written by the Lane-5
-- `ai_risk_gate.promote_statement` runtime gate, and ONLY after it has recorded a
-- `reviewer_decisions` row (a human decision) — there is no other code path. This
-- is the single AI->public path 1.09 §1.2 / 1.11 §0 commit to, with the human
-- G2 gate made a runtime check, not just a rule.
--
-- Additive + idempotent: db.py's ledger skips an already-applied file, and the
-- new-table/index statements use CREATE ... IF NOT EXISTS, so a bare re-run is a
-- no-op. NO table rebuild here (unlike 0009): Lane 4/5 add storage and reuse the
-- existing `statements` gating columns (verification_status / review_state /
-- publication_state / ui_status) — they widen no CHECK and add no column to any
-- existing table.
--
-- NO NEW STATUS VOCABULARY (gap analysis §6 / D-4): the reviewer-gate promotes
-- INTO the existing record-of-truth 6-value verificationStatus enum
-- (publication.ALLOWED_VERIFICATION_STATUSES) and never invents a value. The
-- Lane-4 `risk_category` / `severity` and the Lane-5 `decision` are distinct FLAG
-- / ACTION vocabularies — deliberately NOT the verificationStatus enum.
--
-- SCOPE: Alpine-only, local/vault-only. Both tables, their `matched_signal`,
-- `detail`, and `reason` are reviewer/vault-only evidence and are deliberately NOT
-- on publication.WEB_SAFE_FIELD_ALLOWLIST (1.11 §2.1; AI_GATEWAY §7.1). Only
-- summary counts belong in a Paperclip comment. A column-name guard test asserts
-- no column of either table can ever be web-projected.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- ai_risk_flags — Lane-4 risk-layer findings. One row per
-- (Lane-4 run, statement, risk_category) no-go/review flag. Records WHICH risk
-- category fired, HOW severe, whether it BLOCKS downstream, the deterministic
-- detector id, and a vault-only matched-signal/detail of what tripped it.
-- `run_id` joins the existing gateway-run ledger (ai_extraction_runs with
-- lane='4_risk'); the Lane-4 run carries the input set / tool version / errors /
-- reviewer state / retry required by AI_GATEWAY §17 — this table carries the
-- per-statement findings. A flag is a FLAG: it never mutates the claim row.
-- A reviewer may `resolved=1` a flag (audited) so a downstream gate can clear it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_risk_flags (
    flag_id            TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES ai_extraction_runs(run_id),
    statement_id       TEXT NOT NULL REFERENCES statements(statement_id),
    -- the 1.11 no-go families this lane screens for (AI_GATEWAY lane 4).
    risk_category      TEXT NOT NULL
        CHECK (risk_category IN ('privacy', 'legal', 'publication', 'moderation')),
    -- 'no_go' = hard block (1.11 default-deny); 'review' = route to a human but not
    -- a hard block; 'clear' = screened, nothing found (recorded for audit only).
    severity           TEXT NOT NULL DEFAULT 'no_go'
        CHECK (severity IN ('no_go', 'review', 'clear')),
    -- convenience flag for the downstream gate: 1 unless severity='clear'. The
    -- claim itself stays not_publishable regardless; this just lets the reviewer
    -- queue + promote-gate find blocking flags without re-deriving severity.
    blocks_downstream  INTEGER NOT NULL DEFAULT 1 CHECK (blocks_downstream IN (0, 1)),
    -- the deterministic detector id + the matched signal, for reproducibility/audit.
    detector           TEXT,
    matched_signal     TEXT,   -- vault-only provenance of what tripped the flag.
    detail             TEXT,   -- vault-only.
    -- a reviewer may resolve a flag (e.g. confirmed false positive); auditable.
    resolved           INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    resolved_by        TEXT,
    resolved_reason    TEXT,
    resolved_utc       TEXT,
    scanned_utc        TEXT,
    created_utc        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_risk_statement ON ai_risk_flags(statement_id);
CREATE INDEX IF NOT EXISTS idx_ai_risk_run ON ai_risk_flags(run_id);
CREATE INDEX IF NOT EXISTS idx_ai_risk_category ON ai_risk_flags(risk_category);
CREATE INDEX IF NOT EXISTS idx_ai_risk_open ON ai_risk_flags(statement_id, resolved, blocks_downstream);

-- ---------------------------------------------------------------------------
-- reviewer_decisions — Lane-5 human/reviewer gate ledger. One append-only row
-- per reviewer decision on a statement: WHO decided, WHAT they decided
-- (approve/correct/dispute/hold/reject), the from/to verificationStatus, and WHY
-- (reason + reason_category) — the auditable who/when/reason 1.11 §6.5 requires,
-- and the runtime authority for 1.09 step 11 / G2 ("only a human promotes").
-- `ai_risk_gate.promote_statement` writes exactly one row here per call BEFORE it
-- touches the claim's verificationStatus; a claim with no promoting decision row
-- can never have been moved to a reviewed status through the sanctioned path.
-- Vault-only — the decision/reason is reviewer evidence, never web-projected.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviewer_decisions (
    decision_id              TEXT PRIMARY KEY,
    statement_id             TEXT NOT NULL REFERENCES statements(statement_id),
    -- the producing gateway run this decision reviewed (nullable: a human/auto
    -- statement may have no AI run). Joins the run health into the audit trail.
    run_id                   TEXT REFERENCES ai_extraction_runs(run_id),
    -- WHO: a human reviewer / role id. The gate refuses an empty id or a known
    -- automation/AI actor sentinel (enforced in code — 1.09 §2.5, 1.11 §5).
    reviewer_id              TEXT NOT NULL,
    -- WHAT: the Lane-5 action set (approve / correct / dispute / hold / reject).
    decision                 TEXT NOT NULL
        CHECK (decision IN ('approved', 'corrected', 'disputed', 'hold', 'rejected')),
    -- the claim's verificationStatus before / after this decision (for the audit
    -- chain Stage 1.12 traceability builds on). to_verification_status is one of
    -- the 6-value record enum (validated in code against publication's SSOT).
    from_verification_status TEXT,
    to_verification_status   TEXT,
    -- WHY: free-text reason + a coarse category — required, auditable.
    reason                   TEXT NOT NULL,
    reason_category          TEXT,
    -- 1 when this decision moved the claim toward a reviewed status (approve/
    -- correct). The downstream gate reads this as "a human promoted it".
    promoted                 INTEGER NOT NULL DEFAULT 0 CHECK (promoted IN (0, 1)),
    decided_utc              TEXT,
    created_utc              TEXT
);
CREATE INDEX IF NOT EXISTS idx_reviewer_dec_statement ON reviewer_decisions(statement_id);
CREATE INDEX IF NOT EXISTS idx_reviewer_dec_run ON reviewer_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_reviewer_dec_decision ON reviewer_decisions(decision);
CREATE INDEX IF NOT EXISTS idx_reviewer_dec_promoted ON reviewer_decisions(statement_id, promoted);
