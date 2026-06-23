# Stage 5.03 — Source/Data Inventory Contract (GOV-484)

*Reviewer-internal · Alpine-only. Public launch stays gated on Isaac via GOV-420.*

**Owner:** SourceArchivist · **Parent framework:** GOV-483 §B (premium template), §C
(slice map) · **Parent spec:** GOV-476 · **Stage 5 goal:** `9d3d7fbd` · **Slice goal:**
`6f3df42f`.

This is the Stage-5 analog of the Stage X.03 inventory-contract slices (Stage 4.03
`stage4-... newsletter source-data-inventory contract`; Stage 3.03
`stage3-03-source-inventory-contract.md`). It defines the **data + behavior
contract** that lets the crawler/tooling represent a source's **lifecycle state**
(changed / disappeared / replaced / unchanged) and its **archive availability near
the original scan date**, as a deterministic projection of the existing
reviewer-internal registry — no crawl, no mutation, no AI, no network.

Reference module: [`scripts/stage5_source_inventory.py`](../scripts/stage5_source_inventory.py).
Reference driver: [`tests/test_stage5_source_inventory.py`](../tests/test_stage5_source_inventory.py).

---

## §0 — Scope lock

- **Alpine only.** Broader jurisdictions stay *planned*, never implied (COMPANY
  non-negotiable; GOV-476 §4 linear backbone).
- **Reviewer-internal only.** Every emitted artifact is tagged
  `access: reviewer_internal` / `scope: alpine`. No public / `published_records`
  path emits any Stage-5 inventory key.
- **Additive, no fork.** This is a *separate additive module* layered on top of the
  already-web-safe Stage-3.03 source inventory
  (`stage3_source_inventory.source_inventory`). It never calls `to_web_safe`,
  never mutates `read_api.py` / `publication.py` / `stage3_source_inventory.py`.

---

## §1 — Source lifecycle record shape

For every registered source the inventory emits the Stage-3.03 web-safe entry
(every flat field already in `publication.WEB_SAFE_FIELD_ALLOWLIST`, plus the
derived `coverage` envelope) **plus** a derived `lifecycle` envelope:

```jsonc
"lifecycle": {
  "state": "unchanged | changed | disappeared | replaced",
  "evidence": {
    "sourceChangedFlag":   true | false,    // sources.source_changed (0/1)
    "changeSignal":        "source_changed" | null,
    "disappearanceSignal": "source_missing" | "unavailable" | null,
    "replacementSignal":   "replaced" | "superseded" | null
  }
}
```

### §1.1 Lifecycle states (frozen SSOT — `SOURCE_LIFECYCLE_STATES`)

| State          | Meaning                                                              |
|----------------|---------------------------------------------------------------------|
| `unchanged`    | Source still retrievable and content stable since `scan_date`.      |
| `changed`      | Source still present, but content changed since `scan_date`.        |
| `disappeared`  | Source no longer retrievable at its original locator.               |
| `replaced`     | Source was superseded/replaced by a successor document.             |

### §1.2 Derivation (deterministic, fail-closed, conservative)

Computed at read time from existing registry columns only. Precedence is
**most-degraded-state-wins** so the inventory never optimistically claims
`unchanged` while a degradation signal is present:

1. **`disappeared`** — `verification_status == "source_missing"` **OR**
   `archive_status == "unavailable"`.
2. **`replaced`** — `correction_status ∈ {"replaced", "superseded"}`.
3. **`changed`** — `source_changed == 1` **OR**
   `verification_status == "source_changed"`.
4. **`unchanged`** — none of the above (the default *only* when no degradation
   signal exists).

The signal columns (`source_changed`, `verification_status`, `correction_status`,
`archive_status`) are **read to derive the label, never emitted raw**. An
`evidence.*` field echoes a value **only when it is a member of the frozen trigger
set** — an arbitrary/poisoned free-text status is never echoed (it falls through to
`null`). Same DB → byte-identical label (idempotent).

---

## §2 — Archive-availability record (keyed to original scan date)

For every source the inventory emits a derived `archiveAvailability` envelope
anchored on the immutable original `scan_date`:

```jsonc
"archiveAvailability": {
  "scanDate":          "YYYY-MM-DD" | null,   // sources.scan_date (immutable as-of)
  "archiveStatus":     "not_checked | available | unavailable",
  "snapshotAvailability": "available_near_scan | not_available | not_checked",
  "nearestSnapshotRef": "https://web.archive.org/web/...." | null
}
```

### §2.1 Rules

