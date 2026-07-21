# GOV-1523 Artifact / Dependency Contract Spec (Phase 1a — GOV-1525)

**Program:** GOV-1523 Option C — pinned backend web-artifact contract (accepted plan revision `07bce814-3dc1-4737-8244-b4982f093c86`, Isaac card `ca9b16ce`, 2026-07-21).
**Status:** Contract spec. No implementation in this doc. 1b (backend artifact builder + deny-list tests) and 1c (website consumption + local e2e) implement against it without re-asking.
**Repos:** backend `xXKillerNoobYT/Government-watchdog` (private) · website `xXKillerNoobYT/Government-watchdog-website` (private).
**Standing boundaries:** no public deploy, no domain/DNS/hosting spend (GOV-420 modified hold — deploy fires a separate Isaac card). Raw registry/corpus data is local/vault-only and never enters any artifact. No secrets committed. Both repos stay private (proprietary LICENSE, GOV-1529).

---

## 1. Artifact contents + manifest

One versioned tarball per pinned backend ref:

```
gw-web-artifact-<backend_short_sha>.tar.gz
├── manifest.json
├── data/
│   ├── published.json            # public lane
│   └── reviewer_internal.json    # gated lane (session-auth only, served via service — never as a static asset)
└── service/                      # auth/notification service, server-side only
    ├── run.py                    # single documented entrypoint (see below)
    └── <pinned backend package subset: scripts/accounts/, scripts/notifications/,
         scripts/email_gateway/, scripts/read_api.py + their imports>
```

**Data lanes are produced by calling the frozen serving functions — never re-implemented:**

- `data/published.json` ← `scripts/read_api.py::published_records` (uiStatus publication-eligible AND `publication_state='publishable'` AND not orphaned). Today this lane is honestly **empty**: the public lane stays 0 until the separate owner publish gate (1.11 P8) is flipped.
- `data/reviewer_internal.json` ← `scripts/read_api.py::reviewer_internal_records` — the frozen fail-closed gate. A row ships only when **every** clause holds: (1) `verification_status` is a reviewed value; (2) a promoting Lane-5 reviewer decision exists in the audit ledger (`ai_risk_gate.latest_decision`); (3) no unresolved no-go Lane-4 risk flag (`ai_risk_gate.open_risk_flags`); (4) producing gateway run (if any) is `error_status='ok'`; (5) re-derived `ui_status` is publication-eligible (source-backed); (6) not orphaned (segment edge OR ≥1 evidence pointer); (7) `publication_state` is still `not_publishable` (a publishable row belongs to the public lane, never here — no back-door public surface); (8) every record is web-safe (`to_web_safe` + non-web-URL strip) and the body transport-swept.

**Service entrypoint:** `service/run.py --db <path> --port <port>` starts the auth/notification service. It is the pinned backend code for magic-link accounts (`scripts/accounts/`: `gate.py`, `sessions.py`, `service.py`, `consent.py`, `cohorts.py`), notifications HTTP (`scripts/notifications/http_api.py`), and the email gateway (`scripts/email_gateway/`). It inherits the backend's bind guard: `ALLOWED_BIND_HOSTS = {127.0.0.1, localhost}` — the entrypoint MUST refuse any other bind host (existing behavior, restated as contract).

**`manifest.json` required fields:**

