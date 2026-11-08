"""Immutable Linux ciphertext objects, serialized with directory flock.

The caller provisions a private directory. A 20-byte local generation header
distinguishes recreation from stale receipts; returned ciphertext is unchanged.
Same-uid/root compromise is outside this filesystem boundary.
"""
from contextlib import contextmanager
import hashlib
import hmac
import os
import sys
from uuid import UUID, uuid4

from .contracts import (MAX_BLOB_SIZE, CapacityExceeded, ObjectMetadata, StorageConflict,
                        StorageIntegrityError, StorageReceipt, uuid_value)
from .key_vault import _directory, _publish, _read_entry


class LocalCiphertextStore:
    def __init__(self, root):
        if sys.platform != "linux":
            raise StorageIntegrityError("Linux storage required")
        self._root, self._uid = root, os.geteuid()
        with self._locked():
            pass

    @contextmanager
    def _locked(self):
        if os.geteuid() != self._uid:
            raise StorageIntegrityError("Storage identity changed")
        try:
            with _directory(self._root, self._uid) as directory:
                import fcntl
                fcntl.flock(directory, fcntl.LOCK_EX)
                yield directory
        except FileExistsError:
            raise StorageConflict("Immutable object already exists") from None
        except OSError:
            raise StorageIntegrityError("Local storage unavailable") from None

    @staticmethod
    def _receipt(receipt):
        if type(receipt) is not StorageReceipt or receipt.adapter != "local":
            raise StorageIntegrityError("Local receipt required")
        try:
            if str(UUID(receipt.exact_version)) != receipt.exact_version:
                raise ValueError()
        except ValueError:
            raise StorageIntegrityError("Invalid local generation") from None

    @property
    def storage_binding(self):
        return "local:" + os.path.realpath(self._root)

    def reconcile_create(self, object_id, ciphertext_size, ciphertext_sha256):
        uuid_value(object_id)
        with self._locked() as directory:
            try:
                raw = _read_entry(directory, str(object_id), self._uid, ciphertext_size + 20)
            except FileNotFoundError:
                return None
            if len(raw) != ciphertext_size + 20 or raw[:4] != b"ALS1":
                raise StorageIntegrityError("Exact ciphertext receipt mismatch")
            receipt = StorageReceipt("local", str(object_id), str(UUID(bytes=raw[4:20])),
                                     ciphertext_size, ciphertext_sha256)
            self._read(directory, receipt)
            return receipt

    def create(self, object_id, blob):
        uuid_value(object_id)
        if type(blob) is not bytes or not blob:
            raise StorageIntegrityError("Ciphertext required")
        if len(blob) > MAX_BLOB_SIZE:
            raise CapacityExceeded("Ciphertext capacity exceeded")
        generation = uuid4()
        receipt = StorageReceipt("local", str(object_id), str(generation), len(blob), hashlib.sha256(blob).digest())
        with self._locked() as directory:
            _publish(directory, receipt.object_name, b"ALS1" + generation.bytes + blob)
        return receipt

    def _read(self, directory, receipt):
        raw = _read_entry(directory, receipt.object_name, self._uid, receipt.ciphertext_size + 20)
        if (len(raw) != receipt.ciphertext_size + 20 or raw[:4] != b"ALS1"
                or raw[4:20] != UUID(receipt.exact_version).bytes
                or not hmac.compare_digest(hashlib.sha256(raw[20:]).digest(), receipt.ciphertext_sha256)):
            raise StorageIntegrityError("Exact ciphertext receipt mismatch")
        return raw[20:]

    def read(self, receipt):
        self._receipt(receipt)
        with self._locked() as directory:
            return self._read(directory, receipt)

    def inspect(self, receipt):
        self.read(receipt)
        return ObjectMetadata(receipt.exact_version, receipt.ciphertext_size, receipt.ciphertext_sha256)

    def delete(self, receipt):
        self._receipt(receipt)
        with self._locked() as directory:
            try:
                self._read(directory, receipt)
            except FileNotFoundError:
                return "absent"
            os.unlink(receipt.object_name, dir_fd=directory)
            os.fsync(directory)
        return "deleted"
