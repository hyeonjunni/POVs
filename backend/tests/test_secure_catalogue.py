"""Synthetic B1 schema contracts: disposable SQLite, never providers or real data."""
from __future__ import annotations

import sqlite3
import json
import asyncio
import importlib.util
from dataclasses import FrozenInstanceError
from uuid import UUID
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config, adopt, verify_revision
from test_migrations import build_legacy_database, schema_fingerprint, _seed_legacy_rows, _seed_retrieval_parents, _chunk_values


def service_api():
    assert importlib.util.find_spec("app.services.secure_catalogue"), "B2 detached catalogue writer is absent"
    from app.services import secure_catalogue
    return secure_catalogue


@pytest.fixture
def service_case(catalogue):
    engine, _, _ = catalogue
    uid, rid, aid = (str(uuid4()) for _ in range(3))
    invocation, boot = str(uuid4()), str(uuid4())
    with engine.begin() as db:
        put(db, "users", id=uid)
        put(db, "auth_identities", id=aid, user_id=uid, provider="google", payload_state="ENCRYPTED")
        put(db, "family_rooms", id=rid, owner_user_id=uid)
        put(db, "family_members", id=str(uuid4()), room_id=rid, user_id=uid)
        for domain, owner in (("user", uid), ("room", rid)):
            put(db, "security_scopes", domain=domain, owner_id=owner, state="ACTIVE", deletion_epoch=0,
                authorization_epoch=0, revision=1)
        kid = key(db, rid, state="ACTIVE")
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=kid, key_epoch=1,
            domain="room", owner_id=rid)
        db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,invocation_id=:inv,boot_id=:boot WHERE generation_id=:gid"),
                   dict(inv=invocation, boot=boot, gid=gid))
        control_update(db, mode="CUTOVER", runtime_generation=gid)
        control_update(db, mode="SECURE")
        control_update(db, admission="OPEN")
        put(db, "protected_pointers", owner_type="room", owner_id=rid, domain="room", scope_id=rid,
            state="MIGRATION_PENDING", source_revision=1, deletion_epoch=0, authorization_epoch=0, revision=1)
    return dict(engine=engine, principal=UUID(uid), room=UUID(rid), key=UUID(kid),
                generation=UUID(gid), invocation=UUID(invocation), boot=UUID(boot))


def test_service_snapshot_detached_roundtrip_and_persisted_principal(service_case):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        old = api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))
    assert old.principal_id == case["principal"]
    assert old.scopes == (("room", case["room"], 0, 0, 1), ("user", case["principal"], 0, 0, 1))
    assert api.CatalogueSnapshot.from_json(old.to_json()) == old
    with pytest.raises(FrozenInstanceError):
        old.control_revision = 1
    with case["engine"].begin() as db:
        with pytest.raises(api.SecureStateChanged):
            api.snapshot(db, principal_id=uuid4(), targets=(("room", case["room"]),))
    malformed = json.loads(old.to_json()) | {"payload": "never persist"}
    with pytest.raises(api.SecureStateChanged):
        api.CatalogueSnapshot.from_json(json.dumps(malformed))


@pytest.mark.parametrize("mutation", [
    "UPDATE security_scopes SET authorization_epoch=1,revision=2 WHERE domain='room'",
    "UPDATE security_scopes SET deletion_epoch=1,revision=2 WHERE domain='user'",
    "UPDATE protected_pointers SET source_revision=2,revision=2",
    "UPDATE key_epochs SET state='PREPARED',revision=2",
    "UPDATE security_control SET admission='CLOSED',revision=revision+1",
])
def test_service_stale_snapshot_cannot_revalidate(service_case, mutation):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        old = api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))
    with case["engine"].begin() as db:
        db.execute(text(mutation))
    with case["engine"].begin() as db:
        with pytest.raises(api.SecureStateChanged):
            api.revalidate(db, old)


def test_service_prepared_and_cross_room_pointer_denied(service_case):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        db.execute(text("UPDATE key_epochs SET state='PREPARED',revision=2"))
        with pytest.raises(api.SecureStateChanged):
            api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))


def test_service_tombstoned_principal_denied_even_when_room_remains_active(service_case):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        put(db, "security_tombstones", domain="user", owner_id=str(case["principal"]), target_kind="user",
            target_id=str(case["principal"]), deletion_epoch=1, revision=1)
        with pytest.raises(api.SecureStateChanged):
            api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))


def test_service_pointer_actual_owner_checked_before_key_access(service_case):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        other = scope(db)
        db.execute(text("UPDATE protected_pointers SET scope_id=:other"), dict(other=other))
        with pytest.raises(api.SecureStateChanged):
            api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))


