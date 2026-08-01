# Government Watchdog — backend

Operating rules for anyone working in this repository. Every statement here was
verified against the tree rather than remembered; where a claim has a date, that
is when it was last measured.

> **This repository is PUBLIC.** Anything committed is disclosed. That is why
> `Database/*.db` and every `Logs/` path are individually gitignored *with a
> stated reason* — those entries are a disclosure boundary, not tidiness. Never
> commit evidence databases, run logs, vault contents, keys, tokens, or civic
> PII. Reference where a secret lives; never its value.

## Environment

- **Python 3.12 is required, not preferred.** The suite uses PEP-701 nested
  f-strings; 3.11 fails on `scripts/stage2_traceability.py` (GOV-576/579), and
  CI hard-pins 3.12 for the same reason.
- A `.venv/` at the repo root is gitignored (`.gitignore:1`). Create or reuse it:
  ```bash
  python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
  .venv/bin/python -m pytest tests/ -q      # the FULL suite, not a subset
  ```
- The suite is ~2,100 tests and runs in roughly a minute. Run all of it; the
  cross-module guards below only fire on a full run.

## Things that will surprise you

- **`Docs/` is the canonical directory case — capital D.** `docs/` resolves to
  the same place only because macOS APFS is case-insensitive. `git ls-files`
  records `Docs/`, so a case-sensitive checkout would break every lowercase
  path. Write new files as `Docs/…`.
- **The `docs/auto-go-*.md` trackers are LOCAL-ONLY**, excluded via
  `.git/info/exclude` (not `.gitignore`). They do not exist in a fresh clone or
  on CI, and `.git/info/exclude` is itself per-working-copy — so tracker state
  is not shared between the two backend clones.
- **Docs-only PRs get no CI run.** `.github/workflows/backend-tests.yml` filters
  on `scripts/**`, `tests/**`, `Database/migrations/**`, `requirements.txt`,
  `pytest.ini`, and the workflow file. A `Docs/`-only PR reports "no checks
  reported" and `mergeStateStatus: CLEAN`. That is expected — verify such a PR
  by running the suite locally and saying so.
- **`pytest.ini` is load-bearing and in the CI path filter on purpose.** It
  carries the resource-leak ratchet (below); an edit that drops it must re-run
  the suite rather than silently skip CI.
