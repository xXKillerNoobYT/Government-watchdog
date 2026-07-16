# LEDGER-2026 — Empirical Area-Cost, Capacity, Funding & Entitlement Ledger (Plan)

**Status:** v0.1 **DRAFT plan-of-record** — GOV-720 planning leg. NOT frozen; not code.
**Authority:** implements [REQ-2026-COMM v1.0 (FROZEN)](2026-commercial-platform-requirements-v1.0.md) §2 (area model), §6 (budget), §9 (measurement contract, LED-1…6 + F1–F7), §11 GOV-720 row, §12 acceptance matrix (AM-1/AM-4/AM-7/AM-12), §13 assumptions.
**Owner directive source:** GOV-715 · `/Users/IA/Code/Government-watchdog/.hermes/plans/2026-07-15_190000-commercial-scale-mcp-funding-persona-queue.md`
**Author:** BackendCrawlerEngineer. **Review/merge gate:** CTO (non-author). **No new owner card** — REQ-2026-COMM already froze the WHAT; §11 GOV-720 states *"Gates: none to build."* Activation gates (GATE-F/GATE-P) are downstream and consume this ledger's outputs; they are **not** exercised here.

---

## 0. Scope & non-negotiables

- **Additive-only.** Every new table is `CREATE TABLE IF NOT EXISTS`; **no `ALTER`** on any landed table, so all existing rows stay byte-identical (INV-1/INV-3). Frozen serving surfaces — `read_api.reviewer_internal_records`, `ai_risk_gate`, `stage5_agenda_board` — are untouched (byte-0 diff).
- **Aggregation, not re-collection.** GOV-720 reads the cost substrate that GOV-717/718/719 already landed; it does not re-instrument workers. Substrate already present:
  - `event_jobs` (0021): `area_id, lane, cpu_s, queue_wait_s, retry_count, cache_hit, quality_outcome, reviewer_outcome` — the compute/queue half of LED-1.
  - `mcp_audit_events` (0022): `area_id, job_id, provider, model, input_units, output_units, direct_cost_units, latency_ms, queue_wait_s, cache_hit, retry_count, policy_version, lens_version` — the provider/model half of LED-1.
  - `mcp_budgets` / `mcp_budget_events` (0023): owner-set caps + fail-closed `breach`/`pause`/`resume` lifecycle — the AM-4 substrate.
  - `ai_extraction_runs` (0019): `tokens_input/output, estimated_cost_usd (ASSUMED est.), model_tier`.
  - `crawl_runs` (0019): `skipped_hash` — Lane-1 ingest/preservation accounting.
  - `reviewer_decisions` (0011/0014): reviewer outcomes → LED-2 proxy input.
- **No fabricated prices (hard RED).** The ledger meters **units** (provider-reported) and only ever multiplies by an **OWNER-SET** rate. It asserts **no customer pricing** and **no invented operating-cost dollars**. `estimated_cost_usd` is surfaced with `basis: ASSUMED`. Any report row without a `basis` label fails lint (LED-5 / AM-7).
- **Define, do not activate.** Free / funded / paid / locked eligibility is computed and *recommended*; **no code path auto-transitions an area's state**. All AREA-4 transitions require an `owner_decision_ref` (AREA-5). Entitlement (paid) is schema + evaluator only; activation is GATE-P, downstream.
- **Alpine-first / local-vault-only** (INV-5/INV-7). Registry stays gitignored; only code + tests + synthetic fixtures reach GitHub. No ingest/scope expansion.
- **Migration slot:** next free = **`0024_area_economics.sql`** (main tops out at 0023). Second-lander-renumbers rule applies at the merge gate. `tests/test_smoke.py` exact-table-set assertion is extended additively in the same PR.

---

## 1. New schema — `Database/migrations/0024_area_economics.sql`

All tables additive, nullable/defaulted, one statement per `;`, full-line comments only (db.py splitter contract). Nine tables:

### 1.1 Area rollup spine (AREA-1…3)
```
areas (
  area_id        TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('town','county','state')),
  name           TEXT NOT NULL,
  parent_area_id TEXT REFERENCES areas(area_id),   -- town→county→state
  created_utc    TEXT
)
```
This is the canonical rollup spine that today's loose `area_id` TEXT tags point at. `area_id IS NULL` = the disclosed shared-cost pool (AREA-2), never smeared silently.

