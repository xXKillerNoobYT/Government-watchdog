# GOV-641 CTO spec — deterministic timed-line contract extension (Option A leg 1 of 6)

> Copied verbatim into the repo by GOV-642 (BackendCrawler impl, leg 2) per the
> contract's own instruction ("Leg 2 copies it verbatim into
> `Docs/gov641-timed-line-contract.md` inside the impl PR"). Source of record:
> GOV-641 comment `2aff3450`.

**Authorization:** Isaac accepted Option A on card `7d685b7c` (`confirmation:GOV-612:ai-lane:v1`, 2026-07-07T14:17Z, GOV-639). Scope boundary (whole chain): deterministic lane only — NO AI lane-2, NO publication/beta exposure (public-URL/wayback backfill remains the standing publication blocker), NO sources beyond the local TOA corpus, speaker names never guessed, registry + raw data stay local, only code+tests to GitHub.

---

## §0 Premise correction (measured, 2026-07-07)

The issue said "all 35 Alpine transcripts carry `[SS.s]`". A corpus-wide sweep over `/Users/IA/Documents/TOA/TownOfAlpine` (490 `.txt`/`.md` files) shows the real picture is **four deterministic locator shapes across 14 timed transcripts**; the other 21 materialized transcript docs carry **no locators at all** (single-line walls of text, 4-line header stubs, keyfindings `.md`) and must stay untimed:

| Variant | Shape | Example (real corpus line) | Files | Timed lines |
|---|---|---|---|---|
| V1 | `[SS.s]` bracketed decimal seconds, 1–2 decimals | `[3.3] I'll call this meeting to order.` | 2026-04-14, 05-05, 06-11, 06-23 | 9,163 |
| V2 | `[MM:SS]` bracketed colon; total-minutes field reaches 3 digits (`[100:00]`) | `[00:24] Oh yeah,` | 2026-03-17, 03-25, 04-20, 04-21, 04-22, 04-28 | 14,866 |
| V3 | `[SSS.SSs]` bracketed decimal seconds with `s` unit, 2 decimals | `[188.96s] So, I'll call this meeting to order.` | 2026-05-07 ×2 | 5,467 |
| V4 | bare decimal seconds + literal TAB | `746.32⇥I'll tap on the` | 2026-05-12, 06-30 | 5,200 |
| — | untimed (no locators anywhere) | 2024-10-09 moratorium wall-of-text; 01-20/02-03/02-17/03-03 stubs; 05-19, 06-02 walls; keyfindings `.md` etc. | 21 docs | 0 |

**Total: 34,696 timed lines in 14 transcripts.** A contract covering only `[SS.s]` would leave 10 of the 14 timed transcripts (25,533 lines, 74%) still untimed — defeating the accepted Option A outcome. This spec therefore covers the full deterministic family. This is not scope expansion: same corpus, same deterministic lane, same "the corpus's real timestamp format" goal; flagged for CEO visibility rather than escalated, since no frozen module, publication state, source set, or owner boundary is touched.

Decimal-value sanity (measured): n=9,163 V1 tokens, min 1.4, max 7867.84 (≈2h11m); no integer-only brackets (`[42]`) anywhere; no 3-part bracketed `[H:MM:SS]` (grammar still allows it for symmetry with the legacy contract). **All 14 timed files are monotonically non-decreasing (nonmono=0 measured per file, all variants).**

## §1 Grammar (extends `_LINE_RE` in `scripts/segment_transcript.py`)

One compiled regex, module-level, anchored — still THE single timed-line contract:

```python
_COLON   = r"\d{1,4}(?::\d{2}){1,2}"   # MM:SS / MMM:SS / HH:MM:SS (2-part first field = total minutes)
_DECIMAL = r"\d{1,5}\.\d{1,3}"         # decimal seconds — the dot is REQUIRED

_LINE_RE = re.compile(
    r"^\s*(?:"
    r"\[(" + _COLON + r"|" + _DECIMAL + r"s?)\][ \t]+"   # V1/V2/V3: bracketed token + whitespace
    r"|(" + _COLON + r")[ \t]+"                          # legacy unbracketed MM:SS / HH:MM:SS
    r"|(" + _DECIMAL + r")\t[ \t]*"                      # V4: bare decimal — literal TAB required
    r")(.*\S)\s*$"
)
```

