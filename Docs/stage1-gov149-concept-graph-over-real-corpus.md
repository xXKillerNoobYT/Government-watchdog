# Stage 1 · GOV-149 — Reviewer-internal concept graph over the REAL Alpine corpus

- **Issue:** GOV-149 (surfaced while shipping GOV-129; routed by CEO to BackendCrawlerEngineer under the GOV-146 owner-gate pattern). Does **not** block GOV-129.
- **Stage / scope:** Stage 1 implementation. **Alpine-only, reviewer-internal / vault-only, branch/PR-first.** No public exposure.
- **Owner / build:** BackendCrawlerEngineer. **Owner-gated like GOV-146/GOV-144 — Isaac confirms scope BEFORE any write.**
- **Builds on:** the landed concept-map registry + read-API (`scripts/concept_map.py`, `scripts/read_api.py`, migration 0012/0013, GOV-98), the GOV-105 PII write-boundary guard, and the GOV-146 Option-A reviewer-internal seed (`scripts/gov146_promotion_seed.py`).
- **Real corpus state:** the only reviewed real data is the **6 promoted reviewer-internal statements** from the GOV-146 Option-A seed (vault-only, `reviewed_source_linked`, `not_publishable`). Public serve = 0. There are **0 topics / 0 agenda threads / 0 concept edges** today.

> This is **data population + serve**, not new schema. Migration 0012/0013 already ship the
> `topics` / `agenda_threads` / `concept_edges` / `node_label_aliases` tables and every write
> helper (`insert_topic`, `insert_agenda_thread`, `insert_edge`, `insert_label_alias`) plus the
> two-layer web-safe serve (`topic_tree`, `agenda_thread`). The work is **deciding which
> relationships the real record supports** and writing only those — which is exactly why it is
> owner-gated.

---

## 1. The honest constraint that shapes everything (why this is gated)

The concept graph must be grounded in the **real record**, never inferred. Two record realities decide what we can honestly build:

1. **Topic membership is owner curation, not an algorithm.** No DB column says "this statement is *about* water." Grouping promoted statements into civic topics, and arranging those topics into a `topic_rollup` hierarchy, is a human curation judgment over AI-extracted rows — the same overclaim/defamation surface GOV-146 gated. So the **topic set + rollup hierarchy is the owner-gated curation artifact.**

2. **Agenda threads + lifecycle edges may only come from record structure — and the current real rows have none.** The issue is explicit: typed lifecycle edges (`agenda_item_supersedes` / `_amends` / `_revisits`) and cross-meeting threads are allowed **"where the real record supports them — never inferred from title similarity."** The only record-level signals are:
   - `statements.agenda_item_id` → `agenda_items.meeting_id` → meeting (shared agenda-item membership across meetings = a real thread), and
   - `statements.updates_statement_id` (an explicit, recorded supersession chain).

   The 6 promoted rows are **untimed agent-inline AI extractions with 0 speaker bindings** (GOV-138/125). They were extracted as free statements with `char_span` evidence; they carry **no `agenda_item_id` binding and no `updates_statement_id` chain.** With no agenda-item membership and no updates chain in the real reviewed corpus, **the record supports ZERO agenda threads and ZERO lifecycle edges today.**

**Conclusion (fail-closed, no-overclaim):**
- `topic_tree` **can go real now** via a small, conservative, owner-curated topic set grounded in the 6 promoted statements.
- `agenda_thread` **honestly stays empty on real data** until the real record carries agenda-item membership or an updates chain. We do **not** fabricate threads from title/topic similarity to fill the UI. The frontend keeps its clearly-labelled synthetic fixture behind `?demo=graph` for the agenda-thread surface until real agenda structure exists.

This is a deviation from the issue's acceptance criteria (which assumed *both* surfaces go real). It is surfaced to Isaac/CEO as a finding in the owner gate, not silently resolved.

## 2. The real reviewed corpus (the 6 promoted statements)

From `gov146_promotion_seed.CONFIRMED_STATEMENT_IDS` (vault-only, `reviewed_source_linked`):

| # | statement_id | civic subject (from seed manifest) |
|---|---|---|
| 1 | `alpine_local_corpus:ai:00000064:0021` | Special Town Council meeting, Oct 9 2024 ~7:01pm |
| 2 | `alpine_local_corpus:ai:01617859:0008` | mill levy — 5 mills |
| 3 | `alpine_local_corpus:ai:01661553:0010` | water system shutdown, May 21 2026 (main break) |
| 4 | `alpine_local_corpus:ai:01664750:0013` | Budget Work Session, Thu Jun 11 2026 2pm |
| 5 | `alpine_local_corpus:ai:01819080:0017` | bacteriological testing confirmed safe water |
| 6 | `alpine_local_corpus:ai:01821771:0027` | council took no action in executive session |

## 3. Proposed candidate topic layer (the owner-gated curation — APPROACH for confirmation)

