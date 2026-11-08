"""Authoritative recovery keeps ambiguity charged and scopes every observation."""
from uuid import UUID
import pytest
from sqlalchemy import text
from app.services import secure_catalogue as cat
from app.storage.contracts import StorageIntegrityError
from test_secure_identity import identity_client, create_room
from test_secure_media import media_client, upload, record
from test_secure_storage_quota import binary, used
from test_secure_catalogue import catalogue, control_update
from test_gcs_ciphertext_storage import storage, OBJECT, BLOB, RECEIPT
from google.api_core.exceptions import NotFound


def drained(c, disposable):
    from b6_synthetic import tombstone, inventory, room_ref, drained_authority
    runtime = c.app.state.secure_runtime
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], disposable["upload_id"])
        revision = inventory(db, c.room["room_id"], disposable["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        _, proof, supervisor = drained_authority(db, ref=ref, inventory_revision=revision, runtime=runtime)
    return dict(runtime=runtime, drain_proof=proof, supervisor=supervisor)


@pytest.mark.parametrize("outcome", ["absent", "matched", "ambiguous", "mismatch"])
def test_orphan_recovery_and_retry_are_room_scoped_and_idempotent(media_client, monkeypatch, outcome):
    c = media_client
    first_room = c.room
    c.room, _ = create_room(c, c.session, name="Other accounting room")
    upload(c)
    other_room, other_usage = c.room, used(c)
    c.room = first_room
    disposable = upload(c)
    target = record(c)
    before = used(c)
    store = c.app.state.secure_runtime.store
    create = store.create
    def lost(object_id, blob):
        if outcome in ("matched", "mismatch"):
            create(object_id, blob)
        raise OSError("lost response")
    monkeypatch.setattr(store, "create", lost)
    assert binary(c, target).status_code == 503
    charged = used(c)
    assert charged > before
    with c.engine.connect() as db:
        op = db.execute(text("SELECT operation_id,candidate_object_id FROM secure_operations WHERE state='ORPHAN_UNRESOLVED'")).one()
        bound = db.execute(text("SELECT ciphertext_size,ciphertext_sha256 FROM secure_create_bindings WHERE operation_id=:op"), {"op": op.operation_id}).one()
        assert bound.ciphertext_size == charged - before and len(bound.ciphertext_sha256) == 32
    original_reconcile = store.reconcile_create
    if outcome == "ambiguous":
        monkeypatch.setattr(store, "reconcile_create", lambda *args: (_ for _ in ()).throw(OSError("offline")))
    if outcome == "mismatch":
        monkeypatch.setattr(store, "reconcile_create", lambda *args: (_ for _ in ()).throw(StorageIntegrityError("wrong bytes")))
    args = drained(c, disposable)
    with pytest.raises(cat.SecureStateChanged):
        cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **(args | {"supervisor": None}))
    assert used(c) == charged
    result = cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **args)
    assert result == {op.operation_id: outcome.upper()}
    assert used(c) == (before if outcome == "absent" else charged)
    if outcome == "absent":
        assert cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **args) == {}
    elif outcome == "ambiguous":
        monkeypatch.setattr(store, "reconcile_create", original_reconcile)
        assert cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **args) == {op.operation_id: "ABSENT"}
        assert used(c) == before
        assert cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **args) == {}
    if outcome == "matched":
        from dataclasses import replace
        previous, blob = c.objects[op.candidate_object_id]
        from uuid import uuid4
        c.objects[op.candidate_object_id] = replace(previous, exact_version=str(uuid4())), blob
        assert cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **args) == {op.operation_id: "MISMATCH"}
        assert used(c) == charged
    with c.engine.connect() as db:
        assert cat.room_storage_bytes(db, UUID(other_room["room_id"])) == other_usage
    assert cat.reconcile_orphans(c.engine, store, room_id=UUID(other_room["room_id"]), **args) == {}
    from uuid import uuid4
    assert cat.reconcile_orphans(c.engine, store, room_id=uuid4(), **args) == {}
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_reconciliation_events WHERE outcome='ABSENT'")) <= 1
    with pytest.raises(ValueError):
        cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), limit=1001, **args)


def test_gcs_reconciliation_reads_generation_and_bytes(storage):
    store, bucket, blob = storage
    blob.name = str(OBJECT)
    bucket.list_blobs.return_value = [blob]
    assert store.reconcile_inventory(OBJECT, len(BLOB), RECEIPT.ciphertext_sha256) == RECEIPT
    bucket.list_blobs.assert_called_once_with(prefix=str(OBJECT), versions=True, max_results=2, timeout=60)
    bucket.blob.assert_called_with(str(OBJECT), generation=123)
    blob.download_as_bytes.return_value = b"wrong"
    with pytest.raises(StorageIntegrityError):
        store.reconcile_inventory(OBJECT, len(BLOB), RECEIPT.ciphertext_sha256)
    bucket.list_blobs.return_value = []
    assert store.reconcile_inventory(OBJECT, len(BLOB), RECEIPT.ciphertext_sha256) is None
    bucket.list_blobs.side_effect = OSError("ambiguous")
    with pytest.raises(OSError):
        store.reconcile_inventory(OBJECT, len(BLOB), RECEIPT.ciphertext_sha256)