def test_service_decrypt_key_must_be_in_current_generation(service_case):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        other_key = key(db, str(case["room"]), state="DECRYPT_ONLY")
        obj = envelope(db, str(case["room"]), other_key, state="ACTIVE")
        db.execute(text("UPDATE protected_pointers SET object_id=:obj,object_version=1,state='ENCRYPTED_ACTIVE'"), dict(obj=obj))
        with pytest.raises(api.SecureStateChanged):
            api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))


def test_service_snapshot_noncanonical_array_order_denied(service_case):
    api = service_api()
    case = service_case
    with case["engine"].begin() as db:
        saved = api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))
    value = json.loads(saved.to_json())
    value["scopes"].reverse()
    with pytest.raises(api.SecureStateChanged):
        api.CatalogueSnapshot.from_json(json.dumps(value, separators=(",", ":"), sort_keys=True))


@pytest.mark.parametrize("previous", [False, True])
@pytest.mark.parametrize("tampered", [False, True])
def test_service_publication_journal_and_atomic_final_pointer(service_case, previous, tampered):
    api = service_api()
    from app.storage.contracts import PayloadIdentity, StorageReceipt
    from app.storage.crypto import seal_payload
    import hashlib
    case = service_case
    operation, candidate = uuid4(), uuid4()
    with case["engine"].begin() as db:
        if previous:
            old_object = envelope(db, str(case["room"]), str(case["key"]), state="ACTIVE", exact_version=str(uuid4()))
            db.execute(text("UPDATE protected_pointers SET object_id=:o,object_version=1,state='ENCRYPTED_ACTIVE'"), dict(o=old_object))
        old = api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))
        claimed = api.claim(db, old, operation_id=operation, target=("room", case["room"]), candidate_object_id=candidate)
    # Real crypto outside SQL; runtime guard is owned by the calling lifecycle.
    identity = PayloadIdentity("room", case["room"], None, candidate, 1, "room_metadata", None, None, 2)
    sealed = seal_payload(identity, b"{}", bytes(32))
    receipt = StorageReceipt("local", str(candidate), str(uuid4()), len(sealed.blob), hashlib.sha256(sealed.blob).digest())
    with case["engine"].begin() as db:
        api.record_receipt(db, claimed, operation_id=operation, identity=identity, sealed=sealed, receipt=receipt)
    class Vault:
        def load(self, ref):
            return bytes(32)
    class Store:
        def read(self, supplied):
            assert supplied == receipt
            return sealed.blob
    proof = api.authenticate_readback(identity=identity, sealed=sealed, receipt=receipt,
        key_ref=claimed.key_refs[0], vault=Vault(), store=Store())
    with case["engine"].begin() as db:
        assert db.scalar(text("SELECT state FROM secure_operations")) == "RECEIPTED"
        assert db.scalar(text("SELECT state FROM encrypted_objects WHERE object_id=:o"), dict(o=str(candidate))) == "STAGED"
        with pytest.raises(api.SecureStateChanged):
            api.verify_readback(db, claimed, operation_id=operation)
        api.verify_readback(db, claimed, operation_id=operation, proof=proof)
        if tampered:
            db.execute(text("UPDATE encrypted_objects SET plaintext_length=plaintext_length+1 WHERE object_id=:o"), dict(o=str(candidate)))
            with pytest.raises(api.SecureStateChanged):
                api.publish(db, claimed, operation_id=operation, identity=identity, sealed=sealed, receipt=receipt)
            return
        api.publish(db, claimed, operation_id=operation, identity=identity, sealed=sealed, receipt=receipt)
    with case["engine"].begin() as db:
        assert db.scalar(text("SELECT state FROM secure_operations")) == "COMMITTED"
        assert db.scalar(text("SELECT object_id FROM protected_pointers")) == str(candidate)
        assert db.scalar(text("SELECT revision FROM protected_pointers")) == 2
        if previous:
            assert db.scalar(text("SELECT state FROM encrypted_objects WHERE object_id=:o"), dict(o=old_object)) == "RETIRED"
        with pytest.raises(api.SecureStateChanged):
            api.publish(db, claimed, operation_id=operation, identity=identity, sealed=sealed, receipt=receipt)


@pytest.mark.parametrize("unknown", [False, True])
def test_service_cancelled_and_lost_receipt_claims_are_retained(service_case, unknown):
    api = service_api()
    case = service_case
    op = uuid4()
    with case["engine"].begin() as db:
        saved = api.snapshot(db, principal_id=case["principal"], targets=(("room", case["room"]),))
        api.claim(db, saved, operation_id=op, target=("room", case["room"]), candidate_object_id=uuid4())
    with case["engine"].begin() as db:
        api.abandon(db, operation_id=op, expected_revision=1, receipt_unknown=unknown)
    with case["engine"].connect() as db:
        assert db.scalar(text("SELECT state FROM secure_operations")) == ("ORPHAN_UNRESOLVED" if unknown else "ABANDONED")
        assert db.scalar(text("SELECT COUNT(*) FROM encrypted_objects")) == 0


