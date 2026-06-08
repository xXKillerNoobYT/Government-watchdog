# Stage 1.12 Alpine Traceability and Audit Trail Contract

Issue: GOV-58
Owner role: CTO (`24fddc65`)
Stage: Stage 1.12, planning/specification only
Repo/project: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`
Scope marker: Town of Alpine only
Created: 2026-06-08

## Gate Decision

GOV-58 passes when this document gives CTO, VerificationSafetyReviewer,
BackendCrawlerEngineer, SourceArchivist, TranscriptEvidenceEngineer,
SecurityPrivacyAgent, NewsletterEditor, and FrontendTimelineEngineer a single
authoritative answer to one question: **for every meaningful claim and every
state transition that Government Watchdog records about Alpine, what provenance
must be carried, what append-only audit trail must capture who/what/when, how is
the trail kept tamper-evident and privacy-clean, and how can the exact state
known/presented/AI-interpreted at any past moment be reconstructed without
rewriting history?**

The answer this contract commits to: **every meaningful claim is provenanced to
an exact source pointer (no orphan claims), every state transition is one
append-only audit event in a hash-linked chain, history is never mutated —
later knowledge links forward as a new layer — and the trail itself carries no
private PII and is reconstructable as-of any date.** This contract **defines**
that model; it does **not** build any logging/audit infrastructure, run any
pipeline, publish anything, or expand beyond Alpine (§12). Stage 1 implementation
stays locked.

The provenance, layer, and status vocabularies are **owned upstream** (GOV-36/
37/38/39 and Stage 1.04/1.05/1.07). This contract **consumes** them and adds only
the **audit-event** and **temporal-reconstruction** layer that wraps them. It
never redefines `verificationStatus`, `uiStatus-map.v1`, the publication
allowlist, the `pointer` object, the `layer` enum, or any type enum.

The only downstream unlock is the next sequential Stage 1 planning gate. Any
implementation issue created later must explicitly consume this contract, name
its own narrow Alpine step, and name the audit/provenance check it satisfies plus
its reviewer lane.

## Inputs Read (predecessor evidence — daisy chain)

- Required agent instructions: `AGENTS.md`, `COMPANY.md`, `SOUL.md`, `TOOLS.md`,
  `HEARTBEAT.md`, `CEO_STAGING_WORKFLOW.md`, `WORKFLOW_GOVERNANCE.md`,
  `CTO_WORKFLOWS.md`, `STAGE0_EXECUTION_WORKFLOW.md`, `RISK_ASSESSMENT_WORKFLOW.md`,
  `GATED_BETA_ACCESS_WORKFLOW.md`, `AI_GATEWAY_PROCESSING_WORKFLOW.md`,
  `BACKEND_FRONTEND_EVIDENCE_WORKFLOW.md`.
- Staged master plan and product non-negotiables (source/audit-trail rules,
  raw-before-database, no orphan claims):
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`
- Strict sourcing / auditability / as-of-date testing protocol:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/Strict-Sourcing-Auditability-and-Testing-Rules.md`
- Human-auditor verification model:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-05-Human-Auditor-Verification-Model.md`
- Stage 0.12 base traceability/audit contract: **GOV-23** (backend project, goal
  `9be0c7e1`) — Alpine 1.12 extends the Stage 0 base into the live 6-value
  vocabulary and the layered concept map.
- Stage 1.04 raw preservation / reproducibility contract.
- Stage 1.05 backend/tooling contract `Docs/stage1-backend-tooling-implementation-contract.md`
  (Card Status Vocabulary, `uiStatus-map.v1`, fail-closed publication allowlist;
  GOV-36/37/38/39).
- Stage 1.07 transcript/evidence/statement contract
  `Docs/stage1-transcript-evidence-statement-contract.md` (the `pointer` object
  §2.1, orphan-rejection rule §2.3, speaker-attribution gating §3, the append-only
  `layer` enum §4).
- Stage 1.10 QA & workflow testing plan contract
  `Docs/stage1-qa-workflow-testing-plan-contract.md` (QG-1→QG-3 gates).
- Stage 1.11 security/privacy/publication gates contract
  `Docs/stage1-security-privacy-publication-gates-contract.md` (GOV-55 done;
  GOV-56 CTO APPROVE, GOV-57 VSR APPROVE) — its **§6.5 reserves the
  provenance/audit hooks for this contract to specify**.
- Authoritative status code: `scripts/validate_concept_map_export.py`
  (`SCHEMA_VERSION = "gov-watchdog-card-map.v1"`, `ALLOWED_VERIFICATION_STATUSES`,
  `ALLOWED_UI_STATUSES`, `REVIEWED_VERIFICATION_STATUSES`,
  `PUBLICATION_ELIGIBLE_UI_STATUSES`, `compute_ui_status`, the import-time
  `_VERIFICATION_STATUS_ROLES` parity assertion, `ALLOWED_NODE_TYPES`,
  `ALLOWED_EDGE_TYPES`, `ALLOWED_LINK_TYPES`). Contract test:
  `tests/test_validate_concept_map_export.py`.
