# REQ-2026-COMM — 2026 Commercial Platform Requirements, Measurement Contract & Release Gates

## 0. Document control

- **Package ID:** REQ-2026-COMM · **Version:** **v1.0 (FROZEN)** — owner (Isaac) accepted 2026-07-16T02:17:28Z
- **Source:** [GOV-715](/GOV/issues/GOV-715) owner directive; canonical queue reference `/Users/IA/Code/Government-watchdog/.hermes/plans/2026-07-15_190000-commercial-scale-mcp-funding-persona-queue.md`
- **Consumers:** [GOV-717](/GOV/issues/GOV-717)–[GOV-723](/GOV/issues/GOV-723) and the frontend/business-plan lane.
- **Change rule:** any normative change after acceptance requires a version bump and a fresh owner confirmation card. Queue cards cite requirement IDs; they do not copy text.
- **Basis-label rule (global):** every numeric value in this package and in every downstream report carries `basis: MEASURED | ASSUMED | DERIVED | OWNER-SET`. This package contains **no customer pricing and no operating-cost dollar figures**; those come only from GOV-720/GOV-723 measurement and owner decisions.

## 1. Inherited invariants (binding; not renegotiated here)

- **INV-1** Canonical evidence (facts, exact quotes, source links, ordering, verification status) is never altered by any lens, provider, or policy.
- **INV-2** MCP boundary is self-contained, typed, least-privilege, audited. No generic shell/system tool is exposed as an AI capability.
- **INV-3** Model/provider output is never primary evidence and never makes a publication or access decision.
- **INV-4** No public or raw-data exposure until separate named owner gates. The GOV-420 local-only hold and the public-URL/Wayback publication blocker stay binding; nothing in this package lifts them.
- **INV-5** Alpine-first: Alpine is the free home zone; beta testers' areas are free validation zones. This package authorizes **no** ingest/processing scope expansion by itself.
- **INV-6** A political lens is a transparent, versioned analysis policy. Every lens output discloses lens/version, source links, claims-vs-interpretation separation, and uncertainty. No stereotyping, campaigning, persuasion, or claims to speak for all party members.
- **INV-7** Raw data and the registry stay local/vault-only; only code, tests, and sanitized fixtures go to GitHub.

## 2. Area model — unit of accounting and activation

- **AREA-1** The atomic operating unit is an `area`: `{area_id, kind: town|county|state, name, parent_area_id}`. Towns roll up to a county, counties to a state.
- **AREA-2** Every job, cost record, quality record, notification, and access grant carries `area_id`. Unattributable work uses `area_id = NULL` and lands in a disclosed shared-cost pool — never silently smeared across areas.
- **AREA-3** Rollups are pure aggregation. Fixed costs enter per-area figures only via the declared allocation formula **LED-F2/F7**.
- **AREA-4** Area activation state machine (all transitions owner-gated):
  - `locked` (default) — no processing, no serving
  - `free_home` (Alpine, standing) — processed and served free
  - `free_beta` — owner-approved beta tester's area, free during pilots
  - `funded` — served while funding rule **F-ELIG** holds on measured data
  - `paid` — served under a paid entitlement (designed in GOV-720; activation gated by **GATE-P**)
  - `limited` — reduced cadence/scope fallback when a budget or capacity rule triggers
- **AREA-5** Every state transition writes an audit row `{area_id, from, to, owner_decision_ref, rule_evaluated, timestamp}`.
- **AREA-6** **F-ELIG** (funded eligibility) = `monthly_measured_cost(area) <= monthly_funding_balance(area) × safety_factor`. `safety_factor` is OWNER-SET; cost and balance are MEASURED. No dollar thresholds are asserted here.

## 3. Beta cohort ladder (2 → 3 → 15)

- **COHORT-1** Steps: C0 (owner only) → C1 (2 users) → C2 (3 users) → C3 (≤15 users). Caps are enforced in code, not convention.
- **COHORT-2** Advancing a step requires: minimum soak time at the current step (default 14 days, ASSUMED, owner may change), zero unresolved critical safety/privacy incidents, and a measured step report covering COHORT-3.
- **COHORT-3** Step metric set (all MEASURED): cost/user/month by lane; p50/p95 read latency; ingest freshness; quality (reviewer correction rate, source-coverage rate); support demand (tickets + owner minutes); notification consent and delivery outcomes; safety incident count.
- **COHORT-4** Every step transition is its own owner card (`confirmation:GOV-721:cohort-step:<from>-<to>:v1`). Never auto-advanced.
- **COHORT-5** Revocation/pause takes effect within one access-check interval; pending/revoked/paused accounts receive **zero** civic data (testable).
- **COHORT-6** Cohort membership binds user → area(s). A beta user's area becomes `free_beta` only through an AREA-5 audit row plus the owner card.

## 4. SLO contract

