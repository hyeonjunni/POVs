"""B3 synthetic encrypted identities and capability activation contracts."""
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from test_secure_catalogue import catalogue, scope, key, activation, generation, put, control_update, envelope

import asyncio
import hashlib
import importlib.util
import json
import secrets
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import event
from app.storage.contracts import KeyRef, StorageReceipt
from test_secure_runtime import FakeLock


@pytest.fixture
def identity_client(catalogue, monkeypatch, request):
    from app.core import config, database
    from app.services.secure_runtime import SecureRuntime, SecureUnavailable
    from app.services import secure_catalogue as cat
    engine, url, _ = catalogue
    from alembic import command
    from scripts.adopt_alembic import _build_alembic_config
    command.upgrade(_build_alembic_config(url), getattr(request, "param", "head"))
    material, objects = {}, {}
    checked_out = set()
    event.listen(engine, "checkout", lambda connection, record, proxy: checked_out.add(id(connection)))
    event.listen(engine, "checkin", lambda connection, record: checked_out.discard(id(connection)))
    class Vault:
        def load(self, ref):
            assert not checked_out, "SQL connection held during key access"
            return material[ref]
    class Store:
        storage_binding = "local:synthetic"

        def reconcile_create(self, object_id, ciphertext_size, ciphertext_sha256):
            found = objects.get(str(object_id))
            if found is None:
                return None
            receipt, blob = found
            if len(blob) != ciphertext_size or hashlib.sha256(blob).digest() != ciphertext_sha256:
                from app.storage.contracts import StorageIntegrityError
                raise StorageIntegrityError("ciphertext mismatch")
            return receipt
        def create(self, object_id, blob):
            assert not checked_out, "SQL connection held during ciphertext create"
            assert type(object_id) is UUID
            name = str(object_id)
            assert name not in objects
            receipt = StorageReceipt("local", name, str(uuid4()), len(blob), hashlib.sha256(blob).digest())
            objects[name] = (receipt, blob)
            return receipt
        def read(self, receipt):
            assert not checked_out, "SQL connection held during ciphertext read"
            saved, blob = objects[receipt.object_name]
            assert saved == receipt
            return blob
    def operator_refresh():
        with engine.begin() as db:
            c = db.execute(text("SELECT * FROM security_control")).mappings().one()
            if c["admission"] == "OPEN":
                control_update(db, admission="CLOSED")
            if c["runtime_generation"]:
                db.execute(text("UPDATE credential_generations SET state='FAILED',revision=revision+1 WHERE generation_id=:g"), dict(g=c["runtime_generation"]))
            gid = generation(db)
            for k in db.execute(text("SELECT * FROM key_epochs WHERE state IN ('ACTIVE','DECRYPT_ONLY','PREPARED')")).mappings():
                ref = KeyRef(k["domain"], UUID(k["owner_id"]), UUID(k["key_id"]), k["epoch"])
                material.setdefault(ref, secrets.token_bytes(32))
                db.execute(text("UPDATE key_epochs SET provisioning_state='PERSISTED',revision=revision+1 WHERE key_id=:k AND epoch=:e"), dict(k=k["key_id"], e=k["epoch"]))
                put(db, "credential_generation_keys", generation_id=gid, key_id=k["key_id"], key_epoch=k["epoch"], domain=k["domain"], owner_id=k["owner_id"])
            db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,invocation_id=:i,boot_id=:b WHERE generation_id=:g"), dict(i=str(uuid4()), b=str(uuid4()), g=gid))
            if c["mode"] == "LEGACY":
                control_update(db, mode="CUTOVER")
                control_update(db, mode="SECURE")
            control_update(db, runtime_generation=gid)
            control_update(db, admission="OPEN")
    with engine.begin() as db:
        cid = db.scalar(text("SELECT catalogue_id FROM security_control"))
        put(db, "security_scopes", domain="lookup", owner_id=cid, state="ACTIVE", deletion_epoch=0, authorization_epoch=0, revision=1)
        key(db, cid, "lookup", state="ACTIVE")
    operator_refresh()
    monkeypatch.setattr(config.settings, "security_mode", "SECURE")
    monkeypatch.setattr(database, "engine", engine)
    spec = importlib.util.spec_from_file_location("b3_secure_main", Path(__file__).parents[1] / "app/main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def check():
        try:
            with engine.connect() as db:
                cat.control(db)
        except cat.SecureStateChanged:
            raise SecureUnavailable() from None
    runtime = SecureRuntime(FakeLock(), check_admission=check)
    runtime.vault, runtime.store = Vault(), Store()
    module.app.state.secure_runtime = runtime
    class Client:
        app = module.app
        refresh = staticmethod(operator_refresh)
        def request(self, method, path, **kwargs):
            async def run():
                async def monitored(scope, receive, send):
                    async def checked(message):
                        assert not checked_out, "SQL connection held during ASGI send"
                        await send(message)
                    await self.app(scope, receive, checked)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=monitored), base_url="https://synthetic.local") as client:
                    return await client.request(method, "/api/v1" + path, **kwargs)
            return asyncio.run(run())
        def signup(self, username="synthetic-user", email="synthetic@example.com"):
            body = dict(username=username, email=email, name="Synthetic Person", password="synthetic-password")
            headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
            first = self.request("POST", "/auth/signup", json=body, headers=headers)
            assert first.status_code == 202, first.text
            self.refresh()
            headers["X-Activation-Operation"] = first.json()["operation_id"]
            final = self.request("POST", "/auth/signup", json=body, headers=headers)
            assert final.status_code == 200, final.text
            return final.json(), body, headers
    client = Client()
    client.engine, client.material, client.objects = engine, material, objects
    return client