TABLES = {
    "security_control", "security_scopes", "key_epochs", "credential_generations",
    "credential_generation_keys", "encrypted_objects", "protected_pointers",
    "secure_operations", "blind_indexes", "security_tombstones", "secure_deletion_inventory",
}


@pytest.fixture
def catalogue(tmp_path):
    path = tmp_path / "catalogue.sqlite"
    url = f"sqlite:///{path}"
    command.upgrade(_build_alembic_config(url), "0005_secure_catalogue")
    engine = fk_enforced_engine(url)
    assert TABLES <= set(inspect(engine).get_table_names())
    try:
        yield engine, url, path
    finally:
        engine.dispose()


def put(db, table, **values):
    db.execute(text(f"INSERT INTO {table} ({','.join(values)}) VALUES "
                    f"({','.join(':'+name for name in values)})"), values)


def scope(db, domain="room"):
    owner = str(uuid4())
    put(db, "security_scopes", domain=domain, owner_id=owner, state="ACTIVE",
        deletion_epoch=0, authorization_epoch=0, revision=1)
    return owner


def key(db, owner, domain="room", **changes):
    values = dict(key_id=str(uuid4()), epoch=1, domain=domain, owner_id=owner,
                  state="PREPARED", provisioning_state="PERSISTED", revision=1,
                  recovery_inventory_state="UNKNOWN", recovery_reference_count=None)
    values.update(changes)
    put(db, "key_epochs", **values)
    return values["key_id"]


def generation(db, **changes):
    control = db.execute(text("SELECT catalogue_id,revision FROM security_control WHERE id=1")).one()
    values = dict(generation_id=str(uuid4()), catalogue_id=control[0], inventory_revision=str(uuid4()),
                  state="REQUESTED", unit_name="synthetic.service", approved_uid=1000,
                  control_revision=control[1], revision=1)
    values.update(changes)
    put(db, "credential_generations", **values)
    return values["generation_id"]


def envelope(db, owner, key_id, **changes):
    object_id = str(uuid4())
    values = dict(object_id=object_id, object_version=1, domain="room", owner_id=owner,
                  record_id=None, artifact_kind="room_metadata", plaintext_length=0,
                  payload_version=1, aad_version=1, payload_algorithm="AES-256-GCM",
                  key_id=key_id, key_epoch=1, wrapper_algorithm="AES-256-KW",
                  wrapped_dek=bytes(40), envelope_revision=1, adapter="local",
                  storage_name=object_id, exact_version="synthetic-version", ciphertext_size=100,
                  ciphertext_sha256=bytes(32), state="STAGED")
    values.update(changes)
    put(db, "encrypted_objects", **values)
    return object_id


def control_update(db, **values):
    db.execute(text("UPDATE security_control SET revision=revision+1," +
                    ",".join(f"{k}=:{k}" for k in values) + " WHERE id=1"), values)


def test_fresh_and_adopted0004_match_including_trigger_sql(tmp_path):
    engines = []
    try:
        for name, legacy in (("fresh", False), ("adopted", True)):
            url = f"sqlite:///{tmp_path / (name + '.sqlite')}"
            if legacy:
                build_legacy_database(url).dispose()
            adopt(url)
            engines.append(fk_enforced_engine(url))
        assert TABLES <= set(inspect(engines[0]).get_table_names())
        assert schema_fingerprint(engines[0]) == schema_fingerprint(engines[1])
        triggers = []
        for engine in engines:
            with engine.connect() as db:
                from scripts.adopt_alembic import MERGED_HEAD
                assert verify_revision(db, MERGED_HEAD) == []
                assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
                triggers.append(db.exec_driver_sql(
                    "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name").all())
        assert triggers[0] == triggers[1]
        assert len(triggers[0]) >= 35
    finally:
        for engine in engines:
            engine.dispose()


@pytest.mark.parametrize("column", ["email", "name", "username", "password_hash", "google_sub",
                                    "profile_image", "age", "gender", "phone", "profile_chunk"])
@pytest.mark.parametrize("mode", ["CUTOVER", "QUARANTINED"])
def test_every_user_private_field_rejects_old_insert_and_update(catalogue, column, mode):
    engine, _, _ = catalogue
    with engine.begin() as db:
        uid = str(uuid4())
        put(db, "users", id=uid)
        control_update(db, mode=mode)
        with pytest.raises(IntegrityError):
            put(db, "users", id=str(uuid4()), **{column: 25 if column == "age" else "synthetic"})
        with pytest.raises(IntegrityError):
            db.execute(text(f"UPDATE users SET {column}=:value WHERE id=:uid"),
                       dict(value=25 if column == "age" else "synthetic", uid=uid))
        assert db.scalar(text(f"SELECT {column} FROM users WHERE id=:uid"), dict(uid=uid)) is None


