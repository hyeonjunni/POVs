"""Synthetic envelope and immutable store contracts; no application integration."""
import dataclasses
import json
import os
import struct
import sys
from uuid import UUID, uuid4

import pytest


def api():
    from app.storage import contracts, crypto
    return contracts, crypto


def identity(**changes):
    contracts, _ = api()
    values = dict(domain="room", owner_id=UUID(int=1), record_id=UUID(int=2),
                  object_id=UUID(int=3), object_version=1, artifact_kind="original",
                  source_object_id=None, source_version=None, plaintext_length=5)
    return contracts.PayloadIdentity(**(values | changes))


def test_canonical_aad_and_independent_random_envelopes():
    c, crypto = api()
    item = identity()
    expected = b'["ambient-legacy",1,1,"AES-256-GCM","room","00000000-0000-0000-0000-000000000001","00000000-0000-0000-0000-000000000002","00000000-0000-0000-0000-000000000003",1,"original",null,null,5]'
    assert crypto.encode_aad(item) == expected
    key = os.urandom(32)
    first, second = [crypto.seal_payload(item, b"hello", key) for _ in range(2)]
    assert first.blob != second.blob and first.wrapped_dek != second.wrapped_dek
    assert first.blob[:8] == b"ALM4" + struct.pack(">I", len(expected))
    assert len(first.wrapped_dek) == 40
    assert crypto.open_payload(item, first.blob, first.wrapped_dek, key) == b"hello"
    assert "hello" not in repr(first) and first.wrapped_dek.hex() not in repr(first)


@pytest.mark.parametrize("field,value", [("owner_id", UUID(int=8)), ("record_id", UUID(int=8)),
    ("object_id", UUID(int=8)), ("object_version", 2), ("artifact_kind", "record_metadata"),
    ("source_object_id", UUID(int=8)), ("plaintext_length", 4)])
def test_authoritative_identity_swap_rejected(field, value):
    c, crypto = api()
    item = identity(source_object_id=UUID(int=4), source_version=1)
    key = os.urandom(32)
    sealed = crypto.seal_payload(item, b"hello", key)
    with pytest.raises(c.StorageIntegrityError):
        crypto.open_payload(dataclasses.replace(item, **{field: value}), sealed.blob, sealed.wrapped_dek, key)


@pytest.mark.parametrize("part", ["magic", "header", "nonce", "ciphertext", "tag", "wrapper", "key", "trailing", "truncated"])
def test_tampering_fails_without_plaintext(part):
    c, crypto = api()
    key = os.urandom(32)
    sealed = crypto.seal_payload(identity(), b"hello", key)
    blob, wrapper = sealed.blob, sealed.wrapped_dek
    positions = {"magic": 0, "header": 12, "nonce": -29, "ciphertext": -17, "tag": -1}
    if part in positions:
        raw = bytearray(blob)
        raw[positions[part]] ^= 1
        blob = bytes(raw)
    elif part == "wrapper":
        wrapper = os.urandom(40)
    elif part == "key":
        key = os.urandom(32)
    elif part == "trailing":
        blob += b"x"
    else:
        blob = blob[:20]
    with pytest.raises(c.StorageIntegrityError):
        crypto.open_payload(identity(), blob, wrapper, key)


@pytest.mark.parametrize("changes", [{"object_version": True}, {"plaintext_length": -1},
    {"domain": "lookup"}, {"owner_id": "../escape"}, {"record_id": None},
    {"artifact_kind": "user_profile"}, {"source_version": 1}, {"artifact_kind": "unknown"}])
def test_identity_validation(changes):
    with pytest.raises(ValueError):
        identity(**changes)


def test_payload_limits_and_malformed_header():
    c, crypto = api()
    key = bytes(32)
    with pytest.raises(c.CapacityExceeded):
        identity(plaintext_length=20 * 1024 * 1024 + 1)
    with pytest.raises(c.CapacityExceeded):
        identity(artifact_kind="room_derivatives", plaintext_length=32 * 1024 * 1024)
    with pytest.raises(c.StorageIntegrityError):
        crypto.seal_payload(identity(), b"wrong size", key)
    with pytest.raises(c.StorageIntegrityError):
        crypto.open_payload(identity(), b"ALM4" + struct.pack(">I", 4097), bytes(40), key)
    with pytest.raises(ValueError):
        c.KeyRef("room", UUID(int=1), UUID(int=2), True)


