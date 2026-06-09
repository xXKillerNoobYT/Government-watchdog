-- Stage 1 Slice 3 C (GOV-90): Lane 3 verification layer — the deterministic
-- compare-to-source / flag-uncertainty result ledger.
--
-- Consumes the GOV-88 (Slice 3 A) gap analysis & interface design
-- (Docs/stage3-ai-gateway-gap-analysis.md) §4.2 (Lane 3 L3-1/L3-5/L3-6), against
-- contracts 1.09 (automation-vs-AI boundary, step 11 prep) + 1.11 (publication
-- gates §5) and AI_GATEWAY_PROCESSING_WORKFLOW.md lane 3 ("compare AI output to
-- primary source, assign verification label, flag uncertainty").
--
-- DESIGN — Lane 3 writes NO gating field (gap analysis §4.2 L3-1: a DET compare
-- that "reads pointer, flags mismatch — writes NO gating field"). The verdict for
-- each AI statement lands in this NEW, append-only side table keyed to the
-- statement; the `statements` / `evidence_links` rows are NEVER mutated by Lane 3.
-- Because the claim row is untouched, an AI claim stays
-- `machine_extracted_unreviewed` + `not_publishable` by construction — Lane 3 can
-- flag a claim contested but can NEVER promote it (1.09 step 11 / G2: only a human
-- promotes). This is the same single AI->public path 1.09 §1.2 / 1.11 §0 commit
-- to, implemented without weakening it.
--
-- Additive + idempotent: db.py's ledger skips an already-applied file, and the
-- new-table/index statements use CREATE ... IF NOT EXISTS, so a bare re-run is a
-- no-op. No table rebuild here (unlike 0009): Lane 3 adds storage, it does not
-- widen any existing CHECK.
--
-- NO NEW STATUS VOCABULARY (gap analysis §6, "0009/0010 introduce no new
-- verification_status / ui_status / publication enum"). The Lane-3 `verdict` /
-- `uncertainty_flag` are a distinct *flag* vocabulary — deliberately NOT the
-- record-of-truth 6-value verificationStatus. The verdict never feeds
-- compute_ui_status; promotion stays a human action on `statements`.
--
-- SCOPE: Alpine-only, local/vault-only. This table, its `source_excerpt`, and its
-- `detail` are reviewer/vault-only evidence and are deliberately NOT on
-- publication.WEB_SAFE_FIELD_ALLOWLIST (1.11 §2.1; AI_GATEWAY §7.1). Only summary
-- counts belong in a Paperclip comment. A column-name guard test asserts no
-- ai_verification_results column can ever be web-projected.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- ai_verification_results — one row per (Lane-3 run, AI statement) verdict.
-- Records WHAT was compared (statement + the evidence_link pointer used), the
-- deterministic verdict + score, the reviewer-facing uncertainty flag, and a
-- vault-only excerpt/detail of the compared source. `run_id` joins the existing
-- gateway-run ledger (ai_extraction_runs with lane='3_verification'); the Lane-3
-- run carries the input set / tool version / errors / reviewer state / retry
-- required by AI_GATEWAY §17 — this table carries the per-statement findings.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_verification_results (
    result_id          TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES ai_extraction_runs(run_id),
    statement_id       TEXT NOT NULL REFERENCES statements(statement_id),
    -- which pointer was compared (nullable: a segment-anchored claim may compare
    -- against transcript_segments directly with no evidence_link selected).
    evidence_link_id   TEXT REFERENCES evidence_links(evidence_link_id),
    -- the Lane-3 verdict: a FLAG, never a promotion. 'source_match' means the AI
    -- draft is grounded in the source at its pointer (ready for HUMAN review, not
    -- published); the other three are contested/needs-review states.
    verdict            TEXT NOT NULL
        CHECK (verdict IN ('source_match', 'source_mismatch', 'unverifiable', 'uncertain')),
    -- the deterministic method id + its score, for reproducibility/audit.
    match_method       TEXT,
    match_score        REAL,
    -- reviewer-facing confidence the verdict is right; 'high' uncertainty = trust
    -- the reviewer, not the machine. Distinct from the claim's own `confidence`.
    uncertainty_flag   TEXT NOT NULL DEFAULT 'high'
        CHECK (uncertainty_flag IN ('high', 'medium', 'low')),
    -- contested = 1 unless verdict='source_match'. A convenience flag for the
    -- reviewer queue; the claim itself stays not_publishable regardless.
    contested          INTEGER NOT NULL DEFAULT 1 CHECK (contested IN (0, 1)),
    -- vault-only provenance of the compare (never web-projected).
    source_excerpt     TEXT,
    detail             TEXT,
    compared_utc       TEXT,
    created_utc        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_verif_statement ON ai_verification_results(statement_id);
CREATE INDEX IF NOT EXISTS idx_ai_verif_run ON ai_verification_results(run_id);
CREATE INDEX IF NOT EXISTS idx_ai_verif_verdict ON ai_verification_results(verdict);
CREATE INDEX IF NOT EXISTS idx_ai_verif_contested ON ai_verification_results(contested);
