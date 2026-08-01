# Supplied-file provenance chain (GOV-1566 B2–B6) — as-built contract (v1.0)

## 0. Document control

**Status:** as-built, written 2026-07-31 (GOV-1686, AUTO GO C1 for area `ingest-provenance`).
Every claim was measured against `main` @ `a15c231` before it was written; where a claim is
checkable, the check is named. **This describes what IS, not what should be.**

**Why it exists.** `GOV-1566` was built as a six-slice B-series. **B1 got a `Docs/` spec
(`gov1574-raw-object-store-spec.md`); B2–B6 got none.** Measured: `file_records.py`,
`file_versioning.py`, `file_linkage.py` and `backfill_provenance_note.py` — **1,460 lines** —
are named by **no document anywhere in `Docs/`**. The design is real and coherent, but it
lives only in module docstrings and merged PR bodies, so reading it requires either 1,460
lines of source or archaeology through eight PRs.

That matters more here than elsewhere: this is the **newest and most actively-changed**
subsystem in the repo — four of the five most recent commits on `main` touch it.

**Scope.** The supplied-file record/link/version model and its reviewer-gated backfill.
**Not** B1's raw object store (that has its own spec), and not the *transport* halves —
see P-7, which is the thing most likely to bite.

---

## 1. Shape, measured

| Fact | Value | How to re-measure |
|---|---|---|
| Tables | **4** — `supplied_files`, `supplied_file_links`, `supplied_file_dependencies`, `supplied_file_supersede_events` | `grep -h 'CREATE TABLE.*supplied_file' Database/migrations/*.sql` |
| Migrations | **4** — `0028`–`0031` | `grep -l supplied_file Database/migrations/*.sql` |
| Modules in this area | 4 — `file_records` (317), `file_versioning` (504), `file_linkage` (435), `backfill_provenance_note` (204) | `wc -l` |
| Tests | `test_file_records.py`, `test_file_versioning.py`, `test_file_linkage.py`, `test_gov1625_backfill_provenance_note.py` | |
| Slices with a `Docs/` spec before this file | **1 of 6** (B1 only) | |

**The B-series and where each slice lives:**

| Slice | What | Module | Area |
|---|---|---|---|
| B1 (GOV-1574) | content-addressed immutable raw store | `raw_object_store.py` | ingest-provenance — **has a spec** |
| B2 (GOV-1575) | file record + provenance model | `file_records.py` | ingest-provenance |
| B3 (GOV-1576/1625) | gated intake API + `provenance_note` schema evo | `beta/intake_api.py` | **access-gate** |
| B4 (GOV-1577) | linkage + deterministic gap detection | `file_linkage.py` | ingest-provenance |
| B5 (GOV-1578) | versioning + red-flag on supersede | `file_versioning.py` | ingest-provenance |
| B6 (GOV-1579) | web-safe read projection | `file_read_api.py` | **read-api** |

---

## 2. Invariants

### P-1 — Provenance is mandatory, and absence is a refusal

`insert_file_record` requires six text fields — `area`, `source_type`, `original_filename`,
`supplied_by`, `captured_at`, `mime`. Blank, absent or malformed raises `MissingProvenance`.
**A file with no stated origin is not stored**, which is the whole point: an unattributed
document cannot later be turned into a civic claim.

### P-2 — `review_state` moves only along the legal map, and `rejected` can never jump to `web_safe`

States: `pending`, `reviewing`, `web_safe`, `held`, `rejected`. Transitions are a closed map
(`_LEGAL_TRANSITIONS`); anything else raises `IllegalReviewTransition`.

```
pending   -> reviewing, held, rejected
reviewing -> web_safe, held, rejected, pending
web_safe  -> held, rejected, reviewing
held      -> reviewing, rejected, pending
rejected  -> reviewing            # reopen ONLY; never straight to web_safe
```

**The last line is the load-bearing one and it is deliberate.** A repudiated file must
re-enter review before it can ever be published again — there is no path that turns a
rejection directly into public content. Widening that row would be a publication-safety
change, not a convenience change.

### P-3 — The `no_primary_source` gap tracks COVERAGE, not publishability

This is the invariant most likely to be "fixed" by someone reading the code cold, so it is
stated first among the surprising ones.

`has_primary_source` counts any link with `is_primary_source = 1` whose file's `review_state`
is **not** in `NON_COUNTING_REVIEW_STATES` — and that set contains **only `rejected`**.