SLO **fields** are fixed now; **targets** start ASSUMED and are replaced by MEASURED pilot values in the GOV-723 pack, after which the owner sets binding targets.

| ID | Metric | Definition | Initial target (basis) |
|---|---|---|---|
| SLO-1 | ingest_freshness | source published → preserved + registered | ≤ 72 h (ASSUMED) |
| SLO-2 | processing_latency | preserved → reviewer-ready | ≤ 7 d (ASSUMED) |
| SLO-3 | read_latency_p95 | authenticated area read, server-side | ≤ 500 ms (ASSUMED) |
| SLO-4 | availability | serving surface, monthly | 99% (ASSUMED, local-server class) |
| SLO-5 | review_turnaround | reviewer-ready → reviewed decision | MEASURED only (human-paced; no target until pilot) |
| SLO-6 | notification_outcome_rate | consented sends with a recorded delivery outcome | ≥ 95% (ASSUMED) |

- **SLO-7** Every SLO metric is emitted per-area into the ledger; breaches are logged events that roll into the pilot pack.

## 5. Data retention and data classes

- **RET-1** Classes: (a) raw source snapshots; (b) derived/structured civic records; (c) AI/provider outputs incl. prompts; (d) audit/cost ledger; (e) account + consent records; (f) notification logs; (g) reviewer notes.
- **RET-2** (a),(g): local/vault only, retained indefinitely as the evidence trail, never public. (b): retained indefinitely with correction-not-rewrite semantics. (c): retained with source hash + policy/lens version for reproducibility; purge requires owner approval. (d),(e),(f): retained ≥ pilot duration + 1 year (ASSUMED); owner sets the final policy at GATE-PUB.
- **RET-3** Account deletion: personal data removed or anonymized within 30 days of request (ASSUMED). Civic evidence records contain no beta-user personal data by construction.
- **RET-4** No retention rule may destroy the ability to reproduce a published claim's source trail. Deletion is fail-closed against the evidence chain.

## 6. Provider budget contract

- **BUD-1** Budget object: `{scope: provider|lane|area|global, window: daily|monthly, cap_units, cap_currency?, basis: OWNER-SET}`.
- **BUD-2** Every provider call is metered pre-flight against remaining budget. On breach the lane **pauses (fail-closed)**, emits an event, and creates/updates a Paperclip issue. No silent overruns, no auto-raise.
- **BUD-3** Local models (Ollama) still meter units and latency so per-area cost is comparable across providers.
- **BUD-4** Budget changes are owner decisions with audit refs.
- **BUD-5** Zero-spend default: a newly registered provider has cap 0 and cannot be called until the owner sets a budget.

## 7. Notification and consent contract

- **NOTIF-1** Consent record: `{user_id, channel, purpose, consent_state, timestamp, evidence_ref, unsubscribe_token}`. No consent record → the send hard-fails.
- **NOTIF-2** Email uses double opt-in (ASSUMED standard; owner may relax only by explicit decision).
- **NOTIF-3** Every send records outcome (delivered/bounced/complaint/unsubscribed) and cost into the ledger, per area and user.
- **NOTIF-4** Mass messaging beyond cohort size is structurally impossible below GATE-PUB + GATE-N. Message content obeys INV-6 (no campaigning/persuasion).
- **NOTIF-5** Deliverability/abuse metrics (MEASURED): bounce rate, complaint rate, unsubscribe rate — all in the pilot pack.

## 8. Portability contract

- **PORT-1** Identical evidence, policy, and access semantics in local Compose and the scale topology. Environment differences live only in declared adapters (database, queue, object storage, CDN/edge, observability).
- **PORT-2** A synthetic-data migration drill must show export → restore → verification hashes equal, and access decisions identical pre/post.
- **PORT-3** Civic-domain code may not import provider-specific SDKs directly; the adapter boundary is enforced by a lint/dependency rule.
- **PORT-4** Secrets and private data never leave the private environment during drills; drills use synthetic fixtures only.

## 9. Measurement contract — empirical cost ledger

- **LED-1** Per-job record: `{job_id, area_id, lane, source_hash, cpu_s, storage_bytes_delta, bandwidth_bytes, queue_wait_s, provider?, model?, input_units, output_units, latency_ms, retry_count, cache_hit, direct_cost_units, quality_outcome, reviewer_outcome, policy_version, lens_version?, timestamp}`.
- **LED-2** Reviewer work: `reviewer_minutes` or the declared proxy (decision count × per-decision constant, OWNER-SET), plus correction/rejection rate and source-coverage rate, per batch.
- **LED-3** Notification costs and outcomes per NOTIF-3.
- **LED-4** Monthly fixed infrastructure enters per-area figures only through LED-F2; the weight definition is the single most prominent assumption in every report.
- **LED-5** Basis labels (§0) are mandatory on every reported value; a report row without a basis label fails lint.
- **LED-6** Export interface: CSV/JSON rows of `{field, unit, value, basis, formula_id, area_id, period}` — the exact surface the frontend/business-plan lane consumes. **No prices included.**

