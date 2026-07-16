# CONTRACT-2026-MCP v1.0 — Self-contained MCP service layer

**Status:** implemented (GOV-731, author leg of accepted GOV-717 plan rev `5f52bc06`).
**Package:** `scripts/mcp_service/` · **Migration:** `Database/migrations/0021_mcp_service.sql`.
This doc mirrors the plan's §3 contract specifications; the plan
([GOV-717 plan document](/GOV/issues/GOV-717#document-plan)) remains the authority
and carries the ADR (§2), data-model (§4), non-goals (§5), and test plan (§6).

Scope posture unchanged: Alpine-only, reviewer-internal, **no publication-state
or reviewer-gate change**, **zero provider calls / zero credit spend** this leg.
Registry and raw data stay local (INV-7) — only code, tests, and sanitized
fixtures are in the repo.

## 1. What this boundary is

A typed, least-privilege service layer that lets a worker (a local AI job today,
a future paid provider tomorrow) obtain **job-scoped** canonical evidence and
policy packs and submit a derived output — without ever seeing raw filesystem
paths, PII, reviewer notes, or the registry, and with **no** generic
shell/system capability. It is an *additive leaf*: the frozen serving surfaces
(`read_api`, `ai_risk_gate`, `stage5_agenda_board`) are imported, never modified.

Two independent redaction layers guard every response:

1. **Deny-by-default field allowlist (D3, `allowlists.py`).** Each resource type
   has an exhaustive set of boundary field names; the outgoing dict is built from
   only those. Un-allowlisted columns (`local_path`, `raw_local_path`,
   `local_note_path`, `transcript_path`, reviewer ids/notes, raw rows) are
   structurally absent.
2. **Frozen leak scanners (D2, `redaction.py`).** The finished payload is swept by
   the imported `read_api.assert_no_raw_paths` + `ai_risk_gate.scan_text`. A raw
   path/marker or any privacy/legal/moderation finding fails closed to
   `denied:redaction`. One copy of each scanner in the codebase — no drift.

## 2. Typed resources (read-only)

URI scheme: `gov-evidence://job/<job_id>/<type>/<id>`. Every read is
grant-authorized and job-scoped (a valid token for job J cannot read an id
outside J's input selector — context minimization).

| Resource | Backing tables | Allowlisted boundary fields | Never crosses |
|---|---|---|---|
| `job.spec` | `mcp_jobs` | job_id, area_id, job_kind, input_uris, policy_pack_id, policy_pack_version | selector internals, paths |
| `evidence.statement` | `statements`, `evidence_links`, `transcript_segments` | statement_id, text, segment_id, agenda_item_id, timestamp_seconds/human, verification_status, publication_state, evidence_links[{source_id, locator_kind, page, timestamp_seconds, section, paragraph}] | local_path, review_state internals, reviewer ids/notes, transcript_path |
| `evidence.segment` | `transcript_segments`, `speaker_attributions` | segment_id, transcript_id, char_start/end, timestamp_seconds/human, text (excerpt ≤500), speaker_label (resolved attribution only) | raw file paths, full-transcript dumps |
| `evidence.provenance` | `sources` | source_id, source_class, area_id, captured_at, archive_url, content_hash, version | raw_local_path, local_note_path, crawl internals |
| `policy.pack` | `mcp_policy_packs` | pack_id, kind, version, disclosure, rules_template, required_output_schema_id, content_hash | — |

## 3. Typed tools (narrow, schema-validated, no shell)

| Tool | Scope | Effect | Notes |
|---|---|---|---|
| `list_job_inputs` | `tool:list_job_inputs` | read | enumerates only the job's authorized resource URIs |
| `get_statement` / `get_segment` / `get_provenance` | `tool:get_*` | read | single-id fetch through the §2 allowlists |
| `get_policy_pack` | `tool:get_policy_pack` | read | exact `(pack_id, version)`; no "latest" resolution |
| `submit_output` | `tool:submit_output` | write→staging | `{job_id, output_kind, body, claims[]{source_anchor, confidence, uncertainty}, policy_pack_id, policy_pack_version}` → `mcp_job_outputs`. Body validated against the pack's `required_output_schema_id`. |

Every tool request **and** response is validated against a registered JSON Schema
`{schema_id, semver}`; unknown fields are rejected (`denied:schema`, fail-closed).
There is **no** exec/eval/shell/filesystem/subprocess tool, and a static import
guard test asserts the package never imports `subprocess`/`os.system`/`pty`/…

## 4. Capability scopes and token format (D4)

- Scope strings: `resource:<type>:read`, `tool:<name>` — exact-match allowlist per
  grant, no wildcards.
- Token: `base64url(header.claims.hmac256)` over `header.claims`, claims
  `{grant_id, job_id, scopes[], budget:{max_calls, max_input_units,
  max_output_units}, exp, nonce}`. Signing secret from `MCP_HMAC_SECRET` /
  `MCP_HMAC_SECRET_FILE` — never in the repo (INV-7); absence fails closed.
- Grants stored **hash-only** in `mcp_capability_grants` with a `revoked` column.
  Expired / revoked / wrong-job / out-of-scope ⇒ `denied:capability`; exhausted
  call budget ⇒ `denied:budget`.

## 5. Audit envelope (LED-1 subset, §3.4)

- Audit-ID `mcp-<uuid4>`, `(grant_id, seq)` monotonic per grant.
- Exactly one `mcp_audit_events` row per resource read / tool call / denial:
  `{audit_id, grant_id, seq, job_id, area_id, kind, name, schema_id,
  schema_version, request_hash, response_hash, outcome, error_code, latency_ms,
  queue_wait_s?, provider?, model?, input_units, output_units,
  direct_cost_units, cache_hit, retry_count, policy_version, lens_version?,
  created_at}`. `area_id NULL` = unattributable shared pool (AREA-2). Bodies are
  stored as content hashes, never raw text.

## 6. Provider registry interface (PORT-3, BUD-5)

- `ProviderAdapter` protocol (`providers/base.py`): `capabilities()`,
  `generate(GenerationRequest) -> GenerationResult`; request
  `{model, minimized_context_parts, max_output_units}`; result
  `{text, input_units, output_units, latency_ms, provider_meta}`.
- `mcp_provider_registry` row `{provider_id, kind, enabled, budget_cap_units
  DEFAULT 0}` — a newly registered provider is un-callable (`is_callable` false)
  until an owner sets both `enabled` and a non-zero budget (BUD-5). Enforcement
  wiring is GOV-718; the zero-default lives here.
- Provider SDK imports are confined to `scripts/mcp_service/providers/` (PORT-3
  import-boundary test). GOV-717 ships only a deterministic **fake** adapter.

## 7. Policy/lens pack schema (versioned)

`{pack_id, kind: lens|processing, version (write-once per version), disclosure,
rules_template, required_output_schema_id, content_hash}`. Packs are **data, not
code**: a pack can never mutate a canonical record — the only write surface is
`submit_output` into `mcp_job_outputs` staging (D5), so INV-1/INV-3 are
structurally guaranteed.

## 8. Transport

A minimal stdio JSON-RPC 2.0 binding (`jsonrpc.py`): methods `tools/list`,
`tools/call`, `resources/read`. **No network listener, no new third-party
dependency, no async.** Adopting the official MCP SDK later is a swap of that one
file — the domain core (contracts, allowlists, capability, audit) is
transport-agnostic (D1).

## 9. Verification

`python3.12 -m pytest tests/test_mcp_*.py -q` covers the plan's §6 items 1–8:
boundary red-team (AM-9), no-shell static guard, schema fail-closed, capability
(expired/revoked/wrong-job/out-of-scope/budget), canonical isolation before/after
`submit_output`, audit completeness, provider protocol + PORT-3 import boundary,
and the frozen-surface byte-0 diff.
