"""Detached, opaque catalogue transactions. Caller owns transaction and admission guard.

SQL functions never create/commit sessions or perform slow I/O. authenticate_readback
is explicitly called between transactions and retains only an opaque proof digest.
"""
from dataclasses import dataclass, replace
import hashlib
import re
import json
import time
import asyncio
import os
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.storage.contracts import KeyRef, PayloadIdentity, SealedPayload, StorageReceipt, positive_int


class SecureStateChanged(RuntimeError):
    def __init__(self):
        super().__init__("source_changed")


KINDS = dict(user="user_profile", auth_identity="auth_identity", room="room_metadata",
             record="record_metadata", file="original", job="processing_result", record_index="room_derivatives")


def _uuid(value):
    try:
        if type(value) is UUID:
            return value
        if type(value) is str and str(UUID(value)) == value:
            return UUID(value)
    except ValueError:
        pass
    raise SecureStateChanged()


def _count(value, minimum=1):
    try:
        positive_int(value, minimum=minimum)
        return value
    except ValueError:
        raise SecureStateChanged() from None


def lock_rooms(db, room_ids):
    """Acquire every room guard in sorted order before any child write.

    A no-op scope update leaves snapshot revisions unchanged. It provides the
    same database fence to quota, publication, review and deletion transactions.
    """
    for room_id in sorted({str(_uuid(room)) for room in room_ids}):
        _cas(db, "UPDATE security_scopes SET revision=revision WHERE domain='room' AND owner_id=:r",
             r=room_id)


def _row(db, sql, **params):
    try:
        row = db.execute(text(sql), params).mappings().one_or_none()
    except SQLAlchemyError:
        raise SecureStateChanged() from None
    if row is None:
        raise SecureStateChanged()
    return row


def _cas(db, sql, **params):
    try:
        result = db.execute(text(sql), params)
    except SQLAlchemyError:
        raise SecureStateChanged() from None
    if result.rowcount != 1:
        raise SecureStateChanged()


@dataclass(frozen=True)
class CatalogueSnapshot:
    principal_id: UUID
    scopes: tuple
    control_revision: int
    runtime_generation: UUID
    pointers: tuple
    key_refs: tuple
    operation_id: UUID | None = None

    def __post_init__(self):
        if type(self.principal_id) is not UUID or type(self.runtime_generation) is not UUID:
            raise SecureStateChanged()
        _count(self.control_revision)
        if self.operation_id is not None and type(self.operation_id) is not UUID:
            raise SecureStateChanged()
        for values in (self.scopes, self.pointers, self.key_refs):
            if type(values) is not tuple or len(values) > 10000 or len(set(values)) != len(values):
                raise SecureStateChanged()
        for item in self.scopes:
            if type(item) is not tuple or len(item) != 5 or item[0] not in ("room", "user", "lookup") or type(item[1]) is not UUID:
                raise SecureStateChanged()
            _count(item[2], 0); _count(item[3], 0); _count(item[4])
        for item in self.pointers:
            if type(item) is not tuple or len(item) != 6 or item[0] not in KINDS or type(item[1]) is not UUID:
                raise SecureStateChanged()
            _count(item[2]); _count(item[3])
            if (item[4] is None) != (item[5] is None):
                raise SecureStateChanged()
            if item[4] is not None:
                if type(item[4]) is not UUID:
                    raise SecureStateChanged()
                _count(item[5])
        if any(type(ref) is not KeyRef for ref in self.key_refs):
            raise SecureStateChanged()
        if len({s[:2] for s in self.scopes}) != len(self.scopes) or len({p[:2] for p in self.pointers}) != len(self.pointers):
            raise SecureStateChanged()
        if (self.scopes != tuple(sorted(self.scopes)) or self.pointers != tuple(sorted(self.pointers))
                or self.key_refs != tuple(sorted(self.key_refs, key=lambda r: (r.domain, str(r.owner_id), str(r.key_id), r.epoch)))):
            raise SecureStateChanged()

    def to_json(self):
        value = dict(version=1, principal_id=str(self.principal_id), scopes=self.scopes,
                     control_revision=self.control_revision, runtime_generation=str(self.runtime_generation),
                     pointers=self.pointers, key_refs=[(r.domain, r.owner_id, r.key_id, r.epoch) for r in self.key_refs],
                     operation_id=str(self.operation_id) if self.operation_id else None)
        return json.dumps(value, default=str, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw):
        try:
            if type(raw) is not str or len(raw) > 4 * 1024 * 1024:
                raise ValueError()
            value = json.loads(raw)
            if set(value) != {"version", "principal_id", "scopes", "control_revision", "runtime_generation", "pointers", "key_refs", "operation_id"} or type(value["version"]) is not int or value.pop("version") != 1:
                raise ValueError()
            value["principal_id"] = _uuid(value["principal_id"])
            value["runtime_generation"] = _uuid(value["runtime_generation"])
            value["operation_id"] = _uuid(value["operation_id"]) if value["operation_id"] is not None else None
            value["scopes"] = tuple((d, _uuid(o), de, ae, r) for d, o, de, ae, r in value["scopes"])
            value["pointers"] = tuple((t, _uuid(o), r, sr, _uuid(obj) if obj else None, v) for t, o, r, sr, obj, v in value["pointers"])
            value["key_refs"] = tuple(KeyRef(d, _uuid(o), _uuid(k), e) for d, o, k, e in value["key_refs"])
            result = cls(**value)
            if result.to_json() != raw:
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError):
            raise SecureStateChanged() from None


def control(db):
    row = _row(db, "SELECT * FROM security_control WHERE id=1")
    if row["mode"] != "SECURE" or row["admission"] != "OPEN" or row["schema_version"] != 1:
        raise SecureStateChanged()
    generation = _row(db, "SELECT * FROM credential_generations WHERE generation_id=:id", id=row["runtime_generation"])
    if generation["state"] != "VERIFIED" or generation["catalogue_id"] != row["catalogue_id"]:
        raise SecureStateChanged()
    return row


def ownership(db, owner_type, owner_id):
    """Resolve actual polymorphic ownership, never trust a pointer's claimed scope."""
    oid = str(_uuid(owner_id))
    if owner_type == "user":
        _row(db, "SELECT id FROM users WHERE id=:id", id=oid)
        return "user", oid, None
    if owner_type == "auth_identity":
        row = _row(db, "SELECT user_id FROM auth_identities WHERE id=:id", id=oid)
        return "user", row["user_id"], None
    if owner_type == "room":
        _row(db, "SELECT id FROM family_rooms WHERE id=:id", id=oid)
        return "room", oid, None
    if owner_type in ("record", "record_index"):
        row = _row(db, "SELECT room_id FROM uploads WHERE id=:id", id=oid)
        return "room", row["room_id"], oid
    if owner_type == "file":
        row = _row(db, "SELECT u.id,u.room_id FROM upload_files f JOIN uploads u ON u.id=f.upload_id WHERE f.id=:id", id=oid)
        return "room", row["room_id"], row["id"]
    if owner_type == "job":
        row = _row(db, "SELECT u.id,u.room_id FROM processing_jobs j JOIN uploads u ON u.id=j.record_id AND u.room_id=j.room_id JOIN upload_files f ON f.id=j.upload_file_id AND f.upload_id=u.id WHERE j.id=:id", id=oid)
        return "room", row["room_id"], row["id"]
    raise SecureStateChanged()