def test_missing_control_denies_plaintext(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        # Simulate damaged catalogue; its missing singleton must fail closed.
        for row in db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='security_control'"):
            db.exec_driver_sql(f'DROP TRIGGER "{row[0]}"')
        db.execute(text("DELETE FROM security_control"))
        with pytest.raises(IntegrityError):
            put(db, "users", id=str(uuid4()), name="synthetic")


@pytest.mark.parametrize("changes", [
    {"source_object_id": str(uuid4())}, {"source_version": 1}, {"record_id": str(uuid4())},
    {"artifact_kind": "record_metadata"}, {"artifact_kind": "room_derivatives"},
    {"wrapped_dek": bytes(39)}, {"ciphertext_sha256": bytes(31)}, {"ciphertext_size": 33554433},
    {"domain": "user"}, {"object_version": 0}, {"key_epoch": 2},
])
def test_envelope_null_bounds_and_ownership_constraints(catalogue, changes):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner)
        with pytest.raises(IntegrityError):
            envelope(db, owner, kid, **changes)


def test_composite_key_and_pointer_reject_other_room(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner, other = scope(db), scope(db)
        kid = key(db, owner)
        with pytest.raises(IntegrityError):
            envelope(db, other, kid)
        oid = envelope(db, owner, kid)
        with pytest.raises(IntegrityError):
            put(db, "protected_pointers", owner_type="room", owner_id=other, domain="room", scope_id=other,
                object_id=oid, object_version=1, state="ENCRYPTED_ACTIVE", source_revision=1,
                deletion_epoch=0, revision=1, authorization_epoch=0)
        for kind in ("record_metadata", "room_derivatives"):
            envelope(db, owner, kid, artifact_kind=kind, record_id=str(uuid4()))


def test_generation_and_control_normal_lifecycle_and_reverse_rejection(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        gid = generation(db)
        db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,"
                        "invocation_id=:inv,boot_id=:boot WHERE generation_id=:gid"),
                   dict(inv=str(uuid4()), boot=str(uuid4()), gid=gid))
        control_update(db, mode="CUTOVER", runtime_generation=gid)
        control_update(db, mode="SECURE")
        control_update(db, admission="OPEN")
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE credential_generations SET state='DRAINING',revision=3 WHERE generation_id=:gid"), dict(gid=gid))
        control_update(db, admission="CLOSED")
        db.execute(text("UPDATE credential_generations SET state='DRAINING',revision=3 WHERE generation_id=:gid"), dict(gid=gid))
        db.execute(text("UPDATE credential_generations SET state='DRAINED',revision=4 WHERE generation_id=:gid"), dict(gid=gid))
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=5 WHERE generation_id=:gid"), dict(gid=gid))
        with pytest.raises(IntegrityError):
            control_update(db, admission="OPEN")
        with pytest.raises(IntegrityError):
            control_update(db, mode="LEGACY")


def test_membership_frozen_and_history_retained(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner)
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=kid, key_epoch=1, domain="room", owner_id=owner)
        for statement in ("UPDATE credential_generation_keys SET key_epoch=2", "DELETE FROM credential_generation_keys",
                          "DELETE FROM credential_generations"):
            with pytest.raises(IntegrityError):
                db.execute(text(statement))


@pytest.mark.parametrize("table,column,value", [
    ("auth_identities", "payload_state", "LEGACY"),
    ("auth_identities", "provider_subject", "synthetic"),
    ("auth_identities", "provider_email", "synthetic"),
    ("auth_identities", "credential_hash", "synthetic"),
    ("family_rooms", "name", "synthetic"), ("family_rooms", "invite_code", "synthetic"),
    ("family_members", "relation_to_related_user", "synthetic"),
    ("uploads", "title", "synthetic"), ("uploads", "description", "synthetic"),
    ("upload_files", "storage_bucket", "synthetic"), ("upload_files", "storage_path", "synthetic"),
    ("upload_files", "mime_type", "synthetic"), ("upload_files", "encrypted", 0),
    ("processing_jobs", "result_json", "synthetic"), ("processing_jobs", "failure_message", "synthetic"),
])
def test_every_other_private_field_rejects_insert_and_update(catalogue, table, column, value):
    engine, _, _ = catalogue
    with engine.begin() as db:
        uid, room, record, file = (str(uuid4()) for _ in range(4))
        put(db, "users", id=uid)
        rows = {
            "auth_identities": dict(id=str(uuid4()), user_id=uid, provider="google", payload_state="ENCRYPTED"),
            "family_rooms": dict(id=room, owner_user_id=uid),
            "family_members": dict(id=str(uuid4()), room_id=room, user_id=uid),
            "uploads": dict(id=record, room_id=room, uploader_user_id=uid, type="image"),
            "upload_files": dict(id=file, upload_id=record, encrypted=1),
            "processing_jobs": dict(id=str(uuid4()), record_id=record, upload_file_id=file, room_id=room, processor="ocr"),
        }
        for name, row in rows.items():
            put(db, name, **row)
        control_update(db, mode="CUTOVER")
        for statement, params in (
            (f"UPDATE {table} SET {column}=:value", dict(value=value)),
            (f"INSERT INTO {table} ({','.join(rows[table] | {column: value})}) VALUES " +
             "(" + ",".join(":"+k for k in rows[table] | {column: value}) + ")",
             rows[table] | {"id": str(uuid4()), column: value}),
        ):
            with pytest.raises(IntegrityError, match="secure catalogue rejects plaintext writer"):
                db.execute(text(statement), params)


