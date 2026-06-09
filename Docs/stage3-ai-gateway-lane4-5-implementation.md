# Stage 1 Impl — Slice 3 D: Lane 4 Risk Layer + Lane 5 Runtime Reviewer-Gate (GOV-91)

Issue: GOV-91 · Owner: BackendCrawlerEngineer (`f26f530c`) · **SecurityPrivacyAgent mandatory reviewer**; VerificationSafetyReviewer + CTO consult
Stage: Stage 1 implementation — Slice 3 D (Lanes 4+5). Alpine-only, local/vault-only.
Blocked by: GOV-90 (Slice 3 C Lane-3 verification) — **resolved/merged** (PR #21).
Implements: `Docs/stage3-ai-gateway-gap-analysis.md` §4.3 (Lane 4 L4-5) + §4.4 (Lane 5 L5-1/L5-5)
against 1.09 (automation-vs-AI boundary, step 11 / G2), 1.11 (publication/privacy/legal/
moderation gates §1/§2/§4/§5/§6.5), `AI_GATEWAY_PROCESSING_WORKFLOW.md` lanes 4
("identify privacy/legal/publication/moderation risks and no-go conditions") and 5
("approve, correct, dispute, hold, or reject output before beta/public presentation").

## What shipped

| Artifact | Role |
|---|---|
| `Database/migrations/0011_ai_risk_flags_reviewer_decisions.sql` | Two new append-only **side tables**: `ai_risk_flags` (Lane-4 findings) + `reviewer_decisions` (Lane-5 audit). Additive + idempotent; **no table rebuild**, no CHECK widened, no column added to any existing table. |
| `scripts/ai_risk_gate.py` | Lane 4: deterministic privacy/legal/publication/moderation screen → flags **beside** the claim (never on it), run logged as `lane='4_risk'`. Lane 5: `promote_statement` — the **only** sanctioned path that moves a claim to a reviewed status; fail-closed on missing reviewer / failed run / open no-go flag; never flips `publication_state`. |
| `scripts/slice3d_smoke.py` | End-to-end offline Lane-2 → Lane-4 → Lane-5 smoke over the sanitized Alpine fixture. |
| `tests/test_ai_risk_gate.py`, `tests/test_slice3d_integration_smoke.py` | Unit + integration coverage for the Lane-4/5 invariants. |
| `.github/workflows/local-runner-smoke.yml` | New CI step runs `slice3d_smoke.py`. |

## The load-bearing design choice — the gate is a runtime check, not just a rule

The gap analysis left two GAPs for this slice: **L4-5** (a risk run that flags
no-go and blocks downstream) and **L5-1** (the reviewer-action tooling — "only a
human promotes"). Before GOV-91, no code path promoted a claim at all; the rule
existed but nothing enforced it at runtime. GOV-91 makes the human G2 gate a
**runtime** check:

- **Lane 4** screens each AI claim and records findings in `ai_risk_flags` — a
  side table, exactly like Lane 3's verdict table. It **writes no gating field**;
  the `statements` rows are byte-identical pre/post (`test_lane4_writes_no_gating_field`).
  A `no_go` finding sets `blocks_downstream=1`; a `review` finding (e.g. the
  publication "this AI row isn't reviewed yet" signal every AI row gets) routes to
  a human but does **not** hard-block, so the reviewer-gate can still act on it.
- **Lane 5** `promote_statement` is the single sanctioned promotion path. It
  records a `reviewer_decisions` audit row (who / what / from→to / why — the
  1.11 §6.5 auditable hook, L5-5) **before** it touches the claim, and it is
  fail-closed at four gates (below). It **never** flips `publication_state` to
  `publishable` — that stays the separate owner decision (1.11 P8), so *nothing
  AI-written is publishable by default even after promotion.*

## The four fail-closed gates in `promote_statement`

| Gate | Refuses when | Acceptance test |
|---|---|---|
| 1 — reviewer decision exists | `reviewer_id` is empty or a known automation/AI actor (`ai`/`automation`/`gateway`/`system`/…) | `test_promote_without_reviewer_is_rejected` |
| 2 — promotion names a reviewed target | a promoting decision's `to_verification_status` ∉ `{reviewed_source_linked, human_verified}` | `test_promote_requires_reviewed_target` |
| 3 — producing run healthy | the claim's `ai_extraction_run_id` run is not `error_status='ok'` (failed gateway run blocks downstream) | `test_failed_gateway_run_blocks_promotion` |
| 4 — no open no-go risk flag | an unresolved `blocks_downstream` Lane-4 flag remains (a reviewer must `resolve_flag` it first) | `test_open_risk_flag_blocks_promotion_until_resolved` |

On refusal it raises `ReviewerGateError` having written nothing. The Lane-5 action
set is `approve / correct / dispute / hold / reject`; `approve`+`correct` promote
toward a reviewed status, `dispute`→`disputed`, `reject`→`do_not_publish`, `hold`
records the decision without changing the verification status.

## The Lane-4 deterministic screen

`scan_text()` rule-matches the AI draft for three content families and
`scan_statement()` adds a state-derived publication signal:

| Category | Detects (1.11) | Severity |
|---|---|---|
| `privacy` | phone / email / SSN-shaped / street address / voter-roll language (§2.1 never-publish) | `no_go` |
| `legal` | accusation / legal conclusion / motive / campaign framing about a named individual (§4.1) | `no_go` |
| `moderation` | rumor / brigading / unsourced-validation phrasing (RISK_ASSESSMENT cat 5) | `no_go` |
| `publication` | an AI row not yet at a reviewed `verification_status` — "not ready" (§1/§5) | `review` |

Lane 4 is deterministic, so the run records `model_name = NULL` + a `tool_version`.
The content screen is a *belt* over the real privacy defence (privacy-by-schema-
absence + `to_web_safe()`'s allowlist): it catches PII/accusation/rumor that leaked
into free text before a reviewer can promote it.

## Invariants enforced (GOV-91 acceptance + Slice-3 AI gates)

1. **Promoting an AI row without a reviewer decision is rejected** (gate 1; test).
2. **A failed gateway run blocks downstream** (gate 3; test).
3. **1.11 risk flags recorded** — `ai_risk_flags` carries privacy/legal/moderation
   no-go + publication review flags per AI statement.
4. **No gating write** — the `statements` digest is byte-identical pre/post Lane 4.
5. **AI entry posture preserved** — every AI row stays `produced_by=ai` +
   `machine_extracted_unreviewed` + `not_publishable` (done-bar 7); no-orphan
   inherited from Lane 2 (done-bar 8); the gate binds no speaker name (done-bar 9).
6. **Gateway run-log** — the Lane-4 run is on `ai_extraction_runs` with
   `lane='4_risk'` + input set / tool version / errors / reviewer state / timing.
7. **Fail-closed downstream** — `statement_publication_blocked()` returns True
   unless reviewed **and** a promoting decision exists **and** no open no-go flag
   **and** the producing run is ok **and** ui_status is publication-eligible **and**
   `publication_state='publishable'` (the last clause is the owner gate that
   `promote_statement` never sets — so AI rows stay blocked by construction).
8. **Data-publication boundary** — both side tables, their `matched_signal` /
   `detail` / `reason`, are vault-only; a column-name guard test asserts none can
   reach `publication.WEB_SAFE_FIELD_ALLOWLIST`.

## What this slice does NOT do (correctly out of scope)

- It does **not** flip `publication_state` to `publishable` or authorize any
  publication — that is the owner (CEO/Isaac) decision (1.11 P8).
- It does **not** build the reviewer-queue UI or the gated-beta tier surface —
  `open_risk_flags()` / `latest_decision()` / `statement_publication_blocked()` are
  the backend reads a future frontend/gated-beta slice will consume.
- It does **not** add a real human-auth layer; `reviewer_id` is recorded and
  automation/AI sentinels are refused, but binding a decision to an authenticated
  human identity is the Stage 1.12 traceability / gated-beta account slice.
- No live model, no network: the Lane-2 proposer and the Lane-4 screen are both
  offline/deterministic for reproducible CI.