def _snapshot(db, *, principal_id, targets, invitation_room=None):
    principal_id = _uuid(principal_id)
    _row(db, "SELECT u.id FROM users u WHERE u.id=:id AND EXISTS (SELECT 1 FROM auth_identities a WHERE a.user_id=u.id) AND NOT EXISTS (SELECT 1 FROM security_tombstones t WHERE t.domain='user' AND t.owner_id=u.id AND t.target_kind='user' AND t.target_id=u.id)", id=str(principal_id))
    current = control(db)
    scopes, pointers, refs, operations = {}, [], set(), set()
    object_refs, source_refs = set(), set()
    targets = tuple(targets)
    if not targets or len(targets) > 10000 or len(set(targets)) != len(targets):
        raise SecureStateChanged()
    requested_scopes = {("user", str(principal_id))}
    for kind, owner in sorted(targets):
        domain, scope_id, record_id = ownership(db, kind, owner)
        if domain == "user" and scope_id != str(principal_id):
            raise SecureStateChanged()
        if domain == "room":
            if scope_id != invitation_room:
                _row(db, "SELECT id FROM family_members WHERE room_id=:room AND user_id=:uid", room=scope_id, uid=str(principal_id))
            if record_id is not None:
                from app.services.record_policy import require_record_access
                try:
                    require_record_access(db, str(principal_id), record_id)
                except SQLAlchemyError as error:
                    raise SecureStateChanged() from error
        tombstones = db.execute(text("SELECT target_kind,target_id FROM security_tombstones WHERE domain=:d AND owner_id=:o"), dict(d=domain, o=scope_id)).all()
        if any(t[0] in ("user", "room") or t[1] == record_id for t in tombstones):
            raise SecureStateChanged()
        pointer = _row(db, "SELECT * FROM protected_pointers WHERE owner_type=:t AND owner_id=:o", t=kind, o=str(owner))
        if (pointer["domain"], pointer["scope_id"]) != (domain, scope_id) or pointer["state"] not in ("MIGRATION_PENDING", "ENCRYPTED_ACTIVE"):
            raise SecureStateChanged()
        requested_scopes.add((domain, scope_id))
        pointers.append((kind, _uuid(owner), _count(pointer["revision"]), _count(pointer["source_revision"]),
                         _uuid(pointer["object_id"]) if pointer["object_id"] else None, pointer["object_version"]))
        if pointer["operation_id"]:
            operation = _row(db, "SELECT * FROM secure_operations WHERE operation_id=:id", id=pointer["operation_id"])
            if operation["state"] in ("CLAIMED", "RECEIPTED", "VERIFIED"):
                operations.add(_uuid(pointer["operation_id"]))
        if pointer["object_id"]:
            obj = _row(db, "SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v", o=pointer["object_id"], v=pointer["object_version"])
            if obj["state"] != "ACTIVE" or obj["adapter"] not in ("local", "gcs") or (obj["domain"], obj["owner_id"], obj["record_id"], obj["artifact_kind"]) != (domain, scope_id, record_id, KINDS[kind]):
                raise SecureStateChanged()
            object_refs.add(KeyRef(domain, _uuid(scope_id), _uuid(obj["key_id"]), obj["key_epoch"]))
            if obj["source_object_id"]:
                if kind == "record_index":
                    _row(db, "SELECT object_id FROM encrypted_objects WHERE object_id=:o "
                         "AND object_version=:v AND domain=:d AND owner_id=:scope "
                         "AND record_id=:record AND artifact_kind='record_metadata' "
                         "AND state IN ('ACTIVE','RETIRED')", o=obj["source_object_id"],
                         v=obj["source_version"], d=domain, scope=scope_id, record=record_id)
                else:
                    source_refs.add((_uuid(obj["source_object_id"]), obj["source_version"]))
    for domain, owner in sorted(requested_scopes):
        row = _row(db, "SELECT * FROM security_scopes WHERE domain=:d AND owner_id=:o", d=domain, o=owner)
        if row["state"] != "ACTIVE":
            raise SecureStateChanged()
        scopes[(domain, owner)] = (domain, _uuid(owner), _count(row["deletion_epoch"], 0), _count(row["authorization_epoch"], 0), _count(row["revision"]))
        keys = db.execute(text("SELECT k.* FROM key_epochs k JOIN credential_generation_keys g ON g.key_id=k.key_id AND g.key_epoch=k.epoch AND g.domain=k.domain AND g.owner_id=k.owner_id WHERE k.domain=:d AND k.owner_id=:o AND g.generation_id=:g"), dict(d=domain, o=owner, g=current["runtime_generation"])).mappings().all()
        usable = [k for k in keys if k["state"] in ("ACTIVE", "DECRYPT_ONLY") and k["provisioning_state"] == "PERSISTED"]
        if any(p[0] == "user" and str(p[1]) == owner for p in pointers) or domain == "room":
            if not usable:
                raise SecureStateChanged()
        refs.update(KeyRef(domain, _uuid(owner), _uuid(k["key_id"]), k["epoch"]) for k in usable)
    if len(operations) > 1 or not object_refs <= refs or not source_refs <= {p[4:6] for p in pointers}:
        raise SecureStateChanged()
    return CatalogueSnapshot(principal_id, tuple(scopes.values()), _count(current["revision"]),
        _uuid(current["runtime_generation"]), tuple(pointers), tuple(sorted(refs, key=lambda r: (r.domain, str(r.owner_id), str(r.key_id), r.epoch))), next(iter(operations), None))


_invitation = ContextVar("secure_invitation", default=None)


def snapshot(db, *, principal_id, targets):
    grant = _invitation.get()
    if grant is None:
        return _snapshot(db, principal_id=principal_id, targets=targets)
    saved, guard = grant
    if (not guard.active or guard.poisoned or guard.pid != os.getpid() or guard.task is not asyncio.current_task()
            or _uuid(principal_id) != saved.principal_id or tuple(targets) != tuple(p[:2] for p in saved.pointers)):
        raise SecureStateChanged()
    current = _snapshot(db, principal_id=principal_id, targets=targets, invitation_room=str(saved.pointers[0][1]))
    if replace(current, operation_id=None) != replace(saved, operation_id=None):
        raise SecureStateChanged()
    return current


def invitation_candidate(db, *, principal_id, room_id):
    """Detached candidate only; caller must authenticate capability before any release."""
    return _snapshot(db, principal_id=principal_id, targets=(("room", _uuid(room_id)),), invitation_room=str(_uuid(room_id)))


@dataclass
class _InvitationProof:
    saved: CatalogueSnapshot
    guard: object
    digest: bytes
    used: bool = False


