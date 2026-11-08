"""B6.1 DB-first logical deletion. No storage or key operations."""
from dataclasses import dataclass
from contextlib import contextmanager
import sys
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services import secure_catalogue as cat
from app.storage.contracts import ExactTargetApproval, KeyRef, StorageReceipt, VaultUnavailable


@dataclass(frozen=True)
class SupervisorDrainProof:
    """Opaque supervisor proof; caller-supplied drain claims are never accepted."""
    proof_id: UUID
    generation_id: UUID
    invocation_id: UUID
    boot_id: UUID
    unit_name: str
    approved_uid: int
    inventory_revision: UUID

    def __post_init__(self):
        if (any(type(value) is not UUID for value in (self.proof_id, self.generation_id,
                self.invocation_id, self.boot_id, self.inventory_revision))
                or type(self.unit_name) is not str or not self.unit_name or len(self.unit_name) > 255
                or type(self.approved_uid) is not int or self.approved_uid < 0):
            raise ValueError("Invalid supervisor drain proof")


class DrainSupervisor(Protocol):
    """External authority: production without one must fail closed."""
    def verify(self, proof: SupervisorDrainProof, *, generation_id: UUID, invocation_id: UUID,
               boot_id: UUID, unit_name: str, approved_uid: int,
               inventory_revision: UUID) -> bool: ...


def _destruction_state(db, *, ref, generation_state, inventory_revision=None):
    if type(ref) is not KeyRef or generation_state not in ("VERIFIED", "DRAINED"):
        raise cat.SecureStateChanged()
    control = cat._row(
        db,
        "SELECT c.revision AS control_revision,c.runtime_generation,c.mode,c.admission,c.schema_version,c.catalogue_id,"
        "g.generation_id,g.invocation_id,g.boot_id,g.unit_name,g.approved_uid,g.catalogue_id AS generation_catalogue_id,g.state,"
        "g.revision AS generation_revision FROM security_control c "
        "JOIN credential_generations g ON g.generation_id=c.runtime_generation WHERE c.id=1",
    )
    if (control["mode"] != "SECURE" or control["admission"] != "CLOSED"
            or control["schema_version"] != 1 or control["catalogue_id"] != control["generation_catalogue_id"]
            or control["state"] != generation_state):
        raise cat.SecureStateChanged()
    generation_id = cat._uuid(control["generation_id"])
    invocation_id = cat._uuid(control["invocation_id"])
    boot_id = cat._uuid(control["boot_id"])
    if (type(control["unit_name"]) is not str or not control["unit_name"]
            or type(control["approved_uid"]) is not int or control["approved_uid"] < 0):
        raise cat.SecureStateChanged()
    key = cat._row(
        db,
        "SELECT k.state,k.provisioning_state FROM key_epochs k "
        "JOIN credential_generation_keys g ON g.key_id=k.key_id AND g.key_epoch=k.epoch "
        "AND g.domain=k.domain AND g.owner_id=k.owner_id WHERE g.generation_id=:generation "
        "AND k.domain=:domain AND k.owner_id=:owner_id AND k.key_id=:key_id AND k.epoch=:epoch",
        generation=str(generation_id), domain=ref.domain, owner_id=str(ref.owner_id),
        key_id=str(ref.key_id), epoch=ref.epoch,
    )
    if key["state"] != "ACTIVE" or key["provisioning_state"] != "PERSISTED":
        raise cat.SecureStateChanged()
    try:
        revisions = db.execute(text(
            "SELECT DISTINCT i.inventory_revision FROM secure_deletion_inventory i "
            "WHERE i.domain=:domain AND i.owner_id=:owner_id AND i.key_id=:key_id "
            "AND i.key_epoch=:epoch AND i.logical_state='LOGICALLY_REVOKED' AND EXISTS "
            "(SELECT 1 FROM security_tombstones t WHERE t.domain=i.domain AND t.owner_id=i.owner_id "
            "AND t.target_kind=i.target_kind AND t.target_id=i.target_id "
            "AND t.deletion_epoch=i.deletion_epoch)"
            + (" AND i.inventory_revision=:inventory_revision" if inventory_revision is not None else "")),
            dict(domain=ref.domain, owner_id=str(ref.owner_id), key_id=str(ref.key_id), epoch=ref.epoch,
                 inventory_revision=str(inventory_revision)),
        ).scalars().all()
    except SQLAlchemyError:
        raise cat.SecureStateChanged() from None
    if len(revisions) != 1:
        raise cat.SecureStateChanged()
    return (
        cat._count(control["control_revision"]), generation_id, cat._count(control["generation_revision"]),
        invocation_id, boot_id, control["unit_name"], control["approved_uid"], cat._uuid(revisions[0]),
    )