- Tracked-log removal / boundary-CI precedent: **GOV-22**.
- Premium template:
  `/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`

### Predecessor-evidence note (read before relying on this contract)

The predecessor contracts (1.04–1.11) and the `validate_concept_map_export.py`
validator currently live on **unmerged task branches**
(`gov-17-newsletter-briefing-contract`, `GOV-40-…-transcript…`,
`GOV-50-…-qa…`, `GOV-55-…-security…`), not on `main`. This contract is spec-only
and cites those artifacts by stable path + constant/field name. A future
implementation gate must run its audit/provenance checks against the **merged**
versions of those files; if a name cited here has drifted at merge time, the
merge — not this contract — is the authority, and this contract is patched to
match. This contract never redefines an upstream-owned vocabulary; it defines how
the audit and reconstruction layer consumes them.

---

## 0. Authoritative vocabulary this contract consumes (reference, not redefinition)

Reproduced read-only so the audit layer below can be specified against concrete
values. Upstream is the source of truth.

- **`verificationStatus` (6 values, GOV-36/37/38/39):** `source_recorded`,
  `machine_extracted_unreviewed`, `reviewed_source_linked`, `human_verified`,
  `disputed`, `do_not_publish`. **Reviewed set** (review complete):
  `reviewed_source_linked`, `human_verified`.
- **`uiStatus` (10 kebab-case wire values)** and the **3-value
  `PUBLICATION_ELIGIBLE_UI_STATUSES`** (`source-backed`,
  `archived-source-backed`, `corrected`) — computed by `compute_ui_status`,
  fail-closed to `pending-review` (rule 12).
- **`pointer` object (Stage 1.07 §2.1):** `source_id`, `original_url`,
  `final_url`, `wayback_url`, `archive_status`, `scan_date`, `captured_at_utc`,
  `source_type`, `source_class`, `source_authority_level`, `jurisdiction`,
  `locator_kind` ∈ {`timestamp`,`page`,`section`,`paragraph`} + matching locator
  field, `agenda_item_id`, `transcript_path` (private), `deep_link`,
  `is_verbatim`, `verificationStatus`, `correctionStatus`, `confidence`.
- **`layer` enum (Stage 1.07 §4, append-only):** `known_then`, `presented_then`,
  `ai_thought_then`, `corrected_later`, `actual_later`.
- **`correctionStatus`:** correction lifecycle (`not_applicable`,
  `needs_clarification`, `corrected`, …) — drives `compute_ui_status` rules 5/6.

This contract adds exactly two new concepts: the **`audit_event`** record (§2)
and the **as-of reconstruction** procedure (§5). Everything else is consumed.

---

## 1. Provenance Record Contract (no orphan claims)

**Rule:** Every **meaningful claim** carries a complete, resolvable source trail.
A *meaningful claim* is any node/card the product can surface: a `statement`,
`vote`, `decision`, `outcome`, `document` assertion, or a `card` that presents
any of these. Background structural nodes (`jurisdiction`, `government_body`,
`role`) are provenanced by the meeting/document that introduces them.

### 1.1 The provenance trail = `evidence_link` + resolved `pointer`

Every meaningful claim resolves through one or more `evidence_link` records, each
carrying the complete Stage 1.07 `pointer`. The trail is the union of those
pointers. **No orphan claims**: a claim with zero resolvable `evidence_link`, or
a `pointer` whose `source_id` does not resolve to a registry `source_record`, is
**rejected at validation** (Stage 1.07 §2.3 orphan-rejection rule — this contract
does not relax it, it depends on it).

### 1.2 Required vs optional provenance fields

Required on **every** surfaced claim's trail (deny if missing):

| Field | Why required |
|---|---|
| `source_id` (resolves to a registry `source_record`) | the anchor; orphan rejection |
| `original_url` | the source as first published |
| `scan_date` + `captured_at_utc` | human scan date + machine capture time |
| `source_type`, `source_class`, `source_authority_level`, `jurisdiction` | source classification + Alpine scope check |
| `locator_kind` + the matching locator field (`timestamp_seconds`/`page`/`section`/`paragraph`) | the *exact* in-source pointer; a deep link alone is not evidence |
| `verificationStatus`, `correctionStatus` | drive `uiStatus` and the audit chain |
| `is_verbatim` | distinguishes verbatim quote from AI paraphrase |

Required **when available**, else explicitly null with a recorded reason:

| Field | Rule |
|---|---|
| `wayback_url` + `archive_status` | required pair; if live source is gone and no archive/preserved artifact exists → claim gated `source-missing` |
| `final_url` | required when a redirect/canonical was observed |
| `agenda_item_id` | required when the claim sits under an agenda item |
| `hash`/`version` of the fetched artifact | recorded when the ingest captured it (Strict-Sourcing protocol); enables silent-edit detection |

Private / local-only (present in the trail record, **never** in any surfaced
payload — §6): `transcript_path` and any local raw-artifact path.