def authenticate_invitation(saved, *, values, supplied_capability, vault, store, guard):
    """Exact A authentication outside SQL; mint one scope/guard-bound join proof."""
    import hmac
    from app.storage.crypto import open_payload
    identity, ref, wrapped, receipt = values
    if (len(saved.pointers) != 1 or saved.pointers[0][0] != "room"
            or (identity.object_id, identity.object_version) != saved.pointers[0][4:6]
            or identity.owner_id != saved.pointers[0][1] or identity.artifact_kind != "room_metadata"
            or ref not in saved.key_refs or not guard.active or guard.poisoned
            or guard.task is not asyncio.current_task() or guard.pid != os.getpid()):
        raise SecureStateChanged()
    raw = store.read(receipt)
    if len(raw) != receipt.ciphertext_size or hashlib.sha256(raw).digest() != receipt.ciphertext_sha256:
        raise SecureStateChanged()
    payload = json.loads(open_payload(identity, raw, wrapped, vault.load(ref)))
    if (type(payload) is not dict or payload.get("room_id") != str(identity.owner_id)
            or type(payload.get("invite_code")) is not str
            or not hmac.compare_digest(payload["invite_code"], supplied_capability)):
        raise SecureStateChanged()
    return payload, _InvitationProof(saved, guard, hashlib.sha256(supplied_capability.encode()).digest())


@contextmanager
def invitation_access(saved, *, guard, authenticated_payload, supplied_capability):
    """One guarded join. authenticated_payload is an opaque A-authentication proof.

    This grant is never accepted from an HTTP field, replayed or persisted. The
    complete candidate snapshot fences all ordinary journal calls while it lives.
    """
    import hmac
    proof = authenticated_payload
    if (_invitation.get() is not None or type(proof) is not _InvitationProof or proof.used
            or proof.saved != saved or proof.guard is not guard
            or not hmac.compare_digest(proof.digest, hashlib.sha256(supplied_capability.encode()).digest())
            or not guard.active or guard.poisoned or guard.task is not asyncio.current_task() or guard.pid != os.getpid()):
        raise SecureStateChanged()
    proof.used = True
    token = _invitation.set((saved, guard))
    try:
        yield
    finally:
        _invitation.reset(token)


def revalidate(db, saved):
    if type(saved) is not CatalogueSnapshot or snapshot(db, principal_id=saved.principal_id, targets=tuple(p[:2] for p in saved.pointers)) != saved:
        raise SecureStateChanged()


def claim(db, saved, *, operation_id, target, candidate_object_id):
    lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
    revalidate(db, saved)
    if saved.operation_id is not None or target not in [p[:2] for p in saved.pointers]:
        raise SecureStateChanged()
    operation_id, candidate_object_id = _uuid(operation_id), _uuid(candidate_object_id)
    domain, owner, _ = ownership(db, *target)
    scope = next(s for s in saved.scopes if s[:2] == (domain, _uuid(owner)))
    pointer = next(p for p in saved.pointers if p[:2] == target)
    claimed = replace(saved, operation_id=operation_id)
    _cas(db, "INSERT INTO secure_operations (operation_id,kind,principal_id,snapshot_json,owner_type,owner_id,domain,scope_id,control_revision,pointer_revision,source_revision,deletion_epoch,authorization_epoch,runtime_generation,candidate_object_id,created_at,revision,state) VALUES (:id,'PAYLOAD',:principal,:snapshot,:kind,:owner,:domain,:scope,:control,:pointer,:source,:de,:ae,:generation,:candidate,:now,1,'CLAIMED')",
         id=str(operation_id), principal=str(saved.principal_id), snapshot=claimed.to_json(), kind=target[0], owner=str(target[1]), domain=domain, scope=owner, control=saved.control_revision, pointer=pointer[2], source=pointer[3], de=scope[2], ae=scope[3], generation=str(saved.runtime_generation), candidate=str(candidate_object_id), now=int(time.time()))
    _cas(db, "UPDATE protected_pointers SET operation_id=:op WHERE owner_type=:t AND owner_id=:o AND revision=:r AND source_revision=:sr", op=str(operation_id), t=target[0], o=str(target[1]), r=pointer[2], sr=pointer[3])
    return claimed


def _operation(db, saved, operation_id, state):
    revalidate(db, saved)
    if saved.operation_id != _uuid(operation_id):
        raise SecureStateChanged()
    op = _row(db, "SELECT * FROM secure_operations WHERE operation_id=:id", id=str(operation_id))
    if op["kind"] != "PAYLOAD" or op["parent_operation_id"] is not None or op["state"] != state or CatalogueSnapshot.from_json(op["snapshot_json"]) != saved:
        raise SecureStateChanged()
    pointer = next((p for p in saved.pointers if p[:2] == (op["owner_type"], _uuid(op["owner_id"]))), None)
    scope = next((s for s in saved.scopes if s[:2] == (op["domain"], _uuid(op["scope_id"]))), None)
    if (not pointer or not scope or op["principal_id"] != str(saved.principal_id) or op["runtime_generation"] != str(saved.runtime_generation)
            or (op["control_revision"], op["pointer_revision"], op["source_revision"], op["deletion_epoch"], op["authorization_epoch"]) != (saved.control_revision, pointer[2], pointer[3], scope[2], scope[3])):
        raise SecureStateChanged()
    return op


class RoomStorageQuotaExceeded(ValueError):
    pass


def reserve_storage(db, *, operation_id, identity, ciphertext_size):
    """Reserve before create; the SQLite write CAS serializes room accounting."""
    from app.core.config import settings

    if identity.domain != "room":
        return
    lock_rooms(db, (identity.owner_id,))
    _count(ciphertext_size)
    operation = _row(db, "SELECT * FROM secure_operations WHERE operation_id=:id",
                     id=str(_uuid(operation_id)))
    if (operation["state"] != "CLAIMED" or operation["domain"] != identity.domain
            or operation["scope_id"] != str(identity.owner_id)
            or operation["candidate_object_id"] != str(identity.object_id)):
        raise SecureStateChanged()
    _cas(db, "UPDATE secure_operations SET reserved_ciphertext_bytes=:size,revision=revision+1 "
         "WHERE operation_id=:id AND state='CLAIMED' AND revision=:revision "
         "AND reserved_ciphertext_bytes IS NULL",
         size=ciphertext_size, id=str(operation_id), revision=operation["revision"])
    used = room_storage_bytes(db, identity.owner_id)
    limit = settings.legacy_room_storage_quota_bytes
    if type(limit) is not int or limit <= 0 or used > limit:
        raise RoomStorageQuotaExceeded()


