# PILOT-2026 — Pilot Protocol & Measurement Contract

## 0. Document control

- **Package ID:** PILOT-2026 · **Version:** v1.0 · **Date:** 2026-07-16 · **Author:** CTO
- **Issue:** [GOV-780](/GOV/issues/GOV-780) (leg 1/7 of the [GOV-723](/GOV/issues/GOV-723) chain). **Parent plan:** GOV-723 plan revision `1784e156-4387-4e51-a244-59c6b8f422d2` (owner-accepted; this doc implements plan §4, §5, §7 and does not re-litigate plan §3 boundaries).
- **Normative basis:** `Docs/2026-commercial-platform-requirements-v1.0.md` (REQ-2026-COMM v1.0, FROZEN). Requirement IDs (INV-*, AREA-*, COHORT-*, SLO-*, BUD-*, NOTIF-*, LED-*, AM-*, GATE-*) are cited, never restated. Conflicts resolve in REQ's favor.
- **Consumers:** leg 2 (BackendCrawlerEngineer — harness implementation), leg 3 (CTO merge gate), leg 4 (AutomationOpsEngineer — Wave-0 run), leg 5 (cohort ladder), leg 6 (VSR audit), leg 7 (CEO decision-pack assembly).
- **Change rule:** any normative change bumps the version and is recorded on the GOV-723 chain; changes that touch an owner decision (§6) additionally need a fresh owner card.
- **Basis vocabulary (binding, from `scripts/economics/basis.py`):** `MEASURED | ASSUMED | DERIVED | OWNER-SET | NOT_INSTRUMENTED`. In this protocol, **"observed"** means a MEASURED value produced inside a declared pilot measurement window by the live stack; **"assumed"** covers ASSUMED and any DERIVED value whose worst input is ASSUMED (taint rule already enforced by `scripts/economics/formulas.py`). Seed-synthetic values (Wave 0) are MEASURED per LED-F6 but must carry the seed and be presented in a column explicitly headed *synthetic baseline*, never mixed into an *observed* column.

### 0.1 Standing boundaries (inherited, binding — plan §3)

1. GOV-420 hold (Option 3): local-only. No hosting/DNS/domain/public exposure work anywhere in this chain (INV-4).
2. Credit gates (GOV-631): deterministic-first; default operation = local Ollama, zero credits. Paid-provider spend only via an owner card with an explicit cap (BUD-5).
3. Everything reviewer-internal / `not_publishable`; registry + raw data local; only code + tests + sanitized aggregate metrics to GitHub (INV-7).
4. Alpine + approved beta-tester areas only; county/state pack rows are labeled rollup projections, never new-area ingest (INV-5).
5. AI output is never primary evidence (INV-3); frozen serving surfaces `read_api.reviewer_internal_records`, `ai_risk_gate`, `stage5_agenda_board` byte-0.

---

## 1. Wave-0 synthetic workload definition (plan §4)

**Purpose.** Prove that every §2 metric actually lands in the named table with correct area attribution, and produce the *synthetic baseline* column of the decision pack. No users, no owner gate, zero credits.

**Path under test (the real stack, not mocks):** `mcp_jobs` + `mcp_capability_grants` (scoped token) → typed MCP tools/resources (`scripts/mcp_service/`) → `routing.route_and_generate()` on the Ollama adapter (or the deterministic fake adapter for CI) → audit envelope (`mcp_audit_events`) → queue lanes (`event_jobs` via `scripts/job_queue.py` / `job_worker.py`) → economics pack (`scripts/economics/report.py`).

### 1.1 Typed workload jobs

Each workload type is scripted, bounded, and deterministic (fixed input selectors; fixed seed where synthesis is involved). Default counts are ASSUMED defaults, overridable by CLI flag; the driver hard-caps total calls per run.