### 1.2 Area state machine + audit (AREA-4/5)
```
area_state (
  area_id        TEXT PRIMARY KEY REFERENCES areas(area_id),
  state          TEXT NOT NULL DEFAULT 'locked'
                 CHECK (state IN ('locked','free_home','free_beta','funded','paid','limited')),
  updated_utc    TEXT
)
area_transitions (               -- append-only audit, one row per transition
  transition_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  area_id           TEXT NOT NULL REFERENCES areas(area_id),
  from_state        TEXT,
  to_state          TEXT NOT NULL,
  owner_decision_ref TEXT NOT NULL,   -- AREA-5: no ownerless transition
  rule_evaluated    TEXT,             -- e.g. 'F-ELIG' + evaluated inputs snapshot
  at_utc            TEXT NOT NULL
)
```
Legal-transition table is enforced in code (`areas.py`), not by trigger, so AM-1 unit tests can assert every illegal edge is rejected *before* any write.

### 1.3 Funding / donation ledger (F-ELIG inputs — designed, not activated)
```
area_funding_entries (           -- append-only; donations/grants/allocations
  entry_id     TEXT PRIMARY KEY,
  area_id      TEXT NOT NULL REFERENCES areas(area_id),
  period       TEXT NOT NULL,     -- 'YYYY-MM'
  amount_units INTEGER NOT NULL,  -- OWNER-SET units; NOT auto-derived
  basis        TEXT NOT NULL DEFAULT 'OWNER-SET',
  note         TEXT,
  created_utc  TEXT
)
area_funding_policy (            -- one row per area: the OWNER-SET safety_factor
  area_id       TEXT PRIMARY KEY REFERENCES areas(area_id),
  safety_factor REAL NOT NULL DEFAULT 1.0,   -- OWNER-SET (AREA-6)
  updated_utc   TEXT
)
```

### 1.4 Paid entitlement design (GATE-P — schema + evaluator only)
```
area_entitlements (
  entitlement_id TEXT PRIMARY KEY,
  area_id        TEXT NOT NULL REFERENCES areas(area_id),
  tier           TEXT NOT NULL,   -- opaque tier label; pricing lives in the business-plan lane, NOT here
  state          TEXT NOT NULL DEFAULT 'designed'
                 CHECK (state IN ('designed','offered','active','revoked')),
  owner_decision_ref TEXT,        -- required to leave 'designed'; NULL here = inert
  created_utc    TEXT
)
```
No dollar/price column. `state='designed'` is the only state this build ever writes; `offered/active` require GATE-P downstream.

### 1.5 Fixed-cost allocation (LED-4 / F2 / F7)
```
ledger_fixed_costs (
  period       TEXT PRIMARY KEY,   -- 'YYYY-MM'
  fixed_total_units INTEGER NOT NULL,   -- OWNER-SET monthly infra allocation
  weight_basis TEXT NOT NULL DEFAULT 'document_share',  -- F7 assumption, disclosed on every rollup
  basis        TEXT NOT NULL DEFAULT 'OWNER-SET',
  created_utc  TEXT
)
```

### 1.6 Reviewer-work meter (LED-2)
```
ledger_reviewer_work (
  batch_id            TEXT PRIMARY KEY,
  area_id             TEXT REFERENCES areas(area_id),
  period              TEXT NOT NULL,
  reviewer_minutes    REAL,              -- MEASURED when captured…
  decision_count      INTEGER,           -- …else proxy = decision_count × per_decision_constant
  per_decision_units  REAL,              -- OWNER-SET proxy constant
  correction_rate     REAL,              -- MEASURED
  rejection_rate      REAL,              -- MEASURED
  source_coverage_rate REAL,             -- MEASURED
  basis               TEXT NOT NULL,     -- 'MEASURED' | 'DERIVED'(proxy)
  created_utc         TEXT
)
```
Populated from `reviewer_decisions` when reviewer minutes aren't directly logged; the proxy constant is OWNER-SET and the row's `basis` is `DERIVED`.

### 1.7 Report reproducibility ledger
```
ledger_report_runs (
  report_id     TEXT PRIMARY KEY,
  area_id       TEXT,               -- NULL = multi-area rollup
  period        TEXT NOT NULL,
  scope         TEXT NOT NULL,      -- 'area' | 'county_rollup' | 'state_rollup'
  content_sha256 TEXT NOT NULL,     -- canonical hash of the emitted pack (reproducibility)
  generated_utc TEXT
)
```