@contextmanager
def destruction_authority(db, *, ref, approval, drain_proof, supervisor=None, runtime=None,
                          require_drained=False, inventory_revision=None):
    guard = getattr(runtime, "closed_destruction_guard", None)
    if type(require_drained) is not bool or not callable(guard):
        raise cat.SecureStateChanged()
    try:
        with guard():
            yield _validate_destruction(db, ref=ref, approval=approval, drain_proof=drain_proof,
                                        supervisor=supervisor, require_drained=require_drained,
                                        inventory_revision=inventory_revision)
    except cat.SecureStateChanged:
        raise
    except Exception:
        raise cat.SecureStateChanged() from None


def validate_destruction(db, *, ref, approval, drain_proof, supervisor=None, runtime=None):
    """Preserve B6.4's synthetic VERIFIED-to-DRAINED authority transition."""
    with destruction_authority(db, ref=ref, approval=approval, drain_proof=drain_proof,
                               supervisor=supervisor, runtime=runtime,
                               inventory_revision=getattr(approval, "inventory_revision", None)):
        pass


def _validate_destruction(db, *, ref, approval, drain_proof, supervisor, require_drained,
                          inventory_revision=None):
    target_revision = inventory_revision
    state = _destruction_state(db, ref=ref,
                               generation_state="DRAINED" if require_drained else "VERIFIED",
                               inventory_revision=target_revision)
    (_, generation_id, generation_revision, invocation_id, boot_id, unit_name, approved_uid,
     inventory_revision) = state
    if type(approval) is not ExactTargetApproval or type(drain_proof) is not SupervisorDrainProof:
        raise cat.SecureStateChanged()
    if (drain_proof.generation_id, drain_proof.invocation_id, drain_proof.boot_id,
            drain_proof.unit_name, drain_proof.approved_uid, drain_proof.inventory_revision) != (
                generation_id, invocation_id, boot_id, unit_name, approved_uid, inventory_revision):
        raise cat.SecureStateChanged()
    try:
        approval.require(ref, inventory_revision)
    except (ValueError, VaultUnavailable):
        raise cat.SecureStateChanged() from None
    verify = getattr(supervisor, "verify", None)
    if not callable(verify):
        raise cat.SecureStateChanged()
    try:
        verified = verify(drain_proof, generation_id=generation_id, invocation_id=invocation_id,
                          boot_id=boot_id, unit_name=unit_name, approved_uid=approved_uid,
                          inventory_revision=inventory_revision)
    except Exception:
        raise cat.SecureStateChanged() from None
    if (verified is not True or _destruction_state(
            db, ref=ref, generation_state="DRAINED" if require_drained else "VERIFIED",
            inventory_revision=target_revision) != state):
        raise cat.SecureStateChanged()
    if require_drained:
        return state
    cat._cas(db, "UPDATE credential_generations SET state='DRAINING',revision=revision+1 "
             "WHERE generation_id=:generation AND state='VERIFIED' AND revision=:revision",
             generation=str(generation_id), revision=generation_revision)
    cat._cas(db, "UPDATE credential_generations SET state='DRAINED',revision=revision+1 "
             "WHERE generation_id=:generation AND state='DRAINING' AND revision=:revision",
             generation=str(generation_id), revision=generation_revision + 1)
    return state


