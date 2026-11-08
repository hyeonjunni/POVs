"""B6 RED preparation plus existing defense controls. Synthetic SQLite only."""
import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_secure_media import content_path
from test_secure_catalogue import catalogue, service_case, control_update
from test_secure_identity import identity_client
from test_secure_media import media_client, upload, raw_scope, PNG
from test_secure_retrieval import processed, index_secure, search_secure, ask_secure, QUOTE
from app.retrieval.evaluation import SyntheticTokenHashProvider
from app.services import secure_catalogue as cat
from app.services.secure_runtime import SecureRuntime
from app.storage.contracts import StorageReceipt, VaultUnavailable
from test_secure_runtime import FakeLock
from b6_synthetic import (entry, tombstone, inventory, room_ref, approval_for, RecordingStore,
                          SyntheticDrainSupervisor, drained_authority)


@pytest.mark.parametrize("endpoint", ["content", "job", "index"])
def test_tombstone_immediately_denies_new_public_access(media_client, monkeypatch, endpoint):
    c = media_client
    row, done = processed(c, monkeypatch)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
    runtime = c.app.state.secure_runtime
    monkeypatch.setattr(runtime.vault, "load", lambda ref: pytest.fail("tombstoned target decrypted"))
    if endpoint == "content":
        result = c.request("GET", content_path(row), headers=c.auth)
    elif endpoint == "job":
        result = c.request("GET", "/processing-jobs/" + done["job_id"], headers=c.auth)
    else:
        runtime.embedding_provider = SyntheticTokenHashProvider()
        result = index_secure(c, row, done)
    assert result.status_code in (404, 409, 503)
    assert QUOTE not in result.text and PNG not in result.content


@pytest.mark.parametrize("epoch", ["deletion_epoch", "authorization_epoch"])
def test_stale_epoch_invalidates_old_snapshot(service_case, epoch):
    c = service_case
    with c["engine"].connect() as db:
        old = cat.snapshot(db, principal_id=c["principal"], targets=(("room", c["room"]),))
    with c["engine"].begin() as db:
        db.execute(text(f"UPDATE security_scopes SET {epoch}={epoch}+1,revision=revision+1 WHERE domain='room'"))
    with c["engine"].connect() as db:
        with pytest.raises(cat.SecureStateChanged):
            cat.revalidate(db, old)
        assert cat.snapshot(db, principal_id=c["principal"], targets=(("room", c["room"]),)) != old


def test_delete_commits_tombstone_epochs_and_complete_history_inventory(media_client):
    c = media_client
    row = upload(c)
    upload(c, row)  # Retired originals and metadata must not disappear from inventory.
    with c.engine.connect() as db:
        expected = set(tuple(r) for r in db.execute(text("SELECT object_id,object_version,adapter,storage_name,exact_version,ciphertext_size,ciphertext_sha256 FROM encrypted_objects WHERE record_id=:r"), dict(r=row["upload_id"])))
        before = db.execute(text("SELECT deletion_epoch,authorization_epoch FROM security_scopes WHERE domain='room' AND owner_id=:r"), dict(r=c.room["room_id"])).one()
    from app.services.secure_deletion import tombstone_record
    with c.engine.begin() as db:
        tombstone_record(db, principal_id=c.session["user"]["user_id"], record_id=row["upload_id"])
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM security_tombstones WHERE target_id=:r"), dict(r=row["upload_id"])) == 1
        after = db.execute(text("SELECT deletion_epoch,authorization_epoch FROM security_scopes WHERE domain='room' AND owner_id=:r"), dict(r=c.room["room_id"])).one()
        assert after[0] > before[0] and after[1] > before[1]
        actual = set(tuple(r) for r in db.execute(text("SELECT object_id,object_version,adapter,storage_name,exact_version,ciphertext_size,ciphertext_sha256 FROM secure_deletion_inventory WHERE target_id=:r"), dict(r=row["upload_id"])))
        assert actual == expected