| Field | Meaning |
|---|---|
| `backend_commit` | full 40-char SHA the artifact was built from (must equal the website's `BACKEND_REF` resolution) |
| `artifact_sha256` | sha256 of the tarball contents (computed over a deterministic file order) |
| `generated_at_utc` | build timestamp, ISO-8601 UTC |
| `schema_version` | integer, bumped on any data-shape change; website build refuses unknown versions |
| `gate_functions` | literal list: `["read_api.published_records", "read_api.reviewer_internal_records"]` — proves which frozen gates produced the lanes |
| `row_counts` | `{ "published": n, "reviewer_internal": m }` — consumers can assert honest-empty vs. missing-data |

## 2. Deny-list contract (enforced by tests in 1b)

1b ships automated tests that unpack a freshly built artifact and fail the build on any hit. Deny-list, applied to **every file in the tarball**:

1. **No absolute local paths** — nothing matching `/Users/`, `/home/`, `/var/`, `/private/`, or the vault path prefix. (GOV transport raw-path finding: absolute vault paths on the wire are a hard block.)
2. **No `not_publishable` rows outside the gated lane** — `data/published.json` contains zero rows with `publication_state != 'publishable'`; `reviewer_internal.json` rows exist only in that file and are never emitted into any static/public segment of the website build (1c asserts the built site's static output contains no reviewer-internal content).
3. **No plaintext emails** — no RFC-5322-shaped strings anywhere in `data/`; account/audit records carry `email_hash` only (GOV-802 baseline).
4. **No reviewer notes** — no `reviewer_note`, `note`, or Lane-5 ledger free-text fields in any data lane; decisions surface only as labels/status.
5. **No registry / raw-corpus files** — no source-registry exports, raw crawls, transcripts, media, or `Docs/Source-Data/` content; the artifact contains exactly the two lane files plus service code and manifest, nothing else (test asserts a closed file allowlist, which subsumes the deny-list).

A deny-list failure is a **build failure** of the artifact job — the artifact is not attached, so no downstream website build can consume it (fail closed by absence).

## 3. Pin format — `BACKEND_REF`

- The website repo carries exactly one pin file at its root: **`BACKEND_REF`** — a single line, either a full 40-char commit SHA or an annotated tag on the backend repo. No ranges, no branch names.
- The website build resolves `BACKEND_REF` → downloads `gw-web-artifact-<short_sha>.tar.gz` from the backend repo's GitHub Release for that ref (1b's CI job builds and attaches the artifact on tag push / manual dispatch).
- Build verifies `manifest.backend_commit` matches the resolved ref and `artifact_sha256` matches the tarball; any mismatch = build failure.
- **Bump = explicit one-line PR** changing only `BACKEND_REF`, titled `chore: bump BACKEND_REF to <short_sha>`. That PR's CI runs the website against the new artifact — this is the whole cross-repo integration test surface. Fine-tuning either side independently never breaks the other until a deliberate bump.

## 4. Deploy-token scoping

- Token type: GitHub **fine-grained PAT**, resource = backend repo only, permission = **Contents: Read-only** (covers Releases download). Nothing else — no issues, no actions, no website-repo access.
- Storage: exists **only** as a hosting-platform secret env var, name `GW_BACKEND_DEPLOY_TOKEN`. Never committed, never in build logs (fetch script must not echo it), never reachable from client code (used only in the build/server stage).
- Rotation: owner-held; revoking the token merely breaks the next deploy (fail closed), never the running site.
- **Local dev path:** no PAT needed. `BACKEND_REF=local:<path-to-backend-checkout>` makes the fetch step build the artifact from a local checkout instead of downloading; developers with repo access may alternatively rely on ambient `gh auth token`. The local path mode is what 1c's e2e recipe uses (§8).

## 5. Same-origin `/api/*` proxy contract

- The browser talks **only** to the website origin: every dynamic call is `https://<site>/api/*` (e.g. `/api/notifications`, magic-link + session endpoints). No second public hostname, no CORS surface.
- The hosting platform's server process (or reverse proxy) forwards `/api/*` to the service from §1 on **loopback** (`127.0.0.1:<port>`). The service is never publicly addressable: it binds only `ALLOWED_BIND_HOSTS`, and the deploy configuration must not map its port to the internet.
- **Direct exposure is a build error:** 1c adds a deploy-config check that fails the build if the service port appears in any publicly-exposed port list, and the service itself refuses non-loopback binds (double enforcement).
- Static assets (landing, app shell, `published.json`) are served by the platform's static layer; `reviewer_internal.json` is readable only by the service and served through `/api/*` after session auth (§2 clause 2).

## 6. Fail-closed rules

Never a half-open app. Each failure mode has one defined behavior:

| Condition | Behavior |
|---|---|
| `GW_BACKEND_DEPLOY_TOKEN` missing/invalid at build | **Build fails.** Documented fallback: a `LANDING_ONLY=1` build flag produces the public landing + waitlist with zero `/api` surface — an explicit choice, never an automatic degrade. |
| Artifact download fails / sha or commit mismatch / unknown `schema_version` | **Build fails.** No cached/stale artifact reuse. |
| Deny-list test failure in 1b | Artifact never published (§2). |
| Feature flags (`email_adapter_enabled`, `notifications_http_enabled`, civic-gate flags) | Append-only, **no row = off** (`scripts/email_gateway/flags.py`); gated endpoints answer constant 404 while off. Enabling requires an owner-gated `set_flag` append with `owner_decision_ref`. Deploying activates **nothing**. |
| Service down / not reachable from proxy | `/api/*` returns 502; frontend renders its existing error/gated states (GOV-758) — public landing remains fully functional. |
| Unauthenticated / unapproved user | Existing gated-beta states only (GOV-758/799); no civic data on any pre-auth surface. |

Known pre-activation conditions from the GOV-802 SecPriv pass remain deploy-card-gated (not blockers for 1b/1c): F1 session-cookie `SameSite` alignment, F2 NullAdapter plaintext `to=%s` log line must be replaced by a real adapter + hash-only logging before any real user email flows.

## 7. Hosting-platform candidates (estimates only — NO spend now)

Requirements: build-time secrets, one deploy unit that can run both the static frontend and a private server-side process, private-repo artifact fetch, no forced public exposure of internal ports. All three below satisfy the shape; costs are July-2026 list-price estimates and feed the later Isaac deploy card:

| Platform | Shape | Est. monthly |
|---|---|---|
| **Render** | Web Service (Starter) running proxy+service, static served from same service or free Static Site | ~$7 |
| **Fly.io** | one shared-cpu-1x machine (proxy + loopback service in one VM, most direct match to the loopback contract) + small volume for the SQLite DB | ~$5–8 |
| **Railway** | Hobby plan, single service | ~$5–10 (usage-based on top of $5 base) |

Domain: `isaac4alpine.com` is already owner-held (live static prod), so no new domain spend; pointing it at a platform is part of the deploy card decision, not this contract.

## 8. Local e2e recipe sketch (contract for 1c)

Single command, everything on `127.0.0.1`, no tokens, no network beyond localhost:

```
scripts/local_e2e.sh        # website repo, 1c implements
```

Steps the script must perform:
1. Resolve `BACKEND_REF=local:<backend checkout>` → build the artifact locally (same builder as 1b's CI job) → run the §2 deny-list tests against it.
2. Verify manifest (commit match against the checkout's HEAD, sha256, schema_version).
3. Start `service/run.py --db <seeded demo DB> --port <p>` on loopback; assert a non-loopback bind attempt is refused.
4. Start the website preview (`vite preview`, 127.0.0.1:4173) with `/api/*` proxied to the service.
5. Smoke assertions: (a) unauthenticated → public landing only, gated routes show gated states, `/api/notifications` → 404 while flag off; (b) after appending the feature flags with a test `owner_decision_ref` → magic-link request flows through the NullAdapter outbox, session cookie issued, approved session sees reviewer-internal data via `/api/*` only; (c) built static output contains zero reviewer-internal content and zero deny-listed strings.
6. Exit non-zero on any failure; print the artifact manifest as the run record.

---

**What 1b may now build:** artifact builder script + CI release job on the backend repo, deny-list/allowlist tests (§2), manifest emission (§1), service entrypoint packaging (`service/run.py`).
**What 1c may now build:** `BACKEND_REF` pin + fetch/verify step in the website build, `/api/*` proxy config + direct-exposure check (§5), `LANDING_ONLY` fallback flag (§6), `scripts/local_e2e.sh` (§8).
**Review leg:** GOV-1528 (SecurityPrivacy) reviews token scope, deny-list, and fail-closed behavior across 1b+1c output against this spec.