### 1.3 No-orphan-claims is mechanical, not procedural

The export validator already rejects orphan claims and unresolved pointers. This
contract's provenance requirement is satisfied by passing that validator — it is
the QA-side (Stage 1.10) expression of this section. A future implementation
issue satisfies §1 by green validator output on its Alpine export, not by a
reviewer assertion.

---

## 2. Audit Trail (append-only, who/what/when, tamper-evident)

**Rule:** Every meaningful **state transition** is recorded as exactly one
**append-only `audit_event`**. Events are never edited or deleted; a correction
or reversal is itself a new event. The event log is the single reconstructable
history of how any record reached its current state.

### 2.1 The lifecycle the trail captures

```
ingest → extract(process) → review → gate_decision → publish → correction/revoke
```

Each arrow is at least one `audit_event`. `process` events name whether the
actor was **automation**, **AI**, or **human** (consumes the Stage 1.09
automation-vs-AI boundary). `gate_decision` events record the QG (Stage 1.10) and
publication-gate P1–P8 (Stage 1.11) outcomes. `publish` and `revoke` events
record tier promotions/demotions (Stage 1.11 §3 T0/T1/T2).

### 2.2 The `audit_event` record

```json
{
  "event_id": "alpine:evt:2026-06-08T17:04:22Z:000412",
  "subject_ref": "alpine:2026-05-08:stmt-1043",
  "subject_kind": "statement",
  "event_type": "status_transition",
  "actor_kind": "human",
  "actor_id": "reviewer:vsr",
  "occurred_at_utc": "2026-06-08T17:04:22Z",
  "from_state": { "verificationStatus": "machine_extracted_unreviewed", "uiStatus": "unverified" },
  "to_state":   { "verificationStatus": "reviewed_source_linked", "uiStatus": "source-backed" },
  "reason_category": "review_promotion",
  "gate": "QG-3",
  "source_version_ref": { "source_id": "alpine:video:2026-05-08-regular", "hash": "sha256:…", "scan_date": "2026-05-10" },
  "layer": "known_then",
  "prev_event_hash": "sha256:…",
  "event_hash": "sha256:…"
}
```

### 2.3 `event_type` enum

| `event_type` | Captures |
|---|---|
| `ingest` | a `source_record` first fetched + archived + hashed |
| `extract` | automation/AI produced a candidate node/statement from a source |
| `status_transition` | `verificationStatus`/`uiStatus` changed (the §4 history) |
| `review_decision` | a reviewer approved / disputed / held / corrected |
| `gate_decision` | a QG (1.10) or publication-gate (1.11) allow/deny |
| `publish` | promotion to an access tier (T0→T1, T1→T2) |
| `revoke` | demotion / takedown / pause |
| `correction` | a `corrected_later` layer was linked forward (§3) |

### 2.4 Required `audit_event` fields (deny if missing)

`event_id`, `subject_ref`, `subject_kind`, `event_type`, `actor_kind`
(∈ `automation`|`ai`|`human`), `actor_id`, `occurred_at_utc` (UTC ISO-8601),
`reason_category`, `prev_event_hash`, `event_hash`. `from_state`/`to_state`
required for `status_transition`/`gate_decision`/`publish`/`revoke`. `gate`
required for `gate_decision`. `source_version_ref` required for
`ingest`/`extract`/`correction`.

### 2.5 Tamper-evidence (hash chain)

- Each event carries `event_hash = H(canonical(event_fields_except_event_hash))`
  and `prev_event_hash = event_hash of the prior event for that subject` (or the
  genesis constant for the first). This makes the per-subject history a **hash
  chain**: any silent edit or deletion of a past event breaks the chain at that
  point and is detectable by re-walking it.
- The chain is **append-only by construction**: there is no edit/delete event
  type. A mistake is corrected by appending a `correction`/`review_decision`
  event that supersedes — never by mutating the prior event.
- **Tamper-evidence ≠ tamper-proof.** This is an integrity *check*, not a
  cryptographic notary. A periodic anchor (e.g. committing the chain-head hash to
  a dated artifact / Paperclip comment) raises the bar; full external notarization
  is out of scope for Alpine Stage 1 and is an owner decision if ever pursued.
- **Spec only:** this contract defines the chain shape and the required fields.
  It does **not** build the logger, choose the hash library, or run it (§12).

---

## 3. Temporal Lineage (versioned states, never rewrite history)

**Rule:** The product promise — users see *what was known then, what was
presented then, what AI thought then, what was corrected later, and what actually
happened later* — is kept by the **append-only `layer` enum** (Stage 1.07 §4),
not by editing records. This contract does not invent a new temporal model; it
binds the existing `layer` enum to the audit trail so a layer addition is always
also an audit event.

### 3.1 Layers are append-only links, not edits

| `layer` | Meaning |
|---|---|
| `known_then` | what the source actually said at the event date (the immutable anchor) |
| `presented_then` | how it was framed/presented at the time |
| `ai_thought_then` | what AI extraction/analysis proposed at processing time (always AI-labelled, gated) |
| `corrected_later` | a correction applied after a known-then error was found |
| `actual_later` | the real-world outcome that happened later |

