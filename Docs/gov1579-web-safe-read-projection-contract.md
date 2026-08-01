# Web-safe supplied-file read projection (GOV-1579 / B6) — as-built contract (v1.0)

## 0. Document control

**Status:** as-built, written 2026-08-01 (GOV-1699, AUTO GO C1 for area `read-api`).
Every claim was measured against `main` @ `361c0e3` before it was written; where a claim is
checkable, the guard that checks it is named. **This describes what IS, not what should be.**

**Why it exists.** `scripts/file_read_api.py` is **the sole Backend→Website crossing for
supplied files** — and it was named by **none** of `read-api`'s six bound contracts. The only
document in `Docs/` that mentioned it at all was
`supplied-file-provenance-contract.md`, in three lines that *assign* it to this area and move
on; that contract's own scope section explicitly excludes **"the transport halves"**. So a
350-line module whose entire job is deciding what leaves the building was governed by nothing,
with its design living only in module docstrings.

The module is **well guarded** — 33 tests in `tests/test_gov1579_file_read_api.py` across nine
classes. What was missing is the document that names the invariants, so a reader can tell which
behaviours are load-bearing and which are incidental.

**Do not confuse the two modules.** `scripts/read_api.py` (972 lines) and
`scripts/file_read_api.py` (350 lines) are different things. The six contracts bound to this
area govern `read_api.py`. This one governs `file_read_api.py`, its supplied-file sibling.
`file_read_api` *imports* `read_api` and reuses its transport guard verbatim (W-7).

---

## 1. Shape, measured

| Fact | Value |
|---|---|
| Module | `scripts/file_read_api.py`, 350 lines |
| Public functions | `web_safe_files`, `supersede_views`, `build_files_response` (+ `_main` CLI) |
| Tests | `tests/test_gov1579_file_read_api.py` — **33**, nine classes |
| Allowlists | `WEB_SAFE_FILE_FIELDS` (13), `WEB_SAFE_LINK_FIELDS` (3), `WEB_SAFE_DIFF_FIELDS` (7) |
| Reads | `supplied_files`, `supplied_file_links`, `supplied_file_supersede_events` |
| Writes | **nothing** — read-only, stateless, no network listener |

`WEB_SAFE_DIFF_FIELDS` is **derived by subtraction**, not typed out:
`file_versioning.DIFF_FIELDS` (9) minus `{sha256, supplied_by}` = 7. A field added to B5's diff
is therefore web-safe by default — see the gap in §4.

---

## 2. Invariants

### W-1 — Only `web_safe` crosses, and the state is re-checked AFTER the SQL

The query filters `review_state = 'web_safe'`, and every row is checked again before projection
(`web_safe_files`). `pending`, `reviewing`, `held` and `rejected` are never served.

**The re-check is not redundant** — it is the house rule (`CLAUDE.md`: *review gates re-check
after SQL*) applied here. But note precisely *which* threat it answers: `review_state` is TEXT
with BINARY collation under a five-value `CHECK`, so **no stored row can satisfy the WHERE and
then compare unequal in Python**. Storage cannot lie here. The reachable threat is the other one
the module's docstring names — **a mis-typed query**.

Guards: `TestWebSafeStateGate` (4 tests — the *outcome*) **and
`TestStateGateSurvivesAQueryThatStoppedFiltering` (the *mechanism*)**. The second class exists
because C1b measured the first to be insufficient: **deleting the re-check outright left all 65
tests green**, since the WHERE clause still did the work and every state-gate test asserted the
outcome. It is now exercised by handing the projection a connection whose SELECT has lost its
filter — with a non-vacuity test proving the stand-in really does defeat the filter, so the
guard cannot pass merely because the SQL is still doing the job.

### W-2 — A supersede view requires BOTH versions `web_safe`

`supersede_views` emits a before/after only when the prior **and** the new record resolve and
are both `web_safe`. **An unreviewed version's metadata never crosses even as the "before" side
of a diff** — the case that is easy to miss, because the *new* version being cleared feels
sufficient. Guards: `TestSupersedeViews` — `omitted_when_new_version_not_web_safe`,
`omitted_when_prior_version_demoted`.

### W-3 — `sha256` and `supplied_by` are never SELECTed

Not stripped after reading — **never read**. `_WEB_SAFE_FILE_COLUMNS` (12) omits both, so the
values are not in memory to leak. `sha256` is B1's vault content-address (the `raw_sha256` class
the rulebook forbids on any web surface); `supplied_by` is the uploader's authenticated email.

**A value never read cannot be projected** is a stronger posture than filtering, and it is the
one to preserve: a future `SELECT *` would silently undo it. Guards: `TestServerSideStripping`,
including `db_row_still_has_the_stripped_fields` — which pins that the data is present in
storage and absent from the projection, i.e. that the *projection* is doing the work.

### W-4 — The structural allowlist RAISES; it is not an `assert`

`_assert_file_keys` and `_web_safe_links` raise `FieldLeak`. Despite the helper's name, **there
is no `assert` statement** — deliberately, because `python -O` deletes assert statements
outright and "a future edit adds a field" is exactly the scenario this exists for (GOV-1687).

Guards: `TestFieldAllowlist` — `assert_file_keys_rejects_extra_field`,
`allowlist_excludes_raw_and_pii_fields`.

### W-5 — The diff is RECOMPUTED, never read from stored `diff_json`