| ID | Job | Stack path | What it proves | Default bound |
|---|---|---|---|---|
| WL-1 | Typed resource reads | MCP resource read, no provider call | read-path audit envelope; `latency_ms`; SLO-3 input; redaction choke-point on every response | 50 reads |
| WL-2 | Lens analysis jobs, one per shipped lens pack (3) | MCP job → routing policy → Ollama (`--provider ollama`) or fake adapter (CI) | provider half of LED-1 (`provider`, `model`, `input_units`, `output_units`, `direct_cost_units`); `lens_version` on every output; INV-1 byte-0 regression | 25 jobs/lens |
| WL-3 | Queue-lane jobs | `event_jobs` enqueue → `job_worker.py` lease/dispatch → terminal state | `queue_wait_s`, `cpu_s`, `enqueued_at/started_at/finished_at`, retry/dead-letter accounting, `job_transitions` audit | 50 jobs |
| WL-4 | Safety probes (deliberate failures) | (a) out-of-scope tool call; (b) redaction tripwire payload; (c) synthetic budget breach; (d) grant revocation mid-run | (a) `outcome='deny'` audit; (b) `error_code='denied:redaction'` fail-closed; (c) BUD-2/AM-4 pause + budget event + outbox row; (d) revoked grant denies (AM-9 family) | ≥ 1 each, ≤ 5 each |
| WL-5 | Notification synthetic | consent record → `scripts/notifications/service.py` → `email_outbox` on the **null adapter only** → `email_delivery_log` | NOTIF-1 hard-fail without consent (AM-5), delivery-outcome recording, SLO-6 input. No external email in Wave 0 (external email to any non-owner user needs its own owner card per GOV-721 gates) | 10 sends + 2 no-consent probes |
| WL-6 | Shared-pool attribution probes | jobs submitted with `area_id = NULL` | AREA-2: unattributable work lands in the disclosed shared pool, never smeared (`shared_pool_extras` in `scripts/economics/ledger.py`) | 5 jobs |

All other jobs carry `area_id` = the Alpine town row of the `areas` spine (0024). WL-2 respects D7 local-only routing; the paid-provider registry stays at cap 0 (BUD-5) and the run **asserts** afterward that no audit row names a non-local provider.

### 1.2 Zero-credit assertion (exit check of every Wave-0 run)

After the run, the harness asserts: (a) every `mcp_audit_events.provider` ∈ {local Ollama id, fake adapter id, NULL}; (b) no `mcp_budget_events` row of kind `breach` exists except the deliberate WL-4(c) probe, and that probe shows `paused_at` set (AM-4); (c) `scripts/credit_metering.py` trend summary reports zero paid-provider estimated cost. Violation = RED, run invalid, CTO issue filed.

### 1.3 Run contract (leg 4)

Dry-run is the default; `--apply` only after CTO review of the latest dry-run log (GOV-631 dry-run gate). Logs to `Logs/pilot/` (gitignored, local-only). Each run emits: run manifest (seed, bounds, git sha, db path), metric snapshot (§2), and pack artifacts (§4). Failure handling: any WL type that cannot complete its bound logs the failure and files/updates one Paperclip issue via the outbox dedupe pattern — no silent partial baselines.

---

## 2. Measurement contract — metric → table mapping (plan §5)

Implements the plan §5 table against the real schema. Every reported number carries a basis label (LED-5/AM-7; lint via `scripts/economics/basis.py::lint_report`). "Observed" columns may only contain MEASURED values captured inside the declared measurement window.

### 2.1 Cost

| Component | Source | Basis |
|---|---|---|
| Variable per-job cost (LED-F1) | `mcp_audit_events.direct_cost_units` (+ `input_units`, `output_units`) per (`job_id`, `area_id`, period) via `scripts/economics/ledger.py::job_cost_units` / `lane_rollup` / `provider_rollup` | MEASURED |
| Compute half of LED-1 | `event_jobs.cpu_s`, `event_jobs.queue_wait_s` (0021) | MEASURED |
| AI-lane trend meter | `scripts/credit_metering.py` over `ai_extraction_runs.tokens_input/tokens_output/estimated_cost_usd` (0019) — trend metering only, never billing truth | MEASURED (tokens) / ASSUMED (est. cost) |
| Fixed-cost allocation (LED-F2/F7) | `ledger_fixed_costs` (0024) via `scripts/economics/fixed_cost.py::allocate`; weight default = document share | OWNER-SET total, ASSUMED weight — the most prominent assumption in every pack (LED-4) |
| Reviewer effort (LED-2) | `ledger_reviewer_work` (0024) via `scripts/economics/reviewer_cost.py::reviewer_work` (minutes MEASURED, else decision-count proxy DERIVED) | MEASURED / DERIVED |
| Support cost | §2.5 support log × `support_per_minute_units` (OWNER-SET constant, default 1 unit/min ASSUMED) | DERIVED |
| Per-user / per-document (LED-F4/F5) | `scripts/economics/report.py::build_pack(active_users=…)`; `active_users` = count of `access_grants` rows at `tier='approved'` for the cohort in the window (0025) | DERIVED |

