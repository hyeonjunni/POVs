"""Room quota reservations cover retained SECURE ciphertext, including failed writes."""
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.services import secure_catalogue as cat
from test_secure_catalogue import catalogue
from test_secure_identity import identity_client
from test_secure_media import media_client, upload, record, PNG


def used(c):
    with c.engine.connect() as db:
        return cat.room_storage_bytes(db, c.room["room_id"])


def binary(c, row):
    return c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth,
                     files={"file": ("synthetic.png", PNG, "image/png")})


@pytest.mark.parametrize("adapter", ["local", "gcs"])
def test_secure_quota_blocks_cumulative_create_before_store(media_client, monkeypatch, adapter):
    from app.storage.contracts import StorageReceipt

    c = media_client
    if adapter == "gcs":
        store = c.app.state.secure_runtime.store
        original = store.create

        def gcs_create(object_id, blob):
            receipt = original(object_id, blob)
            receipt = StorageReceipt("gcs", receipt.object_name, "1", receipt.ciphertext_size,
                                     receipt.ciphertext_sha256)
            c.objects[str(object_id)] = receipt, blob
            return receipt

        monkeypatch.setattr(store, "create", gcs_create)
    upload(c)
    before, objects = used(c), dict(c.objects)
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", before)
    result = c.request("POST", "/uploads", headers=c.auth, json=dict(
        room_id=c.room["room_id"], type="image", title="Another record"))
    assert result.status_code == 413, result.text
    assert used(c) == before and c.objects == objects


def test_replacement_counts_old_bytes_and_partial_failed_candidate(media_client, monkeypatch):
    c = media_client
    row = upload(c)
    before = used(c)
    with c.engine.connect() as db:
        original_size = db.scalar(text("SELECT ciphertext_size FROM encrypted_objects "
                                       "WHERE record_id=:id AND artifact_kind='original'"),
                                  {"id": row["upload_id"]})
        active = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='file'"))
    # Exactly enough for a replacement original, but not the new metadata envelope.
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", before + original_size)
    result = binary(c, row)
    assert result.status_code == 413, result.text
    assert used(c) == before + original_size
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='file'")) == active
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects "
                              "WHERE artifact_kind='original' AND state='STAGED'")) == 1
    objects = dict(c.objects)
    assert binary(c, row).status_code == 413
    assert c.objects == objects
    assert used(c) == before + original_size


def test_lost_create_receipt_reservation_remains_charged(media_client, monkeypatch):
    c = media_client
    row = record(c)
    before = used(c)
    store = c.app.state.secure_runtime.store
    original = store.create

    def lost(object_id, blob):
        original(object_id, blob)
        raise OSError("synthetic lost create receipt")

    monkeypatch.setattr(store, "create", lost)
    assert binary(c, row).status_code == 503
    charged = used(c)
    assert charged > before
    with c.engine.connect() as db:
        reservation = db.execute(text("SELECT reserved_ciphertext_bytes FROM secure_operations "
                                      "WHERE state='ORPHAN_UNRESOLVED'")).scalar_one()
        assert reservation == charged - before
    monkeypatch.setattr(store, "create", original)
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", charged)
    objects = dict(c.objects)
    assert binary(c, row).status_code == 413
    assert used(c) == charged and c.objects == objects


def test_logical_deletion_does_not_release_retained_ciphertext(media_client):
    c = media_client
    row = upload(c)
    upload(c, row)
    before, objects = used(c), dict(c.objects)
    assert c.request("DELETE", "/uploads/" + row["upload_id"], headers=c.auth).status_code == 200
    assert used(c) == before and c.objects == objects


