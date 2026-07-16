# DEPLOY-2026 — Deployment portability contract (v1.0)

## 0. Document control

- **Package ID:** DEPLOY-2026 · **Version:** v1.0
- **Basis:** [GOV-716](/GOV/issues/GOV-716) REQ-2026-COMM v1.0 §8 **PORT-1…4**, §5 retention classes, §11 GOV-722 row, §12 **AM-6**; inherited **INV-4/5/7**.
- **Plan of record:** [GOV-722](/GOV/issues/GOV-722) document `plan` (DEPLOY-2026), accepted revision `0212d6ff-b543-4498-8720-41b9de8b936a`.
- **Scope:** Alpine-only, private/local-only, **zero cloud spend**. This contract authorizes no scope expansion, no publication, and no real cloud provisioning. The scale topology is **documented and drilled against local stand-ins**, not deployed.
- **Basis-label rule (global):** every numeric value here and in every drill report carries `basis: MEASURED | ASSUMED | DERIVED | OWNER-SET` (REQ-2026-COMM §0). This document contains no cost or pricing figures.
- **Change rule:** any normative change requires a version bump + a fresh owner card (REQ-2026-COMM §0).

## 1. Portability requirements (restated, binding)

- **PORT-1** Identical evidence, policy, and access semantics in local Compose and the scale topology. Environment differences live **only** in declared adapters.
- **PORT-2** A synthetic-data migration drill shows export → restore → verification **hashes equal**, and **access decisions identical** pre/post.
- **PORT-3** Civic-domain code may not import provider-specific SDKs; the adapter boundary is enforced by a lint/dependency rule.
- **PORT-4** Secrets and private data never leave the private environment during drills; drills use synthetic fixtures only.

## 2. Verified current baseline (2026-07-15)

- Registry: SQLite `Database/gov_watchdog.db` (local/gitignored, INV-7). Migrations `0001`–`0022` on `main` (`f40d234a`); next free `0023`.
- Long-running deployable surfaces (all stdlib): `scripts/webhook_ingress.py`, `scripts/job_worker.py`, `scripts/paperclip_outbox.py`, `scripts/mcp_service/` (stdio JSON-RPC).
- Frozen byte-0 serving surfaces: `read_api`, `ai_risk_gate`, `stage5_agenda_board`, `scripts/mcp_service/`.
- Host runtime: `docker` 29.5.2 + standalone `docker-compose` 5.1.4 over Colima (a **local VM**). The `docker compose` plugin is not installed; drills need `colima start` first. Nothing here is cloud.

## 3. Adapter boundary (PORT-1 / PORT-3) — D1 five-adapter matrix

Every environment difference is confined to one of five adapters, each declared
as `{interface, local backend, scale backend}`. Civic-domain modules import only
the interface (`scripts/deploy_adapters/base.py`); provider-specific code is
confined to the adapter package (`scripts/deploy_adapters/`), the sole location
the lock-in lint exempts.

| Adapter | Interface | Local backend | Scale backend |
|---|---|---|---|
| **database** | `DatabaseAdapter` (export / restore / access_view over canonical row streams) | SQLite file (`db.py`) | managed PostgreSQL (`postgres_adapter.py`, via `psql`) |
| **queue** | job rows + lease/claim semantics (`job_queue`, migration `0021`) | SQLite job tables | managed queue, interface-compatible; the DB-backed queue remains valid at scale |
| **object storage** | content-addressed raw-blob put/get by sha256 | filesystem raw store (`Docs/Source-Data`, vault-only) | S3-compatible object storage (config-only declaration; MinIO drill is a named follow-up) |
| **CDN / edge** | read-surface cache key + TTL | none (direct serve) | CDN in front of the read surfaces (config-only declaration) |
| **observability** | structured event/metric sink | local JSON logs + report snapshots | hosted logs/metrics sink |

**Enforcement.** `tests/test_deploy_lockin_lint.py` walks the import graph of every
civic-domain module (all of `scripts/**` except `scripts/deploy_adapters/`) and
fails on any provider SDK import (`boto3`, `psycopg*`, `google.*`, `azure.*`,
`redis`, `pymysql`, `mysql*`, `snowflake*`, `pymongo`, …). Today the graph is
stdlib + `requests`/`bs4`/`lxml`/`pypdf` — the lint is a forward guard.

## 4. Scale target topology — the seven §11 elements

The scale deployment is a documented target. All seven elements map onto the
adapter boundary above; the migration drill (§7) exercises the one with real
migration risk (database) against a local `postgres:16` stand-in.

1. **Managed database.** PostgreSQL as the system of record. Single logical
   writer; the SQLite→Postgres migration is proven by the drill (hashes equal,
   access decisions identical). Raw source blobs never live in the DB.
2. **Queue + workers.** The event/job control plane (`0021_control_plane`):
   `webhook_ingress` enqueues WRITE-ONCE envelopes; `job_worker` claims jobs by
   lease with retry. Workers are stateless and horizontally scalable (§6);
   a DB-backed queue is valid at scale, or a managed queue behind the same
   interface.
3. **Object storage.** The raw source store moves from filesystem to an
   S3-compatible bucket behind the object-storage adapter; content is addressed
   by sha256 (immutable, INV-7 vault-only ACLs). Registry rows keep hashes +
   archive URLs only.
4. **CDN / edge.** A read-through CDN fronts the (future, gated) public read
   surfaces. Config-only: cache keys + TTLs; no evidence or policy logic lives at
   the edge. Inert until GATE-PUB (GOV-420 hold).
5. **Observability.** Structured JSON logs, drill reports, and per-area metrics
   ship to a hosted logs/metrics sink behind the observability adapter. No raw
   evidence or PII in telemetry (redaction reuses the frozen scanners).