def test_old_client_activation_denial_has_no_side_effects(identity_client):
    c = identity_client
    response = c.request("POST", "/auth/signup", json=dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Secure activation requires a compatible client", "code": "activation_client_required"}
    assert response.headers["retry-after"] == "2"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations")) == 0
        assert db.scalar(text("SELECT count(*) FROM key_epochs WHERE domain='user'")) == 0


def test_pending_exact_contract_capability_replay_and_expiry(identity_client):
    c = identity_client
    body = dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    first = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert first.status_code == 202, first.text
    status = first.json()
    assert set(status) == {"operation_id", "kind", "state", "expires_at", "retry_after_seconds"}
    assert status["kind"] == "SIGNUP" and status["state"] == "WAITING_CREDENTIAL" and status["retry_after_seconds"] == 2
    assert first.headers["location"] == "/api/v1/secure-operations/" + status["operation_id"]
    assert first.headers["retry-after"] == "2" and first.headers["cache-control"] == "no-store"
    assert c.request("POST", "/auth/signup", json=body, headers=headers).json() == status
    assert c.request("GET", "/secure-operations/" + status["operation_id"], headers=headers).json() == status
    assert c.request("GET", "/secure-operations/" + status["operation_id"], headers={"Idempotency-Key": secrets.token_urlsafe(32)}).status_code == 404
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM users")) == 0
        assert db.scalar(text("SELECT count(*) FROM auth_identities")) == 0
        assert db.scalar(text("SELECT count(*) FROM blind_indexes")) == 0
        journal = str(db.execute(text("SELECT * FROM secure_operations")).all())
        assert all(value not in journal for value in body.values())


def test_signup_login_profile_and_committed_replay(identity_client):
    c = identity_client
    session, body, headers = c.signup()
    assert session["token_type"] == "bearer"
    replay = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert replay.status_code == 409 and replay.json()["code"] == "signup_completed"
    login = c.request("POST", "/auth/login", json={k: body[k] for k in ("username", "password")})
    assert login.status_code == 200, login.text
    auth = {"Authorization": "Bearer " + session["access_token"]}
    assert c.request("GET", "/auth/me", headers=auth).json() == session["user"]
    assert c.request("POST", "/auth/login", json=dict(username=body["username"], password="wrong-password")).status_code == 401
    assert c.request("DELETE", "/auth/me", headers=auth).status_code == 503
    with c.engine.connect() as db:
        user = db.execute(text("SELECT * FROM users")).mappings().one()
        assert all(user[k] is None for k in ("email", "name", "username", "password_hash", "profile_chunk"))
        identity = db.execute(text("SELECT * FROM auth_identities")).mappings().one()
        assert identity["payload_state"] == "ENCRYPTED" and identity["credential_hash"] is None


def create_room(c, session, name="Synthetic Room"):
    headers = {"Authorization": "Bearer " + session["access_token"], "Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    body = {"name": name}
    pending = c.request("POST", "/families", json=body, headers=headers)
    assert pending.status_code == 202, pending.text
    c.refresh()
    headers["X-Activation-Operation"] = pending.json()["operation_id"]
    final = c.request("POST", "/families", json=body, headers=headers)
    assert final.status_code == 200, final.text
    return final.json(), headers


def test_profile_email_uniqueness_and_room_scoped_public_dtos(identity_client):
    c = identity_client
    one, _, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    room, headers = create_room(c, one)
    auth = {"Authorization": "Bearer " + one["access_token"]}
    other = {"Authorization": "Bearer " + two["access_token"]}
    assert c.request("GET", "/families", headers=auth).json() == [room]
    assert c.request("GET", "/families", headers=other).json() == []
    assert c.request("GET", "/families/" + room["room_id"] + "/members", headers=other).status_code in (403, 404)
    changed = dict(name="Changed Synthetic", age=40, gender="synthetic", phone="000000", email="changed@example.com")
    updated = c.request("PUT", "/auth/profile", json=changed, headers=auth)
    assert updated.status_code == 200, updated.text
    assert updated.json()["email"] == "changed@example.com"
    members = c.request("GET", "/families/" + room["room_id"] + "/members", headers=auth)
    assert members.status_code == 200 and members.json()[0]["name"] == "Changed Synthetic"
    conflict = c.request("PUT", "/auth/profile", json=changed | {"email": "other@example.com"}, headers=auth)
    assert conflict.status_code == 409
    assert c.request("GET", "/auth/me", headers=auth).json()["email"] == "changed@example.com"
    replay = c.request("POST", "/families", json={"name": "attempted replacement"}, headers=headers)
    assert replay.status_code == 200 and replay.json() == room
    assert c.request("DELETE", "/families/" + room["room_id"], headers=auth).status_code == 503


def test_invite_join_uses_opaque_capability_and_only_room_profile_copies(identity_client):
    c = identity_client
    one, _, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    room, _ = create_room(c, one)
    auth = {"Authorization": "Bearer " + two["access_token"]}
    preview = c.request("GET", "/families/invite/" + room["invite_code"].upper(), headers=auth)
    assert preview.status_code == 200 and preview.json()["members"][0]["user_id"] == one["user"]["user_id"]
    bad = room["invite_code"][:-1] + ("0" if room["invite_code"][-1] != "0" else "1")
    assert c.request("GET", "/families/invite/" + bad, headers=auth).status_code == 404
    joined = c.request("POST", "/families/join", headers=auth, json=dict(invite_code=room["invite_code"], related_to_user_id=one["user"]["user_id"], relation_to_related_user="child"))
    assert joined.status_code == 200 and joined.json() == room
    members = c.request("GET", "/families/" + room["room_id"] + "/members", headers=auth)
    assert members.status_code == 200 and len(members.json()) == 2
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT name FROM family_rooms")) is None
        assert db.scalar(text("SELECT invite_code FROM family_rooms")) is None
        assert db.scalar(text("SELECT count(*) FROM family_members WHERE relation_to_related_user IS NOT NULL")) == 0


@pytest.mark.parametrize("field", ["password", "email", "username"])
def test_secure_validation_never_echoes_sensitive_input(identity_client, field):
    body = dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password")
    body[field] = "PRIVATE-INVALID-" + "!" * 150
    result = identity_client.request("POST", "/auth/signup", json=body)
    assert result.status_code in (400, 422)
    assert "PRIVATE-INVALID" not in result.text


def test_lookup_collision_never_authenticates(identity_client, monkeypatch):
    from app.api.routes import secure_operations as routes
    c = identity_client
    session, body, _ = c.signup()
    with c.engine.connect() as db:
        token = bytes(db.scalar(text("SELECT token FROM blind_indexes WHERE kind='local_username'")))
    monkeypatch.setattr(routes, "blind_index", lambda *args: token)
    result = c.request("POST", "/auth/login", json=dict(username="other-user", password=body["password"]))
    assert result.status_code == 401 and "access_token" not in result.json()


@pytest.mark.parametrize("mutation", ["identity", "user_epoch", "lookup_key", "profile_pointer"])
def test_login_final_revalidation_denies_token_issuance_races(identity_client, monkeypatch, mutation):
    from app.api.routes import secure_operations as routes
    c = identity_client
    session, body, _ = c.signup()
    original = routes.create_access_token
    def race(uid):
        token = original(uid)
        with c.engine.begin() as db:
            if mutation == "identity":
                db.execute(text("UPDATE auth_identities SET provider='google' WHERE user_id=:u"), dict(u=uid))
            elif mutation == "user_epoch":
                db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='user' AND owner_id=:u"), dict(u=uid))
            elif mutation == "lookup_key":
                db.execute(text("UPDATE key_epochs SET state='REVOCATION_PENDING',revision=revision+1 WHERE domain='lookup'"))
            else:
                db.execute(text("UPDATE protected_pointers SET source_revision=source_revision+1,revision=revision+1 WHERE owner_type='user' AND owner_id=:u"), dict(u=uid))
        return token
    monkeypatch.setattr(routes, "create_access_token", race)
    result = c.request("POST", "/auth/login", json={k: body[k] for k in ("username", "password")})
    assert result.status_code == 401 and "access_token" not in result.json()


def test_pending_expiry_closed_generation_and_room_principal_mismatch(identity_client, monkeypatch):
    from app.api.routes import secure_operations as routes
    c = identity_client
    one, _, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    auth = {"Authorization": "Bearer " + one["access_token"], "Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    first = c.request("POST", "/families", json={"name": "pending"}, headers=auth)
    assert first.status_code == 202
    path = "/secure-operations/" + first.json()["operation_id"]
    wrong = auth | {"Authorization": "Bearer " + two["access_token"]}
    assert c.request("GET", path, headers=wrong).status_code == 404
    assert c.request("POST", "/families", json={"name": "pending"}, headers=wrong).status_code == 404
    monkeypatch.setattr(routes.time, "time", lambda: first.json()["expires_at"])
    expired = c.request("GET", path, headers=auth)
    assert expired.status_code == 410 and expired.json()["state"] == "EXPIRED" and expired.json()["retry_after_seconds"] is None
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT state FROM secure_operations WHERE operation_id=:o"), dict(o=first.json()["operation_id"])) == "WAITING_CREDENTIAL"
    with c.engine.begin() as db:
        control_update(db, admission="CLOSED")
    closed = c.request("GET", path, headers=auth)
    assert closed.status_code == 503 and closed.headers["retry-after"] == "2"


def test_interrupted_signup_reentered_payload_uses_fresh_children(identity_client, monkeypatch):
    c = identity_client
    from app.api.routes import secure_operations as routes
    body = dict(username="synthetic", email="synthetic@example.com", name="Initial", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    pending = c.request("POST", "/auth/signup", json=body, headers=headers)
    c.refresh()
    headers["X-Activation-Operation"] = pending.json()["operation_id"]
    original = routes.cat.authenticate_readback
    def interrupted(**kwargs):
        raise RuntimeError("synthetic interruption after journal")
    monkeypatch.setattr(routes.cat, "authenticate_readback", interrupted)
    failed = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert failed.status_code == 503
    monkeypatch.setattr(routes.cat, "authenticate_readback", original)
    final = c.request("POST", "/auth/signup", json=body | {"name": "Reentered"}, headers=headers)
    assert final.status_code == 200 and final.json()["user"]["name"] == "Reentered"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='ABANDONED'")) == 1
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE state='STAGED'")) == 1


def test_unknown_create_receipt_blocks_activation_retry(identity_client, monkeypatch):
    c = identity_client
    body = dict(username="synthetic", email="synthetic@example.com", name="Initial", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    pending = c.request("POST", "/auth/signup", json=body, headers=headers)
    c.refresh()
    headers["X-Activation-Operation"] = pending.json()["operation_id"]
    store = c.app.state.secure_runtime.store
    original = store.create
    def lost(*args):
        original(*args)
        raise RuntimeError("synthetic lost receipt")
    monkeypatch.setattr(store, "create", lost)
    assert c.request("POST", "/auth/signup", json=body, headers=headers).status_code == 503
    monkeypatch.setattr(store, "create", original)
    assert c.request("POST", "/auth/signup", json=body, headers=headers).status_code == 503
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM users")) == 0
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE kind='PAYLOAD'")) == 1


def test_invitation_grant_cannot_be_minted_from_unverified_mapping(identity_client):
    from app.services import secure_catalogue as cat
    c = identity_client
    one, _, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    room, _ = create_room(c, one)
    uid, rid = UUID(two["user"]["user_id"]), UUID(room["room_id"])
    with c.engine.begin() as db:
        saved = cat.invitation_candidate(db, principal_id=uid, room_id=rid)
    async def attempt():
        async with c.app.state.secure_runtime.operation_guard() as guard:
            with pytest.raises(cat.SecureStateChanged):
                with cat.invitation_access(saved, guard=guard, authenticated_payload={"room_id": str(rid), "invite_code": "forged"}, supplied_capability="forged"):
                    with c.engine.begin() as db:
                        cat.snapshot(db, principal_id=uid, targets=(("room", rid),))
    asyncio.run(attempt())


def test_multi_epoch_ambiguity_never_picks_a_candidate(identity_client):
    from app.api.routes import secure_operations as routes
    c = identity_client
    one, body, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    with c.engine.begin() as db:
        cid = db.scalar(text("SELECT catalogue_id FROM security_control"))
        db.execute(text("UPDATE key_epochs SET state='DECRYPT_ONLY',revision=revision+1 WHERE domain='lookup'"))
        kid = key(db, cid, "lookup", state="ACTIVE")
        aid = db.scalar(text("SELECT id FROM auth_identities WHERE user_id=:u"), dict(u=two["user"]["user_id"]))
    c.refresh()
    with c.engine.connect() as db:
        keys = routes.lookup_keys(db)
    values = routes.tokens(c.app.state.secure_runtime, keys, "local_username", body["username"])
    with c.engine.begin() as db:
        routes.insert_tokens(db, "local_username", values, two["user"]["user_id"], aid)
    result = c.request("POST", "/auth/login", json={k: body[k] for k in ("username", "password")})
    assert result.status_code == 401 and "access_token" not in result.json()


def test_duplicate_email_fails_activation_without_linking(identity_client):
    c = identity_client
    one, _, _ = c.signup()
    body = dict(username="other-user", email="synthetic@example.com", name="Other", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    pending = c.request("POST", "/auth/signup", json=body, headers=headers)
    c.refresh()
    headers["X-Activation-Operation"] = pending.json()["operation_id"]
    result = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert result.status_code == 409 and result.json()["code"] == "identity_conflict"
    poll = c.request("GET", "/secure-operations/" + pending.json()["operation_id"], headers=headers)
    assert poll.status_code == 409 and poll.json()["state"] == "FAILED"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM users")) == 1
        assert db.scalar(text("SELECT count(*) FROM auth_identities")) == 1


@pytest.mark.parametrize("mutation", ["room_tombstone", "room_key", "user_tombstone"])
def test_invite_join_final_authority_race_never_adds_membership(identity_client, monkeypatch, mutation):
    from app.api.routes import secure_operations as routes
    c = identity_client
    one, _, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    room, _ = create_room(c, one)
    original = routes.cat.authenticate_readback
    def race(**kwargs):
        proof = original(**kwargs)
        with c.engine.begin() as db:
            if mutation == "room_key":
                db.execute(text("UPDATE key_epochs SET state='REVOCATION_PENDING',revision=revision+1 WHERE domain='room' AND owner_id=:r"), dict(r=room["room_id"]))
            else:
                domain, owner = ("room", room["room_id"]) if mutation == "room_tombstone" else ("user", two["user"]["user_id"])
                put(db, "security_tombstones", domain=domain, owner_id=owner, target_kind=domain, target_id=owner, deletion_epoch=1, revision=1)
        return proof
    monkeypatch.setattr(routes.cat, "authenticate_readback", race)
    result = c.request("POST", "/families/join", headers={"Authorization": "Bearer " + two["access_token"]}, json=dict(invite_code=room["invite_code"], related_to_user_id=one["user"]["user_id"], relation_to_related_user="child"))
    assert result.status_code == 503
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM family_members WHERE room_id=:r"), dict(r=room["room_id"])) == 1


def test_invitation_proof_replay_and_other_principal_fail(identity_client):
    from app.services import secure_catalogue as cat
    c = identity_client
    one, _, _ = c.signup()
    two, _, _ = c.signup("other-user", "other@example.com")
    room, _ = create_room(c, one)
    uid, rid = UUID(two["user"]["user_id"]), UUID(room["room_id"])
    runtime = c.app.state.secure_runtime
    async def attempt():
        async with runtime.operation_guard() as guard:
            with c.engine.begin() as db:
                saved = cat.invitation_candidate(db, principal_id=uid, room_id=rid)
                row = db.execute(text("SELECT * FROM encrypted_objects WHERE object_id=:o"), dict(o=str(saved.pointers[0][4]))).mappings().one()
                values = cat.envelope_values(row)
            payload, proof = cat.authenticate_invitation(saved, values=values, supplied_capability=room["invite_code"], vault=runtime.vault, store=runtime.store, guard=guard)
            with cat.invitation_access(saved, guard=guard, authenticated_payload=proof, supplied_capability=room["invite_code"]):
                with c.engine.begin() as db:
                    with pytest.raises(cat.SecureStateChanged):
                        cat.snapshot(db, principal_id=UUID(one["user"]["user_id"]), targets=(("room", rid),))
                    assert cat.snapshot(db, principal_id=uid, targets=(("room", rid),)) == saved
            with pytest.raises(cat.SecureStateChanged):
                with cat.invitation_access(saved, guard=guard, authenticated_payload=proof, supplied_capability=room["invite_code"]):
                    pass
    asyncio.run(attempt())


def test_membership_removal_blocks_later_room_read(identity_client):
    from app.models.secure_catalogue import catalogue_mutation
    c = identity_client
    one, _, _ = c.signup()
    room, _ = create_room(c, one)
    with c.engine.begin() as db:
        row = db.execute(text("SELECT id FROM family_members WHERE room_id=:r"), dict(r=room["room_id"])).scalar_one()
        with catalogue_mutation(db, "membership", "family_members", (row,)):
            db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room' AND owner_id=:r"), dict(r=room["room_id"]))
            db.execute(text("DELETE FROM family_members WHERE id=:m"), dict(m=row))
    result = c.request("GET", "/families/" + room["room_id"] + "/members", headers={"Authorization": "Bearer " + one["access_token"]})
    assert result.status_code == 404


def test_lookup_missing_accepted_epoch_fails_closed(identity_client):
    c = identity_client
    session, body, _ = c.signup()
    with c.engine.begin() as db:
        cid = db.scalar(text("SELECT catalogue_id FROM security_control"))
        key(db, cid, "lookup", state="DECRYPT_ONLY")
    result = c.request("POST", "/auth/login", json={k: body[k] for k in ("username", "password")})
    assert result.status_code in (401, 503) and "access_token" not in result.json()


def test_stale_ready_poll_readonly_and_continuation_reconciles(identity_client):
    c = identity_client
    body = dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    first = c.request("POST", "/auth/signup", json=body, headers=headers)
    oid = first.json()["operation_id"]
    c.refresh()
    ready = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert ready.status_code == 202 and ready.json()["state"] == "READY"
    c.refresh()
    stale = c.request("GET", "/secure-operations/" + oid, headers=headers)
    assert stale.status_code == 200 and stale.json()["state"] == "WAITING_CREDENTIAL"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT state FROM secure_operations WHERE operation_id=:o"), dict(o=oid)) == "READY"
    final = c.request("POST", "/auth/signup", json=body, headers=headers | {"X-Activation-Operation": oid})
    assert final.status_code == 200


def test_racing_continuations_commit_once_and_expired_commit_never_reissues(identity_client, monkeypatch):
    from app.api.routes import secure_operations as routes
    c = identity_client
    body = dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    first = c.request("POST", "/auth/signup", json=body, headers=headers)
    c.refresh()
    headers["X-Activation-Operation"] = first.json()["operation_id"]
    async def race():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.app), base_url="https://synthetic.local") as client:
            return await asyncio.gather(*(client.post("/api/v1/auth/signup", json=body, headers=headers) for _ in range(2)))
    results = asyncio.run(race())
    assert sorted(r.status_code for r in results) == [200, 409]
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM users")) == 1
        assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE kind='SIGNUP' AND state='COMMITTED'")) == 1
    monkeypatch.setattr(routes.time, "time", lambda: first.json()["expires_at"])
    expired = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert expired.status_code == 410 and "access_token" not in expired.json()


def test_final_uniqueness_race_fails_parent_without_reassigning_owner(identity_client, monkeypatch):
    from app.api.routes import secure_operations as routes
    c = identity_client
    existing, _, _ = c.signup()
    body = dict(username="new-user", email="new@example.com", name="Synthetic", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    first = c.request("POST", "/auth/signup", json=body, headers=headers)
    c.refresh()
    headers["X-Activation-Operation"] = first.json()["operation_id"]
    with c.engine.connect() as db:
        keys = routes.lookup_keys(db)
        aid = db.scalar(text("SELECT id FROM auth_identities WHERE user_id=:u"), dict(u=existing["user"]["user_id"]))
    values = routes.tokens(c.app.state.secure_runtime, keys, "local_username", body["username"])
    original, changed = routes.cat.authenticate_readback, []
    def win(**kwargs):
        proof = original(**kwargs)
        if not changed:
            with c.engine.begin() as db:
                routes.insert_tokens(db, "local_username", values, existing["user"]["user_id"], aid)
            changed.append(True)
        return proof
    monkeypatch.setattr(routes.cat, "authenticate_readback", win)
    result = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert result.status_code == 409 and result.json()["code"] == "identity_conflict"
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM users")) == 1
        assert db.scalar(text("SELECT state FROM secure_operations WHERE operation_id=:o"), dict(o=first.json()["operation_id"])) == "FAILED"


@pytest.mark.parametrize("key_value", ["", "x" * 42, "x" * 44, "!" * 43])
def test_invalid_continuation_key_never_allocates(identity_client, key_value):
    c = identity_client
    result = c.request("POST", "/auth/signup", json=dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password"), headers={"Prefer": "respond-async", "Idempotency-Key": key_value})
    assert result.status_code == 400
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM secure_operations")) == 0


@pytest.mark.parametrize("field", ["user_id", "identity_id", "normalized_identifier"])
def test_authenticated_identity_substitution_and_stale_rename_lookup_are_denied(identity_client, field):
    from app.api.routes import secure_operations as routes
    from app.services import secure_catalogue as cat
    c = identity_client
    session, body, _ = c.signup()
    uid = session["user"]["user_id"]
    runtime = c.app.state.secure_runtime
    async def change_payload():
        async with runtime.operation_guard():
            with c.engine.begin() as db:
                aid = UUID(db.scalar(text("SELECT id FROM auth_identities WHERE user_id=:u"), dict(u=uid)))
                saved = cat.snapshot(db, principal_id=UUID(uid), targets=(("auth_identity", aid),))
                values = routes.detached(db, saved)
            identity = routes.decrypt(runtime, values)[0]
            identity[field] = "renamed-user" if field == "normalized_identifier" else str(uuid4())
            staged = routes.stage_update(runtime, uid, ("auth_identity", aid), identity)
            with c.engine.begin() as db:
                routes.publish_update(db, staged)
    asyncio.run(change_payload())
    result = c.request("POST", "/auth/login", json={k: body[k] for k in ("username", "password")})
    assert result.status_code == 401 and "access_token" not in result.json()


def test_synthetic_opt_in_client_stops_polling_at_30_seconds(identity_client):
    c = identity_client
    body = dict(username="synthetic", email="synthetic@example.com", name="Synthetic", password="synthetic-password")
    headers = {"Prefer": "respond-async", "Idempotency-Key": secrets.token_urlsafe(32)}
    pending = c.request("POST", "/auth/signup", json=body, headers=headers)
    oid = pending.json()["operation_id"]
    trace = []
    # Synthetic caller policy only: no wall-clock sleep and no frontend/device claim.
    for elapsed in range(0, 30, 2):
        status = c.request("GET", "/secure-operations/" + oid, headers=headers)
        assert status.status_code == 200 and status.json()["retry_after_seconds"] == 2
        trace.append((elapsed, status.json()["state"]))
    assert trace[-1] == (28, "WAITING_CREDENTIAL") and len(trace) == 15
    explicit_retry = c.request("POST", "/auth/signup", json=body, headers=headers)
    assert explicit_retry.status_code == 202 and explicit_retry.json()["operation_id"] == oid


@pytest.mark.parametrize("old_state", ["ABANDONED", "ORPHAN_UNRESOLVED"])
def test_activation_retry_retains_known_receipt_history_but_blocks_unknown(catalogue, old_state):
    engine, _, _ = catalogue
    with engine.begin() as db:
        owner = scope(db, "user")
        kid = key(db, owner, "user")
        parent = activation(db, owner, kid, kind="SIGNUP", principal_id=None, owner_type="user", domain="user")
        gid = generation(db)
        put(db, "credential_generation_keys", generation_id=gid, key_id=kid, key_epoch=1, domain="user", owner_id=owner)
        db.execute(text("UPDATE credential_generations SET state='VERIFIED',revision=2,invocation_id=:inv,boot_id=:boot WHERE generation_id=:id"),
                   dict(inv=str(uuid4()), boot=str(uuid4()), id=gid))
        control_update(db, runtime_generation=gid)
        db.execute(text("UPDATE secure_operations SET state='READY',runtime_generation=:gid,revision=2 WHERE operation_id=:id"), dict(gid=gid, id=parent))
        children = []
        for old in (True, False):
            child, candidate = str(uuid4()), str(uuid4())
            put(db, "secure_operations", operation_id=child, kind="PAYLOAD", parent_operation_id=parent, principal_id=owner,
                snapshot_json="{}", owner_type="user", owner_id=owner, domain="user", scope_id=owner,
                control_revision=2, pointer_revision=1, source_revision=1, deletion_epoch=0,
                authorization_epoch=0, runtime_generation=gid, candidate_object_id=candidate,
                created_at=100, revision=1, state="CLAIMED")
            if old and old_state == "ORPHAN_UNRESOLVED":
                db.execute(text("UPDATE secure_operations SET state='ORPHAN_UNRESOLVED',failure_code='receipt_unknown',revision=2 WHERE operation_id=:id"), dict(id=child))
                continue
            envelope(db, owner, kid, object_id=candidate, storage_name=candidate, domain="user", artifact_kind="user_profile")
            db.execute(text("UPDATE secure_operations SET state='RECEIPTED',receipt_json='{}',revision=2 WHERE operation_id=:id"), dict(id=child))
            if old:
                db.execute(text("UPDATE secure_operations SET state='ABANDONED',failure_code='cancelled',revision=3 WHERE operation_id=:id"), dict(id=child))
                continue
            db.execute(text("UPDATE secure_operations SET state='VERIFIED',revision=3 WHERE operation_id=:id"), dict(id=child))
            children.append((child, candidate))
        put(db, "users", id=owner)
        put(db, "auth_identities", id=str(uuid4()), user_id=owner, provider="google", payload_state="ENCRYPTED")
        db.execute(text("UPDATE key_epochs SET state='ACTIVE',revision=2 WHERE key_id=:id"), dict(id=kid))
        for child, candidate in children:
            db.execute(text("UPDATE encrypted_objects SET state='ACTIVE' WHERE object_id=:id"), dict(id=candidate))
            put(db, "protected_pointers", owner_type="user", owner_id=owner, domain="user", scope_id=owner,
                object_id=candidate, object_version=1, state="ENCRYPTED_ACTIVE", source_revision=1,
                deletion_epoch=0, revision=2, authorization_epoch=0, operation_id=child)
            db.execute(text("UPDATE secure_operations SET state='COMMITTED',revision=4 WHERE operation_id=:id"), dict(id=child))
        statement = text("UPDATE secure_operations SET state='COMMITTED',revision=3 WHERE operation_id=:id")
        if old_state == "ORPHAN_UNRESOLVED":
            with pytest.raises(IntegrityError):
                db.execute(statement, dict(id=parent))
        else:
            db.execute(statement, dict(id=parent))
            assert db.scalar(text("SELECT count(*) FROM secure_operations WHERE state='ABANDONED'")) == 1