def activation(db, owner, kid, **changes):
    uid = str(uuid4())
    put(db, "users", id=uid)
    put(db, "auth_identities", id=str(uuid4()), user_id=uid, provider="google", payload_state="ENCRYPTED")
    values = dict(operation_id=str(uuid4()), kind="ROOM_CREATE", principal_id=uid,
                  owner_type="room", owner_id=owner, domain="room", scope_id=owner,
                  control_revision=1, pointer_revision=1, source_revision=1, deletion_epoch=0,
                  authorization_epoch=0, activation_key_id=kid, activation_key_epoch=1,
                  continuation_digest=uuid4().bytes + uuid4().bytes, created_at=100, expires_at=86500,
                  revision=1, state="WAITING_CREDENTIAL")
    values.update(changes)
    put(db, "secure_operations", **values)
    return values["operation_id"]


@pytest.mark.parametrize("changes", [
    {"state": "READY"}, {"state": "COMMITTED"}, {"expires_at": None}, {"expires_at": 86501},
    {"activation_key_epoch": None}, {"activation_key_id": None}, {"continuation_digest": bytes(31)},
    {"owner_type": "file"}, {"scope_id": str(uuid4())}, {"parent_operation_id": str(uuid4())},
    {"revision": 2}, {"candidate_object_id": str(uuid4())},
])
def test_activation_initial_state_null_and_identity_constraints(catalogue, changes):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner)
        with pytest.raises(IntegrityError):
            activation(db, owner, kid, **changes)


def test_activation_requires_delivered_key_and_preserves_terminal_history(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner)
        oid = activation(db, owner, kid)
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=kid, key_epoch=1, domain="room", owner_id=owner)
        db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,"
                        "invocation_id=:inv,boot_id=:boot WHERE generation_id=:gid"),
                   dict(inv=str(uuid4()), boot=str(uuid4()), gid=gid))
        control_update(db, runtime_generation=gid)
        db.execute(text("UPDATE secure_operations SET state='READY',runtime_generation=:gid,revision=2 WHERE operation_id=:oid"),
                   dict(gid=gid, oid=oid))
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE secure_operations SET expires_at=expires_at+1,revision=3 WHERE operation_id=:oid"), dict(oid=oid))
        db.execute(text("UPDATE secure_operations SET state='WAITING_CREDENTIAL',runtime_generation=NULL,revision=3 WHERE operation_id=:oid"), dict(oid=oid))
        db.execute(text("UPDATE secure_operations SET state='EXPIRED',failure_code='activation_expired',revision=4 WHERE operation_id=:oid"), dict(oid=oid))
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE secure_operations SET state='READY',runtime_generation=:gid,revision=5 WHERE operation_id=:oid"), dict(gid=gid, oid=oid))
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM secure_operations WHERE operation_id=:oid"), dict(oid=oid))


@pytest.mark.parametrize("mode", ["CUTOVER", "QUARANTINED"])
def test_old_connection_delete_is_denied_without_function(catalogue, mode):
    engine, _, path = catalogue
    old = sqlite3.connect(path)
    try:
        with engine.begin() as db:
            put(db, "users", id=str(uuid4()))
            control_update(db, mode=mode)
        with pytest.raises(sqlite3.DatabaseError):
            old.execute("DELETE FROM users")
        old.rollback()
        assert old.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        old.close()


def test_unsafe_downgrade_and_empty_legacy_roundtrip(catalogue):
    engine, url, _ = catalogue
    config = _build_alembic_config(url)
    command.downgrade(config, "0004_retrieval")
    with engine.connect() as db:
        assert verify_revision(db, "0004_retrieval") == []
    command.upgrade(config, "0005_secure_catalogue")
    with engine.begin() as db:
        scope(db)
    with pytest.raises(RuntimeError, match="0005 downgrade refused"):
        command.downgrade(config, "0004_retrieval")
    assert "security_scopes" in inspect(engine).get_table_names()


