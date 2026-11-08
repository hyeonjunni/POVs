"""Validated, immutable inputs for the opt-in storage primitives."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from uuid import UUID

MAX_BLOB_SIZE = 32 * 1024 * 1024
HEADER_LIMIT = 4096


class StorageIntegrityError(ValueError):
    pass


class InventoryRequired(RuntimeError):
    """Unknown retained generations require separately authorized inventory."""


class ListPermissionRequired(RuntimeError):
    """Maintenance identity lacks storage.objects.list."""


class StorageCreateUnconfirmed(RuntimeError):
    """Create returned a generation, but exact readback could not be confirmed."""
    def __init__(self, receipt):
        super().__init__("Ciphertext create readback unconfirmed")
        self.receipt = receipt


class VaultUnavailable(RuntimeError):
    pass


class CapacityExceeded(ValueError):
    pass


class StorageConflict(RuntimeError):
    pass


def positive_int(value, *, minimum=1):
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise ValueError("Invalid integer")


def uuid_value(value):
    if type(value) is not UUID:
        raise ValueError("UUID required")


def secret_bytes(value, length):
    if type(value) is not bytes or len(value) != length:
        raise StorageIntegrityError("Invalid secret material")


@dataclass(frozen=True)
class KeyRef:
    domain: str
    owner_id: UUID
    key_id: UUID
    epoch: int

    def __post_init__(self):
        if self.domain not in ("room", "user", "lookup"):
            raise ValueError("Invalid key domain")
        uuid_value(self.owner_id)
        uuid_value(self.key_id)
        positive_int(self.epoch)


@dataclass(frozen=True)
class PayloadIdentity:
    domain: str
    owner_id: UUID
    record_id: UUID | None
    object_id: UUID
    object_version: int
    artifact_kind: str
    source_object_id: UUID | None
    source_version: int | None
    plaintext_length: int

    def __post_init__(self):
        uuid_value(self.owner_id)
        uuid_value(self.object_id)
        positive_int(self.object_version)
        positive_int(self.plaintext_length, minimum=0)
        if self.record_id is not None:
            uuid_value(self.record_id)
        if (self.source_object_id is None) != (self.source_version is None):
            raise ValueError("Incomplete source identity")
        if self.source_object_id is not None:
            uuid_value(self.source_object_id)
            positive_int(self.source_version)
        room_kinds = ("original", "record_metadata", "room_metadata", "room_profile", "processing_result", "room_derivatives")
        if self.domain == "room" and self.artifact_kind in room_kinds:
            if (self.record_id is None) != (self.artifact_kind == "room_metadata"):
                raise ValueError("Invalid room record identity")
        elif self.domain == "user" and self.artifact_kind in ("user_profile", "auth_identity"):
            if self.record_id is not None:
                raise ValueError("User record must be null")
        else:
            raise ValueError("Invalid artifact domain")
        limit = {"original": 20 * 1024 * 1024, "user_profile": 1024 * 1024,
                 "room_profile": 1024 * 1024, "auth_identity": 16 * 1024}.get(self.artifact_kind, MAX_BLOB_SIZE)
        if self.plaintext_length > limit or self.plaintext_length + len(self.aad()) + 36 > MAX_BLOB_SIZE:
            raise CapacityExceeded("Payload capacity exceeded")

    def aad(self):
        values = ["ambient-legacy", 1, 1, "AES-256-GCM", self.domain, str(self.owner_id),
                  str(self.record_id) if self.record_id else None, str(self.object_id),
                  self.object_version, self.artifact_kind,
                  str(self.source_object_id) if self.source_object_id else None,
                  self.source_version, self.plaintext_length]
        return json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True)
class SealedPayload:
    blob: bytes = field(repr=False)
    wrapped_dek: bytes = field(repr=False)

    def __post_init__(self):
        secret_bytes(self.wrapped_dek, 40)
        if type(self.blob) is not bytes or not 36 <= len(self.blob) <= MAX_BLOB_SIZE:
            raise StorageIntegrityError("Invalid sealed payload")


@dataclass(frozen=True)
class ObjectMetadata:
    exact_version: str
    ciphertext_size: int
    ciphertext_sha256: bytes

    def __post_init__(self):
        if type(self.exact_version) is not str or not 1 <= len(self.exact_version) <= 256:
            raise ValueError("Invalid object version")
        positive_int(self.ciphertext_size)
        if self.ciphertext_size > MAX_BLOB_SIZE:
            raise CapacityExceeded("Ciphertext capacity exceeded")
        secret_bytes(self.ciphertext_sha256, 32)


@dataclass(frozen=True)
class StorageReceipt:
    adapter: str
    object_name: str
    exact_version: str
    ciphertext_size: int
    ciphertext_sha256: bytes

    def __post_init__(self):
        if self.adapter not in ("local", "gcs") or type(self.object_name) is not str:
            raise ValueError("Invalid adapter or name")
        if str(UUID(self.object_name)) != self.object_name:
            raise ValueError("Canonical object name required")
        ObjectMetadata(self.exact_version, self.ciphertext_size, self.ciphertext_sha256)


@dataclass(frozen=True)
class ExactTargetApproval:
    operation_id: UUID
    domain: str
    owner_id: UUID
    key_id: UUID
    epoch: int
    expires_at: datetime
    inventory_revision: UUID
    references_drained: bool

    def __post_init__(self):
        uuid_value(self.operation_id)
        uuid_value(self.inventory_revision)
        KeyRef(self.domain, self.owner_id, self.key_id, self.epoch)
        if not isinstance(self.expires_at, datetime) or self.expires_at.tzinfo is None:
            raise ValueError("Aware expiry required")
        if type(self.references_drained) is not bool:
            raise ValueError("Explicit drained status required")

    def require(self, ref, inventory_revision):
        if (ref != KeyRef(self.domain, self.owner_id, self.key_id, self.epoch)
                or inventory_revision != self.inventory_revision or not self.references_drained
                or self.expires_at <= datetime.now(timezone.utc)):
            raise VaultUnavailable("Key destruction not approved")
