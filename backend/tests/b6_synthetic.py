"""B6 preparation fixtures only; no lifecycle implementation or real custody.

Future entry points are proposals documented in M4_B6_IMPLEMENTATION_BRIEF.md.
Missing implementations fail at test execution, never during collection.
"""
import importlib
import importlib.util
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import text

from app.storage.contracts import ExactTargetApproval, KeyRef
from test_secure_catalogue import put


def entry(module, name):
    assert importlib.util.find_spec(module) is not None, f"B6 implementation absent: {module}.{name}"
    function = getattr(importlib.import_module(module), name, None)
    assert callable(function), f"B6 implementation absent: {module}.{name}"
    return function


def tombstone(db, room_id, record_id):
    """Arrange an already committed logical deletion; not a deletion service."""
    db.execute(text("UPDATE security_scopes SET deletion_epoch=deletion_epoch+1,"
                    "authorization_epoch=authorization_epoch+1,revision=revision+1 "
                    "WHERE domain='room' AND owner_id=:r"), dict(r=room_id))
    epoch = db.scalar(text("SELECT deletion_epoch FROM security_scopes WHERE domain='room' AND owner_id=:r"), dict(r=room_id))
    put(db, "security_tombstones", domain="room", owner_id=room_id, target_kind="record",
        target_id=record_id, deletion_epoch=epoch, revision=1)
    return epoch


def inventory(db, room_id, record_id, *, revision=None):
    revision = revision or uuid4()
    epoch = db.scalar(text("SELECT deletion_epoch FROM security_scopes WHERE domain='room' AND owner_id=:r"), dict(r=room_id))
    objects = db.execute(text("SELECT * FROM encrypted_objects WHERE record_id=:r"), dict(r=record_id)).mappings().all()
    assert objects, "fixture needs real encrypted record history"
    for obj in objects:
        put(db, "secure_deletion_inventory", id=str(uuid4()), domain="room", owner_id=room_id,
            deletion_epoch=epoch, revision=1, target_kind="record", target_id=record_id,
            object_id=obj["object_id"], object_version=obj["object_version"],
            key_id=obj["key_id"], key_epoch=obj["key_epoch"], adapter=obj["adapter"],
            storage_name=obj["storage_name"], exact_version=obj["exact_version"],
            ciphertext_size=obj["ciphertext_size"], ciphertext_sha256=obj["ciphertext_sha256"],
            inventory_revision=str(revision), logical_state="LOGICALLY_REVOKED", key_state="PENDING",
            physical_state="PHYSICAL_CLEANUP_PENDING", retention_state="UNKNOWN", backup_state="UNKNOWN",
            crypto_state="CRYPTO_ERASURE_NOT_ESTABLISHED")
    return revision


def approval_for(ref, revision, *, drained=True):
    return ExactTargetApproval(uuid4(), ref.domain, ref.owner_id, ref.key_id, ref.epoch,
        datetime.now(timezone.utc) + timedelta(minutes=5), revision, drained)


def room_ref(db, room_id):
    k = db.execute(text("SELECT * FROM key_epochs WHERE domain='room' AND owner_id=:r AND state='ACTIVE'"), dict(r=room_id)).mappings().one()
    return KeyRef("room", UUID(room_id), UUID(k["key_id"]), k["epoch"])


class RecordingStore:
    storage_binding = "local:synthetic"
    """Inert side-effect recorder, deliberately implements no deletion policy."""
    def __init__(self, result="deleted"):
        self.delete_calls = []
        self.result = result

    def delete(self, receipt):
        self.delete_calls.append(receipt)
        return self.result

    def read(self, receipt):
        raise AssertionError("boundary rejection must happen before object access")


class SyntheticDrainSupervisor:
    """Test-only external supervisor; only final membership drain mints a usable proof."""
    def __init__(self, **binding):
        self.binding = binding
        self.members = {"parent-worker", "descendant-sender"}
        self.issued = set()
        self.on_verify = None

    def acknowledge(self):
        pass

    def timeout(self):
        pass

    def remove(self, member):
        self.members.remove(member)

    def mutate_on_verify(self, action):
        self.on_verify = action

    def issue(self, proof_type, **overrides):
        values = dict(self.binding)
        values.update(overrides)
        proof = proof_type(uuid4(), **values)
        self.issued.add(proof.proof_id)
        return proof

    def verify(self, proof, **expected):
        valid = not self.members and proof.proof_id in self.issued and all(
            getattr(proof, name) == value for name, value in expected.items())
        if valid and self.on_verify is not None:
            action, self.on_verify = self.on_verify, None
            action()
        return valid


def drained_authority(db, *, ref, inventory_revision, runtime):
    """Arrange B6.4's actual drained authority for a synthetic B6.5 cleanup test."""
    generation = db.execute(text(
        "SELECT g.* FROM security_control c JOIN credential_generations g "
        "ON g.generation_id=c.runtime_generation WHERE c.id=1")).mappings().one()
    supervisor = SyntheticDrainSupervisor(
        generation_id=UUID(str(generation["generation_id"])),
        invocation_id=UUID(str(generation["invocation_id"])),
        boot_id=UUID(str(generation["boot_id"])),
        unit_name=generation["unit_name"],
        approved_uid=generation["approved_uid"],
        inventory_revision=inventory_revision,
    )
    supervisor.remove("parent-worker")
    supervisor.remove("descendant-sender")
    proof = supervisor.issue(entry("app.services.secure_deletion", "SupervisorDrainProof"))
    approval = approval_for(ref, inventory_revision)
    entry("app.services.secure_deletion", "validate_destruction")(
        db, ref=ref, approval=approval, drain_proof=proof, supervisor=supervisor, runtime=runtime)
    return approval, proof, supervisor
