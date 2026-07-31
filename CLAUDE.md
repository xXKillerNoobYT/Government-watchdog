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