def test_deletion_cannot_linearize_while_protected_sender_alive(media_client):
    c = media_client
    row = upload(c)
    async def run():
        entered, release, delete_queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        response_messages, delete_messages = [], []
        async def receive():
            await asyncio.Future()
        async def protected_send(message):
            response_messages.append(message)
            if message["type"] == "http.response.body":
                entered.set()
                await release.wait()
        async def delete_send(message):
            delete_messages.append(message)
        async def delete():
            delete_queued.set()
            await c.app(raw_scope(c, "DELETE", "/uploads/" + row["upload_id"], []), receive, delete_send)
        reader = asyncio.create_task(c.app(raw_scope(c, "GET", content_path(row), []), receive, protected_send))
        await asyncio.wait_for(entered.wait(), 3)
        deleter = asyncio.create_task(delete())
        try:
            await delete_queued.wait()
            await asyncio.sleep(.03)
            assert not delete_messages and not deleter.done()
            with c.engine.connect() as db:
                assert db.scalar(text("SELECT count(*) FROM security_tombstones WHERE target_id=:r"), dict(r=row["upload_id"])) == 0
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(reader, deleter), 3)
        assert response_messages[0]["status"] == 200
        assert delete_messages[0]["status"] == 200  # B5 catch-all 503 is not deletion evidence.
        with c.engine.connect() as db:
            assert db.scalar(text("SELECT lifecycle_state FROM records WHERE id=:r"), dict(r=row["upload_id"])) == "SOFT_DELETED"
    asyncio.run(run())


def test_receipt_uncertainty_becomes_orphan_unresolved(media_client, monkeypatch):
    c = media_client
    row = upload(c)
    store = c.app.state.secure_runtime.store
    original = store.create
    created = []
    def lost(object_id, blob):
        original(object_id, blob)
        created.append(str(object_id))
        raise OSError("synthetic lost response")
    monkeypatch.setattr(store, "create", lost)
    result = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth, files={"file": ("synthetic.png", PNG, "image/png")})
    assert result.status_code == 503 and len(created) == 1
    with c.engine.connect() as db:
        op = db.execute(text("SELECT state,receipt_json FROM secure_operations WHERE candidate_object_id=:o"), dict(o=created[0])).one()
        assert tuple(op) == ("ORPHAN_UNRESOLVED", None)
    assert created[0] in c.objects


def test_deletion_removes_record_from_search_and_answer_keeps_other_record(media_client, monkeypatch):
    c = media_client
    c.app.state.secure_runtime.embedding_provider = SyntheticTokenHashProvider()
    first, first_job = processed(c, monkeypatch)
    second, second_job = processed(c, monkeypatch, "어머니 산책은 오후 3시였다.")
    for row, done in ((first, first_job), (second, second_job)):
        assert index_secure(c, row, done).status_code == 200
    assert QUOTE in ask_secure(c).json()["answer"]
    result = c.request("DELETE", "/uploads/" + first["upload_id"], headers=c.auth)
    assert result.status_code == 200, result.text
    for arm in ("substring", "bm25", "vector", "hybrid_rrf"):
        result = search_secure(c, arm, "어머니", 5)
        assert result.status_code == 200, result.text
        ids = [h["record_id"] for h in result.json()["hits"]]
        assert second["upload_id"] in ids and first["upload_id"] not in ids
    answer = ask_secure(c, "어머니 산책은 몇 시였어?")
    assert answer.status_code == 200 and QUOTE not in answer.json()["answer"]
    assert answer.json()["citations"] and all(x["record_id"] != first["upload_id"] for x in answer.json()["citations"])


@pytest.mark.parametrize("proof", [None, {"pid_absent": True}, {"app_ack": True}, {"timed_out": True}])
def test_drain_proof_required_before_destruction_authority(media_client, proof):
    c = media_client
    row = upload(c)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
        rev = inventory(db, c.room["room_id"], row["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
    validate = entry("app.services.secure_deletion", "validate_destruction")
    with c.engine.begin() as db:
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, rev), drain_proof=proof,
                     runtime=c.app.state.secure_runtime)
        assert db.scalar(text("SELECT count(*) FROM credential_generations WHERE state='DRAINED'")) == 0