def room_storage_bytes(db, room_id):
    """Count retained ciphertext and unreceipted creates, including uncertain writes."""
    values = dict(room=str(_uuid(room_id)))
    unknown = db.execute(text(
        "SELECT 1 FROM secure_operations op WHERE op.domain='room' AND op.scope_id=:room "
        "AND op.state='ORPHAN_UNRESOLVED' AND op.reserved_ciphertext_bytes IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM encrypted_objects e WHERE e.object_id=op.candidate_object_id) "
        "AND NOT EXISTS (SELECT 1 FROM secure_reconciliation_events a WHERE a.operation_id=op.operation_id "
        "AND a.room_id=op.scope_id AND a.outcome='ABSENT') LIMIT 1"), values).first()
    if unknown:
        raise SecureStateChanged()
    retained = db.scalar(text(
        "SELECT COALESCE(SUM(e.ciphertext_size),0) FROM encrypted_objects e "
        "WHERE e.domain='room' AND e.owner_id=:room AND NOT EXISTS ("
        "SELECT 1 FROM secure_deletion_inventory i JOIN security_tombstones t "
        "ON t.domain=i.domain AND t.owner_id=i.owner_id AND t.target_kind=i.target_kind "
        "AND t.target_id=i.target_id AND t.deletion_epoch=i.deletion_epoch "
        "WHERE i.domain=e.domain AND i.owner_id=e.owner_id AND i.object_id=e.object_id "
        "AND i.target_kind='record' AND i.target_id=e.record_id "
        "AND i.object_version=e.object_version AND i.key_id=e.key_id AND i.key_epoch=e.key_epoch "
        "AND i.adapter=e.adapter AND i.storage_name=e.storage_name AND i.exact_version=e.exact_version "
        "AND i.ciphertext_size=e.ciphertext_size AND i.ciphertext_sha256=e.ciphertext_sha256 "
        "AND i.physical_state IN ('DELETED','ABSENT') AND i.failure_code IS NULL)"), values)
    # No timeout or ABANDONED state proves that a delayed create did not happen.
    pending = db.scalar(text(
        "SELECT COALESCE(SUM(op.reserved_ciphertext_bytes),0) FROM secure_operations op "
        "WHERE op.domain='room' AND op.scope_id=:room AND NOT EXISTS ("
        "SELECT 1 FROM secure_reconciliation_events a WHERE a.operation_id=op.operation_id "
        "AND a.room_id=op.scope_id AND a.outcome='ABSENT') AND NOT EXISTS ("
        "SELECT 1 FROM encrypted_objects e WHERE e.object_id=op.candidate_object_id)"), values)
    return _count(retained, 0) + _count(pending, 0)


def receipt_json(receipt):
    if type(receipt) is not StorageReceipt:
        raise SecureStateChanged()
    if receipt.adapter == "local":
        _uuid(receipt.exact_version)
    elif receipt.adapter != "gcs" or re.fullmatch(r"[1-9][0-9]*", receipt.exact_version) is None:
        raise SecureStateChanged()
    return json.dumps(dict(adapter=receipt.adapter, object_name=receipt.object_name, exact_version=receipt.exact_version,
        ciphertext_size=receipt.ciphertext_size, ciphertext_sha256=receipt.ciphertext_sha256.hex()), separators=(",", ":"), sort_keys=True)


def _envelope(db, saved, op, identity, sealed, receipt):
    if (op["domain"] == "room" and "reserved_ciphertext_bytes" in op
            and op["reserved_ciphertext_bytes"] != len(sealed.blob)):
        raise SecureStateChanged()
    if type(identity) is not PayloadIdentity or type(sealed) is not SealedPayload:
        raise SecureStateChanged()
    domain, owner, record = ownership(db, op["owner_type"], _uuid(op["owner_id"]))
    if (identity.domain, str(identity.owner_id), str(identity.record_id) if identity.record_id else None, identity.artifact_kind,
        str(identity.object_id), identity.object_version) != (domain, owner, record, KINDS[op["owner_type"]], op["candidate_object_id"], 1):
        raise SecureStateChanged()
    encoded_receipt = receipt_json(receipt)
    if receipt.object_name != str(identity.object_id) or receipt.ciphertext_size != len(sealed.blob) or receipt.ciphertext_sha256 != hashlib.sha256(sealed.blob).digest():
        raise SecureStateChanged()
    if identity.source_object_id:
        if (identity.source_object_id, identity.source_version) not in [p[4:6] for p in saved.pointers]:
            raise SecureStateChanged()
        source = _row(db, "SELECT domain,owner_id FROM encrypted_objects WHERE object_id=:o AND object_version=:v", o=str(identity.source_object_id), v=identity.source_version)
        if tuple(source.values()) != (domain, owner):
            raise SecureStateChanged()
    key = _row(db, "SELECT * FROM key_epochs WHERE domain=:d AND owner_id=:o AND state='ACTIVE' AND provisioning_state='PERSISTED'", d=domain, o=owner)
    if KeyRef(domain, _uuid(owner), _uuid(key["key_id"]), key["epoch"]) not in saved.key_refs:
        raise SecureStateChanged()
    return key, encoded_receipt


def record_receipt(db, saved, *, operation_id, identity, sealed, receipt):
    lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
    op = _operation(db, saved, operation_id, "CLAIMED")
    key, encoded_receipt = _envelope(db, saved, op, identity, sealed, receipt)
    _cas(db, "INSERT INTO encrypted_objects (object_id,object_version,domain,owner_id,record_id,artifact_kind,source_object_id,source_version,plaintext_length,payload_version,aad_version,payload_algorithm,key_id,key_epoch,wrapper_algorithm,wrapped_dek,envelope_revision,adapter,storage_name,exact_version,ciphertext_size,ciphertext_sha256,state) VALUES (:id,1,:domain,:owner,:record,:kind,:source,:version,:length,1,1,'AES-256-GCM',:key,:epoch,'AES-256-KW',:wrapped,1,:adapter,:id,:exact,:size,:hash,'STAGED')",
         id=str(identity.object_id), domain=identity.domain, owner=str(identity.owner_id), record=str(identity.record_id) if identity.record_id else None, kind=identity.artifact_kind, source=str(identity.source_object_id) if identity.source_object_id else None, version=identity.source_version, length=identity.plaintext_length, key=key["key_id"], epoch=key["epoch"], wrapped=sealed.wrapped_dek, adapter=receipt.adapter, exact=receipt.exact_version, size=receipt.ciphertext_size, hash=receipt.ciphertext_sha256)
    _cas(db, "UPDATE secure_operations SET state='RECEIPTED',receipt_json=:receipt,revision=revision+1 WHERE operation_id=:id AND state='CLAIMED' AND revision=:r", receipt=encoded_receipt, id=str(operation_id), r=op["revision"])


@dataclass(frozen=True)
class ReadbackProof:
    fingerprint: bytes


def _fingerprint(identity, key_ref, wrapped_dek, receipt):
    return hashlib.sha256(identity.aad() + wrapped_dek + receipt_json(receipt).encode()
                          + str(key_ref).encode()).digest()


def authenticate_readback(*, identity, sealed, receipt, key_ref, vault, store):
    """Call with no SQL session: exact read plus full A authentication in memory."""
    from app.storage.crypto import open_payload
    try:
        receipt_json(receipt)
        if (key_ref.domain, key_ref.owner_id) != (identity.domain, identity.owner_id):
            raise SecureStateChanged()
        raw = store.read(receipt)
        if (raw != sealed.blob or len(raw) != receipt.ciphertext_size
                or hashlib.sha256(raw).digest() != receipt.ciphertext_sha256):
            raise SecureStateChanged()
        plaintext = open_payload(identity, raw, sealed.wrapped_dek, vault.load(key_ref))
        del plaintext, raw
        return ReadbackProof(_fingerprint(identity, key_ref, sealed.wrapped_dek, receipt))
    except Exception:
        raise SecureStateChanged() from None