@pytest.mark.parametrize("abandoned", [False, True])
def test_reservation_cannot_be_reused_by_stale_create(media_client, abandoned):
    from app.api.routes.secure_operations import create_ciphertext
    from app.storage.crypto import seal_payload
    from app.storage.contracts import PayloadIdentity, KeyRef
    from uuid import UUID, uuid4

    c = media_client
    row = record(c)
    runtime = c.app.state.secure_runtime
    uid = UUID(c.session["user"]["user_id"])
    target = ("record", UUID(row["upload_id"]))
    candidate = uuid4()
    with c.engine.begin() as db:
        saved = cat.snapshot(db, principal_id=uid, targets=(target,))
        saved = cat.claim(db, saved, operation_id=uuid4(), target=target, candidate_object_id=candidate)
        key = cat._row(db, "SELECT key_id,epoch FROM key_epochs WHERE domain='room' AND owner_id=:room",
                       room=c.room["room_id"])
    identity = PayloadIdentity("room", UUID(c.room["room_id"]), target[1], candidate, 1,
                               "record_metadata", None, None, 2)
    sealed = seal_payload(identity, b"{}", runtime.vault.load(
        KeyRef("room", identity.owner_id, UUID(key["key_id"]), key["epoch"])))
    with c.engine.begin() as db:
        cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity,
                            ciphertext_size=len(sealed.blob))
        op = cat._row(db, "SELECT revision FROM secure_operations WHERE operation_id=:id",
                      id=str(saved.operation_id))
        if abandoned:
            cat.abandon(db, operation_id=saved.operation_id, expected_revision=op["revision"])
    before, objects = used(c), dict(c.objects)
    with pytest.raises(cat.SecureStateChanged):
        create_ciphertext(runtime, saved, identity, sealed)
    assert used(c) == before and c.objects == objects
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT state FROM secure_operations WHERE operation_id=:id"),
                         {"id": str(saved.operation_id)}) == ("ABANDONED" if abandoned else "CLAIMED")

@pytest.mark.parametrize("confirmed", [False, True])
def test_quota_releases_only_confirmed_exact_ciphertext_cleanup(media_client, confirmed):
    from b6_synthetic import tombstone, inventory, room_ref, drained_authority, RecordingStore
    from test_secure_catalogue import control_update
    from test_secure_runtime import FakeLock
    from app.services.secure_runtime import SecureRuntime
    from app.services.secure_deletion import cleanup_inventory

    c = media_client
    row = upload(c)
    before = used(c)
    runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
        revision = inventory(db, c.room["room_id"], row["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        released = db.scalar(text("SELECT SUM(ciphertext_size) FROM encrypted_objects WHERE record_id=:id"),
                             {"id": row["upload_id"]})
        control_update(db, admission="CLOSED")
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=revision, runtime=runtime)
    assert used(c) == before
    store = RecordingStore("deleted" if confirmed else "uncertain")
    if confirmed:
        def delete(receipt):
            store.delete_calls.append(receipt)
            c.objects.pop(receipt.object_name)
            return "deleted"
        store.delete = delete
    args = dict(ref=ref, approval=approval, drain_proof=proof, supervisor=supervisor,
                runtime=runtime, inventory_revision=revision)
    store.storage_binding = "local:wrong-namespace"
    with pytest.raises(cat.SecureStateChanged):
        cleanup_inventory(c.engine, store, **args)
    assert store.delete_calls == [] and used(c) == before
    store.storage_binding = "local:synthetic"
    assert cleanup_inventory(c.engine, store, **args) is confirmed
    assert used(c) == before - (released if confirmed else 0)
    if not confirmed:
        store.result = "absent"
        def absent(receipt):
            store.delete_calls.append(receipt)
            return "absent"
        store.delete = absent
        assert cleanup_inventory(c.engine, store, **args) is True
        assert used(c) == before - released
        assert cleanup_inventory(c.engine, store, **args) is True
        assert used(c) == before - released
    if confirmed:
        calls = list(store.delete_calls)
        assert cleanup_inventory(c.engine, store, **args) is True
        assert store.delete_calls == calls
        assert used(c) == before - released


def quota_claim(c, row):
    from uuid import UUID, uuid4
    from app.storage.contracts import PayloadIdentity

    target, candidate = ("record", UUID(row["upload_id"])), uuid4()
    with c.engine.begin() as db:
        saved = cat.snapshot(db, principal_id=UUID(c.session["user"]["user_id"]), targets=(target,))
        saved = cat.claim(db, saved, operation_id=uuid4(), target=target, candidate_object_id=candidate)
    return saved, PayloadIdentity("room", UUID(c.room["room_id"]), target[1], candidate, 1,
                                  "record_metadata", None, None, 2)