- `nearestSnapshotRef` is a **reviewer-internal pointer/marker** — the public
  Wayback web URL (`sources.archive_url`) when it is a genuine `http(s)://` URL,
  else `null`. It is **never** a raw fetched path / vault path / `file://` URI; a
  non-web archive URL is dropped by `read_api._strip_non_web_urls` upstream and a
  leak fails loudly in the §4 transport sweep.
- `archiveStatus` is clamped to the frozen `_ARCHIVE_STATUS_VOCAB`; an unknown
  value fails closed to `not_checked` (drift is never surfaced as availability).
- `snapshotAvailability` honesty label:
  - `available_near_scan` — `archiveStatus == "available"` **AND** a public
    `nearestSnapshotRef` exists;
  - `not_available` — `archiveStatus == "unavailable"`;
  - `not_checked` — otherwise (the conservative default).

---

## §3 — Envelope

```jsonc
{
  "scope":   "alpine",
  "access":  "reviewer_internal",
  "sources": [ /* one entry per registered source, order (source_class, source_id) */ ],
  "inventoryDigest": "<64-hex sha256 over the canonical sources list>"
}
```

- One entry per registered source (1:1 with the registry; a seed-only / disappeared
  source is **never hidden**).
- `inventoryDigest` is the **single** envelope digest (I3): exactly one 64-hex hash
  in the whole body, computed over the *already-web-safe* sources list. **No
  per-source raw-content hash** (`raw_sha256` is never SELECTed).
- Deterministic order `(source_class, source_id)` → same DB yields a byte-identical
  body (idempotent re-projection).

---

## §4 — Boundary rules (I1–I8 instantiated)

| Invariant | Rule in this contract |
|-----------|------------------------|
| **I1** zero raw-path/vault/PII leak | A planted vault path / `..` / 64-hex / email / phone / `file://` in any read column never reaches a served record; `read_api.assert_no_raw_paths` sweeps the whole body and fires loudly. |
| **I2** `localSourcePath` null | No raw locator column (`raw_local_path` / `raw_sha256` / `local_note_path` / `notes` / `owner_agent`) is ever SELECTed or emitted; raw locators stay backend-only. |
| **I3** envelope-only digests | Exactly one hash — top-level `inventoryDigest` — is exposed; no per-source raw-content hash. |
| **I4** byte-0-diff on prod lanes | `read_api` + `publication` are imported read-only and never mutated; the PR diff on both is byte-0 except the additive reviewer-internal module/doc/test. |
| **I5** RED-proof load-bearing | Neutering `derive_lifecycle_state` (force `unchanged`) makes the disappeared/changed/replaced derivation tests go RED for the right reason; neutering the §2 archive derivation makes `snapshotAvailability` tests go RED. |
| **I6** reviewer-internal access state | Every artifact tagged `reviewer_internal` / `alpine`; no public/published state reachable. |
| **I7** additive-only, no fork | Extends `stage3_source_inventory`; new files additive; prod files byte-0-diff. |
| **I8** full suite green at HEAD | Full pytest suite exit 0; this slice's own driver passes 100%. |

---

## §5 — RED list (each maps to a driver test)

- **R-1** lifecycle precedence: a disappeared signal beats a change flag; a
  replacement marker beats a change flag; all four states derive correctly.
- **R-2** fail-closed: a poisoned free-text status is never echoed in `evidence.*`
  and never produces an out-of-vocab state.
- **R-3** archive availability: `available_near_scan` / `not_available` /
  `not_checked` derive correctly and key on the immutable `scan_date`;
  `nearestSnapshotRef` is a public URL or `null`, never a raw path.
- **R-4** no-leak / I1+I2: a fully-poisoned source row leaks no raw value; no raw
  locator column reaches the body.
- **R-5** single envelope digest / I3: exactly one 64-hex string in the body
  (the `inventoryDigest`); no per-source hash.
- **R-6** determinism / never-hidden: order `(source_class, source_id)`, a second
  build is byte-identical, a disappeared/seed-only source is never dropped.
- **R-7** 0-diff guard / I4+I7: the module imports (never monkeypatches)
  `read_api` / `publication` / `stage3_source_inventory`.

---

## §6 — Downstream

This contract + module is what Stage 5.04 (raw preservation) and 5.05
(correction-state / hot-topic / Wayback impl) build on. No downstream slice
(5.04 → 5.15) is created until GOV-484 is `done` + merged (GOV-476 §4 linear
backbone). Escalate to CTO/CEO/Isaac for any scope beyond Alpine/reviewer-internal,
any public-surface requirement, or any backbone parallelization change.