def envelope_values(row):
    """Immutable A values for authenticated I/O after closing the caller's SQL session."""
    try:
        identity = PayloadIdentity(row["domain"], _uuid(row["owner_id"]), _uuid(row["record_id"]) if row["record_id"] else None,
            _uuid(row["object_id"]), row["object_version"], row["artifact_kind"],
            _uuid(row["source_object_id"]) if row["source_object_id"] else None, row["source_version"], row["plaintext_length"])
        key_ref = KeyRef(row["domain"], _uuid(row["owner_id"]), _uuid(row["key_id"]), row["key_epoch"])
        receipt = StorageReceipt(row["adapter"], row["storage_name"], row["exact_version"], row["ciphertext_size"], row["ciphertext_sha256"])
        receipt_json(receipt)
        return identity, key_ref, bytes(row["wrapped_dek"]), receipt
    except (ValueError, TypeError, KeyError):
        raise SecureStateChanged() from None


def verify_readback(db, saved, *, operation_id, proof=None):
    lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
    op = _operation(db, saved, operation_id, "RECEIPTED")
    row = _row(db, "SELECT * FROM encrypted_objects WHERE object_id=:id AND object_version=1", id=op["candidate_object_id"])
    if type(proof) is not ReadbackProof or proof.fingerprint != _fingerprint(*envelope_values(row)):
        raise SecureStateChanged()
    _cas(db, "UPDATE secure_operations SET state='VERIFIED',revision=revision+1 WHERE operation_id=:id AND state='RECEIPTED' AND revision=:r", id=str(operation_id), r=op["revision"])


def publish(db, saved, *, operation_id, identity, sealed, receipt):
    lock_rooms(db, (scope[1] for scope in saved.scopes if scope[0] == "room"))
    op, obj = _publication(db, saved, operation_id, identity, sealed, receipt)
    _switch_publication(db, saved, op, obj, identity)
    scoped = next(s for s in saved.scopes if s[:2] == (op["domain"], _uuid(op["scope_id"])))
    _cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain=:d AND owner_id=:o AND revision=:r", d=op["domain"], o=op["scope_id"], r=scoped[4])


def _publication(db, saved, operation_id, identity, sealed, receipt):
    op = _operation(db, saved, operation_id, "VERIFIED")
    key, encoded_receipt = _envelope(db, saved, op, identity, sealed, receipt)
    obj = _row(db, "SELECT * FROM encrypted_objects WHERE object_id=:id AND object_version=1", id=str(identity.object_id))
    expected_key = KeyRef(identity.domain, identity.owner_id, _uuid(key["key_id"]), key["epoch"])
    if op["receipt_json"] != encoded_receipt or envelope_values(obj) != (identity, expected_key, sealed.wrapped_dek, receipt):
        raise SecureStateChanged()
    return op, obj


def _switch_publication(db, saved, op, obj, identity):
    operation_id = saved.operation_id
    _cas(db, "UPDATE encrypted_objects SET state='ACTIVE',envelope_revision=envelope_revision+1 WHERE object_id=:id AND object_version=1 AND state='STAGED' AND envelope_revision=:r", id=str(identity.object_id), r=obj["envelope_revision"])
    _cas(db, "UPDATE protected_pointers SET object_id=:object,object_version=1,state='ENCRYPTED_ACTIVE',revision=revision+1,deletion_epoch=:de,authorization_epoch=:ae WHERE owner_type=:t AND owner_id=:o AND operation_id=:op AND revision=:r AND source_revision=:sr",
         object=str(identity.object_id), de=op["deletion_epoch"], ae=op["authorization_epoch"], t=op["owner_type"], o=op["owner_id"], op=str(operation_id), r=op["pointer_revision"], sr=op["source_revision"])
    _cas(db, "UPDATE secure_operations SET state='COMMITTED',revision=revision+1 WHERE operation_id=:id AND state='VERIFIED' AND revision=:r", id=str(operation_id), r=op["revision"])
    previous = next(p for p in saved.pointers if p[:2] == (op["owner_type"], _uuid(op["owner_id"])))
    if previous[4] is not None:
        old = _row(db, "SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v", o=str(previous[4]), v=previous[5])
        if not db.scalar(text("SELECT COUNT(*) FROM protected_pointers WHERE object_id=:o AND object_version=:v"), dict(o=str(previous[4]), v=previous[5])):
            _cas(db, "UPDATE encrypted_objects SET state='RETIRED',envelope_revision=envelope_revision+1 WHERE object_id=:o AND object_version=:v AND state='ACTIVE' AND envelope_revision=:r", o=str(previous[4]), v=previous[5], r=old["envelope_revision"])


def publish_many(db, staged):
    """Atomic detached ordinary publications; caller MUST roll back on any failure.

    Items are (snapshot, identity, sealed, receipt, readback proof). Check all live
    claims against the original scope before switching siblings, then advance each
    affected scope once. No staged object can replace another item's source.
    """
    if not staged or len(staged) > 1000:
        raise SecureStateChanged()
    lock_rooms(db, (scope[1] for item in staged for scope in item[0].scopes if scope[0] == "room"))
    targets, candidates, scopes, operations = set(), set(), {}, set()
    first = staged[0][0]
    for saved, identity, sealed, receipt, proof in staged:
        if (saved.principal_id, saved.control_revision, saved.runtime_generation) != (first.principal_id, first.control_revision, first.runtime_generation):
            raise SecureStateChanged()
        op = _operation(db, saved, saved.operation_id, "RECEIPTED")
        target = op["owner_type"], op["owner_id"]
        if target in targets or identity.object_id in candidates or saved.operation_id in operations:
            raise SecureStateChanged()
        targets.add(target); candidates.add(identity.object_id); operations.add(saved.operation_id)
        for scope in saved.scopes:
            if scope[:2] in scopes and scopes[scope[:2]] != scope:
                raise SecureStateChanged()
            scopes[scope[:2]] = scope
        # Source pointers cannot be rewritten by siblings while retaining old AAD.
        if identity.source_object_id and any(identity.source_object_id == other[1].object_id for other in staged):
            raise SecureStateChanged()
    for saved, identity, *_ in staged:
        for p in saved.pointers:
            if identity.source_object_id and (p[0], str(p[1])) in targets and p[4] == identity.source_object_id:
                raise SecureStateChanged()
    checked = []
    for saved, identity, sealed, receipt, proof in staged:
        verify_readback(db, saved, operation_id=saved.operation_id, proof=proof)
        op, obj = _publication(db, saved, saved.operation_id, identity, sealed, receipt)
        checked.append((saved, identity, op, obj))
    touched = set()
    for saved, identity, op, obj in checked:
        _switch_publication(db, saved, op, obj, identity)
        _cas(db, "UPDATE protected_pointers SET source_revision=source_revision+1 WHERE owner_type=:t AND owner_id=:o AND operation_id=:op AND revision=:r", t=op["owner_type"], o=op["owner_id"], op=op["operation_id"], r=op["pointer_revision"]+1)
        touched.add((op["domain"], _uuid(op["scope_id"])))
    for domain, owner in touched:
        _cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain=:d AND owner_id=:o AND revision=:r", d=domain, o=str(owner), r=scopes[(domain, owner)][4])