def _inventory_rows(db, *, ref, inventory_revision, limit=None):
    return db.execute(text(
        "SELECT i.* FROM secure_deletion_inventory i JOIN security_tombstones t "
        "ON (t.domain,t.owner_id,t.target_kind,t.target_id,t.deletion_epoch)="
        "(i.domain,i.owner_id,i.target_kind,i.target_id,i.deletion_epoch) "
        "WHERE i.domain=:domain AND i.owner_id=:owner_id AND i.key_id=:key_id "
        "AND i.key_epoch=:epoch AND i.inventory_revision=:inventory_revision "
        "AND i.logical_state='LOGICALLY_REVOKED' ORDER BY "
        "CASE WHEN i.physical_state IN ('DELETED','ABSENT') THEN 1 ELSE 0 END,i.id"
        + (" LIMIT :limit" if limit is not None else "")),
        dict(domain=ref.domain, owner_id=str(ref.owner_id), key_id=str(ref.key_id), epoch=ref.epoch,
             inventory_revision=str(inventory_revision), limit=limit)).mappings().all()


def destruction_blockers(db, *, ref, inventory_revision):
    """Read-only report: never use a shared key while any reference is uncertain."""
    if type(ref) is not KeyRef or type(inventory_revision) is not UUID:
        raise cat.SecureStateChanged()
    try:
        rows = _inventory_rows(db, ref=ref, inventory_revision=inventory_revision)
        exact = ("SELECT 1 FROM secure_deletion_inventory i JOIN security_tombstones t "
                 "ON (t.domain,t.owner_id,t.target_kind,t.target_id,t.deletion_epoch)="
                 "(i.domain,i.owner_id,i.target_kind,i.target_id,i.deletion_epoch) "
                 "WHERE i.domain=e.domain AND i.owner_id=e.owner_id AND i.key_id=e.key_id "
                 "AND i.key_epoch=e.key_epoch AND i.object_id=e.object_id AND i.object_version=e.object_version "
                 "AND i.inventory_revision=:revision AND i.logical_state='LOGICALLY_REVOKED'")
        values = dict(domain=ref.domain, owner_id=str(ref.owner_id), key_id=str(ref.key_id),
                      epoch=ref.epoch, revision=str(inventory_revision))
        objects = db.execute(text("SELECT 1 FROM encrypted_objects e WHERE e.domain=:domain "
            "AND e.owner_id=:owner_id AND e.key_id=:key_id AND e.key_epoch=:epoch "
            f"AND NOT EXISTS ({exact}) LIMIT 1"), values).first()
        pointers = db.execute(text("SELECT 1 FROM protected_pointers p JOIN encrypted_objects e "
            "ON (e.object_id,e.object_version,e.domain,e.owner_id)="
            "(p.object_id,p.object_version,p.domain,p.scope_id) WHERE p.state='ENCRYPTED_ACTIVE' "
            "AND e.domain=:domain AND e.owner_id=:owner_id AND e.key_id=:key_id AND e.key_epoch=:epoch "
            f"AND NOT EXISTS ({exact}) LIMIT 1"), values).first()
        orphan = db.execute(text("SELECT 1 FROM secure_operations WHERE domain=:domain "
            "AND scope_id=:owner_id AND state='ORPHAN_UNRESOLVED' AND NOT EXISTS (SELECT 1 FROM secure_reconciliation_events a WHERE a.operation_id=secure_operations.operation_id AND a.room_id=secure_operations.scope_id AND a.outcome='ABSENT') LIMIT 1"), values).first()
    except SQLAlchemyError:
        raise cat.SecureStateChanged() from None
    reasons = set()
    if objects:
        reasons.add("live_objects")
    if pointers:
        reasons.add("live_pointers")
    if not rows or objects or pointers or orphan:
        reasons.add("offline_unknown")
    for row in rows:
        try:
            if row["adapter"] not in ("local", "gcs"):
                raise ValueError()
            StorageReceipt(row["adapter"], row["storage_name"], row["exact_version"],
                           row["ciphertext_size"], bytes(row["ciphertext_sha256"]))
        except (TypeError, ValueError):
            reasons.add("offline_unknown")
        if row["physical_state"] == "BLOCKED" or row["failure_code"] is not None:
            reasons.add("offline_unknown")
    return frozenset(reasons)