`_web_safe_diff` calls `file_versioning.compute_before_after` on the two records. The stored
`diff_json` carries `sha256` and `supplied_by`; reading it would reintroduce both after W-3 went
to the trouble of never selecting them. Guard: `diff_never_carries_sha256_or_supplied_by`.

### W-6 — `origin_url` must be a public web URL; `provenance_note` is deliberately NOT validated

`origin_url` survives only if `read_api._is_web_url` accepts it — a `file:///…vault…` provenance
URI is dropped. `provenance_note` is **free text, never a locator**, so it is emitted verbatim
and is *not* URL-checked. **That asymmetry is the design, not an oversight**: GOV-1625 split the
column precisely so prose could stop pretending to be a URL. The transport sweep (W-7) is what
catches a note that carries a vault path. Guards: `TestProvenanceNoteProjection` (7 tests),
`TestNoLeak.vault_origin_uri_is_dropped`.

### W-7 — The whole assembled body is transport-swept, with the guard REUSED not re-typed

`build_files_response` returns `read_api.assert_no_raw_paths(response)`. Every leaf is already
web-safe by W-3/W-4; this is the backstop that fails **loudly** at the boundary if a field were
mis-allowlisted.

**It is imported from `read_api`, never re-implemented.** A second, divergent copy of the raw-marker
list is the real risk — the same reason `backfill_provenance_note` reuses the live intake
predicate rather than restating the URL rule. Guard: `transport_sweep_runs_on_whole_body`.

### W-8 — Supersede ordering is `created_at, rowid`, never `event_id`

`event_id` is `secrets.token_hex` — random. `created_at` is millisecond-granular, so an
`event_id` tie-break served same-millisecond supersedes in a **random order** (GOV-1652 / #177).
`rowid` is arrival order. Guard: `TestDeterminism`; see also
`tests/test_audit_read_order_determinism.py`.

### W-9 — The projection is byte-deterministic: same DB → identical bytes

No `asOf`, no `generatedAt` — both are stamped by the build/export layer and kept **out** of
this module on purpose, so the projection can be compared byte-for-byte in round-trip tests.
`dataOrigin` is the static `reviewed_snapshot`, which is true by construction: this projection
only ever serves reviewer-cleared files.

---

## 3. Changing this projection — the checklist

1. **Adding a field to `WEB_SAFE_FILE_FIELDS` is a publication-safety change.** Say so in the
   PR and name who reviewed it. The allowlist is the disclosure boundary.
2. **Never add `sha256` or `supplied_by` to `_WEB_SAFE_FILE_COLUMNS`** (W-3), and never switch
   to `SELECT *`.
3. **Never read `diff_json`** (W-5).
4. **Do not URL-validate `provenance_note`** (W-6) — and do not remove the transport sweep that
   makes that safe (W-7).
5. **Check the other cross-area end**: `scripts/beta/intake_api.py` (access-gate) — see
   `Docs/supplied-file-provenance-contract.md` P-7. A `review_state` or field change can break a
   surface neither area fully owns.
6. Run the full suite.

---

### W-10 — There are TWO web-safe allowlists, and this one serves a field the other calls unsafe

Measured 2026-08-01 (GOV-1703, C8). Two independent families govern what crosses:

| | governs | checked against the other? |
|---|---|---|
| `publication.WEB_SAFE_FIELD_ALLOWLIST` / `WEB_UNSAFE_FIELDS` | statements / cards (the SSOT) | — |
| this module's four sets | supplied files | **no — `file_read_api` does not import `publication`** |

The overlap is exactly one field: **`review_state`**, which the SSOT names web-**unsafe**.

**It does not leak, and the reason is precise rather than lucky.** W-1 filters to `web_safe` and
re-checks after the SQL, so every projected card carries the *same* value — measured across all
five review states: 1 of 5 projected, one distinct value. **A constant carries no information.**

**But that puts the exemption's safety entirely on W-1.** The structural allowlist is the second
line of defence, and for this one field it has been opened — so if the state gate regressed, the
allowlist would not object. Guarded accordingly by
`TestAllowlistsAgreeWithTheNamedUnsafeSet`, which pins the divergence to its single reviewed
exception **and separately asserts the constancy that justifies it**, so the reason is tested and
not merely written down here.

---

## 4. Known gaps — named, not silently skipped

- **`WEB_SAFE_DIFF_FIELDS` is fail-OPEN by construction.** It is `DIFF_FIELDS` *minus* a
  denylist of two, so **a new field added to B5's `DIFF_FIELDS` becomes web-safe automatically**,
  with no review. Everything else in this module is fail-closed via allowlist; this one place
  inverts it. Stated rather than changed here, because flipping it to an allowlist is a
  behaviour change that deserves its own PR and its own red proof.
- **`read_api.py` is byte-frozen and this module imports it.** `_is_web_url` and
  `assert_no_raw_paths` therefore cannot be changed to suit this projection — correct, but worth
  knowing before planning a change that seems to need it.
- **Six of the fourteen modules bound to `read-api` are byte-frozen** (`read_api`, `publication`,
  `statements`, `stage4_newsletter_feed`, `stage4_newsletter_digest_assembler`,
  `stage5_agenda_board`) — nearly half the area's surface, by two different mechanisms. See
  `CLAUDE.md`, corrected 2026-08-01.
- **No contract covers the *consumption* side.** `export_web_artifact` bakes this output into a
  web artifact, and artifact publication is gated on issue #123's immutability work.
