# Government Watchdog — Phase 2 Pilot Spec (single watchdog brief)

**Issue:** WEI-486
**Parent:** WEI-70
**Standard:** Chaos Coding §5 (spec-before-code) — implementation-ready, frozen before the build.
**Author:** CTO (agent 328fddb9)
**Date:** 2026-05-08
**Status:** Frozen v1 (CTO-authored, board pre-cleared via "yes please" 2026-05-08).
**Scope marker:** Alpine, Wyoming corpus from Phase 1 only. Lincoln County / statewide remain out of scope.

Source-of-truth references:
- Phase 1 spec: `Docs/phase1-spec.md`
- Phase 1 acceptance log: `Logs/acceptance.log`
- Phase 1 corpus: `Database/gov_watchdog.db` (16 PDFs, 9 transcripts, 2,096 embedding chunks as of 2026-05-04T10:25:30Z)

---

## 1. Problem statement

Phase 1 produced a clean Alpine corpus with provenance and embeddings. WEI-486 asks for the smallest possible Phase 2 pipeline that proves we can turn that corpus into one **evidence-cited watchdog brief** — single-topic, factual, no editorial framing — and do it repeatably from a single command.

The pilot is deliberately minimal: no LLM authorship, no newsletter, no lens (Forge/Horizon/Sentinel) yet. We want a deterministic extraction script that selects evidence from the corpus, organises it under a frozen template, and emits a markdown artifact with citations back to source URLs and captured timestamps. If this works on one topic, it generalises. If it does not, we surface the corpus-quality gap before any LLM money is spent.

## 2. Scope

In-scope:
1. **Corpus QA report** — pass/fail check against the Phase 1 corpus (coverage, duplicates, transcript completeness, timestamp/provenance presence). Posted to WEI-486 as a comment, not committed.
2. **Topic selection** — one Alpine topic with strong cross-source signal across both PDFs and transcripts. Selected topic: **Alpine wastewater treatment plant (WWTP) financing and operations** (rationale below).
3. **Brief generator** — `scripts/watchdog_brief.py`. Deterministic, no LLM, no network. Inputs: topic keyword set. Outputs: a markdown brief at `Docs/Briefs/<YYYY-MM-DD>-<topic-slug>.md` plus a JSON sidecar with structured citations.
4. **One pilot brief artifact** — checked into `Docs/Briefs/`.
5. **Repeatability check** — re-running the generator on the unchanged corpus produces a byte-identical artifact (SHA256 match).

Out-of-scope (explicit, deferred):
- LLM authorship, summarisation, paraphrasing.
- Multi-topic or multi-brief generation.
- Newsletter/Telegram delivery.
- Honesty Tracker / History Looks Back / Transparency Alert lenses.
- Vector search over embeddings — Phase 1 stored them; Phase 2 pilot intentionally uses keyword matching only, so the pipeline remains deterministic and auditable.
- Fixing Phase 1 metadata gaps (no `doc_type`, `doc_date`, `meeting_date` extraction in 16/16 PDFs and 9/9 transcripts). Tracked as a Phase 1.5 follow-up; the pilot must work despite the gap.

## 3. Why this topic

Keyword-presence scan of the corpus (case-insensitive, all 24 sources):

| Topic candidate         | Sources containing term |
|-------------------------|-------------------------|
| water                   | 23                      |
| sewer                   | 21                      |
| tax                     | 20                      |
| road                    | 19                      |
| budget                  | 15                      |
| permit                  | 14                      |
| ordinance               | 13                      |
| planning                | 12                      |
| zoning                  | 11                      |
| resolution              | 10                      |
| subdivision             | 10                      |
| snow                    | 10                      |
| police                  | 9                       |
| wastewater              | 8                       |
| lodging tax             | 7                       |
| mill levy               | 5                       |
| liquor                  | 4                       |
| short-term rental       | 2                       |

WWTP financing scores well on three pilot-critical axes:

1. **Cross-source.** WWTP appears in PDFs (financial statements, balance sheets, audit notes — `media/1386`, `media/1411`, `media/1426` etc.) AND in council-meeting transcripts (`xTKDhDwgrdU`, `TQ4Tj1Mt7T8`, `yKXDn4S1DrA`).
2. **Factual.** The strongest snippets are quantitative: SLIB-CWSRF-71 loan principal, annual sewer charges (FY 2019: $494,758; FY 2021: $578,596; FY 2022: $527,952), CWSRF Loan #080 ($3,093,530 → $3,843,530 with additional draw).
3. **No editorial risk.** Reporting "town owes principal X on loan Y, sewer revenue was Z" needs no opinion framing to be useful to residents.

Lower-ranked candidates (e.g. short-term rental) lack PDF evidence; higher-ranked candidates (e.g. "water", "tax") are too broad to bound a single-topic brief. WWTP is the smallest pilot with real cross-source signal.

## 4. Acceptance criteria

