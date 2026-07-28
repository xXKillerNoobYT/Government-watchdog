# GOV-1574 (B1) — Raw source-file preservation store: spec + storage-path note

**Issue:** GOV-1574 · **Parent:** GOV-1566 ("New files": intake / save / reuse) · plan §4 B1, §7 task matrix.
**Owner:** SourceArchivist · **Review:** SecurityPrivacyAgent (SEC leg) · **Merges:** CTO.
**Scope:** Town of Alpine only. Local/vault-only. No public surface. Fail-closed & private-by-default.

Module: `scripts/raw_object_store.py` · Tests: `tests/test_raw_object_store.py` (24) · Dep: `cryptography` (AES-GCM).

---

## 1. Purpose & boundary

B1 is the **physical store that saves a supplied raw file**. It is deliberately
separate from `scripts/raw_preservation.py` (which *verifies* files the crawler
already placed at arbitrary `local_path`s). B1 owns bytes-in / bytes-out for the
files a person hands us through the gated intake path (B3): meeting packets,
agendas, scans, PDFs the automated crawler cannot discover.

It provides exactly the four guarantees the issue asks for — content-addressing,
immutability, encryption-at-rest, dedupe — and nothing that belongs to a sibling
issue:

| Concern | Owner |
|---|---|
| Physical store: address, immutability, encryption, dedupe | **B1 (this)** |
| Canonical file record + provenance model (`review_state`, `version_group_id`, …) | B2 |
| Gated intake API (mime allow-list, size cap, known-bad hash reject) | B3 |
| Web-safe read projection for the Website (reviewed-only, PII/path-stripped) | B6 |

**Hard boundary (GOV-1566 §2).** Raw bytes, encryption keys, content-address
keys, and store paths are **internal, local/vault-only**. B1 exposes **no read
surface**. The only thing that ever crosses to the Website is B6's web-safe
projection of *reviewed* files. Prior finding this hard-blocks: a feed once
served raw absolute vault paths — see §6.

## 2. On-disk layout

Default root: `Vault/raw-object-store/` (gitignored — see §6).

```
<root>/
  STORE_INFO.json                     # descriptor: {store_version, cipher, address, created_utc}. No secrets.
  objects/<h0:2>/<h2:4>/<sha256>      # ONE file per unique plaintext. Contents = ciphertext. Mode 0o444.
  links.jsonl                         # append-only link ledger (audit trail of every put)
```

* **Address = `sha256(plaintext)`.** The on-disk file *name* is the full 64-hex
  key; two-level hex fan-out (`ab/cd/abcd…`) keeps directories small. Retrieval is
  by key, never by a path the caller supplies.
* **Object file = `nonce(12) || AES-256-GCM(ciphertext+tag)`.** The key's SHA-256
  hex is bound in as GCM **AAD**, so tampering with either the address or the
  bytes fails the authenticated decrypt.
* **Link ledger row:** `{link_id, sha256, size_bytes, deduped, supplied_by,
  original_filename, captured_at}`. Minimal provenance only (B2 owns the canonical
  model). **No path field, no raw bytes** ever land here.

## 3. Guarantees (acceptance criteria → mechanism)

1. **Dedupe — identical hash ⇒ one object + a link.** `put()` computes the
   address, and if the object already exists it does **not** rewrite the physical
   bytes (`deduped=True`); it only appends a link row. N puts of identical bytes ⇒
   1 physical object, N link rows. *(test: `TestDedupe`)*
2. **Immutable — no code path mutates raw bytes.** Objects are written once via
   atomic temp→`os.replace` (a partial object never becomes visible) and chmod'd
   `0o444`. The low-level writer refuses to overwrite an existing object
   (`ImmutabilityError`). No method reopens an object for writing. *(test:
   `TestImmutability`)*
3. **Encrypted at rest — documented + verified.** On-disk bytes are AES-256-GCM
   ciphertext; plaintext markers do not survive. `get()` authenticated-decrypts
   and re-hashes to the key. Wrong key ⇒ `IntegrityError`; a single flipped bit ⇒
   `IntegrityError`. The store raises at construction if `cryptography` is absent
   (fail-closed — never a silent plaintext fallback). *(test:
   `TestEncryptionAtRest`)*
4. **Retrieval by key; keys/paths internal only.** `get(sha256)` / `exists()` take
   a **key**, not a path. `PutResult.object_relpath` and the ledger are
   **root-relative** — never absolute. `verify_all()` reports **keys**, never
   paths. *(test: `TestRetrievalByKey`, `TestNoAbsolutePaths`)*

## 4. Encryption key management

* 32-byte AES-256 key, provisioned **out-of-band** via `$GOV_RAWSTORE_KEY_HEX`
  (64 hex chars) — `key_from_env()` — or passed explicitly to the constructor.
* The key is **never** committed and **never** written into `<root>` (that would
  defeat encryption-at-rest). `STORE_INFO.json` carries no secret.
* Missing/short/non-hex key ⇒ hard error; the store never guesses or generates a
  silent key for real data. `generate_key()` exists only for provisioning/tests.
* Key rotation is out of scope for B1 (would re-encrypt objects under a new key
  while preserving addresses); tracked for a later hardening pass if needed.

## 5. Reviewer verification (reproduce the guarantees)

```bash
python3.12 -m venv /tmp/venv && /tmp/venv/bin/pip install -r requirements.txt
/tmp/venv/bin/python -m pytest tests/test_raw_object_store.py -q      # 24 passed

# operational integrity sweep over a real store (exits !=0 on any bad object;
# prints counts + KEYS only — no bytes, no paths):
export GOV_RAWSTORE_KEY_HEX=<64-hex>
python scripts/raw_object_store.py verify --root Vault/raw-object-store
python scripts/raw_object_store.py info   --root Vault/raw-object-store
```

`verify` belongs in the run-log / review checklist after any intake batch: a
non-zero exit is a tamper/corruption issue-creation trigger, not a silent log line
(mirrors the `raw_preservation.py verify` cadence in `stage1-raw-store-layout.md`).

## 6. Data-publication boundary (non-negotiable)

`.gitignore` excludes the whole store:

```
Vault/
```

Raw bytes (ciphertext), the link ledger, `STORE_INFO.json`, and the encryption key
are **local/vault-only, never committed** (WORKFLOW_GOVERNANCE data-publication
boundary; GOV-1566 §2). Only **tooling, tests, and this documentation** are
versioned; this change set adds **no raw data** to the repo.

**No absolute vault paths on any read surface.** B1 has no read surface. Every
value B1 hands a caller (`PutResult.object_relpath`, ledger rows, `verify_all`
output) is a content key or a root-relative path — never an absolute path and
never uploader PII. The Website only ever sees B6's web-safe projection of
reviewed files. If a stored object or any committed artifact were ever found to
contain private identity/address/voter-registry data beyond the boundary rules,
stop and escalate to CEO / SecurityPrivacyAgent.

## 7. Downstream integration (not built here)

B2 will reference an object by its `sha256` key in the canonical file record; B3
calls `put()` after its mime/size/known-bad-hash validation; B6 projects only
`review_state=web_safe` records and never reads B1 paths. B1 stays a pure,
side-effect-free store: bytes in by `put`, bytes out by `get(key)`.