- The `known_then` node is **never mutated**. `corrected_later` and
  `actual_later` records are **new nodes** linked back with a typed edge
  (`outcome_updates`, or `evidence_link.relation: corrects`) and carry their own
  dates. The frontend renders the original alongside the update; it never
  overwrites the original.
- Every layer addition emits a `correction` or `extract` `audit_event` (§2) whose
  `layer` field names which layer was added and whose `source_version_ref`
  anchors the source it was derived from. So *when* each layer entered the record
  is itself auditable.
- `ai_thought_then` is always a separate layer carrying its AI label; it is never
  merged into `known_then` and never publishes by default (Stage 1.11 §5).

### 3.2 Corrections move forward only

A correction applies **forward from the correction date**. It reaches the
publishable `corrected` `uiStatus` only through the reviewed guard
(`compute_ui_status` rule 5: `reviewed and correctionStatus == "corrected"`). The
known-then context that preceded it is preserved and remains auditable — a reader
can always see what was believed before the correction and when it changed.

---

## 4. verificationStatus History (reconstructable gate decisions)

**Rule:** Every change to a record's `verificationStatus` (and the `uiStatus` it
drives) is a `status_transition` `audit_event` (§2.2) recording `from_state`,
`to_state`, the deciding `gate`, the `actor`, and `occurred_at_utc`. The current
status is therefore always **derivable** by replaying the subject's event chain,
and any published state is fully reconstructable.

- The **only** path into a reviewed status (`reviewed_source_linked`/
  `human_verified`) is a `review_decision`/`gate_decision` event with
  `actor_kind: human`. Automation/AI may emit `extract` events that set
  `machine_extracted_unreviewed`, never a reviewed status (Stage 1.11 §5; AI
  Gateway Workflow — AI proposes, human disposes).
- Each `gate_decision` event names the gate it represents — QG-1/QG-2/QG-3
  (Stage 1.10) and/or publication-gate conditions P1–P8 (Stage 1.11 §1) — and its
  allow/deny outcome with `reason_category`. A `deny` is recorded just as durably
  as an `allow`; the trail shows *why* a record did not publish.
- Because P2's `publicExportApproved` flag is dependent on the computed `uiStatus`
  being in the publication allowlist, the audit chain that produced the reviewed
  status is the evidence that the publish was legitimate. Setting the flag without
  the supporting reviewed-promotion event is an integrity failure detectable by
  replay.

---

## 5. Reproducibility (as-of-date reconstruction)

**Rule:** The system must support **as-of [YYYY-MM-DD/UTC]** reconstruction
(Strict-Sourcing protocol): given a cutoff instant T, reconstruct exactly what was
*known*, *presented*, and *AI-interpreted* at T, using only the append-only trail.

### 5.1 The reconstruction procedure (spec)

To reconstruct the state of a subject as-of T:

1. Take the subject's `audit_event` chain; keep only events with
   `occurred_at_utc <= T`.
2. Replay them in order to derive the subject's `verificationStatus`/`uiStatus`
   and which `layer` records existed at T (a `corrected_later`/`actual_later`
   layer added after T is **excluded** — its absence at T is the point).
3. Resolve each surviving claim's `pointer` to the `source_version_ref` (hash +
   scan_date) that was current at T — not a later re-scan — so the *as-of* source
   text is the one that was actually seen then.
4. The result is the known-then / presented-then / ai-thought-then picture as it
   stood at T, with no later correction or outcome leaking backward.

### 5.2 Why this works

Because history is append-only (§2–§4) and sources are versioned by
hash+scan_date (§1.2), nothing in the past is overwritten; reconstruction is a
filter-and-replay, not a guess. This ties directly to Stage 1.04 raw
preservation: the raw artifact for each `source_version_ref` is preserved
local/vault-side, so step 3 can resolve to the bytes that were actually fetched.

### 5.3 Resume / retry

An interrupted ingest/backfill resumes by re-deriving subject state from the
registry + existing audit chain by `source_id`, then continuing at the first
claim with no terminal event — never by re-writing prior events. Replaying a
chain is idempotent.

---

## 6. Privacy Boundary in Trails / Logs

**Rule:** The audit trail and provenance records are subject to the **same
default-deny privacy boundary** as everything else (Stage 1.11 §2; Risk
Assessment Workflow). Making something auditable must never make private data
leak.

- **No private PII in audit events or surfaced trails.** Audit events reference
  subjects and sources by **ID/hash**, not by embedding private content. A home
  address, personal phone/email, government ID, voter-registry datum, or minor's
  identifier is **never collected** and therefore never enters a trail; if
  encountered at ingest it is redacted at the boundary and only a redaction *count*
  is logged (Stage 1.11 §2.3).