> Notification cost (LED-3) tables are **GOV-721's** to create. GOV-720 defines the *aggregation shape* (`area_id, period, channel, sends, delivered, bounced, complaint, cost_units, basis`) and the report reads it via a forward-compatible optional join — absent table ⇒ the notification section renders `basis: n/a (not yet instrumented)`, never a fabricated zero-cost.

---

## 2. Module layer — `scripts/economics/` (sibling of `scripts/mcp_service/`)

New leaf package; imports only stdlib + `db.py`. Never imports a provider SDK (PORT-3). Never imports/edits the frozen serving modules.

| Module | Responsibility | Key REQ |
|---|---|---|
| `areas.py` | area CRUD; legal-transition table; `transition(area, to, owner_decision_ref, rule)`; rollup walk town→county→state | AREA-1…5, AM-1 |
| `formulas.py` | **pure** functions F1–F7 (no I/O); each returns `(value, basis, formula_id)` | LED-F1…F7 |
| `ledger.py` | LED-1 per-job aggregation across `event_jobs` + `mcp_audit_events` + `ai_extraction_runs` + `crawl_runs`, keyed by `(area_id, period, lane)` | LED-1 |
| `fixed_cost.py` | F2/F7 allocation; weight = document-share (ASSUMED default), disclosed | LED-4, F2, F7 |
| `reviewer_cost.py` | LED-2 measured-or-proxy reviewer work | LED-2 |
| `eligibility.py` | evaluate F-ELIG and paid-entitlement readiness → **recommendation only**; never writes `area_state` | AREA-6, GATE-F/P (define-not-activate) |
| `capacity.py` | LED-F6 headroom from a **synthetic** load harness (deterministic seed); `measured_max_sustainable_throughput − current_load` | LED-F6 |
| `budget_link.py` | on `mcp_budget_events.event_kind='breach'` → assert lane paused (fail-closed) + emit a bounded Paperclip outbox row (existing `paperclip_outbox`) | BUD-2, AM-4 |
| `report.py` | assemble per-area pack; **stamp every value with a `basis` label**; compute `content_sha256`; write `ledger_report_runs` | LED-5, AM-7, reproducibility |
| `export.py` | LED-6 surface: rows of `{field, unit, value, basis, formula_id, area_id, period}` as CSV **and** JSON; **no price fields** | LED-6 |
| `basis.py` | the `basis` enum (`MEASURED|ASSUMED|DERIVED|OWNER-SET`) + a `lint_report(pack)` that fails on any unlabeled value | LED-5, AM-7 |