**Formulas (defined here, computed by GOV-720; all exportable):**

- **LED-F1** `area_variable_cost(a, m) = Σ job costs where area_id = a in month m`
- **LED-F2** `area_allocated_fixed(a, m) = fixed_total(m) × weight(a, m)`; default `weight` = share of documents processed (ASSUMED; owner may redefine)
- **LED-F3** `area_total_cost = LED-F1 + LED-F2`
- **LED-F4** `cost_per_active_user(a) = LED-F3 / active_users(a)`
- **LED-F5** `cost_per_document(a) = LED-F3 / documents_processed(a)`
- **LED-F6** `capacity_headroom = measured_max_sustainable_throughput − current_load` (from synthetic load tests, MEASURED)
- **LED-F7** the `weight` definition used in LED-F2 (the allocation assumption; must be disclosed on every rollup)

## 10. Release gates (each a separate owner card; none pre-approved by this package)

| Gate | Decision | Preconditions |
|---|---|---|
| GATE-B1/B2/B3 | cohort step 2 → 3 → 15 | COHORT-2 satisfied on measured data |
| GATE-F | funded-area activation | F-ELIG true on MEASURED inputs; GOV-720 ledger live |
| GATE-P | paid-area activation | entitlement design done (GOV-720); measured cost basis; pricing itself comes from the business-plan lane, not this platform |
| GATE-PUB | public launch | all pilot gates passed; GOV-420 hold explicitly lifted by Isaac; publication blockers (public-URL/Wayback) cleared |
| GATE-N | mass-notification enablement | after GATE-PUB; deliverability/abuse metrics healthy |

## 11. Queue-card contract matrix

Each card: **Inputs / Outputs / Owner gates / Metric fields emitted / Failure (RED) conditions.**

### [GOV-717](/GOV/issues/GOV-717) — MCP contracts
- **In:** this package (§1, §2, §8, §9 field defs); existing registry schema; frozen-surface list (`read_api.reviewer_internal_records`, `ai_risk_gate`, `stage5_agenda_board`).
- **Out:** typed resource/tool schemas; capability scopes; audit-ID format; provider-registry interface; versioned policy/lens package schema.
- **Gates:** CTO non-author merge gate only; no owner card.
- **Metrics:** audit events per tool call including the LED-1 cost-envelope subset.
- **RED:** raw path/PII/reviewer note crossing the boundary in tests; any generic shell/system tool exposed as an AI capability.

### [GOV-718](/GOV/issues/GOV-718) — provider routing + multi-lens
- **In:** GOV-717 contracts; §6 budgets; INV-1/3/6; lens policy schema.
- **Out:** policy-driven routing (local Ollama first); lens outputs as separate typed records; fairness/provenance/output-schema/gate-bypass regression suite.
- **Gates:** owner card required before any **paid** provider budget goes above 0 (BUD-5). Local-only operation needs no owner card.
- **Metrics:** LED-1 provider fields per call; `lens_version` on every output.
- **RED:** a lens run mutates any canonical record (byte-diff test); a provider call bypasses the budget check; an output lacks lens/version/uncertainty labels.

### [GOV-719](/GOV/issues/GOV-719) — event control plane
- **In:** §9 job/event fields; dedupe semantics; signed-webhook requirement.
- **Out:** immutable event envelopes; deterministic source/hash dedupe; bounded job state machine; retry/dead-letter; transactional Paperclip outbox with stable dedupe keys.
- **Gates:** CTO merge gate.
- **Metrics:** queue_wait_s, retry_count, dedupe hit rate, dead-letter count — per area.
- **RED:** duplicate/replayed event produces duplicate work; a worker calls a model/crawler inline from a web request; unsigned ingress accepted.

### [GOV-720](/GOV/issues/GOV-720) — economics ledger
- **In:** LED-1…6 + formulas F1–F7; SLO fields; NOTIF-3; budget events.
- **Out:** ledger schema + report generator producing per-area packs with basis labels; free/funded/paid/locked eligibility policies **defined, not activated**; LED-6 export interface.
- **Gates:** none to build; GATE-F/GATE-P consume its outputs later.
- **Metrics:** all LED fields; report reproducibility hash.
- **RED:** any fabricated price or cost figure; a report value without a basis label; a budget breach that does not pause the lane.