6. **Backups.** The drill's deterministic **export** on a schedule is the backup:
   canonical §5 (b)(c)(d) row streams + sha256 per class. See §5.
7. **DR + horizontal configuration.** Restore path + statelessness rules. See §5–§6.

## 5. Backups / DR (exercised, not just described)

Backup and restore are the **same code the drill runs**, so they are continuously
tested rather than documented-and-rotten (plan D6).

- **Backup** = `portability_drill.py` export → canonical row streams per §5
  retention class + sha256 canonical hashes. Deterministic and hash-verifiable.
  Classes **(a) raw snapshots** and **(g) reviewer notes** are never in a backup
  artifact (RET-2 / PORT-4) — they stay vault-only; the raw store is backed up
  separately by object-storage replication, never through this path.
- **Restore** = the drill's restore path: apply migrations into a fresh backend,
  then load the canonical streams. `--backend postgres` restores into managed PG.
- **Verify** = re-export + hash comparison; **equal manifest + per-class hashes**
  is the recovery-success signal. Access-decision parity through the frozen gates
  confirms semantics survived (AM-6).
- **DR** = run the drill on a **clean host**. Equal hashes + identical access
  decisions prove the system is reconstructable from the backup alone.
- **RPO / RTO** are recorded MEASURED (RTO = restore duration) / DERIVED (synthetic
  RPO = 0; a point-in-time snapshot loses no committed rows) in the drill report.

## 6. Horizontal-service configuration

- **`ingress` — stateless.** Every request is authenticated and canonicalised
  independently; the only state is the WRITE-ONCE envelope in the DB. N replicas
  behind a loopback/edge balancer are equivalent. Binds `127.0.0.1` only (INV-4);
  no public port in Compose (GOV-420).
- **`worker` — stateless, lease-based scale-out.** Workers claim jobs by lease
  with retry (`0021`); adding workers increases throughput without coordination.
  Idempotent job execution makes at-least-once delivery safe.
- **`relay` — dry-run default, single-flight.** The Paperclip outbox relays
  pending rows; `--apply` is opt-in. Grouped delivery is idempotent per umbrella.
- **Single-writer DB rule.** Canonical tables have one logical writer per row
  (WRITE-ONCE statements, WRITE-ONCE anchoring, append-only ledgers). This holds
  identically on SQLite and PostgreSQL — the invariant is in the code, not the
  engine.
- **`mcp` — stateless per call.** Job-scoped HMAC capability tokens carry all
  authorization; the service holds no session state (CONTRACT-2026-MCP).

## 7. Migration / restore drill (PORT-2 / AM-6)

`scripts/portability_drill.py` + `scripts/deploy_adapters/`. Dry-run default;
synthetic-fixture-only (real registry refused by `assert_synthetic_path` + test).

Steps (plan D5): (1) build a synthetic fixture in scratch — deliberately seeding
raw/secret columns; (2) deterministic export of §5 (b)(c)(d) column-allowlisted
row streams + sha256 per class; (3) leak-scan the export (raw paths / secrets /
excluded tables → must be empty); (4) restore into the scale backend (SQLite
stand-in for CI; `postgres:16` via `psql` for leg 4 — no Python driver, stdlib
runtime holds); (5) re-export + assert manifest + per-class **hashes equal**;
(6) run the frozen `read_api` publishability filters and MCP allowlist/redaction
on both backends → **access decisions identical**; (7) record MEASURED
duration/RTO + DERIVED RPO, and leak-scan the target artifact.

**RED conditions encoded as tests** (REQ-2026-COMM §11): semantic drift local↔scale
(any hash or access-decision inequality fails the drill); secrets/private data in a
drill artifact (the leak scan fails closed).

## 8. Secrets handling

- Secrets live only in `deploy/.env` (gitignored). `deploy/.env.example` is
  committed with placeholders; `tests/test_deploy_compose.py` asserts no
  committed compose/Dockerfile/`.env.example` content is secret-shaped (PORT-4).
- The image carries no secrets and no data: the registry DB and raw store are
  volume mounts, never layers (INV-7); `.dockerignore` keeps them out of the
  build context.
- Drill artifacts are scanned for secret-shaped values and raw paths before being
  written; a non-empty finding fails the drill.
- Provider credentials (managed DB, object storage, telemetry) are injected only
  through the adapter config env at runtime, never referenced in civic-domain code.

## 9. Acceptance mapping (commit side)

| AC | Evidence |
|---|---|
| AC-1 | `docker-compose -f deploy/docker-compose.yml config` validates; documented one-command bring-up in `deploy/README.md`. |
| AC-2 | This document — §3 adapter matrix + §4 seven topology elements. |
| AC-4 | `test_deploy_compose.py` secret scan; drill leak scan; `assert_synthetic_path` guard + test. |
| AC-5 | `test_deploy_lockin_lint.py` — zero provider SDK imports in civic-domain modules. |
| AC-6 | MEASURED/DERIVED metrics in the drill report. |
| AC-7 | frozen surfaces byte-0 vs `origin/main`; full py3.12 suite green. |

## 10. Deferred / follow-up (named, not silently skipped)

- **Object-storage drill (MinIO).** The object-storage adapter interface is
  declared (§3); a MinIO local drill target is a plan D3 stretch goal deferred to
  a GOV-722 follow-up. No silent skip.
- **Managed-queue backend.** The DB-backed queue is valid at scale; a dedicated
  managed-queue adapter implementation is future work behind the existing
  interface.
- **Leg 4 execution.** The real `postgres:16` drill run + evidence report is
  GOV-722 leg 4 (this contract + the CLI are leg 2).