**Basis provenance rules (baked into `report.py`):**
- provider/compute units from run ledgers → `MEASURED`
- `estimated_cost_usd`, SLO initial targets, 14-day soak, document-share weight → `ASSUMED`
- F1–F7 outputs → `DERIVED` (carry their inputs' worst basis)
- `safety_factor`, `fixed_total_units`, `per_decision_units`, funding entries → `OWNER-SET`

---

## 3. CLI — `scripts/area_economics.py`

Dry-run-default, read-only except the two explicit writers. Subcommands:

| Command | Effect | Write? |
|---|---|---|
| `report --area <id> --period YYYY-MM` | build + print per-area pack (basis-labeled); record `content_sha256` | writes `ledger_report_runs` only |
| `rollup --scope county\|state --id <id> --period` | aggregate child areas (pure aggregation, AREA-3) | writes `ledger_report_runs` |
| `export --report <id> --format csv\|json` | emit LED-6 rows | read-only |
| `verify-hash --report <id>` | recompute pack, assert `content_sha256` equal (reproducibility proof) | read-only |
| `capacity-forecast --area <id>` | synthetic-load headroom (LED-F6) | read-only |
| `eligibility --area <id> --period` | print F-ELIG + entitlement-readiness **recommendation** (never transitions) | read-only |
| `transition --area <id> --to <state> --owner-decision-ref <ref> --rule <rule>` | the **only** state writer; refuses without `--owner-decision-ref`; refuses illegal edges | writes `area_state` + `area_transitions` |

`transition` is the sole path that can move an area between free/funded/paid/locked, and it is inert without an owner decision reference — this is how "define, not activate" is enforced operationally.

---

## 4. Tests — `tests/test_economics_*.py` (mirror sibling suites; run under python3.12)

1. `test_economics_areas.py` — **AM-1**: every legal transition writes exactly one audit row; every illegal transition is refused with zero writes; `transition` without owner ref refused.
2. `test_economics_formulas.py` — F1–F7 numeric correctness on fixtures; each output carries the correct `basis`.
3. `test_economics_ledger.py` — LED-1 aggregation sums a synthetic job/audit fixture correctly per area/lane; `area_id IS NULL` lands in the shared pool, never smeared (AREA-2).
4. `test_economics_report.py` — **AM-7 / LED-5**: `lint_report` fails on any unlabeled value; a fully-labeled pack passes; `content_sha256` stable across two runs (reproducibility).
5. `test_economics_budget_link.py` — **AM-4**: a synthetic `mcp_budget_events` breach ⇒ lane asserted paused + exactly one bounded `paperclip_outbox` row; no silent overrun.
6. `test_economics_export.py` — **LED-6**: CSV+JSON contain `{field,unit,value,basis,formula_id,area_id,period}` and **no** price/dollar column (RED guard: fabricated-price scanner).
7. `test_economics_slo.py` — **AM-12**: every SLO field (SLO-1…6) is emitted per-area in the ledger surface.
8. `test_economics_capacity.py` — synthetic load harness is deterministic (seeded) and headroom = max − load.
9. `test_smoke.py` — extend exact-table-set assertion with the nine new tables (additive).

**Zero network in tests** (all synthetic fixtures); no raw registry data committed.

---

## 5. Acceptance mapping (REQ-2026-COMM §12) + RED conditions (§11 GOV-720)

| Acceptance | Where satisfied |
|---|---|
| **AM-1** area transitions + audit | `areas.py` + `test_economics_areas.py` |
| **AM-4** budget breach pauses lane + Paperclip issue | `budget_link.py` + `test_economics_budget_link.py` |
| **AM-7** every value basis-labeled (lint) | `basis.py`/`report.py` + `test_economics_report.py` |
| **AM-12** SLO metric per-area in ledger | `ledger.py` + `test_economics_slo.py` |
| Reproducible per-area reports (issue AC) | `content_sha256` + `verify-hash` + `test_economics_report.py` |
| Capacity forecast (issue AC) | `capacity.py` + `capacity-forecast` CLI |
| Budget-cap breach pauses the lane (issue AC) | `budget_link.py` (AM-4) |

| RED (must fail the build) | Guard |
|---|---|
| any fabricated price / cost figure | `export.py` no-price schema + fabricated-price scanner test |
| a report value without a basis label | `basis.lint_report` + AM-7 test |
| a budget breach that does not pause the lane | AM-4 test asserts pause + outbox row |

---

## 6. Delivery legs (child issues of GOV-720)

Mirrors the sibling chains (717→731 impl→732 gate; 718→736→737; 719→733→734→735):

1. **Leg 1 — impl (BackendCrawlerEngineer):** migration `0024_area_economics.sql` + `scripts/economics/` package + `scripts/area_economics.py` CLI + the nine test suites; additive/byte-0/frozen-surface-clean; full suite green under python3.12; synthetic reproducibility + AM-1/4/7/12 proven; **no pilot data** (pilot is GOV-723).
2. **Leg 2 — CTO non-author merge gate:** verify additive/byte-0 on frozen surfaces, migration slot, REQ-ID citations, RED guards, full py3.12 suite on the PR head; squash-merge; renumber migration if a lower slot landed first.

GATE-F / GATE-P / GATE-PUB are **not** part of this chain — they are separate owner decisions that later consume `export.py`'s pack. Pilot/measured runs are GOV-723.

## 7. Open owner/design questions (none block the build)

- **F2 weight basis** default = document-share (ASSUMED); owner may redefine at any GATE-F. Disclosed on every rollup regardless.
- **`per_decision_units`, `safety_factor`, `fixed_total_units`** are OWNER-SET; until an owner sets them the report emits the field with `basis: OWNER-SET (unset)` rather than a fabricated number.
- Real dollar rates / customer pricing are explicitly **out of scope** and belong to the frontend/business-plan lane (§11 GOV-720, GATE-P).
