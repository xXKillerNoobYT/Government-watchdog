"""Content-addressed, immutable, encrypted-at-rest raw-object store (GOV-1574 / B1).

Parent: GOV-1566 "New files" — supplied-source-file intake, save & reuse. This
module is the **B1** deliverable: the physical store that *saves* a supplied raw
file. It is deliberately distinct from ``scripts/raw_preservation.py`` (which
*verifies* files the crawler already placed at arbitrary ``local_path``s). B1
owns bytes-in / bytes-out for files a person hands us:

  * **Content-addressed key.** The address of an object is the SHA-256 of its
    **plaintext** bytes. Same bytes ⇒ same key, always.
  * **Dedupe = link, never duplicate.** Storing identical bytes twice yields ONE
    physical object plus a second *link* record. The physical object is written
    exactly once; the ledger records every logical reference.
  * **Immutable.** An object is written once (atomic temp→final + ``0o444``
    read-only) and NEVER mutated. There is no code path that reopens an existing
    object for writing.
  * **Encrypted at rest.** On-disk bytes are AES-256-GCM ciphertext. The 32-byte
    key is provisioned out-of-band (env/secret) and is NEVER stored in the repo
    or beside the ciphertext. The object's SHA-256 hex is bound in as GCM AAD, so
    tampering with either the address or the bytes fails the authenticated decrypt.

Boundary (GOV-1566 §2, hard gate): raw bytes, keys, and store paths are
**local/vault-only and internal**. Nothing here is a read surface — the only
thing that crosses to the Website is B6's web-safe projection of *reviewed*
files. ``PutResult`` and the link ledger carry only **root-relative** object
paths, never absolute vault paths; :meth:`RawObjectStore.get` takes a key, not a
path, so a caller can never coax an absolute path out of the store.

Encryption note: ``cryptography`` (AES-GCM) is the one dependency this adds; it
ships prebuilt py3.12 wheels used by CI. If it is genuinely unavailable the store
raises at construction rather than silently falling back to plaintext (fail-closed).

Usage:
    from raw_object_store import RawObjectStore, generate_key
    store = RawObjectStore(Path("Vault/raw-object-store"), key=generate_key())
    r = store.put(pdf_bytes, supplied_by="isaac", original_filename="packet.pdf")
    assert store.get(r.sha256) == pdf_bytes            # retrieval by key
    r2 = store.put(pdf_bytes)                           # identical bytes
    assert r2.deduped and store.object_count() == 1     # one object, two links

CLI:
    python scripts/raw_object_store.py verify --root Vault/raw-object-store
    python scripts/raw_object_store.py info   --root Vault/raw-object-store
(both use $GOV_RAWSTORE_KEY_HEX for the key; neither prints raw bytes or absolute paths)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:  # fail-closed: no silent plaintext fallback
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError as exc:  # pragma: no cover - exercised only without the dep
    raise ImportError(
        "raw_object_store requires the 'cryptography' package for encryption at "
        "rest (AES-256-GCM). Install it (see requirements.txt) — the store refuses "
        "to run without real encryption rather than store raw bytes in the clear."
    ) from exc

# --- constants -------------------------------------------------------------

STORE_VERSION = 1
CIPHER_NAME = "AES-256-GCM"
ADDRESS_ALGO = "sha256"
KEY_BYTES = 32          # AES-256
_NONCE_BYTES = 12       # AES-GCM standard nonce width
_HASH_CHUNK = 1 << 20   # 1 MiB streaming read for file hashing
_KEY_ENV = "GOV_RAWSTORE_KEY_HEX"

_OBJECTS_DIR = "objects"
_LINKS_LEDGER = "links.jsonl"
_STORE_INFO = "STORE_INFO.json"


class RawObjectStoreError(Exception):
    """Base error for the raw-object store."""


class ObjectNotFound(RawObjectStoreError):
    """Requested SHA-256 key is not present in the store."""


class IntegrityError(RawObjectStoreError):
    """Stored ciphertext failed authenticated decrypt or re-hash (tamper/corruption)."""


class ImmutabilityError(RawObjectStoreError):
    """A write was attempted against an already-present, immutable object."""


# --- key handling ----------------------------------------------------------

def generate_key() -> bytes:
    """A fresh random 32-byte AES-256 key. Provision once, store as a secret."""
    return secrets.token_bytes(KEY_BYTES)


def key_from_env(env: str = _KEY_ENV) -> bytes:
    """Load the 32-byte key from ``$GOV_RAWSTORE_KEY_HEX`` (64 hex chars).

    Raises if the var is absent or malformed — the store never guesses a key.
    """
    raw = os.environ.get(env)
    if not raw:
        raise RawObjectStoreError(
            f"{env} is not set — the raw-object store key must be provisioned "
            "out-of-band (secret/env), never committed or stored beside the data."
        )
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise RawObjectStoreError(f"{env} is not valid hex") from exc
    if len(key) != KEY_BYTES:
        raise RawObjectStoreError(f"{env} must decode to {KEY_BYTES} bytes (AES-256)")
    return key


# --- put result ------------------------------------------------------------

@dataclass(frozen=True)
class PutResult:
    """Outcome of a :meth:`RawObjectStore.put`.

    ``object_relpath`` is **root-relative and internal**; it is intentionally not
    an absolute path so it can never become a leaked vault path.
    """

    sha256: str
    size_bytes: int
    deduped: bool
    link_id: str
    object_relpath: str


# --- store -----------------------------------------------------------------

class RawObjectStore:
    """A content-addressed, immutable, encrypted-at-rest object store.

    Parameters
    ----------
    root:
        Store root directory (created if absent). Should live under a
        gitignored vault path (e.g. ``Vault/raw-object-store``); raw bytes are
        local/vault-only and never committed.
    key:
        32-byte AES-256 key. Provision out-of-band; never persist it in the repo
        or inside ``root``.
    """

    def __init__(self, root: Path, *, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise RawObjectStoreError(f"key must be exactly {KEY_BYTES} bytes (AES-256)")
        self.root = Path(root)
        self._aead = AESGCM(key)
        self._objects = self.root / _OBJECTS_DIR
        self._ledger = self.root / _LINKS_LEDGER
        self._objects.mkdir(parents=True, exist_ok=True)
        self._write_store_info()

    # -- layout -------------------------------------------------------------

    def _write_store_info(self) -> None:
        """Write the (secret-free) store descriptor once; never overwrite it."""
        info_path = self.root / _STORE_INFO
        if info_path.exists():
            return
        info_path.write_text(
            json.dumps(
                {
                    "store_version": STORE_VERSION,
                    "cipher": CIPHER_NAME,
                    "address": ADDRESS_ALGO,
                    "created_utc": _now_utc_iso(),
                    "note": "content-addressed by sha256(plaintext); on-disk bytes "
                            "are ciphertext; key provisioned out-of-band.",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def object_path(self, sha256: str) -> Path:
        """Internal on-disk path for a key (two-level hex fan-out). Not a read surface."""
        h = _validate_sha256(sha256)
        return self._objects / h[0:2] / h[2:4] / h

    def _relpath(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    # -- write --------------------------------------------------------------

    def put(
        self,
        data: bytes,
        *,
        supplied_by: str | None = None,
        original_filename: str | None = None,
        captured_at: str | None = None,
    ) -> PutResult:
        """Store ``data``, content-addressed by its SHA-256; encrypt at rest.

        Idempotent + deduping: if an object with the same SHA-256 already exists
        the physical bytes are **not** rewritten (``deduped=True``); either way a
        link record is appended, so N puts of identical bytes ⇒ 1 object, N links.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("data must be bytes")
        data = bytes(data)
        sha256 = hashlib.sha256(data).hexdigest()
        path = self.object_path(sha256)
        deduped = path.exists()
        if not deduped:
            self._write_encrypted(path, data, sha256)
        link_id = self._append_link(
            sha256=sha256,
            size_bytes=len(data),
            deduped=deduped,
            supplied_by=supplied_by,
            original_filename=original_filename,
            captured_at=captured_at or _now_utc_iso(),
        )
        return PutResult(
            sha256=sha256,
            size_bytes=len(data),
            deduped=deduped,
            link_id=link_id,
            object_relpath=self._relpath(path),
        )

    def put_file(self, src: Path, **provenance) -> PutResult:
        """Convenience: store a file's bytes (reads the file, then :meth:`put`)."""
        return self.put(Path(src).read_bytes(), **provenance)

    def _write_encrypted(self, path: Path, data: bytes, sha256: str) -> None:
        """Encrypt + atomically write an object, then make it read-only.

        Immutability: refuses to overwrite an existing object; writes to a temp
        file and ``os.replace``s it into place (no partial/observable object);
        chmods the final file ``0o444`` so the OS also rejects mutation.
        """
        if path.exists():  # defensive — put() already deduped, but never overwrite raw
            raise ImmutabilityError(f"object {sha256[:12]}… already exists — immutable")
        path.parent.mkdir(parents=True, exist_ok=True)
        nonce = os.urandom(_NONCE_BYTES)
        # AAD binds the ciphertext to its content address: tampering with either
        # the address or the bytes fails the authenticated decrypt.
        ciphertext = self._aead.encrypt(nonce, data, sha256.encode("ascii"))
        blob = nonce + ciphertext
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o444)
            os.replace(tmp, path)  # atomic; final object appears whole or not at all
        finally:
            if tmp.exists():
                # best-effort cleanup of a failed temp (was made read-only above)
                try:
                    os.chmod(tmp, 0o600)
                    tmp.unlink()
                except OSError:
                    pass

    def _append_link(
        self,
        *,
        sha256: str,
        size_bytes: int,
        deduped: bool,
        supplied_by: str | None,
        original_filename: str | None,
        captured_at: str,
    ) -> str:
        """Append one link record to the JSONL ledger; returns the link id.

        The ledger is the audit trail proving dedupe (N link rows over 1 object).
        It holds minimal provenance only; B2 owns the canonical file/provenance
        model. No absolute paths, no raw bytes are ever written here.
        """
        link_id = secrets.token_hex(12)
        record = {
            "link_id": link_id,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "deduped": deduped,
            "supplied_by": supplied_by,
            "original_filename": original_filename,
            "captured_at": captured_at,
        }
        with open(self._ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return link_id

    # -- read ---------------------------------------------------------------

    def exists(self, sha256: str) -> bool:
        return self.object_path(sha256).exists()

    def get(self, sha256: str) -> bytes:
        """Retrieve plaintext bytes by SHA-256 key.

        Authenticated-decrypts the stored ciphertext (AAD = the key) and asserts
        the recovered plaintext re-hashes to the requested key. Any failure is a
        tamper/corruption signal — raised, never returned as partial/garbage data.
        """
        h = _validate_sha256(sha256)
        path = self.object_path(h)
        if not path.exists():
            raise ObjectNotFound(f"no object for key {h[:12]}…")
        blob = path.read_bytes()
        if len(blob) < _NONCE_BYTES:
            raise IntegrityError(f"object {h[:12]}… truncated")
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            data = self._aead.decrypt(nonce, ciphertext, h.encode("ascii"))
        except InvalidTag as exc:
            raise IntegrityError(
                f"object {h[:12]}… failed authenticated decrypt — tamper/corruption "
                "or wrong key"
            ) from exc
        if hashlib.sha256(data).hexdigest() != h:
            raise IntegrityError(f"object {h[:12]}… content-address mismatch after decrypt")
        return data

    # -- integrity / introspection -----------------------------------------

    def iter_keys(self):
        """Yield every stored object's SHA-256 key (the on-disk file names)."""
        if not self._objects.exists():
            return
        for path in sorted(self._objects.rglob("*")):
            if path.is_file() and _is_sha256(path.name):
                yield path.name

    def object_count(self) -> int:
        return sum(1 for _ in self.iter_keys())

    def link_count(self) -> int:
        if not self._ledger.exists():
            return 0
        with open(self._ledger, encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def verify(self, sha256: str) -> bool:
        """True iff the object decrypts and re-hashes to its key. Never raises for a
        clean/corrupt object — corruption returns False (callers decide)."""
        try:
            self.get(sha256)
            return True
        except (ObjectNotFound, IntegrityError):
            return False

    def verify_all(self) -> dict:
        """Re-decrypt + re-hash every stored object.

        Returns ``{checked, ok, bad: [<key>, ...]}``; ``bad`` lists the KEYS only
        (no paths, no bytes) so the summary is safe to log.
        """
        checked = ok = 0
        bad: list[str] = []
        for key in self.iter_keys():
            checked += 1
            if self.verify(key):
                ok += 1
            else:
                bad.append(key)
        return {"checked": checked, "ok": ok, "bad": bad}


# --- helpers ---------------------------------------------------------------

def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _validate_sha256(value: str) -> str:
    v = (value or "").strip().lower()
    if not _is_sha256(v):
        raise RawObjectStoreError(f"not a valid lowercase sha256 hex key: {value!r}")
    return v


# --- CLI -------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Content-addressed encrypted raw-object store (GOV-1574 / B1)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (
        ("verify", "re-decrypt + re-hash every stored object (exit!=0 on any bad)"),
        ("info", "print store descriptor + object/link counts (no bytes, no paths)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--root", type=Path, required=True, help="store root directory")

    args = parser.parse_args(argv)
    try:
        key = key_from_env()
    except RawObjectStoreError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    store = RawObjectStore(args.root, key=key)

    if args.command == "verify":
        result = store.verify_all()
        print(
            f"raw-object-store: checked={result['checked']} ok={result['ok']} "
            f"bad={len(result['bad'])}"
        )
        for key_ in result["bad"]:
            print(f"  BAD: {key_}", file=sys.stderr)
        return 1 if result["bad"] else 0

    if args.command == "info":
        print(f"store_version={STORE_VERSION} cipher={CIPHER_NAME} address={ADDRESS_ALGO}")
        print(f"objects={store.object_count()} links={store.link_count()}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
