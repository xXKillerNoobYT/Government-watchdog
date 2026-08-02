# AI-provenance integrity audit (GOV-278) — as-built contract (v1.0)

## 0. Document control

**Status:** as-built, written 2026-08-01. Every claim below was measured at `ecb6bd0`
(the GOV-1706 branch, one commit ahead of `main` @ `6e1fed2`) in the session that wrote it; where a claim is checkable, the guard that checks it
is named, and where no guard exists that is said out loud rather than glossed.
**This describes what IS, not what should be.**

**Why it exists.** `scripts/ai_provenance.py` is named by **zero contracts**. Measured: each of
the four contracts bound to area `ai-boundary` —
`stage1-automation-ai-boundary-matrix-contract.md`, `stage3-ai-gateway-lane2-implementation.md`,
`stage3-ai-gateway-lane3-implementation.md`, `stage3-ai-gateway-lane4-5-implementation.md` —
contains **0** occurrences of the string `ai_provenance`. The only file in `Docs/` that names
`ai_provenance.py` with its extension is `auto-go-heartbeat.md`, and it names it twice: once in
the area's `paths:` list, and once in a comment saying the module *"IS claimed and is governed
by NO contract"*. So a 197-line module that decides whether the corpus is clean enough to ship
had its entire design living in a module docstring.

**Grep for the filename, not the stem.** A stem-only search for `ai_provenance` returns
`read_api._ai_provenance_ok` — a *different function in a different module* — and reads as
coverage. That near-miss is already recorded in `auto-go-memory.md`; it is repeated here because
this document is where someone will come looking.