- **Private pointer fields stay local.** `transcript_path` and any raw-artifact
  path live in the trail record for internal reconstruction but are **stripped
  from every T1/T2 surfaced payload** (§7). The public source drawer shows the
  `original_url`/`wayback_url`/locator, never the local path.
- **No tracked local-only logs (GOV-22 lineage).** Run logs, crawler output, raw
  caches, registry dumps, and the raw audit-event store are **local/vault-only**
  and must **not** be git-tracked; `Logs/` and raw paths stay `.gitignore`-covered.
  The boundary CI (GOV-22) fails the build if a commit adds a tracked file under a
  local-only path or matching a private-data/secret pattern. Only summary counts
  surface in Paperclip comments.
- **Reviewer-only audit detail stays T0.** Internal reason notes attached to a
  `review_decision` event are T0-only; the gated-beta/public trail shows the
  *fact* of the decision and its category, never private reviewer notes.
- **Escalation:** any case where it is unclear whether a trail field is private →
  SecurityPrivacyAgent consult **before** it is stored or surfaced. When unclear:
  keep local, label the blocker, route to CEO/owner. Never guess toward
  disclosure.

---

## 7. Backend ↔ Frontend Handoff (what the trail emits, how the drawer renders it)

Field names align with the 1.05–1.11 contracts and the validator. The backend
emits a **sanitized provenance + correction bundle** per surfaced claim; the
frontend source drawer renders the trail and correction history without
manufacturing trust.

### 7.1 What the backend emits per surfaced claim (T1/T2 sanitized subset)

| Field | Meaning | Boundary |
|---|---|---|
| `sourceDrawer.sources[]` | array of source pointers: `sourceRecordId`, `url`, `scanOrFetchUtc`, `verificationStatus`, locator (timestamp/page/section), `archive_url`/`archive_status` | public-safe; **no** `transcript_path`/local path |
| `uiStatus` + `statusLabel` | the computed wire trust signal + its plain-language label | rendered verbatim |
| `correctionHistory[]` | ordered, append-only: each entry `{layer, occurred_at_utc, reason_category, supersedes_ref}` derived from `audit_event`s | public-safe; reviewer notes excluded |
| `layerView` | `known_then`/`presented_then`/`ai_thought_then`/`corrected_later`/`actual_later` framing for the claim | keeps layers separable; AI layer carries AI label |
| `asOfSupported` + `asOfDefault` | whether/which as-of reconstruction the surface offers | drives the as-of UI affordance |
| `publicExportApproved` | publish flag | true only when `uiStatus` ∈ allowlist |

### 7.2 Handoff rules

- **Backend may not call a claim frontend-ready** without a resolvable provenance
  trail (§1, no orphan claims) and a derivable audit history (§4). The API exposes
  only the sanitized subset for the caller's authenticated access tier (Stage 1.11
  §3); the raw audit store and private pointer fields have no public path.
- **Frontend may not manufacture trust.** It renders `uiStatus` and
  `correctionHistory` verbatim, shows the source drawer and the layer framing, and
  must **not** re-derive trust from `card.type` or collapse layers into one edited
  claim. It must not present a `corrected_later`/`actual_later` outcome as if it
  were known at the original date.
- **Source drawer = the user-facing proof of provenance.** Every surfaced claim's
  drawer exposes original URL, scan date, source type, archive link, and the exact
  locator — the visible expression of §1's no-orphan-claims rule.
- **Correction history = the user-facing proof of temporal lineage.** The drawer
  shows, in order, when the claim was corrected/updated and why-category, without
  rewriting the original.
- **Mismatch reopens the gate.** Any disagreement between backend audit/evidence
  state and frontend display **reopens the relevant Paperclip goal/gate**
  (Backend/Frontend Evidence Workflow handoff contract).

### 7.3 UI viewport floor (for any future UI verification)

Per COMPANY.md and the Evidence Workflow: future verification of the source
drawer / correction-history / as-of UI must cover **desktop 1440×900, tablet
768×1024, and mobile 390×844**. Mobile/tablet evidence alone does not pass; a
missing viewport class must be named with its reason and next owner. *(Stated so
the future implementation issue inherits the floor; no UI is built in this pass.)*

---

## 8. Similar-Product Research (provenance / audit-trail / fact-trace patterns)

Per the premium framework. Each entry: how it provenances/audits, what GOV should
adopt, what GOV should avoid, and fit for local Alpine civic records.

### 8.1 Git / content-addressed version history

- **Pattern:** every change is an immutable commit identified by the hash of its
  content + parent; history is a hash-linked DAG that is append-only and
  tamper-evident (rewriting a past commit changes every descendant hash).
- **Adopt:** the hash-chain integrity model (our §2.5) and content-addressing of
  source versions (our `source_version_ref` hash, §1.2) so a silent edit is
  detectable.
- **Avoid:** Git's *rewriteable* history (`rebase`/`--force`) — GOV's audit chain
  has **no** rewrite operation by design; corrections only append.
- **Alpine fit:** strong for the integrity primitive; GOV does not need a full DAG,
  a per-subject linear chain suffices for Alpine volume.