def abandon(db, *, operation_id, expected_revision, receipt_unknown=False, known_receipt=None):
    """Explicit cancellation/reconciliation; no timeout proves an orphan absent."""
    op = _row(db, "SELECT state,runtime_generation FROM secure_operations WHERE operation_id=:id AND revision=:r", id=str(_uuid(operation_id)), r=_count(expected_revision))
    if op["state"] not in ("CLAIMED", "RECEIPTED", "VERIFIED") or (receipt_unknown and op["state"] != "CLAIMED"):
        raise SecureStateChanged()
    if known_receipt is not None:
        binding = _row(db, "SELECT * FROM secure_create_bindings WHERE operation_id=:id",
                       id=str(operation_id))
        encoded = receipt_json(known_receipt)
        if (not receipt_unknown or known_receipt.adapter != "gcs"
                or not binding["storage_binding"].startswith("gcs:")
                or (known_receipt.object_name, known_receipt.ciphertext_size, known_receipt.ciphertext_sha256)
                != (binding["object_name"], binding["ciphertext_size"], bytes(binding["ciphertext_sha256"]))):
            raise SecureStateChanged()
        from uuid import uuid4
        _cas(db, "INSERT INTO secure_reconciliation_events "
             "(id,operation_id,room_id,generation_id,observed_at,outcome,receipt_json,storage_binding) "
             "VALUES (:id,:op,:room,:generation,:now,'AMBIGUOUS',:receipt,:binding)",
             id=str(uuid4()), op=str(operation_id), room=binding["room_id"],
             generation=op["runtime_generation"], now=int(time.time()),
             receipt=json.dumps({"reason": "CREATE_READBACK_UNCONFIRMED", "receipt": json.loads(encoded)},
                                sort_keys=True), binding=binding["storage_binding"])
    _cas(db, "UPDATE secure_operations SET state=:state,failure_code=:code,revision=revision+1 WHERE operation_id=:id AND revision=:r AND state=:old", state="ORPHAN_UNRESOLVED" if receipt_unknown else "ABANDONED", code="receipt_unknown" if receipt_unknown else "cancelled", id=str(operation_id), r=expected_revision, old=op["state"])


def activation_parent(db, *, operation_id, digest, kind=None, principal_id=None):
    """Capability proof is supplied by the live request, never inferred from history."""
    import hmac
    op = dict(_row(db, "SELECT * FROM secure_operations WHERE operation_id=:id", id=str(_uuid(operation_id))))
    if (op["kind"] not in ("SIGNUP", "ROOM_CREATE") or type(digest) is not bytes or len(digest) != 32
            or not hmac.compare_digest(op["continuation_digest"], digest)
            or (kind is not None and op["kind"] != kind)
            or (op["kind"] == "ROOM_CREATE" and op["principal_id"] != principal_id)):
        raise SecureStateChanged()
    return op


def activation_effective(db, op, now):
    current = control(db)
    if now >= op["expires_at"] or now < op["created_at"]:
        return "EXPIRED"
    if op["state"] == "READY" and op["runtime_generation"] != current["runtime_generation"]:
        return "WAITING_CREDENTIAL"
    return op["state"]


def activation_snapshot(db, op, *, digest, principal_id, target, child_id):
    parent = activation_parent(db, operation_id=op["operation_id"], digest=digest,
                               kind=op["kind"], principal_id=principal_id)
    if parent != op or activation_effective(db, parent, int(time.time())) != "READY":
        raise SecureStateChanged()
    current = control(db)
    if parent["kind"] == "SIGNUP":
        principal = _uuid(parent["owner_id"])
        if target[0] not in ("user", "auth_identity") or (target[0] == "user" and str(target[1]) != parent["owner_id"]):
            raise SecureStateChanged()
        scopes = []
    else:
        principal = _uuid(principal_id)
        if target != ("room", _uuid(parent["owner_id"])):
            raise SecureStateChanged()
        # Also binds the existing authenticated principal's profile and scope.
        authenticated = snapshot(db, principal_id=principal, targets=(("user", principal),))
        scopes = list(authenticated.scopes)
    scope = _row(db, "SELECT * FROM security_scopes WHERE domain=:d AND owner_id=:o", d=parent["domain"], o=parent["scope_id"])
    if scope["state"] != "ACTIVE" or db.scalar(text("SELECT count(*) FROM security_tombstones WHERE domain=:d AND owner_id=:o"), dict(d=parent["domain"], o=parent["scope_id"])):
        raise SecureStateChanged()
    key = _row(db, "SELECT * FROM key_epochs WHERE key_id=:k AND epoch=:e", k=parent["activation_key_id"], e=parent["activation_key_epoch"])
    if key["state"] != "PREPARED" or key["provisioning_state"] != "PERSISTED":
        raise SecureStateChanged()
    _row(db, "SELECT * FROM credential_generation_keys WHERE generation_id=:g AND key_id=:k AND key_epoch=:e AND domain=:d AND owner_id=:o",
         g=current["runtime_generation"], k=key["key_id"], e=key["epoch"], d=parent["domain"], o=parent["scope_id"])
    scopes.append((parent["domain"], _uuid(parent["scope_id"]), scope["deletion_epoch"], scope["authorization_epoch"], scope["revision"]))
    ref = KeyRef(parent["domain"], _uuid(parent["scope_id"]), _uuid(key["key_id"]), key["epoch"])
    return CatalogueSnapshot(principal, tuple(sorted(scopes)), current["revision"], _uuid(current["runtime_generation"]),
                             ((target[0], _uuid(target[1]), 1, 1, None, None),), (ref,), _uuid(child_id))


def claim_activation_child(db, op, *, digest, principal_id, target, child_id, candidate_id):
    saved = activation_snapshot(db, op, digest=digest, principal_id=principal_id, target=target, child_id=child_id)
    scope = next(s for s in saved.scopes if s[:2] == (op["domain"], _uuid(op["scope_id"])))
    _cas(db, "INSERT INTO secure_operations (operation_id,kind,parent_operation_id,principal_id,snapshot_json,owner_type,owner_id,domain,scope_id,control_revision,pointer_revision,source_revision,deletion_epoch,authorization_epoch,runtime_generation,candidate_object_id,created_at,revision,state) VALUES (:id,'PAYLOAD',:parent,:principal,:snapshot,:kind,:owner,:domain,:scope,:control,1,1,:de,:ae,:generation,:candidate,:now,1,'CLAIMED')",
         id=str(child_id), parent=op["operation_id"], principal=str(saved.principal_id), snapshot=saved.to_json(), kind=target[0], owner=str(target[1]), domain=op["domain"], scope=op["scope_id"], control=saved.control_revision, de=scope[2], ae=scope[3], generation=str(saved.runtime_generation), candidate=str(_uuid(candidate_id)), now=int(time.time()))
    return saved