def _receipt(db, row):
    if (row["adapter"] not in ("local", "gcs") or row["physical_state"] not in
            ("PHYSICAL_CLEANUP_PENDING", "DELETED", "ABSENT", "BLOCKED") or
            (row["failure_code"] is not None and not (row["physical_state"] == "BLOCKED"
             and row["failure_code"] == "cleanup_unconfirmed"))):
        raise cat.SecureStateChanged()
    try:
        receipt = StorageReceipt(row["adapter"], row["storage_name"], row["exact_version"],
                                 row["ciphertext_size"], bytes(row["ciphertext_sha256"]))
        cat.receipt_json(receipt)
        obj = db.execute(text("SELECT * FROM encrypted_objects WHERE object_id=:id AND object_version=:version"),
                         dict(id=row["object_id"], version=row["object_version"])).mappings().one_or_none()
        orphan = db.execute(text("SELECT 1 FROM secure_operations WHERE candidate_object_id=:id "
            "AND state='ORPHAN_UNRESOLVED' AND NOT EXISTS (SELECT 1 FROM secure_reconciliation_events a WHERE a.operation_id=secure_operations.operation_id AND a.room_id=secure_operations.scope_id AND a.outcome='ABSENT') LIMIT 1"), dict(id=row["object_id"])).first()
        pointers = db.execute(text("SELECT owner_type,owner_id,state FROM protected_pointers "
            "WHERE object_id=:id AND object_version=:version"), dict(id=row["object_id"], version=row["object_version"])).mappings().all()
        operations = db.execute(text("SELECT owner_type,owner_id FROM secure_operations WHERE domain=:domain "
            "AND scope_id=:owner AND state='ORPHAN_UNRESOLVED' AND NOT EXISTS (SELECT 1 FROM secure_reconciliation_events a WHERE a.operation_id=secure_operations.operation_id AND a.room_id=secure_operations.scope_id AND a.outcome='ABSENT')"),
            dict(domain=row["domain"], owner=row["owner_id"])).mappings().all()
    except (SQLAlchemyError, TypeError, ValueError):
        raise cat.SecureStateChanged() from None
    fields = ("object_id", "object_version", "domain", "owner_id", "key_id", "key_epoch", "adapter",
              "storage_name", "exact_version", "ciphertext_size", "ciphertext_sha256")
    if (obj is None or orphan or (row["target_kind"] == "record" and obj["record_id"] != row["target_id"])
            or tuple(obj[field] for field in fields) != tuple(row[field] for field in fields)):
        raise cat.SecureStateChanged()
    for pointer in pointers:
        if pointer["state"] != "ENCRYPTED_ACTIVE" or cat.ownership(db, pointer["owner_type"], pointer["owner_id"])[2] != row["target_id"]:
            raise cat.SecureStateChanged()
    if any(cat.ownership(db, operation["owner_type"], operation["owner_id"])[2] == row["target_id"]
           for operation in operations):
        raise cat.SecureStateChanged()
    return receipt


