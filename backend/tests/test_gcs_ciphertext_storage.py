"""Mock transport only: no GCS credentials, network, or runtime integration."""
from dataclasses import replace
import hashlib
from unittest.mock import MagicMock
from uuid import UUID

from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed
from google.cloud.storage.blob import Blob
import pytest

from app.storage.contracts import MAX_BLOB_SIZE, CapacityExceeded, StorageConflict, StorageIntegrityError, StorageReceipt, StorageCreateUnconfirmed
from app.storage.gcs import GCSCiphertextStore

OBJECT = UUID('a6f862f7-ce47-4547-98d6-d4e9b8e926e2')
BLOB = b'opaque sealed ciphertext'
RECEIPT = StorageReceipt('gcs', str(OBJECT), '123', len(BLOB), hashlib.sha256(BLOB).digest())


@pytest.fixture
def storage():
    bucket = MagicMock()
    bucket.name = 'bound-test-bucket'
    blob = MagicMock(spec=Blob)
    blob.generation = 123
    blob.download_as_bytes.return_value = BLOB
    bucket.blob.return_value = blob
    return GCSCiphertextStore(bucket), bucket, blob


def test_create_read_inspect_and_exact_delete(storage):
    store, bucket, blob = storage
    assert store.create(OBJECT, BLOB) == RECEIPT
    blob.upload_from_string.assert_called_once_with(BLOB, content_type='application/octet-stream', if_generation_match=0, timeout=60)
    assert store.read(RECEIPT) == BLOB
    bucket.blob.assert_called_with(str(OBJECT), generation=123)
    blob.download_as_bytes.assert_called_with(start=0, end=len(BLOB), raw_download=True, if_generation_match=123, timeout=60)
    assert store.inspect(RECEIPT).ciphertext_sha256 == RECEIPT.ciphertext_sha256
    assert store.delete(RECEIPT) == 'deleted'
    blob.delete.assert_called_once_with(if_generation_match=123, timeout=60)


def test_duplicate_create_does_not_overwrite_or_delete(storage):
    store, _, blob = storage
    blob.upload_from_string.side_effect = PreconditionFailed('exists')
    with pytest.raises(StorageConflict):
        store.create(OBJECT, BLOB)
    blob.delete.assert_not_called()
    blob.download_as_bytes.assert_not_called()


@pytest.mark.parametrize('value', [b'', 'plaintext', bytearray(BLOB)])
def test_bad_payload_rejected_before_transport(storage, value):
    store, bucket, _ = storage
    with pytest.raises(StorageIntegrityError):
        store.create(OBJECT, value)
    bucket.blob.assert_not_called()


def test_capacity_and_object_name_before_transport(storage):
    store, bucket, _ = storage
    with pytest.raises(CapacityExceeded):
        store.create(OBJECT, b'x' * (MAX_BLOB_SIZE + 1))
    with pytest.raises(ValueError):
        store.create(str(OBJECT), BLOB)
    bucket.blob.assert_not_called()


@pytest.mark.parametrize('version', ['0', '-1', '01', '+1', '1.0', 'abc', '１２３'])
def test_noncanonical_generation_rejected(storage, version):
    store, bucket, _ = storage
    with pytest.raises(StorageIntegrityError):
        store.read(replace(RECEIPT, exact_version=version))
    bucket.blob.assert_not_called()


def test_wrong_adapter_rejected(storage):
    store, bucket, _ = storage
    with pytest.raises(StorageIntegrityError):
        store.read(replace(RECEIPT, adapter='local'))
    bucket.blob.assert_not_called()


@pytest.mark.parametrize('body', [BLOB + b'x', BLOB[:-1], b'x' * len(BLOB)])
def test_readback_mismatch_never_returns_receipt_or_deletes(storage, body):
    store, _, blob = storage
    blob.download_as_bytes.return_value = body
    with pytest.raises(StorageCreateUnconfirmed) as error:
        store.create(OBJECT, BLOB)
    assert error.value.receipt == RECEIPT
    assert isinstance(error.value.__cause__, StorageIntegrityError)
    with pytest.raises(StorageIntegrityError):
        store.delete(RECEIPT)
    blob.delete.assert_not_called()


def test_missing_read_fails_and_delete_is_idempotent(storage):
    store, _, blob = storage
    blob.download_as_bytes.side_effect = NotFound('missing')
    with pytest.raises(FileNotFoundError):
        store.read(RECEIPT)
    assert store.delete(RECEIPT) == 'absent'
    blob.delete.assert_not_called()


def test_disappeared_between_read_and_delete_is_absent(storage):
    store, _, blob = storage
    blob.delete.side_effect = NotFound('already deleted')
    assert store.delete(RECEIPT) == 'absent'


@pytest.mark.parametrize('operation', ['upload_from_string', 'download_as_bytes', 'delete'])
def test_permission_failure_is_never_absence_or_fallback(storage, operation):
    store, _, blob = storage
    getattr(blob, operation).side_effect = Forbidden('denied')
    with pytest.raises(Forbidden):
        if operation == 'upload_from_string':
            store.create(OBJECT, BLOB)
        else:
            store.delete(RECEIPT)


def test_delete_generation_race_fails_closed(storage):
    store, _, blob = storage
    blob.delete.side_effect = PreconditionFailed('generation changed')
    with pytest.raises(PreconditionFailed):
        store.delete(RECEIPT)


def test_bucket_binding_mutation_fails_before_transport(storage):
    store, bucket, _ = storage
    bucket.name = 'other-bucket'
    with pytest.raises(StorageIntegrityError):
        store.read(RECEIPT)
    bucket.blob.assert_not_called()


def test_existing_envelope_roundtrip_keeps_key_material_local(storage):
    from app.storage.contracts import PayloadIdentity
    from app.storage.crypto import open_payload, seal_payload

    store, _, blob = storage
    plaintext = b'synthetic private original'
    key = bytes(range(32))  # Synthetic test material only.
    identity = PayloadIdentity('room', UUID(int=1), UUID(int=2), OBJECT, 1,
                               'original', None, None, len(plaintext))
    sealed = seal_payload(identity, plaintext, key)
    blob.download_as_bytes.return_value = sealed.blob
    receipt = store.create(OBJECT, sealed.blob)
    uploaded = blob.upload_from_string.call_args.args[0]
    assert uploaded == sealed.blob
    assert plaintext not in uploaded and sealed.wrapped_dek not in uploaded
    assert key not in uploaded
    assert open_payload(identity, store.read(receipt), sealed.wrapped_dek, key) == plaintext
    assert blob.upload_from_string.call_args.kwargs == {
        'content_type': 'application/octet-stream', 'if_generation_match': 0, 'timeout': 60}


def test_upload_without_exact_generation_never_returns_receipt(storage):
    store, _, blob = storage
    blob.generation = None
    with pytest.raises(StorageIntegrityError):
        store.create(OBJECT, BLOB)
    blob.download_as_bytes.assert_not_called()
    blob.delete.assert_not_called()