### 2.2 Quality

- Validation pass/fail per MCP call: `mcp_audit_events.outcome` (`allow`/`deny`) + `error_code` (schema/scope/redaction denials are distinguishable by code).
- Reviewer outcome: `ledger_reviewer_work.correction_rate` / `rejection_rate` / `source_coverage_rate` (COHORT-3 quality set); per-job scalar `event_jobs.quality_outcome` / `reviewer_outcome` where a lane records them.
- Wave 0 has no human review: reviewer-quality cells in the synthetic-baseline column are `NOT_INSTRUMENTED`, never fabricated.

### 2.3 Latency

- Per-call: `mcp_audit_events.latency_ms`; p95 per SLO-3 via `scripts/economics/ledger.py::_read_latency_p95` (already wired into `slo_metrics`).
- Queue time: `event_jobs.queue_wait_s` and the `enqueued_at → started_at → finished_at` timestamps.
- Provider-side: `mcp_provider_health.latency_ms` (0023).
- All six SLO-1…6 fields emit per-area into the pack (SLO-7); targets remain ASSUMED until this pilot replaces them with MEASURED values (REQ §4).

### 2.4 Safety

- Risk-gate triggers: lane-4 risk rows written by `scripts/ai_risk_gate.py::run_risk` into `ai_extraction_runs` (lane `4_risk`), counted per window; unresolved `blocks_downstream` flags reported.
- Redaction events: `mcp_audit_events` rows with `error_code='denied:redaction'` (the `scripts/mcp_service/redaction.py` choke-point importing the frozen scanners).
- Revocation drill (once per cohort step, plan §4): flip `access_grants.tier → 'revoked'` (with `owner_decision_ref`), then verify COHORT-5/AM-2 — the revoked account receives **zero** civic rows within one access-check interval; Wave 0 exercises the MCP analogue via `mcp_capability_grants.revoked` → deny audit.
- No-leak checks: `export.py::assert_no_prices` on every export; redaction scanners clean over every snapshot/pack artifact destined for the issue thread.

### 2.5 Support (the one new measurement surface)

No existing table covers support demand. Protocol: a **structured per-cohort support log**, append-only JSONL at `Logs/pilot/support/<cohort_step>.jsonl` (local-only, gitignored), schema:

```json
{"ts": "...", "cohort_step": "C1", "user_ref": "<pseudonymous>", "channel": "...",
 "category": "...", "minutes_spent": 0, "resolution": "...", "owner_minutes": 0}
```

`user_ref` is pseudonymous — never a name/email (privacy rule). Counted into per-user cost per §2.1. Aggregates (ticket count, total minutes, owner minutes — COHORT-3 support set) enter the pack as MEASURED; the per-minute unit constant is OWNER-SET/ASSUMED.
**Migration 0026 rule:** leg 2 creates a `pilot_support_log` table (slot **0026**, additive-only) **only if** the pack's reproducibility hash (`ledger_report_runs.content_sha256`) needs support rows as deterministic content-hashed inputs, or transactional joins to `users`/`cohort_state` prove necessary. Otherwise no migration ships. This is the only candidate for 0026 in this chain.

### 2.6 Notification