**The module has one commit.** `git log -- scripts/ai_provenance.py` returns exactly one entry:
`43aefa0` (GOV-278, PR #50). It has not been edited since it landed. Nothing below describes
drift; it describes an original that nobody has had reason to revisit.

**Do not confuse the three things named "AI provenance."**

| | what it is | posture |
|---|---|---|
| `statements.insert_statement` / `resolve_ok_run` | the **write-time** gate — rejects an AI row without an `ok` run | raises `AiProvenanceError` |
| `ai_provenance.audit_ai_provenance` (**this module**) | the **read-only** auditor — proves the invariant *persisted* | returns a report; never writes |
| `read_api._ai_provenance_ok` | a **per-row** trust indicator on the serving lane | deliberately **stricter** — see A-9 |

---

## 1. Shape, measured

| Fact | Value |
|---|---|
| Module | `scripts/ai_provenance.py`, **197 lines** |
| Functions | `_table_exists`, `audit_ai_provenance`, `_format_report`, `main` (CLI) |
| Test file | `tests/test_gov278_ai_provenance.py` — **16 tests**, all passing (`3.04s`) |
| …of which exercise this module | **5** (`test_audit_*`, `test_real_db_has_no_provenance_orphans`) |
| Reads | `statements`, `ai_extraction_runs`, `evidence_links` (each existence-probed first) |
| Writes | **none** — 8 `conn.execute` calls, all `SELECT`; zero write verbs in the file |
| CLI flags | `--db` only. **No `--apply`**, because there is nothing to apply |
| Exit codes | `0` clean · `1` orphans found · `2` DB file not found |

The other **11** tests in that file guard the *write-time* half (`insert_statement`,
`resolve_ok_run`) — the sibling this module mirrors. The filename says `gov278`; only a third of
it points here. That split matters when reading §4: a change to the auditor has a much thinner
net under it than the test count suggests.

Coverage does not stop at that file. `stage2_traceability.audit_stage2_traceability` calls
`audit_ai_provenance` verbatim and folds its `clean` into a whole-corpus verdict, so
`tests/test_gov_stage2_traceability.py::test_ai_provenance_orphan_red` is a second, independent
guard (A-2, A-9). Measured baseline across the three provenance-touching test files
(`test_gov278_ai_provenance.py`, `test_gov_stage2_traceability.py`,
`test_gov311_provenance_status.py`): **49 passed**. That 49 is the denominator for every
mutation result quoted below.

### Measured cost — constant, not linear

The audit is seven set-based statements plus existence probes. Query count does **not** grow
with the corpus. Measured 2026-08-01 with a `sqlite3.Connection` subclass counting both
`execute` and `cursor` (the instrument that GOV-1704 proved you need — a wrapper on `execute`
alone is blind to `conn.cursor()` traffic):

| AI statements | evidence links | queries | ms |
|---|---|---|---|
| 1 | 1 | 8 | 1.8 |
| 10 | 10 | 8 | 1.6 |
| 100 | 100 | 8 | 1.7 |
| 1,000 | 1,000 | 8 | 4.0 |
| 5,000 | 5,000 | 8 | 12.9 |

`statements` has no index on `produced_by`, so every pass is a full `SCAN statements`
(measured via `EXPLAIN QUERY PLAN`). The join side is indexed for free: `run_id` is the
`ai_extraction_runs` PRIMARY KEY, and the planner reports
`SEARCH r USING COVERING INDEX sqlite_autoindex_ai_extraction_runs_1`. A full scan of a
five-thousand-row table in 13 ms is not a cost worth an index; a per-row lookup inside a
per-row loop would be, and there isn't one.

### The value space the audit reasons over

`ai_extraction_runs.error_status` is `TEXT NOT NULL DEFAULT 'ok'` under
`CHECK (error_status IN ('ok', 'partial', 'failed'))`. "Non-ok" is therefore exactly
`{partial, failed}` and cannot be anything else — storage enforces it. Likewise
`statements.produced_by` is under `CHECK (produced_by IN ('automation', 'ai', 'human'))` with
BINARY collation, so the `produced_by='ai'` predicate cannot miss an `'AI'` row: no such row can
be stored. Both facts are why the module can compare literals in SQL and stop there.

---

## 2. Invariants

### A-1 — `clean` is exactly `orphan_count == 0`, and only three things are orphans

`orphan_count = len(null_run) + len(unresolved_run) + len(unresolved_evidence_run)`, and
`clean = orphan_count == 0`. Nothing else enters that arithmetic. The report carries a fourth
list, `non_ok_run`, that is deliberately **outside** it — see A-6.

This is the whole shape of the module's judgement, and it is what `stage2_traceability` and the
CLI exit code both hang off.

Guard: mutating the report so `clean` is unconditionally `True` turns **2 of 49** red —
`test_audit_detects_raw_sql_bypass_orphans` and `test_ai_provenance_orphan_red`.

### A-2 — An AI statement with a NULL or blank run id is a **hard** orphan

`SELECT statement_id FROM statements WHERE produced_by='ai' AND (ai_extraction_run_id IS NULL OR
trim(ai_extraction_run_id)='')`. The write-time gate rejects such a row, so its presence means
something bypassed the writer — the FK permits NULL, and SQLite enforces no FKs at all unless a
connection opts in with `PRAGMA foreign_keys = ON`. A raw `sqlite3.connect(...)` load path plants
this and nothing in the database objects.

That is not hypothetical: `test_audit_detects_raw_sql_bypass_orphans` plants it exactly that way.

Guards: `test_audit_detects_raw_sql_bypass_orphans` **and**
`test_gov_stage2_traceability.py::test_ai_provenance_orphan_red`. Deleting the leg turns **2 of
49** red.

### A-3 — An AI statement whose run id does not resolve is a **hard** orphan

`LEFT JOIN ai_extraction_runs r ON r.run_id = s.ai_extraction_run_id … AND r.run_id IS NULL`,
restricted to non-NULL non-blank ids so A-2's rows are not double-counted. Guard:
`test_audit_detects_raw_sql_bypass_orphans` (plants `'ghost-run'`). Deleting the leg turns **1
of 49** red.

### A-4 — An evidence link whose run id does not resolve is a **hard** orphan — and nothing tests it

The same `LEFT JOIN … IS NULL` shape against `evidence_links`, feeding
`unresolved_evidence_run`, which counts toward `orphan_count` on equal footing with A-2 and A-3.
The code is correct; measured directly, a dangling evidence-link run id does produce
`clean=False`.

**It is unguarded.** Measured by mutation: replacing `unresolved_evidence_run` with `[]` and
recomputing `orphan_count` leaves **49 of 49 green**. No test in the repository plants a
dangling evidence-link run id; the string `unresolved_evidence_run` appears in the test suite
exactly once, inside the empty-DB equality assertion, where its value is `[]` either way.

This invariant is written here at the same rank as A-2 and A-3 because the *code* treats it
that way. See §4 for what it would take to make that true of the tests as well.

### A-5 — A missing ledger table fails **CLOSED**

If `ai_extraction_runs` does not exist (a pre-0009 database, or a partially migrated one), the
module does not skip the check and it does not assume innocence: every AI row carrying a
non-blank run id is unresolvable by definition and is reported as an orphan, and every
evidence-link run id likewise. `non_ok_run` is `[]` in that branch, correctly — with no ledger
there is no `error_status` to read, and inventing one would be the fail-open answer.

Measured on a hand-built pre-0009 schema: two AI statements (one with a run id, one NULL) plus
one evidence link with a run id →
`orphan_count=3`, `clean=False`, `non_ok_run=[]`. The docstring's "fail-closed when the ledger
table is absent" is accurate.

**No test covers this branch.** Named in §4.

### A-6 — A non-ok run is **soft**: reported, never suppressed, never `clean`-flipping

This is the distinction the module exists to draw, and it is the one most likely to be
"corrected" by someone who has not read this paragraph.

The write-time gate guarantees a run was `ok` at the moment its rows were written. A run can
*later* finalize `partial` — and the usual reason it finalizes `partial` is that the gate did its
job on the row's siblings and `orphan_rejected_count` went up. Retroactively condemning the rows
that passed would mean punishing the surviving output for the rejection of its neighbours. So a
`produced_by='ai'` row whose run **resolves** but is now `partial`/`failed` is emitted in
`non_ok_run` for reviewer visibility and is **not** an orphan.

The `non_ok_run` query is an INNER `JOIN`, which is what keeps the two categories disjoint: a
row can only be non-ok if its run resolved, and a row can only be an orphan under A-2/A-3 if its
run did not. No row is ever in both.

Guard: `test_audit_reports_non_ok_run_as_info_not_orphan` — and it guards both directions.
Measured: making non-ok rows count toward `orphan_count` turns **1 of 49** red; separately,
dropping `non_ok_run` from the report entirely also turns **1 of 49** red. The distinction is
tested as a distinction, not merely as an outcome.

### A-7 — The module never writes — measured, but by **discipline**, not by construction

The docstring says "read-only by construction." Checked against the code, that overstates the
mechanism while getting the outcome right, and the difference is worth knowing before someone
relies on the stronger reading.

What is true: all 8 `conn.execute` calls are `SELECT`; the file contains no `INSERT`, `UPDATE`,
`DELETE`, `CREATE`, `DROP`, `ALTER`, `commit()`, `executescript`, or file `open()`. Measured
end-to-end on a real migrated DB, `audit_ai_provenance` and the full `main()` CLI each leave the
file **byte-identical** (same SHA-256, same size, same `st_mtime_ns`) and create no `-wal` or
`-shm` sidecar — the schema leaves `journal_mode=delete`, so reads touch nothing.

What is *not* true: nothing structurally prevents a write. `db.open_db` is
`sqlite3.connect(path)` — an ordinary read-write handle, not `file:…?mode=ro`. `main()` wraps it
in `with db.open_db(...)`, which is sqlite3's *transaction* context manager: it commits on exit
(a no-op here) and does **not** close the connection — measured, the handle is still usable
afterwards. The read-only property holds because the SQL is read-only, and it would stop holding
the moment someone adds a statement that isn't. That is the thing to preserve.

**No test asserts it.** Named in §4.

### A-8 — The exit code is the gate

`main` returns `0` when `clean`, `1` when not, and `2` when `--db` names a file that does not
exist. All three measured. The `2` path matters more than it looks: `sqlite3.connect` on a
missing path **creates a zero-byte database** (measured), which would then audit as trivially
clean — a green light produced by the absence of any data at all. The `args.db.exists()` check
in front of `open_db` is what stops that, and it was measured not to create the file.

**No test covers `main`.** Named in §4.

### A-9 — The rule is mirrored, not re-invented — and the serving-lane mirror is deliberately stricter

`stage2_traceability` imports this module and calls `audit_ai_provenance(conn)` **verbatim**,
lifting `ai_statement_count`, `orphan_count`, `non_ok_run` and `clean` into its own report and
`AND`-ing that `clean` into a seven-check corpus verdict. There is no second copy of the orphan
rule. The soft/hard split survives the trip: `stage2_traceability` carries `non_ok_run` through
for display and bases its verdict on `clean`, so an informational run does not turn the corpus
audit red either.

`read_api._ai_provenance_ok` is the one place a *different* answer is given on purpose. For the
GOV-311 per-row trust indicator, an AI row whose run is `partial`/`failed` reads `unverified` —
stricter than this auditor, which calls that row informational. Both are right for their
surface: a corpus-integrity verdict should not go red because a run finalized `partial`, and a
per-record trust badge should not read "grounded" when the run behind it is not healthy. The
divergence is stated in `read_api`'s own docstring; it is recorded here so that a future attempt
to "make them agree" is recognised as a behaviour change to two surfaces, not a cleanup.

Guards: `test_gov_stage2_traceability.py::test_ai_provenance_orphan_red` (the verbatim reuse —
red under the A-1 and A-2 mutations), `test_gov311_provenance_status.py::test_ai_provenance_ok_unit`
(the stricter mirror).

---

## 3. Changing this auditor — the checklist

1. **Adding a category to `orphan_count` tightens a shipping gate.** `stage2_traceability` folds
   `clean` into a corpus verdict and the CLI returns `1` on it. Say so in the PR, and expect
   previously green databases to go red.
2. **Do not promote `non_ok_run` into `orphan_count`** (A-6). It is the docstring's central
   distinction and it is tested in both directions; if the intent is really to condemn those
   rows, that is a policy change for GOV-233 §2.05, not a bug fix here.
3. **Keep the two "non-ok" definitions apart** (A-9). `read_api._ai_provenance_ok` being
   stricter is deliberate. Changing one to match the other changes what reviewers see.
4. **Keep every statement a `SELECT`** (A-7). The connection is read-write; the discipline is
   the whole guarantee, and no test will catch you.
5. **Keep the existence probe in front of `open_db`** (A-8), or a missing DB becomes a
   zero-byte DB that audits clean.
6. **If you touch the evidence-link legs, write the test first** (A-4) — mutation-tested, they
   are currently free to delete.
7. Run the full suite. CI (`.github/workflows/backend-tests.yml`) runs `pytest -q` on 3.12 and
   never invokes this CLI, so the tests are the only thing standing between a change and
   `main`.

---

## 4. Known gaps — named, not silently skipped

- **~~`unresolved_evidence_run` is UNGUARDED (A-4)~~ — CLOSED in the PR that landed this
  contract.** The gap was real, and was measured by mutation: dropping `len(unresolved_ev)` from
  `orphan_count` left **49 of 49 tests green**, so a third of the orphan definition was free to
  delete. `TestEvidenceLinkOrphansCountAsOrphans` now fails on exactly that mutation, and on the
  over-correction that flags every evidence link naming any run at all.

  **Writing the test corrected something this section had wrong.**
  `evidence_links.ai_extraction_run_id` carries a FOREIGN KEY to `ai_extraction_runs.run_id`, and
  `db.open_db` sets `PRAGMA foreign_keys=ON` — so through the normal handle the dangling row
  **cannot be inserted at all**, and the first version of the test died on that FK rather than on
  its assertion. That does not make the leg redundant, it names its real threat: **SQLite enforces
  foreign keys only when asked, and the default is OFF, per connection.** Anything reaching this
  DB with a plain `sqlite3.connect` writes with no enforcement, and such rows persist while
  resolving to nothing — precisely the "prove the invariant PERSISTED" job this auditor exists for.
  The test writes the way such a client would, and says so.

- **An AI-produced evidence link with a NULL run id is structurally undetectable.** The
  statement side can flag a blank run id because `produced_by='ai'` says which rows *should* have
  one. `evidence_links` has **no `produced_by` column** — measured against the migrated schema,
  its 30 columns do not include it. So the auditor can only ask "does this run id resolve?", never
  "should this row have had one?". A-2 has no evidence-link counterpart and cannot be given one
  without a schema change.

- **A missing `statements` table short-circuits the entire audit — the one fail-OPEN path.**
  The early return at the top of `audit_ai_provenance` yields the all-zero `clean=True` report
  and never reaches the evidence-link legs. Measured: a database with **no `statements` table**
  but an `evidence_links` row carrying a dangling run id returns
  `clean=True, orphan_count=0, unresolved_evidence_run=[]`. Everywhere else in this module
  table-absence fails closed (A-5); here it fails open. Stated rather than changed, because the
  fix is a behaviour change that deserves its own PR and its own red-first proof.

- **A-5's fail-closed ledger-absent branch has no test.** Verified by hand this session
  (`orphan_count=3` on a pre-0009 shape); nothing in the suite pins it, so it is free to
  regress to the fail-open answer.

- **On a pre-0009 `statements` table the auditor raises rather than reporting.** If `statements`
  exists but predates the `ai_extraction_run_id` column, the first orphan query raises
  `sqlite3.OperationalError: no such column: ai_extraction_run_id` — measured. That is
  fail-loud, which is the right direction, but it is an asymmetry worth knowing:
  `statements.resolve_ok_run` catches `OperationalError` for the same class of schema gap and
  returns `False`; this module does not catch it at all.

- **`trim()` and Python's `strip()` disagree about whitespace, so a run id can land in the wrong
  bucket.** SQLite's `trim(X)` strips ASCII spaces only — measured: `trim(char(9))` returns a
  tab, not `''` — while the write gate's `_is_missing` uses Python `str.strip()`, which strips
  tabs, newlines and NBSP too. A tab-only run id planted by raw SQL is therefore **not** counted
  in `null_run_statement_ids`. It is still caught: measured, it falls through to
  `unresolved_run` (no ledger row has a tab for a `run_id`) and `orphan_count` is unaffected.
  The bug is one of *classification*, not of *detection*, and the fail-closed outcome holds.

- **`main` and `_format_report` have no tests at all.** Exit codes `0`/`1`/`2` and the entire
  human-readable rendering were verified by hand this session and are pinned by nothing. The
  exit code is the module's contract with any caller that treats it as a gate.

- **Nothing asserts the read-only property (A-7).** The byte-identity measurement in this
  document was taken by hand. A test that hashes the DB file before and after an audit would
  make the module's headline claim self-defending; there is none.

- **~~This contract is not yet bound to its area~~ — bound in this PR.** It is now the
  fifth entry in `ai-boundary`'s `contracts:` list. The heartbeat comment claiming this had
  already happened was written before it had; the discrepancy was caught by checking the
  list itself rather than trusting the comment describing it.