Conservative, plain-English `canonicalHumanLabel` topics, each grounded in ≥1 promoted statement's already-cited source. Government strings are NOT primary labels — they attach as `node_label_aliases` with mandatory `sourceRef` provenance (the GOV-98 label layer). Proposed cap: **≤ 6 topic nodes, ≤ 4 `topic_rollup` edges.**

Candidate topics (final IDs/labels re-derived against the live op-DB in Phase A1):

- **Town water system** — grounds statements #3 (main-break shutdown) and #5 (bacteriological testing confirmed safe).
- **Town budget & taxes** — grounds statements #2 (mill levy) and #4 (Budget Work Session).
- **Town Council governance** — grounds statements #1 (special meeting) and #6 (executive-session no-action).

Candidate `topic_rollup` (child → parent), only where the hierarchy is self-evident and not an editorial claim:
- `drinking-water-safety` → `town-water-system` (only if we split safety as a child; otherwise flat).
- (Budget/governance proposed **flat** — no rollup invented just to have depth.)

Each topic node carries its `canonicalHumanLabel`; each is reachable from a real promoted statement via that statement's evidence source. No topic is created that is not grounded in a promoted statement.

## 4. Agenda-thread layer (record-supported only)

Phase B runs a **read-only candidate-derivation** over the live op-DB that proposes an `agenda_thread` + `agenda_item_in_thread` membership **only** from (a) shared `agenda_item` membership across ≥2 meetings, or (b) an explicit `updates_statement_id` chain. Lifecycle edges (`supersedes`/`amends`/`revisits`) are emitted **only** from an explicit recorded relation. **Title similarity is never a thread signal.** If the derivation finds no record-supported structure (expected for the current corpus), agenda-thread serve returns honest-empty and nothing is written.

## 5. Thread completeness (when a real thread exists)

Backend-computed, **verbatim, fail-closed**: completeness is the count of thread members that resolve to a real meeting + agenda-item over the count of members; an unresolved member fails the member closed (counts against completeness), never silently inflates it. Computed only for threads that actually exist in the record.

## 6. Serve + safety invariants (reused, never re-typed)

- Served through `read_api` reviewer-internal serve: `topic_tree(root)` and `agenda_thread(id)`, included in `build_response`.
- **Eligible-only:** topic/thread nodes are served, but any statement reachable through them is still subject to the existing fail-closed eligibility gate (`reviewer_internal_records`). Nothing AI/unverified surfaces without its label.
- **Two-layer web-safe:** `to_web_safe` field allowlist + whole-body `assert_no_raw_paths` transport sweep (file://-aware).
- **PII write-boundary guard (GOV-105):** every `canonicalHumanLabel`, topic/thread title, alias `term`, and projected locator passes `concept_map.assert_no_pii` at write time (fail-closed).
- **Reviewer-internal / vault-only:** no write flips `publication_state`; public serve stays 0. The op-DB is git-ignored/ephemeral — the durable deliverable is the **script** (GOV-135/GOV-146 precedent).

## 7. Two-gate owner pattern (mirrors GOV-144 → GOV-146)

1. **Gate 1 — approach (this confirmation).** Isaac approves: (a) the topic-curation approach + the ≤6-node/≤4-edge cap, (b) the record-supported-only thread rule with honest-empty agenda threads on the current corpus, (c) the fail-closed/vault-only posture. No write occurs on Gate-1 acceptance alone.
2. **Gate 2 — concrete manifest (before write).** I compute the exact node/edge manifest against the **live op-DB** (real IDs, real grounding, real completeness), and re-confirm the concrete ≤N list with Isaac — exactly as GOV-146 re-confirmed the 6-row promotion manifest via card `89318528` before writing. Phase B (the write + serve + tests) runs only after Gate-2 acceptance.

## 8. Build plan (Phase B, post-acceptance)

- `scripts/gov149_concept_graph_seed.py` — read-only preflight (re-derive candidates from the live reviewer-internal corpus, re-run `assert_no_pii`, re-validate acyclicity), single-transaction write of the confirmed topics + rollup edges (+ any record-supported threads), and post-state assertions (reviewer-internal serve returns the topic_tree; public serve == 0; transport sweep PASS). `--dry-run` default. Mirrors `gov146_promotion_seed.py`.
- Tests in `tests/` over a synthetic fixture DB (no real data): topic_tree non-empty + acyclic, agenda-thread honest-empty when no record structure, web-safe sweep holds, PII guard fail-closed.
- Frontend flip (separate, GOV-150): repoint `concept-graph-demo.json` → a real capture + `?demo=graph` for the topic surface; agenda-thread surface keeps the labelled synthetic fixture until real agenda structure exists.

## 9. Acceptance (revised, honest)

- `read_api` reviewer-internal serve returns a **non-empty real `topic_tree`** for real Alpine data; web-safe sweep holds; eligible-only. ✅ achievable now.
- `agenda_thread` serve returns real data **iff** the record supports a thread; otherwise honest-empty (documented finding, not a fabrication). ⚠️ likely empty on the current corpus.
- Frontend `/topics` renders **real** topic data (3-viewport evidence, GOV-150).