### 8.2 Certificate Transparency / Merkle append-only logs

- **Pattern:** publicly verifiable, append-only logs where inclusion and
  consistency proofs let anyone confirm nothing was removed or reordered.
- **Adopt:** the append-only + periodic-anchor idea (our chain-head anchor, §2.5)
  as a tamper-evidence bar above a plain log.
- **Avoid:** full public cryptographic notarization / gossip infrastructure — far
  beyond Alpine Stage 1 scope and an owner decision if ever pursued (§12).
- **Alpine fit:** the *concept* (append-only, externally checkable) fits; the heavy
  machinery does not.
- Source: https://certificate.transparency.dev/

### 8.3 Wikipedia revision history + talk/diff model

- **Pattern:** every edit is a retained revision with author, timestamp, and diff;
  prior states are never destroyed; disputes live in a separate talk layer.
- **Adopt:** never-destroy-prior-state (our append-only layers, §3), and the
  separation of disputed/interpretation from the record (our `disputed`/AI layers).
- **Avoid:** open anonymous editing and crowd-promotion — GOV reviewers are named
  and accountable; community volume never promotes a civic claim (Stage 1.11 §10).
- **Alpine fit:** the revision-retention model fits directly; the governance must
  be GOV's named reviewer lane, not an open crowd.
- Source: https://en.wikipedia.org/wiki/Help:Page_history

### 8.4 DocumentCloud + Wayback/Internet Archive provenance

- **Pattern:** primary-source documents retained with capture metadata; Wayback
  stores time-stamped snapshots so an altered/removed page can still be cited as it
  was at a date.
- **Adopt:** versioned source snapshots by date (our `wayback_url` + `scan_date` +
  hash, §1.2) enabling as-of reconstruction (§5) and silent-edit detection.
- **Avoid:** treating "captured/uploaded" as "verified" — capture ≠ review; GOV
  requires the reviewed guard before publish.
- **Alpine fit:** very strong — Alpine records *are* primary-source documents and
  meeting videos; snapshotting matches the Strict-Sourcing protocol exactly.
- Sources: https://www.documentcloud.org/ , https://web.archive.org/

**Cross-cutting lesson:** durable provenance/audit systems converge on
*immutable-append history, content/version addressing, never-destroy-prior-state,
and an externally checkable integrity proof.* GOV's §1–§5 encode this at Alpine
scale; the validator + hash chain make the integrity mechanical rather than
procedural, while staying well short of heavyweight public notarization.

---

## 9. GOV Premium Success Criteria

Stage: **Stage 1.12** — Alpine traceability & audit-trail contract
(planning/specification only).
Scope: **Town of Alpine only.** Defines the contract; builds/runs/publishes
nothing.
Project/repo: `xXKillerNoobYT/Government-watchdog` / `0a1832c4-1556-49a1-bcc5-857f2ca72962`.
Owner role: CTO (`24fddc65`).
Reviewer path: VerificationSafetyReviewer (`3f95c8ce`) — provenance / no-orphan-
claims / temporal-lineage correctness; BackendCrawlerEngineer (`f26f530c`) —
backend feasibility of the trail/log/hash-chain model.
Blockers / unlock rule: builds on Stage 1.04–1.11 (done/approved) and Stage 0.12
(GOV-23); consumes Stage 1.11 §6.5 reserved hooks; unlocks only the next
sequential Stage 1 planning gate. Implementation stays locked.

### Success Definition

- **Success means:** an implementer or reviewer can take one Alpine claim and,
  using only this contract, know exactly (a) what provenance trail it must carry
  (§1), (b) what append-only `audit_event` each of its transitions must emit, with
  who/what/when and a hash link (§2), (c) how its known-then/corrected-later layers
  stay separate and forward-only (§3), (d) how to replay its `verificationStatus`
  history and reconstruct its as-of-T state (§4–§5), (e) what stays private in the
  trail (§6), and (f) what the backend emits and the source drawer renders (§7) —
  and every vocabulary used is consumed from upstream, not invented here.
- **Evidence proving success:** this file (path + line count below); §1 maps to the
  validator's no-orphan-claims check; §2/§4 define concrete `audit_event`
  fields/enums; §5 gives a replay procedure tied to Stage 1.04; §6 matches the Risk
  Assessment + GOV-22 boundary; two reviewer sign-off child issues created (VSR +
  BackendCrawlerEngineer).

### Failure Definition

- **Failure looks like:** a transition with no audit event or no actor/when; an
  audit chain with an edit/delete operation (history rewrite); a correction that
  overwrites known-then; an as-of reconstruction that leaks later corrections
  backward; a private field (`transcript_path`, address, voter datum) in a surfaced
  trail or a tracked log; this contract *redefining* `verificationStatus`/
  `uiStatus`/the `pointer`/the `layer` enum; or authorizing building the logger,
  running a pipeline, publishing, or expanding beyond Alpine.
