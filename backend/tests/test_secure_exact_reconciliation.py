"""Runtime exact GET and explicitly elevated unknown-generation inventory."""
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID
import pytest
from google.api_core.exceptions import Forbidden, NotFound
from sqlalchemy import text
from app.services import secure_catalogue as cat
from app.storage.contracts import InventoryRequired, StorageIntegrityError
from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import upload, record
from test_secure_storage_quota import binary, used
from test_secure_orphan_reconciliation import drained
from test_gcs_secure_runtime import gcs_client
from test_gcs_ciphertext_storage import storage, OBJECT, BLOB, RECEIPT


@pytest.mark.parametrize("case", ["exists", "absent", "mismatch", "unavailable", "precondition"])
def test_exact_get_has_no_list(storage, case):
    from google.api_core.exceptions import PreconditionFailed
    store, bucket, blob = storage
    bucket.list_blobs.side_effect = Forbidden("LIST denied")
    if case == "absent": blob.download_as_bytes.side_effect = NotFound("exact generation absent")
    if case == "unavailable": blob.download_as_bytes.side_effect = Forbidden("GET denied")
    if case == "precondition": blob.download_as_bytes.side_effect = PreconditionFailed("generation mismatch")
    if case == "mismatch": blob.download_as_bytes.return_value = b"wrong"
    if case in ("exists", "absent"):
        assert store.reconcile_exact(RECEIPT) == (RECEIPT if case == "exists" else None)
    else:
        with pytest.raises({"mismatch": StorageIntegrityError, "unavailable": Forbidden,
                            "precondition": PreconditionFailed}[case]):
            store.reconcile_exact(RECEIPT)
    bucket.list_blobs.assert_not_called()
    bucket.blob.assert_called_once_with(str(OBJECT), generation=123)
    assert blob.download_as_bytes.call_args.kwargs["if_generation_match"] == 123


@pytest.mark.parametrize("outcome", ["exists", "absent", "mismatch", "unavailable"])
@pytest.mark.parametrize("failure", ["readback", "journal"])
def test_known_generation_durable_reconciliation(gcs_client, monkeypatch, outcome, failure):
    c = gcs_client
    disposable, target = upload(c), record(c)
    baseline = used(c)
    # Upload succeeded with generation=1, but exact readback was denied. The
    # production adapter carries the receipt into the durable orphan operation.
    store = c.app.state.secure_runtime.store
    original = store.create
    def readback_failure(object_id, blob):
        c.bucket.failure = "read"
        try:
            return original(object_id, blob)
        finally:
            c.bucket.failure = None
    with monkeypatch.context() as patch:
        if failure == "readback":
            patch.setattr(store, "create", readback_failure)
        else:
            def failed_receipt(*args, **kwargs):
                raise cat.SecureStateChanged()
            patch.setattr(cat, "record_receipt", failed_receipt)
        assert binary(c, target).status_code == 503
    charged = used(c)
    assert charged > baseline
    with c.engine.connect() as db:
        op = db.execute(text("SELECT operation_id,candidate_object_id,receipt_json FROM secure_operations "
                             "WHERE state='ORPHAN_UNRESOLVED'")).one()
        acknowledgement = db.scalar(text("SELECT receipt_json FROM secure_reconciliation_events "
            "WHERE operation_id=:op"), {"op": op.operation_id})
    assert json.loads(acknowledgement)["receipt"]["exact_version"] == "1"
    if outcome == "absent": del c.bucket.objects[op.candidate_object_id]
    if outcome == "mismatch": c.bucket.objects[op.candidate_object_id] = b"different"
    if outcome == "unavailable": c.bucket.failure = "read"
    listing = Mock(side_effect=Forbidden("LIST denied"))
    monkeypatch.setattr(c.bucket, "list_blobs", listing, raising=False)
    args = drained(c, disposable)
    result = cat.reconcile_orphans(c.engine, args["runtime"].store, room_id=UUID(c.room["room_id"]), **args)
    expected = {"exists": "MATCHED", "absent": "ABSENT", "mismatch": "MISMATCH", "unavailable": "AMBIGUOUS"}[outcome]
    assert result == {op.operation_id: expected}
    assert used(c) == (baseline if outcome == "absent" else charged)
    if outcome == "absent":
        assert cat.reconcile_orphans(c.engine, args["runtime"].store, room_id=UUID(c.room["room_id"]), **args) == {}
        assert used(c) == baseline
        with c.engine.connect() as db:
            assert db.scalar(text("SELECT count(*) FROM secure_reconciliation_events WHERE outcome='ABSENT'")) == 1
    listing.assert_not_called()


@pytest.mark.parametrize("maintenance", ["exists", "absent", "denied", "multiple"])
def test_unknown_generation_requires_explicit_maintenance(gcs_client, monkeypatch, maintenance):
    c = gcs_client
    disposable, target = upload(c), record(c)
    baseline = used(c)
    c.bucket.failure = "uncertain"
    assert binary(c, target).status_code == 503
    c.bucket.failure = None
    charged = used(c)
    with c.engine.connect() as db:
        op = db.execute(text("SELECT operation_id,candidate_object_id,receipt_json FROM secure_operations "
                             "WHERE state='ORPHAN_UNRESOLVED'")).one()
    assert op.receipt_json is None
    listing = Mock(side_effect=Forbidden("LIST denied"))
    monkeypatch.setattr(c.bucket, "list_blobs", listing, raising=False)
    args = drained(c, disposable) | {"room_id": UUID(c.room["room_id"])}
    store = args["runtime"].store
    assert cat.reconcile_orphans(c.engine, store, **args) == {op.operation_id: "ORPHAN_NEEDS_INVENTORY"}
    listing.assert_not_called()
    assert used(c) == charged
    with c.engine.connect() as db:
        event = db.execute(text("SELECT outcome,receipt_json FROM secure_reconciliation_events")).one()
    assert event.outcome == "AMBIGUOUS"
    assert json.loads(event.receipt_json) == {"reason": "ORPHAN_NEEDS_INVENTORY"}
    if maintenance != "denied":
        listing.side_effect = None
        listing.return_value = ([SimpleNamespace(name=op.candidate_object_id, generation=1)]
            * (2 if maintenance == "multiple" else 1) if maintenance != "absent" else [])
    if maintenance == "absent": del c.bucket.objects[op.candidate_object_id]
    result = cat.reconcile_orphans_maintenance(c.engine, store, **args)
    expected = {"exists": "MATCHED", "absent": "ABSENT", "denied": "LIST_PERMISSION_REQUIRED", "multiple": "MISMATCH"}[maintenance]
    assert result == {op.operation_id: expected}
    assert used(c) == (baseline if maintenance == "absent" else charged)
    listing.assert_called_once()
    if maintenance == "exists":
        # The durable inventory observation supplies the generation on retry.
        listing.reset_mock()
        listing.side_effect = Forbidden("runtime has no LIST")
        assert cat.reconcile_orphans(c.engine, store, **args) == {op.operation_id: "MATCHED"}
        listing.assert_not_called()
    if maintenance == "denied":
        with c.engine.connect() as db:
            events = db.execute(text("SELECT receipt_json FROM secure_reconciliation_events")).scalars().all()
        assert {json.loads(value)["reason"] for value in events} == {"ORPHAN_NEEDS_INVENTORY", "LIST_PERMISSION_REQUIRED"}
