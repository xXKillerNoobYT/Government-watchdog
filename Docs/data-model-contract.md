# Data model & schema evolution — as-built contract (v1.0)

## 0. Document control

**Status:** as-built, written 2026-07-31 (GOV-1679, AUTO GO C1 for area `data-model`).
Every claim below was measured against `main` @ `f4da543` before it was written; where a
claim is checkable, the check is named. **This is a description of what IS, not a proposal.**

**Why it exists.** `data-model` was bound to `docs/2026-deployment-portability-contract.md`,
which is a *deployment* contract — adapters, scale topology, backups, secrets. Its one
migration-shaped section (§7) is a **database export/restore drill**, not schema-evolution
discipline, and it is legitimately `deploy-release`'s. So this area had **no plan**, while
governing **31 migrations / 2,499 lines of SQL / 75 tables**. The rules did exist — scattered
across `CLAUDE.md`, `tests/test_migration_slots.py` and `scripts/db.py`'s docstrings — but
nothing said what the data model *is* or how it is allowed to change.

**Scope.** `Database/migrations/**` and `scripts/db.py`'s application machinery. Not: what
individual tables mean (that belongs to each feature's own plan), and not the SQLite↔Postgres
portability drill (DEPLOY-2026 §7).

---

## 1. Shape, measured

| Fact | Value | How to re-measure |
|---|---|---|
| Migrations | **31**, slots `0001`–`0031`, **contiguous, no gaps** | `ls Database/migrations/*.sql` |
| Tables created | **75** | `CREATE TABLE` count, comment lines excluded |
| Indexes created | **99** | `CREATE INDEX` / `CREATE UNIQUE INDEX` count |
| Applied-version ledger | `schema_migrations(version TEXT PRIMARY KEY, applied_utc TEXT NOT NULL)` | `scripts/db.py:82` |
| `version` value | the **filename stem**, e.g. `0025_accounts_cohorts_notifications` | `scripts/db.py:87` |

---

## 2. Invariants

### INV-1 — Slots are contiguous from 0001, and each is claimed once

`apply_migrations` runs `sorted(MIGRATIONS_DIR.glob("*.sql"))`. That is **alphabetical order on
the full filename**, which equals numeric order *only because* the prefix is zero-padded to four
digits. Drop the padding and `10_x.sql` sorts before `9_x.sql`.

Enforced by `tests/test_migration_slots.py`: filename convention, no two files sharing a slot,
contiguity from 0001, and next-free-slot discoverability.

**Contiguity is why a gap is not an escape hatch.** When slot *N* is contested, taking *N+1* does
not sidestep the collision — it fails `test_migration_slots_are_contiguous_from_0001`.

> **LIVE, as of 2026-07-31:** slot **0032 is claimed by two open PRs** — [#199] and [#132].
> `main`'s highest is 0031, so **both were correct when their branches were cut**; the collision
> is structurally invisible to per-PR CI, because every PR is tested against `main` and never
> against its siblings. It currently blocks [#217] (an `email_outbox` index) and [#160] (three
> new tables). Whoever renumbers unblocks both.

### INV-2 — Every statement is re-runnable, and `ADD COLUMN` is the one exception

`_apply_statement` (`scripts/db.py:62`) special-cases `ADD COLUMN` — SQLite has no
`ADD COLUMN IF NOT EXISTS`, so it checks the column first and skips. Its docstring states the
matching obligation for everything else: *"expected to use `IF NOT EXISTS` and is passed
through."*

**Measured: 75 of 75 `CREATE TABLE` and 99 of 99 `CREATE INDEX` comply. There are zero
violations today** — which is exactly why the guard went in now rather than after the first one
(`tests/test_migration_reruns.py`). A ratchet costs nothing while the count is zero and costs a
cleanup project afterwards.

### INV-3 — Foreign keys are ON, on every connection

`PRAGMA foreign_keys = ON` is set in **both** `apply_migrations` and `open_db`. SQLite defaults
it **off** per connection, so a connection opened any other way silently stops enforcing every
`REFERENCES` clause in the schema. Open databases through `db.open_db`.

### INV-4 — Changing a constraint means rebuilding the table

SQLite cannot `ALTER` a `CHECK`, a foreign key, or a column type. Two migrations therefore use
the rebuild pattern (`0009_ai_extraction_runs.sql`, `0016_evidence_link_char_span.sql`):

```sql
PRAGMA legacy_alter_table = ON;
DROP TABLE statements;
ALTER TABLE statements_new RENAME TO statements;
PRAGMA legacy_alter_table = OFF;
```

**The `legacy_alter_table` pragma is load-bearing and easy to omit.** With it OFF, SQLite
"helpfully" rewrites `REFERENCES` clauses in *other* tables to follow the rename — so tables that
deliberately name `statements` would be silently repointed at the scratch table. `0009`'s own
comment records this. **These two `DROP TABLE`s are a rebuild, not data destruction** — do not
read the grep hit as a destructive migration.

### INV-5 — A failed run is PARTIALLY applied, and INV-2 is what rescues it

> **CORRECTED 2026-07-31 (GOV-1680, C1b) — supersedes the original claim that a run is
> "all-or-nothing".** That was written from code structure (`with sqlite3.connect(...)`, one
> commit after the loop) and **never tested**. The test written to pin it disproved it.

`apply_migrations` commits once after the loop, inside a `with` block that rolls back on
exception — but **DDL is not covered by that rollback**. Python's `sqlite3` opens its implicit
transaction before **DML only** (the same rule as INV-2's neighbourhood, and the one that caused
GOV-1676's cohort race). `CREATE TABLE` is DDL, so it runs in **autocommit**.

The boundary is precise, and measured — three good migrations then a deliberately broken one:

| What | Outcome |
|---|---|
| `0001`'s tables (`documents`, `crawl_runs`, `meetings`, `embeddings`, …) | **survive** — DDL, no transaction open yet |
| `0001`'s `INSERT INTO schema_migrations` | **opens** the transaction (DML) |
| `0003`'s `sources`, and the broken migration's table | **rolled back** — inside the transaction |
| `schema_migrations` rows | **0** |

So a failed run leaves **the first migration's schema durably applied with an empty ledger**.
The retry therefore re-runs `0001` — and that is safe **only because of INV-2**: every statement
is `IF NOT EXISTS`. **INV-2 is not merely hygiene; it is what makes a failed run recoverable.**

Consequences worth stating: do not rely on a failed run leaving a clean database, and do not
"fix" this by moving the commit inside the loop — that would make *every* migration durable
independently and produce genuinely half-applied schemas with a ledger that agrees. The current
behaviour plus INV-2 is recoverable; that combination is the property to preserve.

### INV-6 — Four serving surfaces are byte-frozen, and they read this schema

`tests/test_deploy_frozen_surface.py` freezes `scripts/read_api.py`, `scripts/ai_risk_gate.py`,
`scripts/stage5_agenda_board.py`, `scripts/mcp_service/`. They read tables including
`statements`, `evidence_links`, `topics`, `agenda_items`, `agenda_threads`,
`speaker_attributions`, `concept_edges`, `completeness_gaps`.

**A migration that renames or retypes a column those surfaces read cannot be fixed by editing the
surface** — it is frozen. Either the migration stays additive, or the change needs an explicit
card that also re-derives the frozen allowlist. This is why `CLAUDE.md` pairs "renumber" with
"re-derive the allowlist in `tests/test_deploy_frozen_surface.py`".

### INV-7 — No migration may defeat the naive statement splitter

`db._statements` strips **full-line** `--` comments and then splits on `;`. Its docstring calls
this "adequate for the project's simple, trigger-free migration files" — and triggers really are
**0**. But the assumption is broader than that sentence, and two shapes break it:

| Input | What `_statements` returns |
|---|---|
| `CREATE TABLE t (id TEXT);  -- note; caveat` <br> `CREATE TABLE u (id TEXT);` | `['CREATE TABLE t (id TEXT)', '-- note', "caveat\nCREATE TABLE u (id TEXT)"]` — a bogus statement, **and the next real one corrupted by a prefix** |
| `CREATE TABLE t (c TEXT DEFAULT 'a;b');` | `["CREATE TABLE t (c TEXT DEFAULT 'a", "b')"]` — split mid-literal, both halves invalid |

**In both cases the error surfaces on the FOLLOWING statement**, not the line that caused it,
which is what makes this worth guarding rather than leaving to be debugged.

So: **an inline (end-of-line) `--` comment must not contain `;`, and no string literal may
contain `;`.** Measured when written: **zero** violations across all 31 migrations — every
`;`-bearing quote in the tree sits inside a full-line comment, which is stripped.
`tests/test_migration_reruns.py::test_no_migration_defeats_the_naive_splitter` enforces it.

**Why guard the corpus instead of hardening the splitter.** `_statements` runs on every
migration apply, making it the highest-blast-radius function in the repo; a subtle bug in a
cleverer splitter breaks every database build at once. A simple, obviously-correct splitter plus
an enforced precondition is the smaller system. If someone does harden it later, delete this
invariant and both its tests rather than keeping two mechanisms.

---

## 3. Adding a migration — the checklist this contract exists to make followable

1. **Read `Database/migrations/` on current `main`** and take the next free slot. Check open PRs
   too — `main` alone cannot show you a sibling branch's claim (INV-1's live example).
2. Zero-pad to four digits. `NNNN_short_snake_name.sql`.
3. Every `CREATE TABLE` / `CREATE INDEX` uses **`IF NOT EXISTS`** (INV-2, now guarded).
   No `;` in an inline `--` comment or a string literal (INV-7, guarded).
4. Prefer additive. If a constraint must change, use the rebuild pattern **with**
   `PRAGMA legacy_alter_table = ON` (INV-4).
5. If it touches a table a frozen surface reads, say so in the PR and re-derive the allowlist
   (INV-6).
6. Run the full suite. `tests/test_migration_slots.py` and `tests/test_migration_reruns.py` are
   the two that speak specifically to this area.

---

## 4. Known gaps — named, not silently skipped

- **`email_outbox` has no index at all**, so its pending sweep is a full scan plus a temp sort —
  14.16 ms at 100k rows against 0.01 ms indexed, measured, on a sweep finding *zero* pending
  rows. Blocked on the 0032 collision ([#217]).
- **No down-migrations exist**, by construction. Rollback is restore-from-backup (DEPLOY-2026
  §5), not schema reversal. Stated so nobody goes looking for a `down()`.
- **`schema_migrations` records the filename stem**, so renaming a merged migration file makes
  the ledger disagree with the directory and the migration re-applies. Do not rename a migration
  that has shipped.

[#199]: https://github.com/xXKillerNoobYT/Government-watchdog/pull/199
[#132]: https://github.com/xXKillerNoobYT/Government-watchdog/pull/132
[#217]: https://github.com/xXKillerNoobYT/Government-watchdog/issues/217
[#160]: https://github.com/xXKillerNoobYT/Government-watchdog/issues/160