- **Stop/escalation trigger:** any owner-sensitive decision (build audit infra, run
  a pipeline against real targets, publish, official contact, privacy/defamation
  judgment on a named individual, AI-label change, budget, beyond-Alpine,
  external notarization) → **stop, route to CEO → Isaac.**

### Workability

- **Real user/operator workflow:** a specialist finishing a Stage 1 implementation
  step, or a reviewer at QG-2/QG-3, records each transition as an `audit_event` and
  checks the claim's provenance trail before promoting it; an auditor later replays
  the chain to reconstruct an as-of-T state.
- **Inputs:** a claim with its `pointer`(s), `verificationStatus`,
  `correctionStatus`, `layer`, and the actor performing the transition.
- **Outputs:** an appended `audit_event` (hash-linked) + a derivable current state
  + a reconstructable history.
- **Missing/stale/disputed source behavior:** missing/stale source → gated
  `source-missing`/`source-changed` (no publishable state); the *deny* is itself an
  audited `gate_decision` event.
- **Resume/retry behavior:** §5.3 — re-derive from registry + chain by `source_id`,
  resume at the first claim with no terminal event; replay is idempotent; never
  rewrite prior events.

### Ease of Use

- **Resident/Isaac comprehension target:** a resident opening a claim's source
  drawer sees where it came from (URL, date, exact locator, archive) and a plain
  correction history ("corrected 2026-06: …"); an unreviewed/AI/disputed item is
  visibly marked. Isaac, as designer, can read §1–§7 without code to see how every
  claim is traceable and how history is kept honest.
- **Labels/statuses/gaps visible:** the 10 `uiStatus` values + source drawer +
  correction history + layer framing + as-of affordance carry this; gaps/unavailable
  sources are labelled, never hidden.
- **Required screenshot/prototype/wireframe/review note:** none in this planning
  pass (spec-only, no UI built); the future UI implementation issue inherits the
  §7.3 viewport floor and must provide desktop+tablet+mobile evidence.

### Comparable Research

- **Comparable tools reviewed:** Git content-addressed history; Certificate
  Transparency / Merkle append-only logs; Wikipedia revision history;
  DocumentCloud + Wayback provenance (§8).
- **Lessons GOV should use:** immutable-append history; content/version addressing
  by hash+date; never destroy prior state; externally checkable integrity.
- **Patterns GOV should avoid:** rewriteable history; open/anonymous crowd
  promotion; treating capture/upload as verification; heavyweight public
  notarization at Alpine scale.
- **Source links:** in §8.

### Tradeoffs

- **Main tradeoffs:** audit completeness vs storage/operational weight; full
  cryptographic notarization vs a lightweight hash chain; rich local trail vs the
  GitHub/public boundary; as-of reconstruction fidelity vs raw-preservation cost;
  Alpine depth vs premature Wyoming/US generalization.
- **Chosen approach and reason:** **append-only hash-linked audit events +
  versioned source snapshots + append-only layers**, all consuming the existing
  vocabulary. It gives tamper-evidence and full as-of reproducibility without
  heavyweight notarization, keeps the trail private-clean by referencing IDs/hashes
  rather than embedding content, and stays Alpine-scoped. For a civic watchdog, a
  reconstructable, never-rewritten history is the core trust asset; making it
  mechanical (validator + chain) means honesty does not depend on vigilance.

### Plan Before Implementation

- **Concept/data model:** consumes the concept map + status/`pointer`/`layer`
  vocab; adds the `audit_event` record (§2) and the as-of reconstruction procedure
  (§5). No new status vocabulary.
- **UI/operator behavior:** §7 handoff fields (source drawer, correction history,
  layer view, as-of affordance); operator appends an `audit_event` per transition.
- **Verification commands or review steps:** future implementation runs the export
  validator (no-orphan-claims), a future audit-chain integrity check (replay +
  hash-walk), and QA QG-1→QG-3. *(Not run in this spec-only pass — no code
  changed.)*
- **Artifact paths:** this contract; `scripts/validate_concept_map_export.py`;
  `tests/test_validate_concept_map_export.py`; future audit-chain checker + its
  test (implementation issue, not authorized here).
- **Failure handling:** any missing/edited audit event or orphan claim → deny +
  route to named owner + (the deny is itself an audit event); any secret/PII
  exposure → Incident Response.

### Source and Auditability

- **Required source fields:** §1.2 — `source_id`, `original_url`, `scan_date`,
  `captured_at_utc`, `source_type`, `source_class`, `source_authority_level`,
  `jurisdiction`, `locator_kind` + locator, `verificationStatus`,
  `correctionStatus`, `is_verbatim`; archive pair + hash/version when available.
- **Local source-data paths:** `Docs/Source-Data/` and vault paths; raw + raw
  audit store never git-tracked (§6, GOV-22).
- **Archive/Wayback/timestamp/page requirements:** `wayback_url` + `archive_status`
  pair; exact locator per `locator_kind`; `source_version_ref` hash+date for as-of
  replay.
- **Verification/correction status handling:** per §4 (status-transition events)
  and §3 (forward-only corrections).