(Implementation may use named groups / a post-match normalizer instead of three capture groups; the shapes and anchors above are normative.)

**Fail-closed decisions (normative, with rationale):**
1. **Dot required in decimal variants** — `[1]`, `[42]` footnote/list markers can never parse. Zero integer-bracket lines exist in the corpus; this guards keyfindings `.md` (doc_type `transcript`).
2. **Literal TAB required for V4** — agenda/packet section numbers and dollar figures (`12.5 Discussion…`, space-separated) can never parse. Measured: bare-decimal+space lines exist only in `MEET-Packet`/`PN-*` docs (never in the transcript lane); bare-decimal+TAB exists only in the 2 real V4 transcripts.
3. **Legacy colon first field widened `\d{1,2}` → `\d{1,4}`** — fixes a latent legacy gap (fetch_transcripts emits total-minutes, which exceeds 99 past 1h40m) and admits the measured `[100:00]` V2 tokens. Still requires `:` + 2-digit seconds, so prose numbers can't match. Additive only: every previously matching line still matches identically.
4. **Token-only lines (no trailing text) still skipped** — no empty segments (unchanged).
5. **Trailing-`s` unit accepted only inside brackets** (V3 as measured); bare `188.96s⇥` does not occur and is not admitted.

## §2 `parse_timestamp(token) -> int` extension