def test_exact_receipt_validation():
    c, _ = api()
    for changes in ({"object_name": "../escape"}, {"ciphertext_size": True},
                    {"ciphertext_sha256": b"bad"}, {"exact_version": ""}):
        with pytest.raises(ValueError):
            c.StorageReceipt(**(dict(adapter="local", object_name=str(UUID(int=3)),
                exact_version="v1", ciphertext_size=50, ciphertext_sha256=bytes(32)) | changes))


linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux immutable filesystem requires DGX Linux")


def test_local_store_rejects_non_linux(tmp_path, monkeypatch):
    from app.storage import local
    c, _ = api()
    monkeypatch.setattr(local.sys, "platform", "win32")
    with pytest.raises(c.StorageIntegrityError):
        local.LocalCiphertextStore(tmp_path)


@linux
def test_local_immutable_roundtrip_exact_delete_and_stale_receipt(tmp_path):
    from app.storage.local import LocalCiphertextStore
    c, crypto = api()
    tmp_path.chmod(0o700)
    store = LocalCiphertextStore(tmp_path)
    blob = crypto.seal_payload(identity(), b"hello", bytes(32)).blob
    receipt = store.create(UUID(int=3), blob)
    assert store.read(receipt) == blob
    assert store.inspect(receipt).ciphertext_size == len(blob)
    with pytest.raises(c.StorageConflict):
        store.create(UUID(int=3), blob)
    for changes in ({"exact_version": str(uuid4())}, {"ciphertext_size": len(blob) + 1},
                    {"ciphertext_sha256": bytes(32)}, {"adapter": "gcs"}):
        wrong = dataclasses.replace(receipt, **changes)
        for operation in (store.read, store.inspect, store.delete):
            with pytest.raises(c.StorageIntegrityError):
                operation(wrong)
    assert store.delete(receipt) == "deleted"
    assert store.delete(receipt) == "absent"
    newer = store.create(UUID(int=3), blob)
    assert newer.exact_version != receipt.exact_version
    with pytest.raises(c.StorageIntegrityError):
        store.delete(receipt)
    assert store.read(newer) == blob


@linux
def test_local_tamper_symlink_modes_and_path_rejected(tmp_path):
    from app.storage.local import LocalCiphertextStore
    c, _ = api()
    tmp_path.chmod(0o700)
    store = LocalCiphertextStore(tmp_path)
    receipt = store.create(UUID(int=3), b"synthetic ciphertext")
    target = tmp_path / receipt.object_name
    target.chmod(0o644)
    with pytest.raises(c.StorageIntegrityError):
        store.read(receipt)
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    with pytest.raises(c.StorageIntegrityError):
        store.delete(receipt)
    target.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    target.symlink_to(outside)
    with pytest.raises(c.StorageIntegrityError):
        store.delete(receipt)
    with pytest.raises(c.StorageConflict):
        store.create(UUID(int=3), b"new")
    assert outside.read_bytes() == b"keep"
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(c.StorageIntegrityError):
        LocalCiphertextStore(alias)
    with pytest.raises(ValueError):
        store.create("../escape", b"bad")


@linux
def test_local_failed_fsync_never_publishes_partial_content(tmp_path, monkeypatch):
    from app.storage import local, key_vault
    c, _ = api()
    tmp_path.chmod(0o700)
    store = local.LocalCiphertextStore(tmp_path)
    def fail(_):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(key_vault.os, "fsync", fail)
    with pytest.raises(c.StorageIntegrityError):
        store.create(UUID(int=3), b"synthetic")
    assert not (tmp_path / str(UUID(int=3))).exists()
    assert list(tmp_path.iterdir()) == []


def _local_create_attempt(root):
    from app.storage.local import LocalCiphertextStore
    c, _ = api()
    try:
        return LocalCiphertextStore(root).create(UUID(int=3), b"synthetic")
    except c.StorageConflict:
        return None


@linux
def test_local_concurrent_create_has_one_winner(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    tmp_path.chmod(0o700)
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("fork")) as pool:
        results = list(pool.map(_local_create_attempt, [tmp_path] * 8))
    assert len([item for item in results if item]) == 1


@linux
def test_local_bounded_read_rejects_large_disk_object(tmp_path):
    from app.storage.local import LocalCiphertextStore
    c, _ = api()
    tmp_path.chmod(0o700)
    store = LocalCiphertextStore(tmp_path)
    receipt = store.create(UUID(int=3), b"synthetic")
    target = tmp_path / receipt.object_name
    with target.open("r+b") as stream:
        stream.truncate(33 * 1024 * 1024)
    with pytest.raises(c.StorageIntegrityError):
        store.read(receipt)