def test_populated_adopted0004_preserves_all_rows_and_constraints(tmp_path):
    url = f"sqlite:///{tmp_path / 'populated.sqlite'}"
    legacy = build_legacy_database(url)
    _seed_legacy_rows(legacy)
    legacy.dispose()
    config = _build_alembic_config(url)
    command.stamp(config, "0001_baseline")
    command.upgrade(config, "0004_retrieval")
    engine = fk_enforced_engine(url)
    try:
        with engine.connect() as db:
            names = [t for t in inspect(db).get_table_names() if t != "alembic_version"]
            before = {name: db.exec_driver_sql(f"SELECT * FROM {name}").all() for name in names}
        command.upgrade(config, "0009_ocr_review")
        with engine.connect() as db:
            assert verify_revision(db, "0009_ocr_review") == []
            for name in names:
                columns = [c["name"] for c in inspect(db).get_columns(name) if c["name"] not in ("payload_state", "file_uploader_user_id", "review_json")]
                assert db.exec_driver_sql(f"SELECT {','.join(columns)} FROM {name}").all() == before[name]
            assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()


def test_membership_exception_is_exact_and_resets_on_rollback_and_pool_reuse(catalogue):
    from app.models.secure_catalogue import catalogue_mutation
    engine, _, _ = catalogue
    with engine.begin() as db:
        uid = str(uuid4())
        room = scope(db)
        put(db, "users", id=uid)
        put(db, "family_rooms", id=room, owner_user_id=uid)
        mid = str(uuid4())
        put(db, "family_members", id=mid, user_id=uid, room_id=room)
        control_update(db, mode="CUTOVER")
    with engine.connect() as db:
        with db.begin():
            assert db.scalar(text("SELECT m4_mutation_authorized('membership','family_members',:id)"), {"id": mid}) == 0
            with pytest.raises(RuntimeError, match="synthetic rollback"):
                with catalogue_mutation(db, "membership", "family_members", [mid]):
                    with pytest.raises(IntegrityError):
                        db.execute(text("DELETE FROM family_members WHERE id=:id"), {"id": mid})
                    db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE owner_id=:room"), {"room": room})
                    assert db.scalar(text("SELECT m4_mutation_authorized('membership','family_members','other')")) == 0
                    db.execute(text("DELETE FROM family_members WHERE id=:id"), {"id": mid})
                    raise RuntimeError("synthetic rollback")
            assert db.scalar(text("SELECT m4_mutation_authorized('membership','family_members',:id)"), {"id": mid}) == 0
            db.rollback()
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM family_members")) == 1
        assert db.scalar(text("SELECT m4_mutation_authorized('membership','family_members',:id)"), {"id": mid}) == 0
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM family_members"))


def test_derivative_plaintext_and_exact_cutover_cleanup(catalogue):
    from app.models.secure_catalogue import catalogue_mutation
    engine, _, _ = catalogue
    _seed_retrieval_parents(engine)
    with engine.begin() as db:
        values = _chunk_values()
        values["user_confirmed"] = int(values["user_confirmed"])
        put(db, "record_chunks", **values)
        put(db, "record_embeddings", id="embedding-1", chunk_id="chunk-1", model_key="synthetic", dimensions=1024, embedding="[]")
        control_update(db, mode="CUTOVER")
        for table in ("record_chunks", "record_embeddings"):
            with pytest.raises(IntegrityError, match="secure catalogue rejects plaintext writer"):
                db.execute(text(f"UPDATE {table} SET id=id"))
            with pytest.raises(IntegrityError, match="secure catalogue rejects plaintext writer"):
                db.execute(text(f"INSERT INTO {table} SELECT * FROM {table}"))
            with pytest.raises(IntegrityError):
                db.execute(text(f"DELETE FROM {table}"))
        with catalogue_mutation(db, "cutover_cleanup", "record_embeddings", ["embedding-1"]):
            db.execute(text("DELETE FROM record_embeddings"))
        with catalogue_mutation(db, "cutover_cleanup", "record_chunks", ["chunk-1"]):
            db.execute(text("DELETE FROM record_chunks"))
        assert db.scalar(text("SELECT count(*) FROM record_chunks")) == 0