def activation_child(db, op, saved, *, digest, principal_id, state):
    target = saved.pointers[0][:2]
    if activation_snapshot(db, op, digest=digest, principal_id=principal_id, target=target, child_id=saved.operation_id) != saved:
        raise SecureStateChanged()
    child = _row(db, "SELECT * FROM secure_operations WHERE operation_id=:id", id=str(saved.operation_id))
    scope = next(s for s in saved.scopes if s[:2] == (op["domain"], _uuid(op["scope_id"])))
    if (child["parent_operation_id"] != op["operation_id"] or child["kind"] != "PAYLOAD" or child["state"] != state
            or CatalogueSnapshot.from_json(child["snapshot_json"]) != saved
            or (child["owner_type"], child["owner_id"], child["domain"], child["scope_id"], child["principal_id"],
                child["control_revision"], child["runtime_generation"], child["pointer_revision"], child["source_revision"], child["deletion_epoch"], child["authorization_epoch"])
            != (target[0], str(target[1]), op["domain"], op["scope_id"], str(saved.principal_id), saved.control_revision,
                str(saved.runtime_generation), 1, 1, scope[2], scope[3])):
        raise SecureStateChanged()
    return child


def receipt_activation_child(db, op, saved, *, digest, principal_id, identity, sealed, receipt):
    child = activation_child(db, op, saved, digest=digest, principal_id=principal_id, state="CLAIMED")
    if (child["domain"] == "room" and "reserved_ciphertext_bytes" in child
            and child["reserved_ciphertext_bytes"] != len(sealed.blob)):
        raise SecureStateChanged()
    expected = PayloadIdentity(op["domain"], _uuid(op["scope_id"]), None, _uuid(child["candidate_object_id"]), 1,
                               KINDS[child["owner_type"]], None, None, identity.plaintext_length)
    if (identity != expected or receipt.object_name != str(identity.object_id)
            or receipt.ciphertext_size != len(sealed.blob) or receipt.ciphertext_sha256 != hashlib.sha256(sealed.blob).digest()):
        raise SecureStateChanged()
    encoded = receipt_json(receipt)
    _cas(db, "INSERT INTO encrypted_objects (object_id,object_version,domain,owner_id,record_id,artifact_kind,plaintext_length,payload_version,aad_version,payload_algorithm,key_id,key_epoch,wrapper_algorithm,wrapped_dek,envelope_revision,adapter,storage_name,exact_version,ciphertext_size,ciphertext_sha256,state) VALUES (:id,1,:domain,:owner,NULL,:kind,:length,1,1,'AES-256-GCM',:key,:epoch,'AES-256-KW',:wrapped,1,:adapter,:id,:exact,:size,:hash,'STAGED')",
         id=str(identity.object_id), domain=identity.domain, owner=str(identity.owner_id), kind=identity.artifact_kind, length=identity.plaintext_length, key=op["activation_key_id"], epoch=op["activation_key_epoch"], wrapped=sealed.wrapped_dek, adapter=receipt.adapter, exact=receipt.exact_version, size=receipt.ciphertext_size, hash=receipt.ciphertext_sha256)
    _cas(db, "UPDATE secure_operations SET state='RECEIPTED',receipt_json=:receipt,revision=revision+1 WHERE operation_id=:id AND state='CLAIMED' AND revision=:r", receipt=encoded, id=child["operation_id"], r=child["revision"])


def verify_activation_child(db, op, saved, *, digest, principal_id, proof):
    child = activation_child(db, op, saved, digest=digest, principal_id=principal_id, state="RECEIPTED")
    obj = _row(db, "SELECT * FROM encrypted_objects WHERE object_id=:id AND object_version=1", id=child["candidate_object_id"])
    if type(proof) is not ReadbackProof or proof.fingerprint != _fingerprint(*envelope_values(obj)):
        raise SecureStateChanged()
    _cas(db, "UPDATE secure_operations SET state='VERIFIED',revision=revision+1 WHERE operation_id=:id AND state='RECEIPTED' AND revision=:r", id=child["operation_id"], r=child["revision"])
    return dict(child)


def bind_create(db, *, operation_id, identity, sealed, store):
    """Commit expected immutable bytes and storage namespace before remote create."""
    if identity.domain != "room":
        return
    binding = getattr(store, "storage_binding", None)
    if type(binding) is not str or not binding:
        raise SecureStateChanged()
    _cas(db, "INSERT INTO secure_create_bindings "
         "(operation_id,room_id,object_name,storage_binding,ciphertext_size,ciphertext_sha256) "
         "VALUES (:op,:room,:name,:binding,:size,:digest)",
         op=str(operation_id), room=str(identity.owner_id), name=str(identity.object_id),
         binding=binding, size=len(sealed.blob), digest=hashlib.sha256(sealed.blob).digest())


def _reconciliation_authority(db, drain_proof, supervisor):
    from app.services.secure_deletion import SupervisorDrainProof
    if type(drain_proof) is not SupervisorDrainProof:
        raise SecureStateChanged()
    row = _row(db, "SELECT g.* FROM credential_generations g JOIN security_control c "
               "ON c.runtime_generation=g.generation_id WHERE c.id=1 AND c.mode='SECURE' "
               "AND c.admission='CLOSED' AND g.state='DRAINED'")
    values = dict(generation_id=_uuid(row["generation_id"]), invocation_id=_uuid(row["invocation_id"]),
                  boot_id=_uuid(row["boot_id"]), unit_name=row["unit_name"], approved_uid=row["approved_uid"],
                  inventory_revision=drain_proof.inventory_revision)
    if any(getattr(drain_proof, key) != value for key, value in values.items()):
        raise SecureStateChanged()
    if supervisor is None or supervisor.verify(drain_proof, **values) is not True:
        raise SecureStateChanged()
    return str(values["generation_id"])


def reconcile_orphans(engine, store, *, room_id, runtime, drain_proof, supervisor, limit=100):
    """Runtime recovery: exact known generations never require LIST."""
    return _reconcile_orphans(engine, store, room_id=room_id, runtime=runtime,
        drain_proof=drain_proof, supervisor=supervisor, limit=limit, inventory=False)


def reconcile_orphans_maintenance(engine, store, *, room_id, runtime, drain_proof, supervisor, limit=100):
    """Operator entry point; use a separately authorized identity for GCS LIST.

    The same bound namespace, external drain and closed-runtime guard are required.
    Permission denial returns LIST_PERMISSION_REQUIRED and retains the charge.
    """
    return _reconcile_orphans(engine, store, room_id=room_id, runtime=runtime,
        drain_proof=drain_proof, supervisor=supervisor, limit=limit, inventory=True)


