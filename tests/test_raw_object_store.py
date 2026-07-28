"""Tests for the content-addressed encrypted raw-object store (GOV-1574 / B1).

Each acceptance criterion from the issue maps to a test class below:

  AC1 dedupe          -> TestDedupe
  AC2 immutability     -> TestImmutability
  AC3 encryption-at-rest -> TestEncryptionAtRest
  AC4 retrieval-by-key + internal-only paths -> TestRetrievalByKey / TestNoAbsolutePaths
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from raw_object_store import (  # noqa: E402
    ImmutabilityError,
    IntegrityError,
    ObjectNotFound,
    PutResult,
    RawObjectStore,
    RawObjectStoreError,
    generate_key,
    key_from_env,
)

SAMPLE = b"%PDF-1.4 Town of Alpine council packet\n" + b"x" * 5000
OTHER = b"%PDF-1.4 a completely different agenda\n" + b"y" * 4096


@pytest.fixture()
def key() -> bytes:
    return generate_key()


@pytest.fixture()
def store(tmp_path: Path, key: bytes) -> RawObjectStore:
    return RawObjectStore(tmp_path / "raw-object-store", key=key)


# --- AC1: dedupe -----------------------------------------------------------

class TestDedupe:
    def test_same_bytes_twice_one_object_two_links(self, store: RawObjectStore):
        r1 = store.put(SAMPLE, supplied_by="isaac", original_filename="packet.pdf")
        r2 = store.put(SAMPLE, supplied_by="mark", original_filename="packet-copy.pdf")

        assert r1.sha256 == r2.sha256          # same content address
        assert r1.deduped is False             # first write is physical
        assert r2.deduped is True              # second is a link only
        assert store.object_count() == 1       # ONE physical object
        assert store.link_count() == 2         # TWO logical links
        assert r1.link_id != r2.link_id

    def test_distinct_bytes_distinct_objects(self, store: RawObjectStore):
        store.put(SAMPLE)
        store.put(OTHER)
        assert store.object_count() == 2
        assert store.link_count() == 2

    def test_address_is_sha256_of_plaintext(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        assert r.sha256 == hashlib.sha256(SAMPLE).hexdigest()

    def test_dedupe_does_not_rewrite_physical_object(self, store: RawObjectStore):
        r1 = store.put(SAMPLE)
        path = store.object_path(r1.sha256)
        before = path.read_bytes()
        mtime_before = path.stat().st_mtime_ns
        store.put(SAMPLE)  # dedupe path — must not touch the file
        assert path.read_bytes() == before
        assert path.stat().st_mtime_ns == mtime_before


# --- AC2: immutability -----------------------------------------------------

class TestImmutability:
    def test_object_file_is_read_only(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        mode = store.object_path(r.sha256).stat().st_mode
        assert (mode & 0o222) == 0  # no write bits for anyone

    def test_no_put_overwrites_existing_object(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        # low-level write guard must refuse to overwrite an existing object
        with pytest.raises(ImmutabilityError):
            store._write_encrypted(store.object_path(r.sha256), SAMPLE, r.sha256)

    def test_get_after_many_dedupes_returns_original(self, store: RawObjectStore):
        store.put(SAMPLE)
        for _ in range(5):
            store.put(SAMPLE)
        assert store.get(hashlib.sha256(SAMPLE).hexdigest()) == SAMPLE


# --- AC3: encryption at rest -----------------------------------------------

class TestEncryptionAtRest:
    def test_on_disk_bytes_are_not_plaintext(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        on_disk = store.object_path(r.sha256).read_bytes()
        assert on_disk != SAMPLE
        # a recognizable plaintext marker must not survive in the ciphertext
        assert b"Town of Alpine" not in on_disk
        assert b"%PDF" not in on_disk

    def test_round_trip_decrypts_exactly(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        assert store.get(r.sha256) == SAMPLE

    def test_wrong_key_cannot_decrypt(self, tmp_path: Path):
        root = tmp_path / "s"
        r = RawObjectStore(root, key=generate_key()).put(SAMPLE)
        attacker = RawObjectStore(root, key=generate_key())  # different key, same bytes
        with pytest.raises(IntegrityError):
            attacker.get(r.sha256)

    def test_tampered_ciphertext_is_rejected(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        path = store.object_path(r.sha256)
        blob = bytearray(path.read_bytes())
        blob[-1] ^= 0xFF  # flip a tag/ciphertext bit
        os.chmod(path, 0o644)
        path.write_bytes(bytes(blob))
        with pytest.raises(IntegrityError):
            store.get(r.sha256)
        assert store.verify(r.sha256) is False

    def test_key_must_be_32_bytes(self, tmp_path: Path):
        with pytest.raises(RawObjectStoreError):
            RawObjectStore(tmp_path / "s", key=b"tooshort")

    def test_store_info_carries_no_secret(self, store: RawObjectStore):
        info = json.loads((store.root / "STORE_INFO.json").read_text())
        assert info["cipher"] == "AES-256-GCM"
        assert "key" not in info and "secret" not in json.dumps(info).lower()


# --- AC4: retrieval by key + integrity -------------------------------------

class TestRetrievalByKey:
    def test_get_missing_key_raises(self, store: RawObjectStore):
        with pytest.raises(ObjectNotFound):
            store.get("0" * 64)

    def test_get_rejects_malformed_key(self, store: RawObjectStore):
        with pytest.raises(RawObjectStoreError):
            store.get("not-a-hash")

    def test_exists(self, store: RawObjectStore):
        assert store.exists("0" * 64) is False
        r = store.put(SAMPLE)
        assert store.exists(r.sha256) is True

    def test_verify_all_clean(self, store: RawObjectStore):
        store.put(SAMPLE)
        store.put(OTHER)
        summary = store.verify_all()
        assert summary == {"checked": 2, "ok": 2, "bad": []}


# --- AC4: internal-only paths (no absolute vault paths leak) ---------------

class TestNoAbsolutePaths:
    def test_put_result_path_is_relative(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        assert not os.path.isabs(r.object_relpath)
        assert str(store.root) not in r.object_relpath

    def test_ledger_holds_no_absolute_paths(self, store: RawObjectStore):
        store.put(SAMPLE, supplied_by="isaac", original_filename="packet.pdf")
        text = (store.root / "links.jsonl").read_text()
        assert str(store.root) not in text
        for line in text.splitlines():
            rec = json.loads(line)
            # ledger fields are provenance + hash only — no path field at all
            assert "path" not in rec and "local_path" not in rec

    def test_verify_all_reports_keys_not_paths(self, store: RawObjectStore):
        r = store.put(SAMPLE)
        path = store.object_path(r.sha256)
        blob = bytearray(path.read_bytes())
        blob[-1] ^= 0x01
        os.chmod(path, 0o644)
        path.write_bytes(bytes(blob))
        summary = store.verify_all()
        assert summary["bad"] == [r.sha256]  # a key, never a path


# --- key provisioning ------------------------------------------------------

class TestKeyProvisioning:
    def test_key_from_env_roundtrip(self, monkeypatch):
        k = generate_key()
        monkeypatch.setenv("GOV_RAWSTORE_KEY_HEX", k.hex())
        assert key_from_env() == k

    def test_key_from_env_missing(self, monkeypatch):
        monkeypatch.delenv("GOV_RAWSTORE_KEY_HEX", raising=False)
        with pytest.raises(RawObjectStoreError):
            key_from_env()

    def test_key_from_env_bad_length(self, monkeypatch):
        monkeypatch.setenv("GOV_RAWSTORE_KEY_HEX", "abcd")
        with pytest.raises(RawObjectStoreError):
            key_from_env()


def test_put_file(tmp_path: Path, store: RawObjectStore):
    src = tmp_path / "packet.pdf"
    src.write_bytes(SAMPLE)
    r = store.put_file(src, supplied_by="isaac")
    assert isinstance(r, PutResult)
    assert store.get(r.sha256) == SAMPLE