**So a `pending`, entirely unreviewed file closes the `no_primary_source` gap.** That looks
fail-open in a fail-closed house, and it is not: the gap asks *"does this subject have a
source at all?"*, not *"may we publish it?"*. Publishability is a separate gate — B6's
web-safe projection — and **the two must not be conflated.** Only a repudiated file fails to
count, because a repudiated file is not a source.

The threshold is a keyword parameter (`non_counting_states=`), so a caller that genuinely
needs publishability can pass a wider set. **Changing the default changes what "gap closed"
means across the whole completeness surface.**

### P-4 — A supersede retains BOTH versions and red-flags everything downstream

`supersede_file` does four things atomically-in-intent: writes the new version, **preserves
the prior record**, appends one immutable `supplied_file_supersede_events` row carrying a
deterministic before/after diff (`compute_before_after`), and flips every dependent's
`review_flag` from `current` to `needs_re_review`.

**Nothing is overwritten and nothing is silently invalidated.** Work built on the old version
is flagged for a human (`list_needs_re_review`), and only a human clears it
(`resolve_re_review`). Flags move along a closed map; anything else raises
`IllegalFlagTransition`.

### P-5 — Link identity is deterministic, and it IS the uniqueness key

`make_link_id(subject_node_type, subject_node_id, file_id)` slugs its inputs into the id, so
the same (file, subject) pair always produces the same link id. Re-linking is therefore
idempotent rather than duplicating. **Do not generate link ids any other way** — a random id
would silently allow the same file to be attached to the same subject twice, and every
count over `supplied_file_links` would drift.

### P-6 — The subject vocabulary is closed

`LINK_SUBJECT_TYPES` = `{area, meeting, agenda_item}`. Anything else raises
`UnknownSubjectType`. This is the house fail-closed rule (`CLAUDE.md`: *unknown keys deny*)
applied to a vocabulary — a new subject type is a deliberate edit here plus a migration
review, never an incidental string.

### P-7 — The chain spans THREE areas, and two of its ends are constrained surfaces

Ownership is not where the module list suggests:

- **B3's intake** is `scripts/beta/intake_api.py` — `access-gate`'s area, and the one loopback
  server deliberately **not** threaded (its handler closes over a `RawObjectStore` whose
  ledger append is not established as thread-safe — [#206]).
- **B6's read projection** is `file_read_api.py` — `read-api`'s area, and *the sole
  Backend→Website crossing*.

**So a change to the record model can break a surface that this area does not own.** Any
field rename or `review_state` change must be checked against both ends before it ships.

### P-8 — The backfill is reviewer-gated, and its audit log is a disclosure boundary

`apply_backfill` refuses without an owner/reviewer reference (`BackfillRefused`).
`plan_backfill` computes and writes nothing, so the plan is always safe to run.

Its `--apply` audit log is **individually gitignored (GOV-1637)** because **this repository is
public** and the log contains the before/after prose of real civic records. That entry is a
disclosure boundary, not tidiness — the same rule `CLAUDE.md` states for `Database/*.db` and
`Logs/`.

---

## 3. Changing this chain — the checklist

1. **Provenance fields are mandatory** (P-1). Adding one means deciding what existing rows
   hold, and `ADD COLUMN` is the one non-re-runnable statement (data-model INV-2).
2. **A `review_state` or transition change is a publication-safety change** (P-2). Say so in
   the PR and name who reviewed it.
3. **Do not "tighten" `NON_COUNTING_REVIEW_STATES` without deciding what a closed gap means**
   (P-3). Coverage and publishability are different questions.
4. **Check both cross-area ends** (P-7) — `beta/intake_api.py` and `file_read_api.py`.
5. Migrations follow `Docs/data-model-contract.md`: next free slot, `IF NOT EXISTS`,
   and re-derive the frozen-surface allowlist if a frozen surface reads the table.
6. Run the full suite.

---

## 4. Known gaps — named, not silently skipped

- **`stage3_verify_at_source_audit.py` (226 lines) is governed by nothing.** Its sibling
  `stage3_verify_at_source.py` is covered by `stage3-07`, which never names the audit module.
  Smaller than the B-series gap this file closes, and left open deliberately rather than
  guessed at.
- **`extract_metadata.py` (198 lines) appears only in a *gap-analysis* document**, which
  records a finding rather than governing behaviour. It has **zero** test references — the
  strongest C4 signal in this area.
- **`GOV-1566` itself is not an issue in this repo** (searched all states). The umbrella
  design exists only as the B-numbers in commit subjects.
- **No contract covers the ordering guarantees between B4 and B5** — whether a link may be
  created against a superseded version is currently answered only by the code.

[#206]: https://github.com/xXKillerNoobYT/Government-watchdog/issues/206
