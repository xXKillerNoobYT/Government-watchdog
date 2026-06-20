# Stage 3.03 — Source/Data Inventory Contract over the Alpine Read Surface

> **Issue:** GOV-362 (Stage 3.03 · Plan · CEO→BackendCrawlerEngineer). **Parent/tracker:** GOV-355.
> **Stage:** 3.03 — `contract→impl` **contract (planning) child only**. **NON-implementation. NON-unlock.**
> **Scope:** Town of Alpine only · reviewer-internal · no public launch · no new crawl.
> **Grounded on:** canonical remote `origin/main` HEAD `6d65bd3` (GOV-347 / PR #63 — Stage 3.05 card feed live).
> **Goal of record:** HEAD GOAL `5e8b8006-94ed-4489-8fa2-643f8ec16724` (find / understand / verify at original sources).
> **Inputs of record (read-only grounding, no edit by this child):**
> - `Database/migrations/0003_sources.sql` — the `sources` registry table this inventory projects (GOV-74).
> - `scripts/source_inventory.py` — the committed Alpine seed loader (the inventory's *write* side; this contract is the *read* side).
> - `scripts/read_api.py` @ `6d65bd3` — `completeness_gap_cards` (`:612`), `reviewer_internal_records` (`:509`), `assert_no_raw_paths` (`:125`), `build_response` (`:898`). The live read surface this projection joins.
> - `scripts/publication.py` @ `6d65bd3` — `WEB_SAFE_FIELD_ALLOWLIST` (`:268`) / `WEB_UNSAFE_FIELDS` (`:345`). Field source-of-record (read-only; **0-diff** required).
> - `scripts/stage3_card_feed.py` (GOV-347) — the Stage 3 *separate-additive-module* precedent (read_api.py / publication.py 0-diff).
> - `Docs/stage2-reviewer-internal-read-surface-reference.md` (GOV-326) — the 5-overlay read-surface reference this inventory becomes a 6th, reviewer-internal overlay of.

This document pins the **source/data inventory** read-contract: *what sources are registered, their class/authority,
how much real data sits behind each (coverage), and the as-of dates* — projected over the **existing** Alpine read
surface, **reviewer-internal-gated**. It surfaces only what already exists in the `sources` registry + already-ingested
artifacts. It authorizes **no** new crawl, **no** new source, **no** public projection, **no** scope/launch/budget
unlock, and **no** `publication.py` / `read_api.py` field change. See §7.

The **implementation** child (separate issue, blocked-by THIS doc) builds the projection + RED tests per §3–§6.

---

## 0. What this child owns, and what it must not touch

**Owns (contract only):** the inventory field set (§1), the derived **coverage** metric (§2), the feed envelope (§3),
the lane-gating + no-leak invariants the impl must satisfy (§4), the impl shape + RED test list (§5), the risk gate (§6).

**Must NOT touch / re-derive (carry Stage 2 forward — GOV-362 "do not contradict Stage 2"):**
- the `publication.WEB_SAFE_FIELD_ALLOWLIST` / `WEB_UNSAFE_FIELDS` SSOT — **consumed read-only**, `publication.py` stays **0-diff**;
- the reviewer-internal vs public lane separation (GOV-146 / GOV-298 / GOV-306 / GOV-311) — **reused, never forked**;
- the `sources` registry schema (GOV-74) — the inventory is **read-only** over it; no migration, no new column.

**Goal nuance (mirror of GOV-346/GOV-337):** at CTO non-author merge of **this doc**, no goal flips to *achieved*. The
3.03 work is satisfied only when the **implementation** child merges. A contract defines the shape; it does not satisfy it.

---

## 1. The inventory field set (one row per registered source)

GOV-362 names four required fields — *source id/handle, class, coverage metric, as-of date*. Pinned below, each tagged
with its provenance and web-safety. **Every projected field is either already in `WEB_SAFE_FIELD_ALLOWLIST` or is a
derived aggregate that carries no raw locator/PII** (§2). The reviewer-internal-only columns (`raw_local_path`,
`raw_sha256`, `local_note_path`, `notes`, `owner_agent`, `robots_policy`, `registered_utc`, `raw_preservation_status`)
are **never SELECTed** — the strongest no-leak posture, identical to `completeness_gap_cards` (`read_api.py:650`).

| # | Inventory field | Source (`sources` col) | Web-safe basis | Notes |
|---|---|---|---|---|
| 1 | `source_id` | `source_id` | allowlisted (`publication.py:270`) | the stable slug handle (GOV-74 §1.02-c); already opaque/derived, not title-derived. No further hashing needed (cf. card handle) — it is the registry's public natural key. |
| 2 | `name` | `name` | allowlisted (`:271`) | human label. |
| 3 | `source_class` | `source_class` | allowlisted (`:275`) | e.g. `municipal_primary`, `county_relevant`, `codified_ordinances`, `meeting_video`. |
| 4 | `source_authority_level` | `source_authority_level` | allowlisted (`:276`) | `primary` / `secondary`. |
| 5 | `jurisdiction` | `jurisdiction` | allowlisted (`:273`) | `Alpine` / `Lincoln County (Alpine-relevant)` / … Alpine-scope only. |
| 6 | `source_type` | `source_type` | allowlisted (`:274`) | `website` / `legal_code` / `video_channel`. |
| 7 | `scan_date` | `scan_date` | allowlisted (`:285`) | **as-of #1** — immutable first-scan date (GOV-74 §1.02-i). |
| 8 | `last_validated_utc` | `last_validated_utc` | allowlisted (`:286`) | **as-of #2** — latest validation (updates). |
| 9 | `archive_status` | `archive_status` | allowlisted (`:281`) | `not_checked` / `available` / … (Wayback leg, GOV-74 Issue C). |
| 10 | `url` / `original_url` / `archive_url` | resp. | allowlisted (`:278–280`) | **public locators only** — passed through `read_api._strip_non_web_urls`; a `file://` vault URI is dropped, source identity still rides via `source_id`. Optional per row. |
| 11 | `coverage` (envelope) | **derived** (§2) | derived aggregate, attached AFTER projection | the coverage metric — counts + a fail-closed coverage state. Never a raw status string. |

**`raw_preservation_status` is deliberately excluded** (it is in `WEB_UNSAFE_FIELDS`, `publication.py:351`). Coverage is
instead expressed through the **derived, honest** `coverage.state` (§2.2), which is computed from artifact/served counts —
"surface reviewed progress + gaps, never pretend the backfill is complete" (COMPANY / June-6 directive).

---

## 2. The coverage metric (derived, read-time, fail-closed)

"Coverage" answers: *how much real, traceable data sits behind this registered source today?* It is computed **at read
time from existing rows only** — no crawl, no mutation. Attached as a `coverage` envelope dict (like `provenance_status`
/ `confidence_label` — AFTER the field projection), so no raw column is ever added to the web-safe surface.

### 2.1 The three counts (per `source_id` = `s`)

| Count | Definition (existing tables only) | Reuses |
|---|---|---|
| `documents_total` | `COUNT(*) FROM documents WHERE source_id = s` | GOV-74 reconciliation back-fills `documents.source_id`. |
| `transcripts_total` | `COUNT(*) FROM transcripts WHERE source_id = s` | GOV-74 reconciliation back-fills `transcripts.source_id`. |
| `reviewable_statements` | # of statements served by `read_api.reviewer_internal_records` that trace to `s` (via `segment_id → transcript_segments → transcripts.source_id = s`, OR an `evidence_links.to_source_id = s`). | **Reuses the Stage 2 reviewer-internal lane verbatim** — does not re-derive eligibility. A statement counts only if it already passes every gate in `reviewer_internal_records` (`read_api.py:509`). |

Counts are **aggregate integers** — no path, no PII, no raw locator. They are reviewer-internal context (the whole
surface is `access: reviewer_internal`, §4), and honest: a seed-only source reads `0/0/0`, which the reviewer UI shows
as a **gap**, never hidden or padded.

### 2.2 The derived coverage state (fail-closed enum, replaces the unsafe raw status)

```
coverage.state =
  "reviewable"  if reviewable_statements > 0
  "ingested"    elif (documents_total + transcripts_total) > 0
  "seeded"      otherwise            # registered, no artifacts behind it yet
```

Frozen 3-value SSOT `SOURCE_COVERAGE_STATES = {"seeded", "ingested", "reviewable"}` (a `frozenset`, like
`GAP_CARD_FIELDS` / `PROVENANCE_STATUS_VALUES`, so any future value is a conscious reviewed change). The default for an
empty/seed source is the most conservative `"seeded"` — coverage is **never** optimistically overstated (GOV-230 §default
posture). This is a *derived honesty label*, computed here; it never reads or surfaces the raw `raw_preservation_status`
column.

### 2.3 As-of date

The inventory's as-of dates ride the two allowlisted timing columns: `scan_date` (immutable first-scan) and
`last_validated_utc` (latest validation). No derived "data freshness" is invented in 3.03 — the impl child may add a
derived `coverage.last_artifact_utc` (max `captured_at` over the source's artifacts) **only if** it is proven web-safe
under test; otherwise it is deferred. The contract requires only the two registry columns.

---

## 3. The feed envelope (the JSON shape the impl produces)

```jsonc
{
  "scope": "alpine",
  "access": "reviewer_internal",          // never "public" — §4
  "sources": [
    {
      "source_id": "alpinewy_gov",
      "name": "Town of Alpine official website",
      "source_class": "municipal_primary",
      "source_authority_level": "primary",
      "jurisdiction": "Alpine",
      "source_type": "website",
      "scan_date": "2026-06-08",
      "last_validated_utc": "2026-06-14T03:25:00.000+00:00",
      "archive_status": "available",
      "url": "https://www.alpinewy.gov/",
      "coverage": {
        "state": "reviewable",
        "documents_total": 12,
        "transcripts_total": 0,
        "reviewable_statements": 7
      }
    }
    // … one entry per registered source, ORDER BY source_class, source_id (deterministic)
  ]
}
```

- Deterministic order (`source_class, source_id`) → same DB yields a **byte-identical** feed (idempotent re-projection).
- Every entry's flat fields are a **subset** of the frozen `SOURCE_INVENTORY_FIELDS` set (§4 / §5 test).
- `coverage` is the only nested object; its keys are a subset of the frozen coverage-key set.

---

## 4. Lane-gating + no-leak invariants (must hold by construction)

The impl child MUST satisfy all of:

- **INV-1 — reviewer-internal only.** The inventory is produced at `access: reviewer_internal` and exposed **only** via an
  opt-in flag (mirrors `include_completeness_gaps`, `read_api.py:929`). It is **absent from any public/`published_records`
  path** — surfacing seed/registry rows publicly would imply coverage that does not exist. The public lane stays **byte-identical**.
- **INV-2 — field allowlist subset.** Every projected flat field ∈ `SOURCE_INVENTORY_FIELDS` (frozen), and every member of
  that set is ∈ `publication.WEB_SAFE_FIELD_ALLOWLIST` (asserted at test time). No `WEB_UNSAFE_FIELDS` member is ever SELECTed.
- **INV-3 — no raw query of unsafe columns.** The `sources` SELECT names **only** the §1 columns — `raw_local_path`,
  `raw_sha256`, `local_note_path`, `notes`, `owner_agent`, `robots_policy`, `registered_utc`, `raw_preservation_status`
  are never read (so they can never reach a projected body), exactly as `completeness_gap_cards` omits its internal cols.
- **INV-4 — non-web URL strip.** `url` / `original_url` / `archive_url` pass through `read_api._strip_non_web_urls`; a
  `file://`/vault URI is dropped, identity still rides `source_id`.
- **INV-5 — transport sweep backstop.** The whole assembled feed is swept by `read_api.assert_no_raw_paths` before return
  — a FS path / `.sha256` / vault marker fails LOUDLY at the boundary (GOV-34), independent of INV-2/3.
- **INV-6 — `publication.py` AND `read_api.py` 0-diff.** Per GOV-347, the projection lives in a **separate additive
  module** (`scripts/stage3_source_inventory.py`) that *consumes* `read_api`/`publication` read-only. Neither file is edited.
- **INV-7 — honesty / never hidden.** A seed-only source (`0/0/0`, `state: "seeded"`) is still emitted — coverage gaps are
  shown, never dropped or padded (back-gap discipline, GOV-322 pattern).

---

## 5. Implementation shape + RED test list (for the impl child)

**Module:** `scripts/stage3_source_inventory.py` (new, additive). Imports `db`, `read_api`, `publication` (read-only).
Public API: `source_inventory(conn) -> list[dict]`, `build_inventory(conn) -> dict` (the §3 envelope, transport-swept),
plus a `_main` CLI (`--db`, JSON to stdout) mirroring `read_api._main`. `read_api.py` / `publication.py` **0-diff**.

**Tests:** `tests/test_gov362_source_inventory.py` — each must be RED before the impl, GREEN after:

- **T-1 (INV-2/3 no-leak, allowlist subset).** Build a fixture DB with a fully-populated source row (incl. raw paths,
  notes, raw_sha256). Assert every flat key of every entry ∈ `SOURCE_INVENTORY_FIELDS` ⊆ `WEB_SAFE_FIELD_ALLOWLIST`, and
  that no value contains any `read_api.RAW_PATH_MARKERS` member / no `WEB_UNSAFE_FIELDS` key is present.
- **T-2 (INV-5 transport sweep).** Plant a `raw_local_path = "/Users/IA/…/Source-Data/x.pdf"` on a source row; assert the
  field is absent from output AND that `build_inventory` does not raise (because the col is never SELECTed) — then plant a
  vault marker into an allowlisted free field and assert `assert_no_raw_paths` raises (the backstop fires).
- **T-3 (§2 coverage correctness).** Fixture: source A with 2 documents + 1 reviewable statement → `state: "reviewable",
  documents_total: 2, reviewable_statements: 1`; source B with 1 document, no reviewable statement → `"ingested"`;
  source C seed-only → `"seeded", 0/0/0`. Assert exact counts + states.
- **T-4 (INV-1 lane gating).** Assert the inventory carries the seed-only source C (reviewer-internal shows the gap) AND
  that no public/`published_records` path emits a `sources`/inventory key (the public lane is unchanged).
- **T-5 (INV-7 / determinism).** Assert order is `(source_class, source_id)` and a second build over the same DB is
  byte-identical; assert a seed-only source is never dropped.
- **T-6 (INV-6 0-diff guard, optional).** A doc-drift-style assert that this module imports — does not monkeypatch —
  `publication`/`read_api` (defensive; the PR diff is the real proof of 0-diff).

**CTO feasibility evidence expected at merge (mirror GOV-347 #63):** N additive files, `read_api.py`/`publication.py`
0-diff, full `pytest` suite green exit 0, non-author merge.

---

## 6. Risk gate (RISK_ASSESSMENT_WORKFLOW)

| Category | Touched? | Disposition |
|---|---|---|
| Evidence/source | yes | Inventory is a *read* over registered sources + ingested artifacts; every row keeps `source_id` + as-of dates. No orphan: coverage counts only artifacts/statements that already resolve to the source. |
| AI-overclaim | no | No AI in this lane. Coverage is a deterministic count, not an inference. |
| Privacy/account | **yes — primary** | Mitigated by construction (INV-2/3/4/5): raw paths, `raw_sha256`, `local_note_path`, `notes`, `owner_agent` are never SELECTed; non-web URLs stripped; transport-swept. Counts are aggregates (no PII). |
| Defamation/legal | no | No claims, no allegations — registry metadata + counts only. |
| Moderation/community | no | Reviewer-internal; no public/community surface. |
| Publication/readiness | **yes** | INV-1 keeps it `access: reviewer_internal`, opt-in, absent from the public lane. A seeded `0/0/0` source must NOT be mistaken for "covered" — `coverage.state` makes the gap explicit. |

**Reviewer routing (the GOV-362 lane):** contract → **VSR (leg-1)** → **SecPriv (leg-2)** → **CTO non-author merge.**
VSR confirms the coverage metric is honest (no overclaim, gaps shown). SecPriv confirms INV-2…INV-5 close the leak surface
(no raw path / `raw_sha256` / notes / PII can reach the body). CTO confirms `publication.py`/`read_api.py` 0-diff +
suite-green + non-author. No owner escalation is required for Step 1 (Docs-only, Alpine-only, reviewer-internal).

---

## 7. Non-unlock statement

This contract authorizes **no** production code, **no** new crawl, **no** new source, **no** `sources` migration, **no**
`publication.py`/`read_api.py` edit, **no** public projection, and **no** Stage/scope/launch/budget unlock. It is the
Step-1 design that the separate **implementation** child builds against; that child is blocked-by this doc and itself goes
through VSR (leg-1) + SecPriv (leg-2) → CTO non-author merge before any code lands. Alpine-only. Reviewer-internal only.
