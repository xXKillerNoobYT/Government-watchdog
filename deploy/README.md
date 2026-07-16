# DEPLOY-2026 — local Compose deployment (GOV-722)

Reproducible, **private/loopback-only** packaging of the Government Watchdog
serving surfaces, plus a synthetic managed-DB migration drill. No host ports are
published; nothing leaves the local machine (GOV-420 hold, INV-4/INV-7).

Normative contract: [`Docs/2026-deployment-portability-contract.md`](../Docs/2026-deployment-portability-contract.md)
(DEPLOY-2026 v1.0). Basis: REQ-2026-COMM §8 PORT-1…4.

## What's in the package

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, **code + migration SQL only** — the registry DB and raw store are volume mounts, never image layers (INV-7). |
| `docker-compose.yml` | services `ingress` / `worker` / `relay` / `mcp`; `scale-shape` profile adds a `postgres:16` drill target. |
| `.env.example` | committed template, placeholders only; copy to `.env` (gitignored) for real values. |
| `mcp_stdio.py` | Compose entrypoint that runs the frozen MCP stdio JSON-RPC loop (the frozen `scripts/mcp_service/` cannot carry a `__main__`). |

The drill CLI and the adapter boundary live under `scripts/`:
`scripts/portability_drill.py`, `scripts/deploy_adapters/`.

## Prerequisites

Docker with the Compose CLI. On this Mac (verified 2026-07-15) the runtime is
`docker` 29.5.2 + standalone `docker-compose` 5.1.4 over Colima — the
`docker compose` plugin is **not** installed, so use the hyphenated
`docker-compose` binary and run `colima start` first. All of this is a **local
VM**, not cloud (zero spend).

## One-command bring-up (default profile — private)

```bash
# from the repo root
cp deploy/.env.example deploy/.env          # then edit deploy/.env
mkdir -p deploy/data                        # synthetic/scratch mount — NOT the real vault
docker-compose -f deploy/docker-compose.yml up --build
```

This starts `ingress`, `worker`, `relay`, and `mcp` against the mounted
`deploy/data/gov_watchdog.db`. Because `webhook_ingress` binds `127.0.0.1`
*inside* its container and no `ports:` are mapped, the surfaces are reachable
only via `docker compose exec` — private by construction. Migrations are applied
on first DB open.

Validate the compose file without starting anything (this is an acceptance
check, AC-1):

```bash
docker-compose -f deploy/docker-compose.yml config
```

## Portability drill (AM-6)

Dry-run (plan of record, no mutation — the default):

```bash
python3 scripts/portability_drill.py --dry-run
```

Full drill against the CI-safe SQLite stand-in (deterministic, no Docker):

```bash
python3 scripts/portability_drill.py --apply
```

Full drill against the real managed-DB stand-in (`postgres:16`, GOV-722 leg 4):

```bash
docker-compose -f deploy/docker-compose.yml --profile scale-shape up -d postgres
python3 scripts/portability_drill.py --apply --backend postgres \
    --pg-dsn "$GOV722_PG_DSN" --report deploy/data/drill-report.json
```

The drill: builds a **synthetic** fixture (never the real registry — refused by
code), exports the §5 (b)(c)(d) retention classes with sha256 canonical hashes,
restores into the scale backend, re-exports, and asserts **hashes equal** +
**access decisions identical** through the frozen `read_api` / `mcp_service`
gates, then leak-scans every artifact (PORT-4). Metrics (duration, RTO, RPO) are
basis-labelled `MEASURED`/`DERIVED`.

## Backup / DR (exercised, not just described)

- **Backup** = the drill's export step on a schedule (`portability_drill.py`
  export → canonical row streams + hashes).
- **Restore** = the drill's restore path into a fresh backend.
- **DR** = run the drill on a clean host; equal hashes prove a recoverable
  system.

See the DEPLOY-2026 contract §6–§8 for the adapter matrix, scale topology, and
secrets-handling rules.

## Hard stops

Real cloud spend, the real registry/raw data in any drill path, or a public port
are all forbidden (plan §7). Keep `DATA_DIR` pointed at a synthetic/scratch dir.