def test_exact_supervisor_proof_requires_final_descendant_and_current_binding(media_client):
    c = media_client
    row = upload(c)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
        revision = inventory(db, c.room["room_id"], row["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        generation = db.execute(text("SELECT g.* FROM security_control c JOIN credential_generations g "
                                     "ON g.generation_id=c.runtime_generation WHERE c.id=1")).mappings().one()
    proof_type = entry("app.services.secure_deletion", "SupervisorDrainProof")
    validate = entry("app.services.secure_deletion", "validate_destruction")
    runtime = c.app.state.secure_runtime
    supervisor = SyntheticDrainSupervisor(
        generation_id=UUID(str(generation["generation_id"])),
        invocation_id=UUID(str(generation["invocation_id"])),
        boot_id=UUID(str(generation["boot_id"])),
        unit_name=generation["unit_name"],
        approved_uid=generation["approved_uid"],
        inventory_revision=revision,
    )
    with c.engine.begin() as db:
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, revision), drain_proof=None, supervisor=supervisor, runtime=runtime)
    supervisor.acknowledge()
    supervisor.timeout()
    supervisor.remove("parent-worker")
    with c.engine.begin() as db:
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, revision),
                     drain_proof=supervisor.issue(proof_type), supervisor=supervisor, runtime=runtime)
    supervisor.remove("descendant-sender")
    with c.engine.begin() as db:
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, revision),
                     drain_proof=supervisor.issue(proof_type, inventory_revision=uuid4()), supervisor=supervisor, runtime=runtime)
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, revision),
                     drain_proof=supervisor.issue(proof_type, invocation_id=uuid4()), supervisor=supervisor, runtime=runtime)
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, revision, drained=False),
                     drain_proof=supervisor.issue(proof_type), supervisor=supervisor, runtime=runtime)
        supervisor.mutate_on_verify(
            lambda: db.execute(text("UPDATE security_control SET revision=revision+1 WHERE id=1")))
        with pytest.raises(cat.SecureStateChanged):
            validate(db, ref=ref, approval=approval_for(ref, revision),
                     drain_proof=supervisor.issue(proof_type), supervisor=supervisor, runtime=runtime)
        releases = runtime.lock.releases
        validate(db, ref=ref, approval=approval_for(ref, revision),
                 drain_proof=supervisor.issue(proof_type), supervisor=supervisor, runtime=runtime)
        assert db.scalar(text("SELECT count(*) FROM credential_generations WHERE state='DRAINED'")) == 1
        assert runtime.lock.releases == releases + 1 and not runtime.lock.held


def test_shared_key_referenced_by_other_record_cannot_be_destroyed(media_client):
    c = media_client
    first, second = upload(c), upload(c)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], first["upload_id"])
        rev = inventory(db, c.room["room_id"], first["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE record_id=:r AND key_id=:k AND state='ACTIVE'"), dict(r=second["upload_id"], k=str(ref.key_id))) > 0
        control_update(db, admission="CLOSED")
    blockers = entry("app.services.secure_deletion", "destruction_blockers")
    with c.engine.connect() as db:
        reasons = blockers(db, ref=ref, inventory_revision=rev)
        assert "live_objects" in reasons and "live_pointers" in reasons
        assert "offline_unknown" in reasons
    assert c.request("GET", content_path(second), headers=c.auth).status_code == 503
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT state FROM key_epochs WHERE key_id=:k"), dict(k=str(ref.key_id))) == "ACTIVE"


def test_mismatched_inventory_revision_rejects_exact_target_cleanup(media_client):
    c = media_client
    row = upload(c)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
        rev = inventory(db, c.room["room_id"], row["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=rev, runtime=runtime)
    stale = uuid4()
    with pytest.raises(VaultUnavailable):
        approval_for(ref, rev).require(ref, stale)
    store = RecordingStore()
    cleanup = entry("app.services.secure_deletion", "cleanup_inventory")
    with pytest.raises(cat.SecureStateChanged):
        cleanup(c.engine, store, ref=ref, approval=approval, drain_proof=proof,
                supervisor=supervisor, runtime=runtime, inventory_revision=stale)
    assert store.delete_calls == []


def test_cleanup_deletes_only_current_inventory_receipts_and_retries_idempotently(media_client):
    c = media_client
    first, second = upload(c), upload(c)
    runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], first["upload_id"])
        rev = inventory(db, c.room["room_id"], first["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        expected = {
            StorageReceipt(row["adapter"], row["storage_name"], row["exact_version"],
                           row["ciphertext_size"], bytes(row["ciphertext_sha256"]))
            for row in db.execute(text(
                "SELECT adapter,storage_name,exact_version,ciphertext_size,ciphertext_sha256 "
                "FROM secure_deletion_inventory WHERE target_id=:target "
                "AND inventory_revision=:revision"), dict(target=first["upload_id"], revision=str(rev))).mappings()
        }
        assert expected
        control_update(db, admission="CLOSED")
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=rev, runtime=runtime)
    store = RecordingStore()
    cleanup = entry("app.services.secure_deletion", "cleanup_inventory")
    cleanup(c.engine, store, ref=ref, approval=approval, drain_proof=proof,
            supervisor=supervisor, runtime=runtime, inventory_revision=rev)
    assert set(store.delete_calls) == expected and len(store.delete_calls) == len(expected)
    with c.engine.connect() as db:
        assert db.scalar(text(
            "SELECT count(*) FROM secure_deletion_inventory WHERE target_id=:target "
            "AND inventory_revision=:revision AND physical_state!='DELETED'"), {
                "target": first["upload_id"], "revision": str(rev)}) == 0
        assert db.scalar(text(
            "SELECT count(*) FROM encrypted_objects WHERE record_id=:target AND state='ACTIVE'"), {
                "target": second["upload_id"]}) > 0
        assert db.scalar(text("SELECT state FROM key_epochs WHERE key_id=:key"), {
            "key": str(ref.key_id)}) == "ACTIVE"
    cleanup(c.engine, store, ref=ref, approval=approval, drain_proof=proof,
            supervisor=supervisor, runtime=runtime, inventory_revision=rev)
    assert set(store.delete_calls) == expected and len(store.delete_calls) == len(expected)