### Timeline and Concept Integrity

- **Known-then vs later-outcome handling:** §3 append-only `layer` enum +
  `outcome_updates` forward links; `known_then` never mutated; as-of replay (§5)
  excludes later layers.
- **Correction handling:** `corrected_later` layer + `evidence_link.relation:
  corrects`; publishable `corrected` `uiStatus` only via the reviewed guard.
- **Concept records kept separate:** audit events reference subjects by ID; raw /
  reviewed / AI / reviewer-note separation preserved (§6); AI is its own labelled
  layer.
- **Required typed relationships:** `source_supports`, `outcome_updates`,
  `card_presents`, `correction_notice`, document-chain edges — consumed from the
  validator's `ALLOWED_EDGE_TYPES`/`ALLOWED_LINK_TYPES`, not redefined here.

### Acceptance Evidence

- **Required artifacts:** this contract committed on the GOV-58 branch.
- **Required tests/checks:** none executed in this spec-only pass; no code changed.
  Future implementation must pass the export validator (no-orphan-claims), a
  future audit-chain integrity check, and QA QG-1→QG-3.
- **Required issue/PR/screenshot/API/source evidence:** file path + line count in
  the GOV-58 disposition comment; VerificationSafetyReviewer sign-off child issue;
  BackendCrawlerEngineer feasibility sign-off child issue.

---

## 10. Audit-trail / provenance workflow hooks (governance)

Per `WORKFLOW_GOVERNANCE.md`, the automation/log aspects of the future
implementation must define: command/run trigger for the audit-chain integrity
check; input/output contract (export + chain in, pass/fail report out); **log
location** (local/vault-only, never git-tracked, GOV-22); normal success output
(chain intact, no orphan claims); failure examples (broken `prev_event_hash`,
orphan claim, private field in surfaced trail); retry policy (re-walk is
idempotent); issue-creation threshold (any chain break or orphan → blocker issue
to the named owner); review cadence; and owner responsible for checking logs.
*(This section reserves the workflow requirements; the future implementation issue
patches the relevant `*_WORKFLOWS.md` when the checker is built — not in this
pass.)*

---

## 11. Coverage Summary (acceptance-criteria map)

| GOV-58 required section | Where in this contract |
|---|---|
| 1. Provenance record contract (no orphan claims; required vs optional) | §1 |
| 2. Append-only audit trail (who/what/when; tamper-evidence) | §2 |
| 3. Temporal lineage (versioned states, no rewrite) | §3 |
| 4. verificationStatus history (transitions + gate decisions) | §4 |
| 5. Reproducibility (reconstruct as-of-T) | §5 |
| 6. Privacy boundary in trails/logs | §6 |
| 7. Backend↔frontend handoff | §7 |
| 8. Similar-product research (2–4 examples, pros/cons/tradeoffs) | §8 |
| 9. Premium success-criteria template (completed) | §9 |
| 10. Stage boundary (locked scope) | §12 |

---

## 12. Stage Boundary — Locked Scope

**Stage 1.12 authorizes only this planning/specification document.** It does
**not** authorize:

- building any logging, audit-event store, hash-chain logger, or integrity-checker
  infrastructure;
- running any crawler, transcriber, AI step, exporter, validator, scheduler, or
  pipeline against real Alpine targets;
- publishing any record, page, export, newsletter, screenshot-as-approved, or API
  surface;
- contacting any official, agency, subscriber, or government system;
- making a privacy or defamation judgment about a specific real person;
- changing any AI-label, verification, or publication policy;
- redefining `verificationStatus`, `uiStatus-map.v1`, the publication allowlist,
  the `pointer` object, the `layer` enum, or any type enum (owned upstream by
  GOV-36/37/38/39 and Stage 1.04/1.05/1.07);
- adopting external cryptographic notarization / public transparency-log
  infrastructure (§8.2);
- granting any access tier, beta approval, or public launch;
- budget/donation decisions;
- expanding beyond the Town of Alpine.

Each of these is an **owner-escalation trigger**: defining the traceability and
audit contract is in scope; building or exercising it is **not** — **stop and
route to CEO → Isaac.**

The only downstream unlock is the next sequential Stage 1 planning gate. Stage 1
implementation stays locked until its own gates pass.

## Next Action

1. Commit this contract on branch
   `GOV-58-stage-1-12-cto-define-alpine-traceability-and-audit-trail-contract`.
2. Create the **VerificationSafetyReviewer** sign-off child issue (mirror
   GOV-57), assigned to `3f95c8ce`, for provenance / no-orphan-claims /
   temporal-lineage correctness; review target = this file.
3. Create the **BackendCrawlerEngineer** feasibility sign-off child issue,
   assigned to `f26f530c`, for backend feasibility of the audit-event / hash-chain
   / as-of-replay model; review target = this file.
4. Comment the disposition on GOV-58 with file path + line count and mark it
   `done` (the sign-off child issues carry the live next action; this avoids the
   GOV-49 `in_review_without_action_path` liveness incident).