def _known_orphan_receipt(db, row, binding):
    """Only durable operation receipts or matching inventory observations qualify."""
    from app.storage.contracts import StorageIntegrityError
    encoded = [row["receipt_json"]] if row["receipt_json"] is not None else []
    events = db.execute(text("SELECT outcome,receipt_json,storage_binding FROM secure_reconciliation_events "
        "WHERE operation_id=:op AND room_id=:room AND outcome IN ('MATCHED','AMBIGUOUS')"),
        dict(op=row["operation_id"], room=row["room_id"])).mappings().all()
    receipts = []
    try:
        for event in events:
            if event["outcome"] == "MATCHED":
                value = event["receipt_json"]
            else:
                details = json.loads(event["receipt_json"]) if event["receipt_json"] else {}
                if details.get("reason") != "CREATE_READBACK_UNCONFIRMED":
                    continue
                value = json.dumps(details["receipt"])
            if event["storage_binding"] != binding:
                raise ValueError()
            encoded.append(value)
        for value in encoded:
            data = json.loads(value)
            data["ciphertext_sha256"] = bytes.fromhex(data["ciphertext_sha256"])
            receipt = StorageReceipt(**data)
            receipt_json(receipt)
            if (receipt.object_name, receipt.ciphertext_size, receipt.ciphertext_sha256) != (
                    row["object_name"], row["ciphertext_size"], bytes(row["ciphertext_sha256"])):
                raise ValueError()
            if not binding.startswith(receipt.adapter + ":"):
                raise ValueError()
            receipts.append(receipt)
    except (TypeError, ValueError, KeyError, SecureStateChanged):
        raise StorageIntegrityError("Invalid durable orphan receipt") from None
    if receipts and any(item != receipts[0] for item in receipts):
        raise StorageIntegrityError("Conflicting durable orphan receipts")
    return receipts[0] if receipts else None


def _reconcile_orphans(engine, store, *, room_id, runtime, drain_proof, supervisor, limit, inventory):
    """Bounded operator recovery after externally proven writer drain.

    Historical GCS absence requires the actual factory-verified runtime store;
    existing objects with unknown hashes remain charged. No timeout proves absence.
    Provider work happens after closing the SQL connection.
    """
    from uuid import uuid4
    from app.storage.contracts import StorageIntegrityError, InventoryRequired, ListPermissionRequired
    room = str(_uuid(room_id))
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Reconciliation limit must be 1..1000")
    results = {}
    with runtime.closed_destruction_guard():
        with engine.connect() as db:
            generation = _reconciliation_authority(db, drain_proof, supervisor)
            from app.services.secure_runtime import verified_gcs_binding
            historical_binding = (verified_gcs_binding(db, store)
                                  if getattr(runtime, "store", None) is store else None)
            rows = db.execute(text("SELECT op.operation_id,op.scope_id AS room_id,op.candidate_object_id AS object_name,"
                "op.reserved_ciphertext_bytes AS ciphertext_size,op.revision,op.receipt_json,b.storage_binding,b.ciphertext_sha256 "
                "FROM secure_operations op LEFT JOIN secure_create_bindings b ON op.operation_id=b.operation_id "
                "WHERE op.scope_id=:room AND op.domain='room' AND op.state='ORPHAN_UNRESOLVED' "
                "AND (b.operation_id IS NULL OR (op.scope_id=b.room_id AND op.candidate_object_id=b.object_name "
                "AND op.reserved_ciphertext_bytes=b.ciphertext_size)) "
                "AND NOT EXISTS (SELECT 1 FROM encrypted_objects e WHERE e.object_id=op.candidate_object_id) "
                "AND NOT EXISTS (SELECT 1 FROM secure_reconciliation_events a "
                "WHERE a.operation_id=op.operation_id AND a.room_id=op.scope_id AND a.outcome='ABSENT') "
                "ORDER BY (SELECT COUNT(*) FROM secure_reconciliation_events a "
                "WHERE a.operation_id=op.operation_id AND a.room_id=op.scope_id),op.operation_id LIMIT :limit"),
                dict(room=room, limit=limit)).mappings().all()
        for row in rows:
            encoded = None
            binding = row["storage_binding"] or historical_binding
            expected_hash = bytes(row["ciphertext_sha256"]) if row["ciphertext_sha256"] is not None else None
            try:
                if binding is None or store.storage_binding != binding:
                    raise StorageIntegrityError("Storage binding mismatch")
                with engine.connect() as db:
                    known = _known_orphan_receipt(db, row, binding)
                if known is not None and known.adapter == "gcs":
                    receipt = store.reconcile_exact(known)
                elif inventory and binding.startswith("gcs:"):
                    receipt = store.reconcile_inventory(_uuid(row["object_name"]), row["ciphertext_size"],
                                                        expected_hash)
                else:
                    receipt = store.reconcile_create(_uuid(row["object_name"]), row["ciphertext_size"],
                                                     expected_hash)
                outcome = "ABSENT" if receipt is None else "MATCHED"
                if receipt is not None:
                    if expected_hash is None:
                        raise StorageIntegrityError("Historical object identity is unknown")
                    if (receipt.object_name, receipt.ciphertext_size, receipt.ciphertext_sha256) != (
                            row["object_name"], row["ciphertext_size"], bytes(row["ciphertext_sha256"])):
                        raise StorageIntegrityError("Receipt mismatch")
                    encoded = receipt_json(receipt)
            except InventoryRequired:
                outcome = "ORPHAN_NEEDS_INVENTORY"
            except ListPermissionRequired:
                outcome = "LIST_PERMISSION_REQUIRED"
            except StorageIntegrityError:
                outcome = "MISMATCH"
            except Exception:
                outcome = "AMBIGUOUS"
            with engine.begin() as db:
                lock_rooms(db, (room_id,))
                if outcome == "ABSENT" and (store.storage_binding != binding or
                        (expected_hash is None and (getattr(runtime, "store", None) is not store
                         or verified_gcs_binding(db, store) != binding))):
                    raise SecureStateChanged()
                if _reconciliation_authority(db, drain_proof, supervisor) != generation:
                    raise SecureStateChanged()
                op = _row(db, "SELECT * FROM secure_operations WHERE operation_id=:id", id=row["operation_id"])
                if (op["state"], op["revision"], op["scope_id"], op["candidate_object_id"]) != (
                        "ORPHAN_UNRESOLVED", row["revision"], room, row["object_name"]):
                    raise SecureStateChanged()
                prior = db.execute(text("SELECT outcome,receipt_json FROM secure_reconciliation_events "
                    "WHERE operation_id=:op AND room_id=:room"), dict(op=row["operation_id"], room=room)).all()
                if any(p.outcome == "ABSENT" for p in prior):
                    results[row["operation_id"]] = "ABSENT"
                    continue
                if outcome == "MATCHED" and any(p.outcome == "MATCHED" and p.receipt_json != encoded for p in prior):
                    outcome, encoded = "MISMATCH", None
                # 0013 already supports durable ambiguous observations with details.
                # Keep the operation ORPHAN_UNRESOLVED so all quota/key fences apply.
                event_outcome = outcome
                if outcome in ("ORPHAN_NEEDS_INVENTORY", "LIST_PERMISSION_REQUIRED"):
                    event_outcome = "AMBIGUOUS"
                    encoded = json.dumps({"reason": outcome}, sort_keys=True)
                _cas(db, "INSERT INTO secure_reconciliation_events "
                     "(id,operation_id,room_id,generation_id,observed_at,outcome,receipt_json,storage_binding) "
                     "VALUES (:id,:op,:room,:generation,:now,:outcome,:receipt,:binding)",
                     id=str(uuid4()), op=row["operation_id"], room=room, generation=generation,
                     now=int(time.time()), outcome=event_outcome, receipt=encoded, binding=binding)
            results[row["operation_id"]] = outcome
    return results