1. **Spec frozen** — this document, before any code change to `scripts/`.
2. **Corpus QA report posted** — pass/fail summary on WEI-486 with the gaps list (especially the missing `doc_type` / `doc_date` / `meeting_date` extraction).
3. **Brief generator committed** — `scripts/watchdog_brief.py`. Pure stdlib + sqlite3; no Ollama, no HTTP, no LLM.
4. **Pilot brief committed** — `Docs/Briefs/2026-05-08-alpine-wwtp-financing.md` plus `Docs/Briefs/2026-05-08-alpine-wwtp-financing.citations.json`.
5. **Citations valid** — every numbered footnote in the brief resolves to a `(source_url, fetch_time_utc, char_offset, char_len)` tuple in the JSON sidecar; every `source_url` and `fetch_time_utc` matches a row in `documents.source_url` / `transcripts.video_url` and the corresponding `fetch_time_utc`.
6. **Repeatable** — running `python scripts/watchdog_brief.py --topic alpine-wwtp-financing` twice on the same DB produces a byte-identical brief and identical citations JSON. SHA256 of the second run must equal the first.
7. **No new corpus mutation** — the generator opens the DB read-only (`sqlite3.connect(..., uri=True, mode=ro)`).
8. **No paid API calls** — `git grep -E 'anthropic|openai|api\.openai|api\.anthropic'` returns nothing in the new code.

## 5. Brief template (frozen)

The generator emits this structure verbatim — no prose generation, only field substitution and evidence selection.

```markdown
# Alpine watchdog brief: {{topic_title}}

**Brief id:** {{brief_id}}
**Generated:** {{generated_utc}}
**Corpus snapshot:** {{db_path}} @ documents={{n_docs}} transcripts={{n_transcripts}}
**Method:** deterministic keyword extraction (no LLM); see `scripts/watchdog_brief.py`.

## What this brief covers

Topic keywords: `{{keywords_joined}}`
Source filter: any document or transcript whose extracted text contains at least one keyword (case-insensitive).

## Evidence

{{#each evidence_snippets}}
- {{snippet_text}} [^{{n}}]
{{/each}}

## Sources cited

{{#each sources}}
[^{{n}}]: {{source_url}} — fetched {{fetch_time_utc}} ({{kind}}, char_offset={{offset}}, len={{length}})
{{/each}}

## Method & reproducibility

Run: `python scripts/watchdog_brief.py --topic {{topic_slug}}`
Brief SHA256: see citations JSON.
Repeatability: identical corpus → identical brief (verified by re-run hash).
```

Snippet selection rules (deterministic):
- For each source row containing any keyword, take all keyword matches; expand each match to a window of 220 characters before / 260 characters after the match.
- Collapse runs of whitespace to single spaces; strip leading/trailing whitespace.
- Drop snippets shorter than 60 characters of useful content.
- Within a single source, merge two snippets if their windows overlap or are within 40 chars of each other.
- Sort: (kind ASC: `doc` before `tx`), (source id ASC), (char_offset ASC).
- Cap: at most 3 snippets per source, at most 30 snippets total. Citation index is assigned in final emission order.

## 6. Risks & mitigations

1. **Phase 1 metadata gaps.** PDFs lack `doc_type` and `doc_date`; transcripts lack `meeting_date`. Mitigation: brief cites by `source_url` + `fetch_time_utc` + char offset only, which the corpus does carry on every row. Date-of-event extraction is a Phase 1.5 backlog item.
2. **OCR/extraction noise.** One PDF (`media/1406`, doc id 10) has empty `raw_text` (length < 200). Mitigation: such rows are simply absent from snippet selection — no special handling, and the brief is honest about which sources contributed.
3. **Determinism drift.** Anything time-dependent in the brief (e.g. `datetime.utcnow()`) breaks the SHA256 repeatability check. Mitigation: `generated_utc` is read from `--generated-utc` CLI flag, defaulting to a topic-pinned constant baked into the topic config; no implicit `now()` calls.
4. **Topic creep.** Selecting a broad keyword (e.g. `tax`) would balloon evidence and force editorial filtering. Mitigation: topic config is a small, named keyword set committed in the script — adding/changing a topic is a code change reviewable in PR.
5. **Mistaking presence for accuracy.** A keyword match isn't a verified fact. Mitigation: brief explicitly frames itself as evidence excerpts, not assertions; readers click the citation to read context.

## 7. Open questions

1. **Generated-UTC timestamp policy.** Should `generated_utc` advance on every run (breaking SHA repeatability check) or be pinned to the brief id (preserving repeatability)? **Default if no answer:** pin to brief id for the pilot — repeatability is an explicit acceptance check. Future briefs can carry a `--regenerate` flag that bumps it.
2. **Evidence count cap.** 30 total snippets / 3 per source is a guess. **Default if no answer:** ship with these caps; tune in Phase 2.1 once we see real briefs.
3. **Topic config file vs in-script registry.** A YAML/JSON file is more flexible; an in-script dict is more auditable. **Default if no answer:** in-script dict for the pilot (one topic only); externalise when we have ≥3 topics.

Defaults above are explicit so the pilot ships even with no further direction.

## 8. Plan / sequencing

After spec freeze (this document committed):
1. Author `scripts/watchdog_brief.py` with the topic registry containing `alpine-wwtp-financing`.
2. Run it; commit `Docs/Briefs/2026-05-08-alpine-wwtp-financing.md` + citations JSON.
3. Re-run; verify SHA256 stable.
4. Post the corpus QA report + run output + artifact path + repeatability hash on WEI-486.

No child issues created — the pilot is small enough to be one PR. Child issues come back into play in Phase 2.1 (multi-topic, lens framing, delivery).

## 9. Approval

Per the wake comment on WEI-486 ("start with spec freeze per Chaos Coding before any implementation"), this spec is frozen on commit and the implementation lands in the same heartbeat. The board's standing "yes please" on Phase 2 (2026-05-08) plus the explicit instruction to ship the smallest pipeline are taken as approval to proceed without a separate `request_confirmation`. If the board wants stricter gating, raise it on WEI-486 and the pilot can be reverted before Phase 2.1.