### [GOV-721](/GOV/issues/GOV-721) — accounts, cohorts, notifications
- **In:** COHORT-1…6; NOTIF-1…5; RET-3; Stage-0 gated-beta workflow contract.
- **Out:** secure accounts, waitlist, manual approval, revocation; cohort flags with hard caps; consent store; provider-agnostic email abstraction; in-app notifications; desktop+tablet+mobile+ARIA evidence.
- **Gates:** GATE-B1/B2/B3 per cohort step; owner card before ANY external email to a non-owner user.
- **Metrics:** COHORT-3 set; NOTIF-5 set.
- **RED:** civic data served to a pending/revoked/paused account; any message without a consent record; cohort cap bypass.

### [GOV-722](/GOV/issues/GOV-722) — portability
- **In:** PORT-1…4; §5 retention classes.
- **Out:** reproducible local Compose deployment; documented scale topology; synthetic migration/restore drill report with verification hashes.
- **Gates:** CTO merge gate; if the drill needs real cloud spend, a prior owner budget card.
- **Metrics:** drill duration; hash-verification pass; restore RPO/RTO (MEASURED).
- **RED:** semantic drift local ↔ scale (access/evidence test diff); secrets or private data present in drill artifacts.

### [GOV-723](/GOV/issues/GOV-723) — pilots + decision pack
- **In:** everything above; cohort ladder; ledger reports.
- **Out:** per-step pilot reports and the final town/county/state decision pack (measured cost, capacity forecast, activation conditions) — every value basis-labeled.
- **Gates:** GATE-B1…B3 during execution; its outputs feed GATE-F/GATE-P/GATE-PUB, which remain separate owner decisions.
- **Metrics:** full COHORT-3 + SLO set + ledger rollups.
- **RED:** an ASSUMED value presented as measured; any area/public activation performed by this card itself.

## 12. Acceptance matrix (testable)

| ID | Requirement | Test | Pass condition | Evidence | Card |
|---|---|---|---|---|---|
| AM-1 | AREA-4/5 | unit tests over all transitions | illegal transition rejected; audit row written | test output | 720/721 |
| AM-2 | COHORT-5 | integration test | revoked/pending account receives 0 civic rows | test output | 721 |
| AM-3 | INV-1 | gate-bypass regression | canonical records byte-identical before/after lens run | test output | 718 |
| AM-4 | BUD-2 | synthetic budget breach | lane paused + event + Paperclip issue created | logs + issue link | 718/720 |
| AM-5 | NOTIF-1 | send without consent | hard-fail + logged | test output | 721 |
| AM-6 | PORT-2 | migration drill | hashes equal; access decisions identical | drill report | 722 |
| AM-7 | LED-5 | report lint | every value carries a basis label | report artifact | 720 |
| AM-8 | GOV-719 dedupe | replay/duplicate events | zero duplicate jobs | test output | 719 |
| AM-9 | INV-2 / MCP redaction | boundary tests | no raw path/PII/reviewer note crosses | test output | 717 |
| AM-10 | COHORT-1 caps | cap tests | user N+1 blocked at each step cap | test output | 721 |
| AM-11 | BUD-5 | new-provider call attempt | rejected at cap 0 until owner budget set | test output | 718 |
| AM-12 | SLO-7 | metric emission check | every SLO metric present per-area in ledger | ledger rows | 720 |
| AM-13 | completeness | doc cross-check | every queue card cites the REQ IDs it implements | card descriptions/comments | all |

## 13. Assumptions register (every ASSUMED value and its replacement path)

| Assumption | Where | Replaced by |
|---|---|---|
| 14-day cohort soak time | COHORT-2 | owner decision at each GATE-B card |
| SLO targets (72 h / 7 d / 500 ms / 99% / 95%) | §4 | MEASURED pilot values → owner-set binding targets |
| Retention durations (pilot + 1 yr; 30-day deletion) | §5 | owner policy decision at GATE-PUB |
| Fixed-cost allocation weight = document share | LED-F2/F7 | owner may redefine; disclosed on every rollup |
| Double opt-in standard | NOTIF-2 | owner decision (explicit relaxation only) |

## 14. Approval and versioning

- **v1.0 — FROZEN.** Owner (Isaac) accepted board confirmation card `2fe50efb-c2df-4389-ac5c-111042bf7082` (idempotency key `confirmation:43c4be32-1522-4128-9b56-6545d1eedddb:plan:2883e628-64f6-4275-9681-db8165d213c4`) targeting plan revision `2883e628-64f6-4275-9681-db8165d213c4` at 2026-07-16T02:17:28Z. This committed file is the frozen v1.0 package; the GOV-716 issue plan document holds the accepted source revision.
- On acceptance: CEO commits the frozen package to the backend repo as `Docs/2026-commercial-platform-requirements-v1.0.md` (documentation-only commit on this issue branch), marks [GOV-716](/GOV/issues/GOV-716) done, which unblocks [GOV-717](/GOV/issues/GOV-717) and [GOV-719](/GOV/issues/GOV-719) (CTO).
- On rejection or superseding owner comment: revise, publish a new revision, fire a fresh card.