- Consented sends: `email_outbox` rows joined to `consent_preferences` (NOTIF-1: no consent record → hard-fail, AM-5) plus in-app `notification_events` (0025).
- Delivery outcomes: `email_delivery_log.event_kind` ∈ {sent, delivered, bounced, complaint, unsubscribed, suppressed, failed} → NOTIF-5 bounce/complaint/unsubscribe rates and SLO-6 outcome rate.
- Opt-outs: `consent_preferences` updates + `unsubscribed` delivery events + `unsubscribe_confirmed` notification events.
- **Disclosure (carried, not fixed here):** FE↔BE notification wiring gap per [GOV-771](/GOV/issues/GOV-771); metrics are captured at the backend service layer; the HTTP endpoint stays inert (feature-flag fail-closed) until owner-gated enablement. Every pack states this disclosure verbatim.

### 2.7 Capacity

- Observed job rates: computed from `mcp_audit_events.created_at` and `event_jobs` timestamps within the measurement window (jobs/min sustained and peak).
- Fed through LED-F6 (`scripts/economics/formulas.py::f6_capacity_headroom`). `scripts/economics/capacity.py::forecast` is today seed-synthetic by design; leg 2 adds an **additive, optional observed-rates override** (new keyword/param or a `scripts/pilot/` wrapper computing rates and calling `f6_capacity_headroom` directly — implementer's choice, both additive, existing seed path untouched so `test_economics_capacity.py` determinism holds).
- Pack shows both: *synthetic baseline* (seeded, labeled with seed) and *observed* (window-labeled). They never share a column.

---

## 3. Cohort card templates (plan §4 ladder; steps gated per COHORT-4, GATE-B1…B3)

Each step is its own fresh Isaac board card fired from the GOV-723 chain (leg 5). **A card is invalid if any field below is missing.** Card key follows the COHORT-4 idempotency pattern (`confirmation:…:cohort-step:<from>-<to>:v1`). Never auto-advanced.

### 3.1 Template — common card body (all steps)

```markdown
## Cohort step <C0→C1 | C1→C2 | C2→C3> — owner decision card

1. **Users (named):** <full list — 2 / 3 / ≤15 people; each with home area;
   any non-Alpine home area becomes `free_beta` only via an AREA-5 audit row
   tied to THIS card (COHORT-6)>
2. **Access path under the GOV-420 local-only hold (DECIDE, never assumed):**
   <owner picks: supervised local session | local-network access |
   an explicit hold-modifying instruction naming GOV-420>.
   No hosting/DNS/public exposure is built regardless of choice.
3. **Consent script:** <exact text shown to each user: what is collected
   (usage metrics, support log, notification consent NOTIF-1/NOTIF-2),
   what is never collected/published, revocation path, RET-3 deletion terms>
4. **Spend cap:** default **$0 / local Ollama only** (BUD-5). Any non-zero
   paid-provider cap must be stated here as an explicit number + window,
   and lands as an `mcp_budgets` row with `basis='OWNER-SET'`.
5. **Measurement window:** ≥ 7 days OR a defined job quota of ≥ <N> jobs,
   whichever is stated here. (Advancing to the NEXT step additionally needs
   the COHORT-2 soak: default 14 days, ASSUMED, owner may change on card.)
6. **Prior-step metrics attached:** link to the previous step's snapshot +
   pack artifact and its `ledger_report_runs.content_sha256`
   (C1 card attaches the Wave-0 baseline). No metrics → no card.
7. **Safety commitments:** one revocation drill during the step (§2.4);
   zero unresolved critical safety/privacy incidents to advance (COHORT-2);
   support log active from day one (§2.5).
```

### 3.2 Step-specific rows

| Step | Size cap (enforced in code, COHORT-1/AM-10) | Extra requirements |
|---|---|---|
| C1 — 2 users | 2 | first real-user access-path decision (GOV-420 question is asked HERE); consent flow exercised end-to-end before any civic read |
| C2 — 3 users | 3 | C1 metrics attached; any C1 incident disposition recorded |
| C3 — ≤ 15 users | 15 | C2 metrics attached; owner may split C3 into sub-batches, each with its own card |

---

## 4. Decision-pack template (plan §1; REQ §10–11 GOV-723 card contract)

One pack per period, assembled by leg 7 from `scripts/economics/report.py::build_pack` / `build_rollup` + the pilot extensions, exported via `scripts/economics/export.py` (LED-6 rows: `{field, unit, value, basis, formula_id, area_id, period}` — **no prices, ever**; `assert_no_prices` is the RED guard).

### 4.1 Rows

| Row | Source | Labeling |
|---|---|---|
| Alpine (town) | observed pilot data | MEASURED columns + declared assumptions |
| Lincoln County | `build_rollup(scope='county_rollup')` — pure aggregation of observed town rows + labeled projection factors | **PROJECTION** header; every projected cell DERIVED/ASSUMED |
| Wyoming (state) | `build_rollup(scope='state_rollup')` — same rule | **PROJECTION** header |

County/state rows are rollup projections from observed Alpine data only — never new-area ingest (INV-5).

### 4.2 Columns (per row)

1. **Cost:** LED-F1 variable, LED-F2 allocated fixed (weight disclosed per LED-F7), LED-F3 total, LED-F4 per-active-user, LED-F5 per-document — with §2.1 sources.
2. **Capacity:** LED-F6 headroom — synthetic-baseline and observed sub-columns (§2.7).
3. **Quality / Latency / Safety / Support / Notification:** the §2.2–§2.6 aggregates, incl. full COHORT-3 + SLO set.
4. **Funding:** `area_funding_entries` balance vs measured cost → F-ELIG evaluation via `scripts/economics/eligibility.py` (RECOMMEND-ONLY — it never writes `area_state`).
5. **Activation conditions** (stated, never executed by this chain):
   - **free** (`free_home`/`free_beta`): standing Alpine + owner-approved beta areas; condition = COHORT/GATE-B evidence healthy.
   - **donated** (= `funded` state, AREA-4): F-ELIG true on MEASURED inputs (AREA-6) → GATE-F owner card.
   - **paid**: entitlement design (0024 `area_entitlements`) + measured cost basis → GATE-P owner card; pricing itself comes from the business-plan lane, never this pack.
   - **locked** (default) / **limited**: stated fallback conditions (budget/capacity rule triggers).
6. **Assumptions register:** every ASSUMED/OWNER-SET input in the pack, with its replacement path (mirrors REQ §13).
7. **Disclosures:** GOV-771 FE notification gap; small-N validity (2–15 users ⇒ ranges, not point estimates; extrapolations labeled ASSUMED — plan §9); Ollama-only cost realism (paid-provider scenarios priced as ASSUMED from measured token volumes unless a card authorized a paid batch).

### 4.3 Reproducibility

Every pack: `record_run` → `ledger_report_runs.content_sha256`; `verify_hash` must pass on re-generation. RED (REQ §11 GOV-723 card): an ASSUMED value presented as measured; any activation performed by this chain.

---

## 5. Harness design — `scripts/pilot/` (contract for leg 2; docs only here)

### 5.1 Modules

| Module | Responsibility | Reuses |
|---|---|---|
| `scripts/pilot/__init__.py` | package marker, shared constants (WL bounds, seed default) | — |
| `scripts/pilot/workload.py` | WL-1…WL-6 typed job definitions + driver; dry-run default, `--apply` for live writes; per-type bounds; deterministic input selectors | `scripts/mcp_service/` service layer, `scripts/job_queue.py`, `scripts/notifications/service.py` |
| `scripts/pilot/snapshot.py` | metric snapshot extractor: reads exactly the §2 sources for (`area_id`, window) → one basis-labeled dict; any unavailable metric emits `NOT_INSTRUMENTED`, never a fabricated value; deterministic key order | `scripts/economics/ledger.py`, `reviewer_cost.py`, `fixed_cost.py`, `scripts/credit_metering.py` |
| `scripts/pilot/pack.py` | decision-pack builder: §4 template; adds pilot columns (safety/support/notification/observed-capacity) around the economics pack; content-hash + verify | `scripts/economics/report.py`, `export.py`, `eligibility.py`, `basis.py` |
| `scripts/pilot_run.py` | thin CLI: `--db`, `--area`, `--period`, `--seed`, `--out`, `--provider {fake|ollama}`, `--apply`; subcommands `workload | snapshot | pack` | all of the above |

### 5.2 Constraints (RED conditions for leg 3 merge gate)

- **Additive-only.** New files under `scripts/pilot/` + `tests/`; byte-0 diff on frozen serving modules (`scripts/read_api.py`, `scripts/ai_risk_gate.py`, `scripts/stage5_agenda_board.py`) — importing them is fine, editing them is RED.
- The **only** permitted touch outside `scripts/pilot/` + tests is the additive optional observed-rates path in `scripts/economics/capacity.py` (§2.7) — and only if the wrapper approach is not chosen.
- **Migration 0026** only under the §2.5 rule; otherwise no schema change. Slot 0026 is reserved for `pilot_support_log` and nothing else in this chain.
- No network beyond localhost Ollama; no paid-provider calls (registry caps stay 0).
- Logs/artifacts under `Logs/pilot/` (gitignored). Raw snapshots stay local; only sanitized aggregate metrics (redaction-clean, no PII, no prices, units only) may be posted to issue threads or committed as test fixtures (INV-7).
- Tests green on **python3.12**; dry-run default on every CLI (GOV-631).

### 5.3 Required test set (leg 2)

1. Workload dry-run determinism: same seed + bounds ⇒ identical planned-job manifest.
2. Snapshot completeness: every §2 metric present or explicitly `NOT_INSTRUMENTED`; basis lint (`lint_report`) passes — a missing basis label is a test failure (AM-7).
3. RED-proofs (reusing merged patterns): send-without-consent hard-fails (AM-5); revoked account gets zero civic rows (AM-2); synthetic budget breach pauses + emits event (AM-4); redaction tripwire denies (AM-9 family); `assert_no_prices` on every export.
4. Zero-credit assertion (§1.2) as a test over a fake-adapter run.
5. Pack reproducibility: `content_sha256` stable across two builds on the same substrate; `verify_hash` passes.
6. Byte-0 guard: hash check on the three frozen serving modules before/after the full suite.

### 5.4 Failure handling / logs (WORKFLOW_GOVERNANCE)

Log root `Logs/pilot/` (runs, support, artifacts). Every failed run writes a structured failure record; repeated failure (≥2 consecutive) files one deduped Paperclip issue (outbox umbrella pattern). CTO reviews Wave-0 dry-run logs before `--apply` (GOV-631 gate); leg 6 (VSR) audits artifacts against this contract.

---

## 6. Decisions reserved for owner cards (plan §7 — listed, NOT made here)

1. **Access path for real users** under the GOV-420 local-only hold — asked on the C1 card (§3.1 item 2), never assumed.
2. **Who the 2 / 3 / ≤15 users are** and their consent handling (§3.1 items 1, 3).
3. **Any paid-provider spend** — default $0/Ollama; a card must set any non-zero cap (§3.1 item 4).
4. **Any free/donated(funded)/paid/locked activation** — the pack states conditions (§4.2.5); activation is Isaac's separate decision via GATE-F / GATE-P / GATE-PUB on [GOV-715](/GOV/issues/GOV-715).
5. **Cohort-step advancement** — every step its own card (COHORT-4); soak-time changes owner-set on the card (COHORT-2).
6. **External email to any non-owner user** (GOV-721 gate) — Wave 0 and defaults use the null adapter.

## 7. Acceptance mapping (GOV-780 → chain)

- Plan §4 (pilot design) → §1 (Wave 0) + §3 (cohort ladder templates) of this doc.
- Plan §5 (metrics contract) → §2 metric→table mapping, basis rules in §0.
- Plan §7 (owner decisions) → §6, decisions listed not made.
- Leg 2 builds §5 exactly; leg 3 gates on §5.2 RED conditions; leg 4 runs §1 under §1.3; leg 5 fires §3 cards; leg 6 audits §2/§4 completeness and labeling; leg 7 assembles §4.

## 8. Change log

- **v1.0 (2026-07-16, CTO, GOV-780):** initial protocol; implements accepted GOV-723 plan rev `1784e156`; docs only, no code.