def test_payload_receipt_staging_commit_and_terminal_fences(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db)
        kid = key(db, owner)
        uid = str(uuid4())
        put(db, "users", id=uid)
        put(db, "auth_identities", id=str(uuid4()), user_id=uid, provider="google", payload_state="ENCRYPTED")
        gid = generation(db)
        oid, candidate = str(uuid4()), str(uuid4())
        put(db, "secure_operations", operation_id=oid, kind="PAYLOAD", principal_id=uid,
            snapshot_json="{}", owner_type="room", owner_id=owner, domain="room", scope_id=owner,
            control_revision=1, pointer_revision=1, source_revision=1, deletion_epoch=0,
            authorization_epoch=0, runtime_generation=gid, candidate_object_id=candidate,
            created_at=100, revision=1, state="CLAIMED")
        receipt = json.dumps(dict(adapter="local", object_name=candidate, exact_version="synthetic-version",
                                  ciphertext_size=100, ciphertext_sha256=bytes(32).hex()))
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE secure_operations SET state='RECEIPTED',receipt_json=:receipt,revision=2 WHERE operation_id=:id"), {"receipt": receipt, "id": oid})
        envelope(db, owner, kid, object_id=candidate, storage_name=candidate)
        db.execute(text("UPDATE secure_operations SET state='RECEIPTED',receipt_json=:receipt,revision=2 WHERE operation_id=:id"), {"receipt": receipt, "id": oid})
        db.execute(text("UPDATE secure_operations SET state='VERIFIED',revision=3 WHERE operation_id=:id"), {"id": oid})
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=4 WHERE operation_id=:id"), {"id": oid})
        db.execute(text("UPDATE encrypted_objects SET state='ACTIVE' WHERE object_id=:id"), {"id": candidate})
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=4 WHERE operation_id=:id"), {"id": oid})
        put(db, "protected_pointers", owner_type="room", owner_id=owner, domain="room", scope_id=owner,
            object_id=candidate, object_version=1, state="ENCRYPTED_ACTIVE", source_revision=1,
            deletion_epoch=0, revision=2, authorization_epoch=0, operation_id=oid)
        db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=4 WHERE operation_id=:id"), {"id": oid})
        for statement in ("UPDATE secure_operations SET revision=5", "UPDATE secure_operations SET state='VERIFIED',revision=5",
                          "DELETE FROM secure_operations"):
            with pytest.raises(IntegrityError):
                db.execute(text(statement))


def test_payload_can_be_abandoned_after_principal_deletion(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db)
        uid = str(uuid4())
        put(db, "users", id=uid)
        put(db, "auth_identities", id=str(uuid4()), user_id=uid, provider="google", payload_state="ENCRYPTED")
        gid = generation(db)
        oid = str(uuid4())
        put(db, "secure_operations", operation_id=oid, kind="PAYLOAD", principal_id=uid,
            snapshot_json="{}", owner_type="room", owner_id=owner, domain="room", scope_id=owner,
            control_revision=1, pointer_revision=1, source_revision=1, deletion_epoch=0,
            authorization_epoch=0, runtime_generation=gid, candidate_object_id=str(uuid4()),
            created_at=100, revision=1, state="CLAIMED")
        db.execute(text("DELETE FROM auth_identities"))
        db.execute(text("DELETE FROM users"))
        db.execute(text("UPDATE secure_operations SET state='ABANDONED',failure_code='source_changed',revision=2 WHERE operation_id=:id"), {"id": oid})
        assert db.scalar(text("SELECT state FROM secure_operations")) == "ABANDONED"


