"""Opt-in ciphertext transport; secure runtime validates the durable catalogue binding.

The caller supplies a trusted, bound SDK bucket and already sealed bytes. Keys,
plaintext and catalogue authority stay local. Receipts lack bucket identity, so
the secure runtime requires a private durable binding before enabling this store.
A failed/uncertain create must be reconciled by the caller, never overwritten or
blindly deleted. Delete success is not proof of physical erasure under GCS retention.
"""
import hashlib
import hmac
import re

from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed

from .contracts import (MAX_BLOB_SIZE, CapacityExceeded, ObjectMetadata, StorageConflict,
                        StorageIntegrityError, StorageReceipt, uuid_value, InventoryRequired,
                        ListPermissionRequired, StorageCreateUnconfirmed)


class GCSCiphertextStore:
    def __init__(self, bucket):
        if type(bucket.name) is not str or not bucket.name:
            raise StorageIntegrityError('Bound bucket required')
        self._bucket = bucket
        self._bucket_name = bucket.name

    def _blob(self, name, generation=None):
        if self._bucket.name != self._bucket_name:
            raise StorageIntegrityError('Storage bucket changed')
        return self._bucket.blob(name, generation=generation)

    @staticmethod
    def _generation(receipt):
        if (type(receipt) is not StorageReceipt or receipt.adapter != 'gcs'
                or re.fullmatch(r'[1-9][0-9]*', receipt.exact_version) is None):
            raise StorageIntegrityError('Exact GCS receipt required')
        return int(receipt.exact_version)

    @property
    def storage_binding(self):
        if self._bucket.name != self._bucket_name:
            raise StorageIntegrityError('Storage bucket changed')
        return "gcs:" + self._bucket_name

    def reconcile_create(self, object_id, ciphertext_size, ciphertext_sha256):
        """Runtime cannot establish absence across unknown retained generations."""
        uuid_value(object_id)
        raise InventoryRequired("ORPHAN_NEEDS_INVENTORY")

    def reconcile_exact(self, receipt):
        """GET the immutable generation; only its authoritative 404 means absent."""
        try:
            self.read(receipt)
        except FileNotFoundError:
            return None
        return receipt

    def reconcile_inventory(self, object_id, ciphertext_size, ciphertext_sha256):
        """Maintenance only: requires separately granted storage.objects.list."""
        uuid_value(object_id)
        # Include noncurrent generations: a live-name 404 alone cannot release
        # bytes retained by object versioning. Two results already fail closed.
        self.storage_binding
        try:
            versions = list(self._bucket.list_blobs(prefix=str(object_id), versions=True,
                                                   max_results=2, timeout=60))
        except Forbidden:
            raise ListPermissionRequired("LIST_PERMISSION_REQUIRED") from None
        if not versions:
            return None
        if ciphertext_sha256 is None:
            raise StorageIntegrityError('Historical object identity is unknown')
        if len(versions) != 1 or versions[0].name != str(object_id):
            raise StorageIntegrityError('Ambiguous object generations')
        receipt = StorageReceipt('gcs', str(object_id), str(versions[0].generation),
                                 ciphertext_size, ciphertext_sha256)
        self.read(receipt)
        return receipt

    def create(self, object_id, blob):
        uuid_value(object_id)
        if type(blob) is not bytes or not blob:
            raise StorageIntegrityError('Ciphertext required')
        if len(blob) > MAX_BLOB_SIZE:
            raise CapacityExceeded('Ciphertext capacity exceeded')
        target = self._blob(str(object_id))
        try:
            target.upload_from_string(blob, content_type='application/octet-stream',
                                      if_generation_match=0, timeout=60)
        except PreconditionFailed:
            raise StorageConflict('Immutable object already exists') from None
        receipt = StorageReceipt('gcs', str(object_id), str(target.generation),
                                 len(blob), hashlib.sha256(blob).digest())
        self._generation(receipt)
        try:
            self.read(receipt)
        except Exception as error:
            raise StorageCreateUnconfirmed(receipt) from error
        return receipt

    def read(self, receipt):
        generation = self._generation(receipt)
        target = self._blob(receipt.object_name, generation)
        try:
            # Inclusive end requests one extra byte to detect a size mismatch.
            raw = target.download_as_bytes(start=0, end=receipt.ciphertext_size,
                                           raw_download=True,
                                           if_generation_match=generation, timeout=60)
        except NotFound:
            raise FileNotFoundError('Exact ciphertext not found') from None
        if (type(raw) is not bytes or len(raw) != receipt.ciphertext_size
                or not hmac.compare_digest(hashlib.sha256(raw).digest(), receipt.ciphertext_sha256)):
            raise StorageIntegrityError('Exact ciphertext receipt mismatch')
        return raw

    def inspect(self, receipt):
        self.read(receipt)
        return ObjectMetadata(receipt.exact_version, receipt.ciphertext_size, receipt.ciphertext_sha256)

    def delete(self, receipt):
        generation = self._generation(receipt)
        try:
            self.read(receipt)
        except FileNotFoundError:
            return 'absent'
        try:
            self._blob(receipt.object_name, generation).delete(if_generation_match=generation, timeout=60)
        except NotFound:
            return 'absent'
        return 'deleted'