- **A `UNIQUE` constraint IS an index.** `auth_sessions.token_hash`,
  `users.email` and `consent_preferences.unsubscribe_token` have no
  `CREATE INDEX` line and are all indexed, via `sqlite_autoindex_*`. Audit
  coverage with `EXPLAIN QUERY PLAN`, never by grepping `CREATE INDEX` — that
  under-reports and invites a "fix" for a problem that does not exist.
  (`email_outbox` genuinely has no index; that one is real, and blocked on a
  migration slot — [#217].)
- **…but a `REFERENCES` clause gets NOTHING.** SQLite auto-indexes `UNIQUE`
  and does *not* auto-index foreign keys, so an unindexed child column makes
  every parent `DELETE` — and every `ON DELETE CASCADE` — full-scan the child
  table. **22 of 70** FK child columns are unindexed and that is currently
  **free**: all of `scripts/` holds exactly one `DELETE FROM`, against a table
  nothing points at. Do not "fix" this by adding 22 indexes. The precondition
  is guarded instead —
  `test_no_shipped_delete_targets_a_parent_with_unindexed_children` fails on the
  first `DELETE` against a pointed-at table. See INV-8.

- **`Path(root) / value` DISCARDS `root` when `value` is absolute.**
  `Path("/repo") / "/etc/passwd"` is `/etc/passwd` — no error, no warning. Every
  stored-path column in this repo is *intended* to be repo-relative, and that
  intent was enforced at the write sites and checked at **none** of the read
  sites until GOV-1693. If a stored value is ever absolute, the join silently
  reads outside the repo. Containment belongs at the consumer:
  `candidate.resolve().is_relative_to(root.resolve())`, raising rather than
  clamping.

[#217]: https://github.com/xXKillerNoobYT/Government-watchdog/issues/217

## Concurrency and irreversible actions

**`sqlite3`'s implicit transaction covers DML only.** Under
`LEGACY_TRANSACTION_CONTROL` — the Python 3.12 default, which this repo uses
everywhere — the module opens a transaction before `INSERT`/`UPDATE`/`DELETE`
and **never before a `SELECT`**. So in *"read the count → decide → write"*, the
read runs in **autocommit holding no lock**, and the transaction begins at the
write, after the decision was already made. Two connections both read "one slot
free" and both write.

Any invariant enforced by reading-then-writing needs an explicit
`conn.execute("BEGIN IMMEDIATE")` **before the first read**. Wrapping only the
write is insufficient *and looks correct*: `accounts/cohorts.py` carried a
comment asserting it was in-transaction for weeks, and no single-connection test
could tell the difference. Proving a concurrency property takes a two-connection
barrier — cheap, ~40 lines with `threading.Barrier` and a **file-backed** DB
(in-memory connections do not share).

**When the contended thing is ONE row, put the atomicity in the `WHERE` clause
instead.** A single-winner conditional update needs no `BEGIN IMMEDIATE`,
because the condition and the write are one statement:

```python
cur = conn.execute("UPDATE t SET status = 'x' WHERE id = ? AND status = 'pending'", ...)
if cur.rowcount != 1:
    ...  # someone else won; treat as already taken
```

Used by the magic-code single-use guard (`beta/tokens.py`) and the outbox claim.
Reach for `BEGIN IMMEDIATE` only when the decision must read *other* rows — a
`COUNT` against a cap.

**`email_gateway.outbox` is at-most-once, and the claim is load-bearing.** A
send is irreversible: an over-admitted cohort member can be corrected in the
database, a delivered email cannot be undelivered. Each row is claimed —
`status='failed'`, **committed** — before `adapter.send()` is called, and moved
to `sent` only after the adapter returns. A crash therefore leaves a **visible
stuck row** instead of silently re-delivering. Do not remove the claim, and do
not move the commit back after the loop; that is exactly the shape that
delivered duplicates. The commit between claim and send matters only on *hard*
process death, so it is pinned by a subprocess test using `os._exit`.

**`is not None` is not "is truthy", and credentials are where it bites.**
`password=""` is not `None`, and `PasswordHasher().hash("")` returns a perfectly
valid argon2 string — so an empty password briefly became a **working
credential**. `create_user` and `set_password` now refuse it explicitly
(`InvalidPassword`), and `set_password` raises `UnknownUser` rather than
silently no-op'ing on an unknown id. When a sentinel means "absent", decide what
the *empty* value means too.

## Rules with teeth

**Migrations take the next free slot only.** Check `Database/migrations/` on
current `main` first. `tests/test_migration_slots.py` enforces it and names both
files on a collision. A collision means renumber, update every reference, **and
re-derive the allowlist in `tests/test_deploy_frozen_surface.py`**.

**Before writing a migration, read `Docs/data-model-contract.md`.** It is the
as-built contract for `Database/migrations/**` — eight numbered invariants, each
with the guard that enforces it, plus a checklist for adding one. Three of them
you will otherwise violate without any error telling you so: **INV-4** (changing
a constraint means rebuilding the table, and the rebuild needs
`PRAGMA legacy_alter_table = ON` or SQLite silently repoints other tables'
`REFERENCES` at your scratch table), **INV-7** (`_statements` splits on `;` after
stripping full-line comments, so a `;` inside an inline comment or a string
literal corrupts the *following* statement), and **INV-5** (a failed run is
**partially** applied — `CREATE TABLE` is DDL and runs in autocommit, outside the
rollback; it is recoverable only because every statement is `IF NOT EXISTS`).

**Eight paths are byte-frozen against `origin/main`, by TWO different mechanisms**,
and only the first is easy to find. Changes are additive elsewhere; these stay
byte-0 unless a card explicitly says otherwise.

*Central list* — `FROZEN` in `tests/test_deploy_frozen_surface.py` (and a subset in
`tests/test_mcp_frozen_surface.py`): `scripts/read_api.py`,
`scripts/ai_risk_gate.py`, `scripts/stage5_agenda_board.py`, `scripts/mcp_service/`.

*Scattered per-test assertions* — a `git diff origin/main -- <path>` assertion
inside an unrelated stage test, naming no central list:
`scripts/publication.py` (pinned by **seven** separate test files),
`scripts/stage4_newsletter_feed.py` (three),
`scripts/stage4_newsletter_digest_assembler.py`, and `scripts/statements.py`.

**The second mechanism is the one that bites**, because nothing announces it. An
edit to `publication.py` fails seven tests in files whose names do not mention it,
and `git grep publication.py tests/test_deploy_frozen_surface.py` finds nothing.
This list was wrong until 2026-08-01: it read *"four serving surfaces"* and named
only the central four, while `test_claude_md_lists_exactly_the_frozen_surfaces`
reported green — because it compared this file against `FROZEN`, and **both lists
were incomplete in the same way.** Two lists agreeing is not ground truth. The
guard now derives the set from the assertions the suite actually makes.

**Leaked resources fail the suite.** `pytest.ini` promotes `ResourceWarning`
**and** `PytestUnraisableExceptionWarning` to errors. Both lines are required —
`error::ResourceWarning` alone is inert, because the warning fires during garbage
collection where no exception can propagate and pytest re-emits it under the
other class. An HTTP-server fixture must call `server_close()`, not just
`shutdown()`; `shutdown()` stops the serve loop and leaves the listening socket.

**A `DeprecationWarning` from OUR OWN modules also fails the suite.**
`tests/conftest.py` installs `error::DeprecationWarning` scoped to a module regex
**derived from `scripts/` at run time**, so a module added tomorrow is covered
without anyone updating a list. Third-party deprecations stay warnings *on
purpose* — CI must not break on someone else's release schedule. If a new module
suddenly fails on a deprecation, that is this working, not a bug. One known gap,
left open deliberately: a module-level `warnings.warn(..., stacklevel=2)` is
attributed to the *importing* test module and escapes the filter.

**Both loopback HTTP servers are threaded with a request timeout.**
`beta/http_api.py` and `notifications/http_api.py` each use
`ThreadingHTTPServer` + `daemon_threads` + a 15s handler timeout, because a
plain `HTTPServer` serves one request at a time and **one client that opens a
socket and goes silent denies the whole API** — measured at 6s on both. Safe
only because each handler opens and closes its own sqlite connection per
request. `beta/intake_api.py` is deliberately **not** threaded: its handler
closes over a `RawObjectStore` whose `_append_link` appends to a shared ledger
file that is not established as thread-safe ([#206]).

[#206]: https://github.com/xXKillerNoobYT/Government-watchdog/issues/206

**Fail-closed is the house style.** Unknown keys deny. Review gates re-check
after SQL. Ambiguity is a refusal, not a guess. On a gated surface the **flag
check goes first and every other answer comes after it** — replying before the
gate (even an error) tells a prober the route exists.

## Access gate (`scripts/beta/`) — read the contract first

`Docs/gov801-access-gate-contract.md` is the as-built contract: five front-door
routes, five tables, eight numbered invariants, the data flow, and §7's
**two-lane boundary**. Read it before changing anything under `scripts/beta/`.

The one thing that catches everyone: **two access lanes exist.** `beta`
(migrations 0026/0027 — email identity, 7-day cookie `gw_beta_session`) and
`accounts` (0025 — uuid identity, 24-hour bearer, `access_grants` tier). A
verified beta sign-in now provisions an accounts row, so identity is unified —
but the **transport** half is still open ([#192]). `accounts.gate` remains the
single civic-data gate.

[#192]: https://github.com/xXKillerNoobYT/Government-watchdog/issues/192

## Supplied-file provenance (`file_records` / `file_linkage` / `file_versioning`)

`Docs/supplied-file-provenance-contract.md` is the as-built contract for the
GOV-1566 B-series — nine numbered invariants, the review-state map, and the
measured query plans. Read it before touching a supplied-file table. It exists
because **B1 got a spec and B2–B6 got none**: 1,460 lines whose design lived only
in module docstrings and eight merged PR bodies.

Two invariants bite silently, and both are the kind a careful reader "fixes":

- **P-3 — the `no_primary_source` gap tracks COVERAGE, not publishability.**
  `NON_COUNTING_REVIEW_STATES` contains **only `rejected`**, so a `pending`,
  entirely unreviewed file *closes* the gap. That reads fail-open in a fail-closed
  house and is not: the gap asks *"does this subject have a source at all?"*, not
  *"may we publish it?"* Publishability is a separate gate — the web-safe read
  projection. Widening the default silently redefines "gap closed" across the
  whole completeness surface.
- **P-7 — the chain spans THREE areas, and this one owns neither end.** Intake is
  `scripts/beta/intake_api.py` (access gate); the read projection is
  `scripts/file_read_api.py`, which is **the sole Backend→Website crossing**. A
  field rename or a `review_state` change here can break a surface this area does
  not own. Check both ends before shipping.

## Working agreements

- **Never commit to `main`.** Branch, push, open a PR.
- **Nothing outward-facing without the owner** — no deploy, publish, or send.
  Merging is not deploying, but publishing is.
- **A guard is not shipped until it has been observed failing.** Break the thing
  it exists to catch, watch it fail, restore. Green after a fix proves the code
  and the guard agree; it does not prove the guard would object if they stopped.
  Mutate the *over-correction* too — a fix that disarms the feature entirely can
  still satisfy a regression-only test.
- **Several agents work this repo.** Check open PRs and recent branches before
  starting; if one owns the work, leave a coordinating comment rather than
  duplicating. Two open PRs can both be correct and still collide — compare
  their shared-resource claims (migration slots, allowlists, route constants).
