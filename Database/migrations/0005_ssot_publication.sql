-- Stage 1 Slice 1, Issue D (GOV-76): SSOT fields + uiStatus publication allowlist.
-- Contract 1.05 (single-source-of-truth + publication control), aligned to
-- 1.06 / 1.11 / 1.12. Source: GOV-71 §2.D, GOV-72 gap analysis §3.4 / §5.1.
--
-- Additive + idempotent (db.py guards each ADD COLUMN with PRAGMA table_info).
--
-- Publication-control posture: NO record defaults to publishable. Every column
-- below defaults to a not-publishable / unreviewed state; a record becomes
-- publishable only via an explicit reviewed transition that flips
-- publication_state. The fail-closed allowlist + compute_ui_status() live in
-- scripts/publication.py (the data-layer enforcement, not just UI).
--
-- ENUM-OF-RECORD NOTE (GOV-72 §5.1 / Issue D D-1): the authoritative 6-value
-- `verificationStatus` enum is NOT a schema-of-record change here. The existing
-- `sources.verification_status` column legitimately holds the 1.02 *registry*
-- 11-value vocabulary; the 11->6 reconciliation is a MAPPING concern
-- (scripts/publication.py VERIFICATION_STATUS_MAP, parity-tested). This
-- migration therefore does NOT re-type or CHECK-constrain verification_status
-- (doing so would reject valid registry values or silently drop the
-- changed_needs_review -> sourceChanged signal). CHECK constraints below apply
-- only to the NEW publication-control columns.

PRAGMA foreign_keys = ON;

-- producedBy (1.05-b): automation | ai | human. Deterministic tooling owns the
-- evidence path; AI output is draft-only and must be labeled, never primary
-- evidence. Default 'automation' (seed/crawler rows are tool-produced).
ALTER TABLE sources ADD COLUMN produced_by TEXT NOT NULL DEFAULT 'automation'
    CHECK (produced_by IN ('automation', 'ai', 'human'));

-- reviewState (1.05-d): default unreviewed. Excluded from the web-safe
-- allowlist (reviewer-state, never published).
ALTER TABLE sources ADD COLUMN review_state TEXT NOT NULL DEFAULT 'unreviewed';

-- publicationState (1.05-e): the data-layer publish gate. Default
-- not_publishable; flipped to publishable only by an explicit reviewed
-- transition AND only when compute_ui_status() is on the publication allowlist.
ALTER TABLE sources ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'not_publishable'
    CHECK (publication_state IN ('not_publishable', 'publishable'));

-- sourceChanged signal (GOV-36/37 split): "source changed / needs review" was
-- moved out of the verificationStatus enum into this boolean. Mapped from the
-- registry changed_needs_review value (VERIFICATION_STATUS_MAP). 0 = unchanged.
ALTER TABLE sources ADD COLUMN source_changed INTEGER NOT NULL DEFAULT 0
    CHECK (source_changed IN (0, 1));

-- uiStatus (1.05-e/f): cached output of uiStatus-map.v1. Nullable; NULL = not
-- yet computed and is treated as non-publishable (fail-closed). The publish
-- allowlist is enforced in scripts/publication.py, not by a DB CHECK, because
-- the value space is versioned (uiStatus-map.v1).
ALTER TABLE sources ADD COLUMN ui_status TEXT;
