"""Independent records share custody, but never share cleanup fencing."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from app.services import secure_catalogue as cat
from app.services.secure_deletion import cleanup_inventory, SupervisorDrainProof
from b6_synthetic import room_ref, drained_authority, approval_for, SyntheticDrainSupervisor
from test_secure_catalogue import catalogue, control_update
from test_secure_identity import identity_client, create_room
from test_secure_media import media_client, upload
from test_secure_storage_quota import used


def delete(c, row):
    from app.services.secure_deletion import tombstone_record
    with c.engine.begin() as db:
        tombstone_record(db, principal_id=c.session["user"]["user_id"], record_id=row["upload_id"])
    with c.engine.connect() as db:
        return UUID(db.scalar(text("SELECT DISTINCT inventory_revision FROM secure_deletion_inventory "
                                   "WHERE target_id=:id"), {"id": row["upload_id"]}))


def authorities(c, revisions):
    runtime = c.app.state.secure_runtime
    with c.engine.begin() as db:
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        approval, proof, supervisor = drained_authority(db, ref=ref, inventory_revision=revisions[0], runtime=runtime)
    result = []
    for revision in revisions:
        supervisor = SyntheticDrainSupervisor(**{name: getattr(proof, name) for name in (
            "generation_id", "invocation_id", "boot_id", "unit_name", "approved_uid")}, inventory_revision=revision)
        supervisor.members.clear()
        result.append(dict(ref=ref, approval=approval_for(ref, revision),
            drain_proof=supervisor.issue(SupervisorDrainProof), supervisor=supervisor,
            runtime=runtime, inventory_revision=revision))
    return result


def deleting_store(c):
    class Store:
        storage_binding = "local:synthetic"
        calls = []
        def delete(self, receipt):
            self.calls.append(receipt)
            saved = c.objects.get(receipt.object_name)
            if saved is None:
                return "absent"
            assert saved[0] == receipt
            del c.objects[receipt.object_name]
            return "deleted"
    return Store()


@pytest.mark.parametrize("order", [(0, 1), (1, 0), "concurrent"])
def test_two_authorized_cleanup_targets_release_each_charge_once(media_client, order):
    c = media_client
    rows = [upload(c), upload(c)]
    before = used(c)
    revisions = [delete(c, row) for row in rows]
    assert used(c) == before
    args = authorities(c, revisions)
    store = deleting_store(c)
    with c.engine.connect() as db:
        amounts = [db.scalar(text("SELECT SUM(ciphertext_size) FROM secure_deletion_inventory "
            "WHERE inventory_revision=:r"), {"r": str(revision)}) for revision in revisions]
    if order == "concurrent":
        from app.services.secure_runtime import SecureRuntime
        from test_secure_runtime import FakeLock
        from threading import Barrier
        barrier = Barrier(2)
        original = store.delete
        def simultaneous(receipt):
            if receipt.object_name not in seen:
                seen.add(receipt.object_name)
                if len(seen) <= 2:
                    barrier.wait(timeout=10)
            return original(receipt)
        seen = set()
        store.delete = simultaneous
        # Independent consumers overlap remote work; each retains its guard.
        args = [arg | {"runtime": SecureRuntime(FakeLock(), check_admission=lambda: None)} for arg in args]
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(lambda a: cleanup_inventory(c.engine, store, **a), args)) == [True, True]
    else:
        remaining = before
        for i in order:
            assert cleanup_inventory(c.engine, store, **args[i])
            remaining -= amounts[i]
            assert used(c) == remaining
    assert used(c) == before - sum(amounts)
    count = len(store.calls)
    for arg in args:
        assert cleanup_inventory(c.engine, store, **arg)
    assert len(store.calls) == count
    assert used(c) == before - sum(amounts)


def test_unrelated_upload_after_delete_preserves_cleanup(media_client):
    c = media_client
    row = upload(c)
    revision = delete(c, row)
    upload(c)
    before = used(c)
    with c.engine.connect() as db:
        amount = db.scalar(text("SELECT SUM(ciphertext_size) FROM secure_deletion_inventory"))
    assert cleanup_inventory(c.engine, deleting_store(c), **authorities(c, [revision])[0])
    assert used(c) == before - amount


@pytest.mark.parametrize("tamper", ["room", "generation", "hash", "inventory", "tombstone"])
def test_wrong_or_stale_target_fails_before_delete(media_client, tamper):
    c = media_client
    row = upload(c)
    revision = delete(c, row)
    args = authorities(c, [revision])[0]
    before = used(c)
    store = deleting_store(c)
    if tamper == "room":
        args["ref"] = replace(args["ref"], owner_id=uuid4())
    else:
        with c.engine.begin() as db:
            if tamper == "generation":
                db.execute(text("UPDATE secure_deletion_inventory SET exact_version=:v"), {"v": str(uuid4())})
            elif tamper == "hash":
                db.execute(text("UPDATE secure_deletion_inventory SET ciphertext_sha256=:v"), {"v": b'x' * 32})
            elif tamper == "inventory":
                db.execute(text("UPDATE secure_deletion_inventory SET inventory_revision=:v"), {"v": str(uuid4())})
            else:
                db.execute(text("UPDATE security_tombstones SET deletion_epoch=deletion_epoch+1"))
    with pytest.raises(cat.SecureStateChanged):
        cleanup_inventory(c.engine, store, **args)
    assert store.calls == [] and used(c) == before


def test_other_existing_room_cannot_reuse_cleanup_authority(media_client):
    c = media_client
    first = c.room
    row = upload(c)
    revision = delete(c, row)
    c.room, _ = create_room(c, c.session, name="Other room")
    upload(c)
    other_usage = used(c)
    with c.engine.connect() as db:
        other_ref = room_ref(db, c.room["room_id"])
    c.room = first
    args = authorities(c, [revision])[0]
    store = deleting_store(c)
    with pytest.raises(cat.SecureStateChanged):
        cleanup_inventory(c.engine, store, **(args | {"ref": other_ref}))
    assert store.calls == []
    with c.engine.connect() as db:
        assert cat.room_storage_bytes(db, other_ref.owner_id) == other_usage


def test_same_target_change_during_remote_delete_cannot_release_charge(media_client):
    c = media_client
    row = upload(c)
    revision = delete(c, row)
    args = authorities(c, [revision])[0]
    before = used(c)
    store = deleting_store(c)
    original = store.delete
    def changed(receipt):
        result = original(receipt)
        # Change the deletion generation while preserving a valid inventory join.
        with c.engine.begin() as db:
            db.execute(text("UPDATE security_tombstones SET deletion_epoch=deletion_epoch+1"))
            db.execute(text("UPDATE secure_deletion_inventory SET deletion_epoch=deletion_epoch+1"))
        return result
    store.delete = changed
    with pytest.raises(cat.SecureStateChanged):
        cleanup_inventory(c.engine, store, **args)
    assert used(c) == before
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_deletion_inventory "
                              "WHERE physical_state IN ('DELETED','ABSENT')")) == 0