def test_historical_unbound_orphan_is_not_released(media_client):
    from test_secure_storage_quota import quota_claim
    c = media_client
    disposable, target = upload(c), record(c)
    saved, identity = quota_claim(c, target)
    with c.engine.begin() as db:
        cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity, ciphertext_size=100)
        revision = db.scalar(text("SELECT revision FROM secure_operations WHERE operation_id=:op"), {"op": str(saved.operation_id)})
        cat.abandon(db, operation_id=saved.operation_id, expected_revision=revision, receipt_unknown=True)
    before = used(c)
    args = drained(c, disposable)
    assert cat.reconcile_orphans(c.engine, c.app.state.secure_runtime.store, room_id=identity.owner_id, **args) == {str(saved.operation_id): "MISMATCH"}
    assert used(c) == before


def test_gcs_noncurrent_generation_is_not_absence(storage):
    store, bucket, blob = storage
    blob.name = str(OBJECT)
    blob.reload.side_effect = NotFound("live generation absent")
    bucket.list_blobs.return_value = [blob]
    assert store.reconcile_inventory(OBJECT, len(BLOB), RECEIPT.ciphertext_sha256) == RECEIPT
    bucket.list_blobs.return_value = [blob, blob]
    with pytest.raises(StorageIntegrityError):
        store.reconcile_inventory(OBJECT, len(BLOB), RECEIPT.ciphertext_sha256)


def test_concurrent_reconciliation_releases_once(media_client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.secure_runtime import SecureRuntime
    from test_secure_runtime import FakeLock
    c = media_client
    disposable, target = upload(c), record(c)
    before = used(c)
    store = c.app.state.secure_runtime.store
    def unavailable(*args):
        raise OSError("lost create")
    monkeypatch.setattr(store, "create", unavailable)
    assert binary(c, target).status_code == 503
    assert used(c) > before
    args = drained(c, disposable)
    barrier = Barrier(2)
    def absent(*args):
        barrier.wait(timeout=10)
        return None
    monkeypatch.setattr(store, "reconcile_create", absent)
    def reconcile(_):
        runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
        return cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), **(args | {"runtime": runtime}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reconcile, range(2)))
    assert all(list(result.values()) == ["ABSENT"] for result in results)
    assert used(c) == before
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_reconciliation_events WHERE outcome='ABSENT'")) == 1


def test_bounded_reconciliation_does_not_starve_later_orphans(media_client, monkeypatch):
    c = media_client
    disposable = upload(c)
    targets = [record(c), record(c)]
    store = c.app.state.secure_runtime.store
    def unavailable(*args):
        raise OSError("unknown")
    monkeypatch.setattr(store, "create", unavailable)
    for target in targets:
        assert binary(c, target).status_code == 503
    args = drained(c, disposable)
    monkeypatch.setattr(store, "reconcile_create", unavailable)
    first = cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), limit=1, **args)
    assert list(first.values()) == ["AMBIGUOUS"]
    monkeypatch.setattr(store, "reconcile_create", lambda *args: None)
    second = cat.reconcile_orphans(c.engine, store, room_id=UUID(c.room["room_id"]), limit=1, **args)
    assert list(second.values()) == ["ABSENT"] and set(first).isdisjoint(second)


from test_gcs_secure_runtime import gcs_client


@pytest.mark.parametrize("outcome", ["absent", "exists", "ambiguous", "unbound", "replaced"])
def test_historical_gcs_absence_requires_verified_runtime_namespace(gcs_client, monkeypatch, outcome):
    from types import SimpleNamespace
    from app.storage.gcs import GCSCiphertextStore
    from test_secure_storage_quota import quota_claim
    c = gcs_client
    disposable, target = upload(c), record(c)
    baseline = used(c)
    saved, identity = quota_claim(c, target)
    with c.engine.begin() as db:
        cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity, ciphertext_size=100)
        revision = db.scalar(text("SELECT revision FROM secure_operations WHERE operation_id=:op"), {"op": str(saved.operation_id)})
        cat.abandon(db, operation_id=saved.operation_id, expected_revision=revision, receipt_unknown=True)
    assert used(c) == baseline + 100
    args = drained(c, disposable)
    calls = []
    def versions(**kwargs):
        calls.append(kwargs)
        if outcome == "ambiguous":
            raise OSError("provider unavailable")
        if outcome == "exists":
            return [SimpleNamespace(name=str(identity.object_id), generation=1)]
        return []
    monkeypatch.setattr(c.bucket, "list_blobs", versions, raising=False)
    store = c.app.state.secure_runtime.store
    if outcome in ("unbound", "replaced"):
        store = GCSCiphertextStore(c.bucket)
        if outcome == "replaced":
            c.app.state.secure_runtime.store = store
    result = cat.reconcile_orphans_maintenance(c.engine, store, room_id=identity.owner_id, **args)
    expected = {"absent": "ABSENT", "exists": "MISMATCH", "ambiguous": "AMBIGUOUS",
                "unbound": "MISMATCH", "replaced": "MISMATCH"}[outcome]
    assert result == {str(saved.operation_id): expected}
    assert used(c) == baseline + (0 if outcome == "absent" else 100)
    if outcome == "absent":
        assert cat.reconcile_orphans_maintenance(c.engine, store, room_id=identity.owner_id, **args) == {}
        assert len(calls) == 1 and used(c) == baseline
        with c.engine.connect() as db:
            assert db.execute(text("SELECT outcome,storage_binding FROM secure_reconciliation_events WHERE operation_id=:op"),
                              {"op": str(saved.operation_id)}).one() == ("ABSENT", "gcs:synthetic-bucket")
    elif outcome in ("unbound", "replaced"):
        assert calls == []