- Normalize: strip enclosing `[`/`]`, strip one trailing `s`.
- Token contains `:` → **existing rules unchanged** (2-part = total-minutes×60+seconds; 3-part = H:M:S).
- Else decimal seconds → **`int(float(token))` — floor**. Rationale: `transcript_segments.timestamp_seconds` is `INTEGER NOT NULL` (migration 0006) so no schema change; floor is deterministic and a video seek lands ≤1s *before* the utterance, never after. `timestamp_human` derives from the floored value via existing `format_human`. Sub-second precision is not stored in the row — the sha-addressed raw file remains the source-of-record for the exact token (unchanged posture: the MM:SS token isn't stored verbatim today either).
- A grammar-matched token that fails to parse raises `ValueError` (loud abort, unchanged) — grammar and parser must stay co-extensive; a parse failure is a defect, never a silent skip.

## §3 Single-source-of-truth invariant (preserved)

`_LINE_RE` stays module-level in `scripts/segment_transcript.py`; `transcript_from_documents.has_parseable_timestamps()` **continues to import that same object** — no copy, no second regex anywhere in the repo. Classification and segmentation therefore extend simultaneously and can never disagree.

Classification threshold stays **≥1 matching line** (unchanged semantics). Safety evidence: sweeping the *extended* grammar over all 490 corpus text files matches exactly the 14 real transcripts at scale; the only stray matches corpus-wide are 2 digest `.md` (doc_type `document`), 1 `MEET-Agenda` line and 1 `MEET-Packet` line (doc_types `agenda`/`meeting_packet`) — none enter `TRANSCRIPT_DOC_TYPES`, so false-positive exposure in the transcript lane is **zero** today. Residual risk (future doc with one stray locator line classifying as timed) is documented and guarded by negative fixtures (§6); if a future corpus drop trips it, a threshold change is a **separate spec'd issue**, not a silent tweak.

## §4 Monotonicity and malformed-line handling

- Monotonicity is **expected but not runtime-enforced**. Segment order = file order (`segment_index`); rows are never re-sorted by timestamp; the locator is exact-source. Rejecting or reordering out-of-order lines would fabricate structure — forbidden.
- Guard: a regression test asserts non-decreasing timestamps over the real-format fixtures (drift in a future corpus format surfaces in CI), and the impl adds a per-transcript `nonmonotonic_lines` count to the run summary log (observability only, no mutation, no new gate).
- Non-matching lines: skipped deterministically (unchanged). Fully untimed docs: zero segments + `missing_timestamps` completeness gap (unchanged); the gap `detail` wording in `transcript_from_documents.py` is updated to name the extended family instead of "MM:SS" only (cosmetic, allowed-touch).
- Mixed-variant files: none measured; grammar matches per-line, so a mixed file is handled deterministically with no per-file variant lock.

## §5 No-regression + frozen-module boundary

- All 13 existing tests in `tests/test_segment_transcript.py` stay green **unedited**; suite baseline **1073 passed** (py3.12, main `fe23e1e` post-PR#99). Legacy `MM:SS`/`HH:MM:SS` inputs parse byte-identically (§1 change is additive-only).
- **Byte-0-diff required** on frozen modules: `scripts/read_api.py`, `scripts/publication.py`, `scripts/ai_extraction.py` (filenames verified in-repo).
- **Allowed-touch set for GOV-642 (exhaustive):** `scripts/segment_transcript.py`; `tests/test_segment_transcript.py` + new fixture file(s) under `tests/fixtures/`; `scripts/transcript_from_documents.py` *only* for the §4 gap-detail wording; `Docs/gov641-timed-line-contract.md` (this contract). Anything else → stop, escalate to CTO/CEO. `structure_real_corpus.py` is unchanged by design (it keys off bridge classification + segmenter output).

## §6 Test plan (leg 2, GOV-642)

Fixtures: real-**format**, synthetic-**text** sample lines (generic procedural sentences — "call this meeting to order", pledge, motions). No person names, no PII: fixtures go to GitHub, and the data-publication boundary keeps real corpus text local.

1. **Per-variant positive units:** `[3.3] …` → 3; `[188.96s] …` → 188 (floor); `[00:24] …` → 24; `[100:00] …` → 6000 (3-digit total-minutes); `746.32⇥…` → 746; legacy `72:15 …` → 4335 and `1:12:15 …` → 4335 unchanged.
2. **Negative units (must NOT match):** `[42] footnote text`; `12.5 Discussion of budget` (space, no TAB); bare `746.32` end-of-line (token-only); `[1.2.3] bad`; `[3.3]` alone; `188.96s no-bracket-no-tab text`.
3. **End-to-end per variant:** fixture transcript → `segment_transcript()` → expected row count, `segment_id` sequence, `timestamp_seconds`/`timestamp_human`, `is_verbatim=1`; idempotent re-run inserts 0 new rows.
4. **Single-source:** `has_parseable_timestamps()` flips true on each variant fixture and false on each negative fixture (structurally guaranteed by the shared import; the test documents it).
5. **Monotonicity regression** over the timed fixtures (§4).
6. **Speaker-name guard:** `>>` turn markers remain inside verbatim `segment_text`; no field ever carries an attributed name (assert no `speaker`-like key appears).

## §7 Expected outcome bands for the full re-run (leg 4)

Same runbook as GOV-637 (fresh DB in ops clone), deterministic lane only:

- documents **154**, meetings **134**, transcripts materialized **35** — all unchanged.
- timed transcripts **14** / untimed **21** (GOV-637 baseline: 0/35).
- segments **34,500–34,900** (measured 34,696 timed lines; small variance for header/final unterminated lines).
- statements **= segments** (1:1 via `_statements_for_timed_transcript`), all `machine_extracted_unreviewed`, `not_publishable`, `produced_by=automation`, `layer=known_then`, `is_verbatim=1`.
- speaker names **0**; all `ai_*` counts **0** (lane-2 excluded).
- `missing_timestamps` gaps drop 35 → **21**.
- `agenda_board` cardCount stays **0 fail-closed** after leg 4 — statements exist but are unreviewed; promotion needs the reviewer gate. That is correct behavior, not a defect; reviewer/closeout is legs 5–6.

Any result outside these bands = **halt and report; never tune until green.**

## §8 Merge-gate criteria for leg 3 (GOV-643, CTO non-author)

1. PR file set ⊆ §5 allowed-touch set; `read_api.py` / `publication.py` / `ai_extraction.py` byte-0-diff vs base.
2. Full suite green on py3.12: ≥1073 passed + new tests, 0 regressions, 0 edited existing tests.
3. **Physical RED-proof:** neuter one grammar branch (e.g. drop the V4 alternation) → that variant's tests go red → restore (grep-confirm the neuter landed before trusting the red).
4. No live-AI, no network, no data files in the PR (gitignore intact; fixtures are synthetic per §6).
5. Evidence comment: test output, diff summary, RED-proof transcript.

## §9 Decisions log (no open contract questions)

floor-not-round for decimals · dot required · TAB required for V4 · legacy minute field widened to `\d{1,4}` · `s` unit only inside brackets · ≥1-line classification unchanged · monotonicity observed-not-enforced · `timestamp_seconds` stays INTEGER (no migration) · gap-detail wording update allowed · contract file lands in impl PR.