def test_delete_requires_target_tombstone_and_exact_receipt_inventory(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        uid = scope(db, "user")
        put(db, "users", id=uid)
        kid = key(db, uid, "user")
        oid = envelope(db, uid, kid, domain="user", artifact_kind="user_profile")
        put(db, "protected_pointers", owner_type="user", owner_id=uid, domain="user", scope_id=uid,
            object_id=oid, object_version=1, state="ENCRYPTED_ACTIVE", source_revision=1,
            deletion_epoch=0, revision=1, authorization_epoch=0)
        control_update(db, mode="CUTOVER")
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM users"))
        db.execute(text("UPDATE security_scopes SET deletion_epoch=1,revision=2 WHERE owner_id=:id"), {"id": uid})
        put(db, "security_tombstones", domain="user", owner_id=uid, target_kind="user", target_id=uid, deletion_epoch=1, revision=1)
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM users"))
        inventory = dict(id=str(uuid4()), domain="user", owner_id=uid, deletion_epoch=1, revision=1,
                         target_kind="user", target_id=uid, object_id=oid, object_version=1, key_id=kid, key_epoch=1,
                         adapter="local", storage_name=oid, exact_version="wrong-version", ciphertext_size=100,
                         ciphertext_sha256=bytes(32), inventory_revision=str(uuid4()), logical_state="LOGICALLY_REVOKED",
                         key_state="PENDING", physical_state="PHYSICAL_CLEANUP_PENDING", retention_state="UNKNOWN",
                         backup_state="UNKNOWN", crypto_state="CRYPTO_ERASURE_NOT_ESTABLISHED")
        for changes in ({"object_version": None}, {"key_epoch": None}, {"ciphertext_sha256": None}, {"target_id": str(uuid4())}):
            with pytest.raises(IntegrityError):
                put(db, "secure_deletion_inventory", **(inventory | changes))
        put(db, "secure_deletion_inventory", **inventory)
        with pytest.raises(IntegrityError):
            db.execute(text("DELETE FROM users"))
        db.execute(text("UPDATE secure_deletion_inventory SET exact_version='synthetic-version'"))
        db.execute(text("DELETE FROM users"))
        assert db.scalar(text("SELECT count(*) FROM security_tombstones")) == 1
        assert db.scalar(text("SELECT count(*) FROM secure_deletion_inventory")) == 1


@pytest.mark.parametrize("path", [
    ("FAILED",), ("VERIFIED", "FAILED"), ("VERIFIED", "DRAINING", "FAILED"),
    ("VERIFIED", "DRAINING", "DRAINED"),
])
def test_generation_edges_and_all_unlisted_reverse_edges(catalogue, path):
    engine, _, _ = catalogue
    states = ("REQUESTED", "VERIFIED", "DRAINING", "DRAINED", "FAILED")
    allowed = {"REQUESTED": {"VERIFIED", "FAILED"}, "VERIFIED": {"DRAINING", "FAILED"},
               "DRAINING": {"DRAINED", "FAILED"}, "DRAINED": set(), "FAILED": set()}
    with engine.begin() as db:
        gid = generation(db)
        state, revision = "REQUESTED", 1
        for target in (*path, None):
            for bad in set(states) - allowed[state]:
                with pytest.raises(IntegrityError):
                    db.execute(text("UPDATE credential_generations SET state=:state,revision=:rev WHERE generation_id=:id"),
                               {"state": bad, "rev": revision+1, "id": gid})
            if target is not None:
                evidence = ",invocation_id=:inv,boot_id=:boot" if target == "VERIFIED" else ""
                db.execute(text(f"UPDATE credential_generations SET state=:state,revision=:rev{evidence} WHERE generation_id=:id"),
                           {"state": target, "rev": revision+1, "id": gid, "inv": str(uuid4()), "boot": str(uuid4())})
                state, revision = target, revision+1


def test_signup_child_is_bound_and_parent_commit_needs_owner_key_and_child(catalogue):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db, "user")
        kid = key(db, owner, "user")
        parent = activation(db, owner, kid, kind="SIGNUP", principal_id=None, owner_type="user", domain="user")
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=kid, key_epoch=1, domain="user", owner_id=owner)
        db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,invocation_id=:inv,boot_id=:boot WHERE generation_id=:id"),
                   {"inv": str(uuid4()), "boot": str(uuid4()), "id": gid})
        control_update(db, runtime_generation=gid)
        db.execute(text("UPDATE secure_operations SET state='READY',runtime_generation=:gid,revision=2 WHERE operation_id=:id"), {"gid": gid, "id": parent})
        child, candidate = str(uuid4()), str(uuid4())
        values = dict(operation_id=child, kind="PAYLOAD", parent_operation_id=parent, principal_id=owner,
                      snapshot_json="{}", owner_type="user", owner_id=owner, domain="user", scope_id=owner,
                      control_revision=2, pointer_revision=1, source_revision=1, deletion_epoch=0,
                      authorization_epoch=0, runtime_generation=gid, candidate_object_id=candidate,
                      created_at=100, revision=1, state="CLAIMED")
        with pytest.raises(IntegrityError):
            put(db, "secure_operations", **(values | {"principal_id": str(uuid4())}))
        put(db, "secure_operations", **values)
        with pytest.raises(IntegrityError):
            db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=3 WHERE operation_id=:id"), {"id": parent})
        envelope(db, owner, kid, object_id=candidate, storage_name=candidate, domain="user", artifact_kind="user_profile")
        db.execute(text("UPDATE secure_operations SET state='RECEIPTED',receipt_json='{}',revision=2 WHERE operation_id=:id"), {"id": child})
        db.execute(text("UPDATE secure_operations SET state='VERIFIED',revision=3 WHERE operation_id=:id"), {"id": child})
        put(db, "users", id=owner)
        put(db, "auth_identities", id=str(uuid4()), user_id=owner, provider="google", payload_state="ENCRYPTED")
        db.execute(text("UPDATE key_epochs SET state='ACTIVE',revision=2 WHERE key_id=:id"), {"id": kid})
        db.execute(text("UPDATE encrypted_objects SET state='ACTIVE' WHERE object_id=:id"), {"id": candidate})
        put(db, "protected_pointers", owner_type="user", owner_id=owner, domain="user", scope_id=owner,
            object_id=candidate, object_version=1, state="ENCRYPTED_ACTIVE", source_revision=1,
            deletion_epoch=0, revision=2, authorization_epoch=0, operation_id=child)
        db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=4 WHERE operation_id=:id"), {"id": child})
        db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=3 WHERE operation_id=:id"), {"id": parent})
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='COMMITTED'")) == 2
