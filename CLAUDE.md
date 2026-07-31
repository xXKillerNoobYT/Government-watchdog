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

**Four serving surfaces are frozen** (`tests/test_deploy_frozen_surface.py`):
`scripts/read_api.py`, `scripts/ai_risk_gate.py`, `scripts/stage5_agenda_board.py`,
`scripts/mcp_service/`. Changes are additive elsewhere; these stay byte-0 against
`origin/main` unless a card explicitly says otherwise.

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