def test_concurrent_reservations_use_distinct_connections_without_oversubscription(media_client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    c = media_client
    claims = [quota_claim(c, record(c)) for _ in range(2)]
    before = used(c)
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", before + 100)
    barrier = Barrier(2)
    connections = set()

    def reserve(claim):
        saved, identity = claim
        try:
            with c.engine.begin() as db:
                connections.add(id(db.connection.driver_connection))
                barrier.wait(timeout=5)
                cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity,
                                    ciphertext_size=100)
            return True
        except (cat.RoomStorageQuotaExceeded, cat.SecureStateChanged):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, claims))
    assert len(connections) == 2 and sorted(results) == [False, True]
    assert used(c) == before + 100


def test_precreate_quota_failure_leaves_no_charge_and_can_retry(media_client, monkeypatch):
    c = media_client
    row = record(c)
    before, objects = used(c), dict(c.objects)
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", before)
    assert binary(c, row).status_code == 413
    assert used(c) == before and c.objects == objects
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='ABANDONED' "
                              "AND reserved_ciphertext_bytes IS NOT NULL")) == 0
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", before + 10000)
    assert binary(c, row).status_code == 200


def test_full_room_does_not_consume_another_rooms_quota(media_client, monkeypatch):
    from test_secure_identity import create_room

    c = media_client
    for _ in range(4):
        upload(c)
    first_room, first_usage = c.room, used(c)
    c.room, _ = create_room(c, c.session, name="Independent Room")
    monkeypatch.setattr(settings, "legacy_room_storage_quota_bytes", first_usage)
    upload(c)
    assert used(c) < first_usage
    c.room = first_room
    assert used(c) == first_usage


def test_reservation_size_is_single_assignment_even_after_receipt(media_client):
    from sqlalchemy.exc import IntegrityError

    c = media_client
    row = record(c)
    saved, identity = quota_claim(c, row)
    with c.engine.begin() as db:
        cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity, ciphertext_size=100)
    with pytest.raises(cat.SecureStateChanged), c.engine.begin() as db:
        cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity, ciphertext_size=100)
    with pytest.raises(IntegrityError), c.engine.begin() as db:
        db.execute(text("UPDATE secure_operations SET reserved_ciphertext_bytes=99,revision=revision+1 "
                        "WHERE operation_id=:id"), {"id": str(saved.operation_id)})
    with c.engine.connect() as db:
        committed = db.scalar(text("SELECT operation_id FROM secure_operations "
                                   "WHERE domain='room' AND state='COMMITTED' LIMIT 1"))
    with pytest.raises(IntegrityError), c.engine.begin() as db:
        db.execute(text("UPDATE secure_operations SET reserved_ciphertext_bytes=1,revision=revision+1 "
                        "WHERE operation_id=:id"), {"id": committed})


def test_historical_unknown_create_without_reservation_fails_closed(media_client):
    c = media_client
    saved, identity = quota_claim(c, record(c))
    with c.engine.begin() as db:
        cat.abandon(db, operation_id=saved.operation_id, expected_revision=1, receipt_unknown=True)
    with c.engine.connect() as db, pytest.raises(cat.SecureStateChanged):
        cat.room_storage_bytes(db, identity.owner_id)


def test_known_precreate_envelope_failure_has_no_reservation(media_client, monkeypatch):
    from app.api.routes import secure_media
    from app.storage.contracts import CapacityExceeded

    c = media_client
    row = record(c)
    before, objects = used(c), dict(c.objects)
    seal = secure_media.seal_payload

    def rejected(*args, **kwargs):
        raise CapacityExceeded("Synthetic envelope validation rejection")

    monkeypatch.setattr(secure_media, "seal_payload", rejected)
    assert binary(c, row).status_code == 503
    assert used(c) == before and c.objects == objects
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='ABANDONED' "
                              "AND reserved_ciphertext_bytes IS NOT NULL")) == 0
    monkeypatch.setattr(secure_media, "seal_payload", seal)
    assert binary(c, row).status_code == 200