def cleanup_inventory(engine, store, *, ref, approval, drain_proof, supervisor=None, runtime=None,
                      inventory_revision, limit=100):
    """Fence this target by its inventory UUID, tombstone and exact receipts.

    Sibling inventories do not invalidate cleanup. Shared-key destruction retains
    its separate, whole-key checks; this path only deletes exact ciphertext.
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Cleanup limit must be 1..1000")
    if type(inventory_revision) is not UUID or not callable(getattr(store, "delete", None)):
        raise cat.SecureStateChanged()
    try:
        with engine.connect() as db:
            with destruction_authority(db, ref=ref, approval=approval, drain_proof=drain_proof,
                                       supervisor=supervisor, runtime=runtime, require_drained=True,
                                       inventory_revision=inventory_revision) as state:
                if state[-1] != inventory_revision:
                    raise cat.SecureStateChanged()
                targets = db.execute(text("SELECT DISTINCT target_kind,target_id,deletion_epoch "
                    "FROM secure_deletion_inventory WHERE domain=:domain AND owner_id=:owner "
                    "AND key_id=:key AND key_epoch=:epoch AND inventory_revision=:revision "
                    "AND logical_state='LOGICALLY_REVOKED' LIMIT 2"),
                    dict(domain=ref.domain, owner=str(ref.owner_id), key=str(ref.key_id), epoch=ref.epoch,
                         revision=str(inventory_revision))).all()
                if len(targets) != 1:
                    raise cat.SecureStateChanged()
                rows = _inventory_rows(db, ref=ref, inventory_revision=inventory_revision, limit=limit + 1)
                more = len(rows) > limit and rows[-1]["physical_state"] not in ("DELETED", "ABSENT")
                rows = rows[:limit]
                if not rows:
                    raise cat.SecureStateChanged()
                if len({(row["domain"], row["owner_id"], row["target_kind"], row["target_id"],
                         row["deletion_epoch"]) for row in rows}) != 1:
                    raise cat.SecureStateChanged()
                receipts, ids = [], set()
                for row in rows:
                    binding = db.scalar(text("SELECT b.storage_binding FROM secure_create_bindings b "
                        "JOIN secure_operations op ON op.operation_id=b.operation_id "
                        "WHERE b.object_name=:object AND b.room_id=:room AND op.candidate_object_id=b.object_name "
                        "AND op.scope_id=b.room_id"), dict(object=row["object_id"], room=row["owner_id"]))
                    if binding is not None:
                        if getattr(store, "storage_binding", None) != binding:
                            raise cat.SecureStateChanged()
                    elif getattr(runtime, "store", None) is not store:
                        raise cat.SecureStateChanged()
                    receipt, identity = _receipt(db, row), (row["object_id"], row["object_version"])
                    if identity in ids or receipt in receipts:
                        raise cat.SecureStateChanged()
                    ids.add(identity)
                    receipts.append(receipt)
                pending = [(row, receipt) for row, receipt in zip(rows, receipts)
                           if row["physical_state"] in ("PHYSICAL_CLEANUP_PENDING", "BLOCKED")]
                if not pending:
                    return True
                if ref.domain == "room":
                    cat.lock_rooms(db, (ref.owner_id,))
                for row, _ in pending:
                    cat._cas(db, "UPDATE secure_deletion_inventory SET physical_state='BLOCKED',"
                             "failure_code='cleanup_unconfirmed',revision=revision+1 WHERE id=:id "
                             "AND revision=:revision AND physical_state IN ('PHYSICAL_CLEANUP_PENDING','BLOCKED')",
                             id=row["id"], revision=row["revision"])
                db.commit()
                for row, receipt in pending:
                    try:
                        result = store.delete(receipt)
                    except Exception:
                        return False
                    if result not in ("deleted", "absent"):
                        return False
                    with db.begin():
                        if ref.domain == "room":
                            cat.lock_rooms(db, (ref.owner_id,))
                        _validate_destruction(db, ref=ref, approval=approval, drain_proof=drain_proof,
                                              supervisor=supervisor, require_drained=True,
                                              inventory_revision=inventory_revision)
                        current = _inventory_rows(db, ref=ref, inventory_revision=inventory_revision)
                        current = next((item for item in current if item["id"] == row["id"]), None)
                        target_fields = ("domain", "owner_id", "target_kind", "target_id", "deletion_epoch",
                                         "object_id", "object_version", "key_id", "key_epoch", "inventory_revision")
                        if (current is None or any(current[field] != row[field] for field in target_fields)
                                or _receipt(db, current) != receipt):
                            raise cat.SecureStateChanged()
                        result = db.execute(text(
                            "UPDATE secure_deletion_inventory SET physical_state=:state,failure_code=NULL,"
                            "revision=revision+1 WHERE id=:id AND revision=:revision AND physical_state='BLOCKED' "
                            "AND failure_code='cleanup_unconfirmed'"),
                            dict(state="DELETED" if result == "deleted" else "ABSENT", id=row["id"],
                                 revision=row["revision"] + 1))
                        if result.rowcount != 1:
                            return False
                return not more
    except cat.SecureStateChanged:
        raise
    except Exception:
        raise cat.SecureStateChanged() from None


def tombstone_record(db, *, principal_id, record_id):
    """Commit a record tombstone, scope epochs, and exact object history together."""
    record_id = cat._uuid(record_id)
    saved = cat.snapshot(db, principal_id=UUID(str(principal_id)), targets=(("record", record_id),))
    scope = next(scope for scope in saved.scopes if scope[0] == "room")
    domain, owner_id, deletion_epoch, authorization_epoch, revision = scope
    cat.lock_rooms(db, (owner_id,))
    next_epoch = deletion_epoch + 1
    cat._cas(
        db,
        "UPDATE security_scopes SET deletion_epoch=:de,authorization_epoch=:ae,revision=revision+1 "
        "WHERE domain=:d AND owner_id=:o AND state='ACTIVE' AND revision=:r "
        "AND EXISTS (SELECT 1 FROM uploads u JOIN family_rooms room ON room.id=u.room_id "
        "JOIN family_members member ON member.room_id=room.id AND member.user_id=:principal "
        "WHERE u.id=:record AND u.room_id=:o "
        "AND (u.uploader_user_id=:principal OR room.owner_user_id=:principal))",
        principal=str(saved.principal_id),
        record=str(record_id),
        de=next_epoch,
        ae=authorization_epoch + 1,
        d=domain,
        o=str(owner_id),
        r=revision,
    )
    cat._cas(
        db,
        "INSERT INTO security_tombstones (domain,owner_id,target_kind,target_id,deletion_epoch,revision) "
        "VALUES (:d,:o,'record',:target,:de,1)",
        d=domain,
        o=str(owner_id),
        target=str(record_id),
        de=next_epoch,
    )
    inventory_revision = str(uuid4())
    objects = db.execute(
        text("SELECT object_id,object_version,key_id,key_epoch,adapter,storage_name,exact_version,"
             "ciphertext_size,ciphertext_sha256 FROM encrypted_objects WHERE record_id=:record "
             "ORDER BY object_id,object_version"),
        dict(record=str(record_id)),
    ).mappings().all()
    for obj in objects:
        cat._cas(
            db,
            "INSERT INTO secure_deletion_inventory "
            "(id,domain,owner_id,deletion_epoch,revision,target_kind,target_id,object_id,object_version,"
            "key_id,key_epoch,adapter,storage_name,exact_version,ciphertext_size,ciphertext_sha256,"
            "inventory_revision,logical_state,key_state,physical_state,retention_state,backup_state,crypto_state) "
            "VALUES (:id,:d,:o,:de,1,'record',:target,:object_id,:object_version,:key_id,:key_epoch,"
            ":adapter,:storage_name,:exact_version,:ciphertext_size,:ciphertext_sha256,:inventory_revision,"
            "'LOGICALLY_REVOKED','PENDING','PHYSICAL_CLEANUP_PENDING','UNKNOWN','UNKNOWN',"
            "'CRYPTO_ERASURE_NOT_ESTABLISHED')",
            id=str(uuid4()),
            d=domain,
            o=str(owner_id),
            de=next_epoch,
            target=str(record_id),
            inventory_revision=inventory_revision,
            **obj,
        )