def test_unconfirmed_synthetic_cleanup_is_durably_blocked(media_client):
    c = media_client
    row = upload(c)
    runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
        rev = inventory(db, c.room["room_id"], row["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=rev, runtime=runtime)
    store = RecordingStore("uncertain")
    store.synthetic_cleanup = True  # Current red seam has a caller-controlled gate.
    cleanup = entry("app.services.secure_deletion", "cleanup_inventory")
    assert cleanup(c.engine, store, ref=ref, approval=approval, drain_proof=proof,
                   supervisor=supervisor, runtime=runtime, inventory_revision=rev) is False
    assert len(store.delete_calls) == 1
    with c.engine.connect() as db:
        rows = db.execute(text(
            "SELECT physical_state,failure_code FROM secure_deletion_inventory "
            "WHERE inventory_revision=:revision"), {"revision": str(rev)}).all()
        assert rows and all(row == ("BLOCKED", "cleanup_unconfirmed") for row in rows)


def test_no_plaintext_fallback_after_encrypted_pointer_activation(media_client, monkeypatch):
    from app.services import media_loader
    c = media_client
    row = upload(c)
    monkeypatch.setattr(media_loader, "load_media_bytes", lambda *a, **k: pytest.fail("legacy plaintext fallback"))
    with c.engine.connect() as db:
        oid = db.scalar(text("SELECT object_id FROM protected_pointers WHERE owner_type='file'"))
    receipt, blob = c.objects[oid]
    c.objects[oid] = receipt, blob[:-1] + bytes([blob[-1] ^ 1])
    response = c.request("GET", content_path(row), headers=c.auth)
    assert response.status_code == 503 and PNG not in response.content


def test_legacy_writer_and_delete_fences_stay_fail_closed(media_client):
    c = media_client
    row = upload(c)
    with c.engine.begin() as db:
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE uploads SET title='synthetic plaintext' WHERE id=:r"), dict(r=row["upload_id"]))
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM upload_files WHERE upload_id=:r"), dict(r=row["upload_id"]))
        assert db.scalar(text("SELECT count(*) FROM upload_files WHERE upload_id=:r"), dict(r=row["upload_id"])) == 1


class RecorderImpostor:
    """Mimics the inert test recorder but is never cleanup authority."""
    def __init__(self):
        self.delete_calls = []

    def delete(self, receipt):
        self.delete_calls.append(receipt)
        return "deleted"

    def read(self, receipt):
        raise AssertionError("impostor must be rejected before object access")


def test_recorder_impostor_cannot_gain_cleanup_authority(media_client):
    c = media_client
    row = upload(c)
    runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], row["upload_id"])
        revision = inventory(db, c.room["room_id"], row["upload_id"])
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=revision, runtime=runtime)
    store = RecorderImpostor()
    cleanup = entry("app.services.secure_deletion", "cleanup_inventory")
    with pytest.raises(cat.SecureStateChanged):
        cleanup(c.engine, store, ref=ref, approval=approval, drain_proof=proof,
                supervisor=supervisor, runtime=runtime, inventory_revision=revision)
    assert store.delete_calls == []


def test_duplicate_inventory_revision_cannot_authorize_two_targets(media_client):
    c = media_client
    first, second = upload(c), upload(c)
    runtime = SecureRuntime(FakeLock(), check_admission=lambda: None)
    with c.engine.begin() as db:
        tombstone(db, c.room["room_id"], first["upload_id"])
        revision = inventory(db, c.room["room_id"], first["upload_id"])
        tombstone(db, c.room["room_id"], second["upload_id"])
        assert inventory(db, c.room["room_id"], second["upload_id"], revision=revision) == revision
        ref = room_ref(db, c.room["room_id"])
        control_update(db, admission="CLOSED")
        approval, proof, supervisor = drained_authority(
            db, ref=ref, inventory_revision=revision, runtime=runtime)
    store = RecordingStore()
    cleanup = entry("app.services.secure_deletion", "cleanup_inventory")
    with pytest.raises(cat.SecureStateChanged):
        cleanup(c.engine, store, ref=ref, approval=approval, drain_proof=proof,
                supervisor=supervisor, runtime=runtime, inventory_revision=revision)
    assert store.delete_calls == []
