"""Opaque catalogue rows. Payloads and key material never belong in these models."""
import sqlalchemy as sa
import sqlite3
from contextlib import contextmanager

from app.core.database import Base


@sa.event.listens_for(sa.engine.Engine, "connect")
def _default_deny_mutation(dbapi_connection, _record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("m4_mutation_authorized", 3, lambda *_: 0)


@contextmanager
def catalogue_mutation(connection, operation, table, row_ids):
    """Exact connection-local cleanup/ACL exception, never a plaintext bypass.

    The catalogue caller owns the transaction. Membership deletion requires its
    room authorization epoch to advance after entry, in that same transaction.
    """
    allowed = {"membership": {"family_members"}, "cutover_cleanup": {"record_chunks", "record_embeddings"}}
    if connection.dialect.name != "sqlite" or not connection.in_transaction() or table not in allowed.get(operation, set()):
        raise ValueError("Invalid catalogue mutation")
    mode = connection.scalar(sa.text("SELECT mode FROM security_control WHERE id=1"))
    if mode not in ("CUTOVER", "SECURE", "QUARANTINED") or (operation == "cutover_cleanup" and mode != "CUTOVER"):
        raise ValueError("Invalid catalogue mutation mode")
    targets = frozenset(row_ids)
    if not targets or any(type(row) is not str for row in targets) or connection.info.get("m4_mutation_active"):
        raise ValueError("Invalid catalogue mutation targets")
    epochs = {}
    if operation == "membership":
        for row in targets:
            observed = connection.execute(sa.text("SELECT s.owner_id,s.authorization_epoch FROM family_members m JOIN security_scopes s ON s.domain='room' AND s.owner_id=m.room_id WHERE m.id=:id"), {"id": row}).one()
            epochs[row] = tuple(observed)
    raw = connection.connection.driver_connection

    def authorized(requested_operation, requested_table, row_id):
        if (requested_operation, requested_table) != (operation, table) or row_id not in targets:
            return 0
        if operation == "membership":
            owner, epoch = epochs[row_id]
            current = raw.execute("SELECT authorization_epoch FROM security_scopes WHERE domain='room' AND owner_id=?", (owner,)).fetchone()
            return int(current is not None and current[0] > epoch)
        return 1

    connection.info["m4_mutation_active"] = True
    raw.create_function("m4_mutation_authorized", 3, authorized)
    try:
        yield
    finally:
        raw.create_function("m4_mutation_authorized", 3, lambda *_: 0)
        connection.info.pop("m4_mutation_active", None)


class SecurityControl(Base):
    __table__ = sa.Table(
        "security_control", Base.metadata,
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.CheckConstraint("id = 1", name="ck_m4_security_control_1"),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.CheckConstraint("mode IN ('LEGACY','CUTOVER','SECURE','QUARANTINED')", name="ck_m4_security_control_2"),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("schema_version = 1", name="ck_m4_security_control_3"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_security_control_4"),
        sa.Column("catalogue_id", sa.Text(), nullable=False, unique=True),
        sa.Column("runtime_generation", sa.Text(), nullable=True),
        sa.Column("admission", sa.Text(), nullable=False),
        sa.CheckConstraint("admission IN ('CLOSED','OPEN')", name="ck_m4_security_control_5"),
        sa.ForeignKeyConstraint(["runtime_generation", "catalogue_id"], ["credential_generations.generation_id", "credential_generations.catalogue_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("admission='CLOSED' OR (mode='SECURE' AND runtime_generation IS NOT NULL)", name="ck_m4_security_control_6"),
    )


class SecurityScope(Base):
    __table__ = sa.Table(
        "security_scopes", Base.metadata,
        sa.Column("domain", sa.Text(), nullable=False),
        sa.CheckConstraint("domain IN ('room','user','lookup')", name="ck_m4_security_scopes_1"),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('ACTIVE','REVOKED')", name="ck_m4_security_scopes_2"),
        sa.Column("deletion_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("deletion_epoch >= 0", name="ck_m4_security_scopes_3"),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("authorization_epoch >= 0", name="ck_m4_security_scopes_4"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_security_scopes_5"),
        sa.PrimaryKeyConstraint("domain", "owner_id"),
    )


class KeyEpoch(Base):
    __table__ = sa.Table(
        "key_epochs", Base.metadata,
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("epoch >= 1", name="ck_m4_key_epochs_1"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('PREPARED','ACTIVE','DECRYPT_ONLY','REVOCATION_PENDING','DESTROYED_LOCAL')", name="ck_m4_key_epochs_2"),
        sa.Column("provisioning_state", sa.Text(), nullable=False),
        sa.CheckConstraint("provisioning_state IN ('REQUESTED','PERSISTED','UNCERTAIN','REMOVED')", name="ck_m4_key_epochs_3"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_key_epochs_4"),
        sa.Column("recovery_inventory_state", sa.Text(), nullable=False),
        sa.CheckConstraint("recovery_inventory_state IN ('UNKNOWN','VERIFIED')", name="ck_m4_key_epochs_5"),
        sa.Column("recovery_reference_count", sa.Integer(), nullable=True),
        sa.CheckConstraint("recovery_reference_count >= 0", name="ck_m4_key_epochs_6"),
        sa.PrimaryKeyConstraint("key_id", "epoch"),
        sa.UniqueConstraint("key_id", "epoch", "domain", "owner_id"),
        sa.ForeignKeyConstraint(["domain", "owner_id"], ["security_scopes.domain", "security_scopes.owner_id"]),
        sa.CheckConstraint("(recovery_inventory_state='UNKNOWN' AND recovery_reference_count IS NULL) OR (recovery_inventory_state='VERIFIED' AND recovery_reference_count IS NOT NULL)", name="ck_m4_key_epochs_7"),
    )


class CredentialGeneration(Base):
    __table__ = sa.Table(
        "credential_generations", Base.metadata,
        sa.Column("generation_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("catalogue_id", sa.Text(), sa.ForeignKey("security_control.catalogue_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("inventory_revision", sa.Text(), nullable=False, unique=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('REQUESTED','VERIFIED','DRAINING','DRAINED','FAILED')", name="ck_m4_credential_generations_1"),
        sa.Column("unit_name", sa.Text(), nullable=False),
        sa.Column("invocation_id", sa.Text(), nullable=True),
        sa.Column("boot_id", sa.Text(), nullable=True),
        sa.Column("approved_uid", sa.Integer(), nullable=False),
        sa.CheckConstraint("approved_uid > 0", name="ck_m4_credential_generations_2"),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("control_revision >= 1", name="ck_m4_credential_generations_3"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_credential_generations_4"),
        sa.UniqueConstraint("generation_id", "catalogue_id"),
        sa.UniqueConstraint("boot_id", "unit_name", "invocation_id"),
        sa.CheckConstraint("length(unit_name) BETWEEN 1 AND 255", name="ck_m4_credential_generations_5"),
        sa.CheckConstraint("(invocation_id IS NULL AND boot_id IS NULL) OR (invocation_id IS NOT NULL AND boot_id IS NOT NULL)", name="ck_m4_credential_generations_6"),
        sa.CheckConstraint("state NOT IN ('VERIFIED','DRAINING','DRAINED') OR invocation_id IS NOT NULL", name="ck_m4_credential_generations_7"),
    )


class CredentialGenerationKey(Base):
    __table__ = sa.Table(
        "credential_generation_keys", Base.metadata,
        sa.Column("generation_id", sa.Text(), sa.ForeignKey("credential_generations.generation_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("key_epoch >= 1", name="ck_m4_credential_generation_keys_1"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.CheckConstraint("domain IN ('room','user','lookup')", name="ck_m4_credential_generation_keys_2"),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "key_id", "key_epoch"),
        sa.ForeignKeyConstraint(["key_id", "key_epoch", "domain", "owner_id"], ["key_epochs.key_id", "key_epochs.epoch", "key_epochs.domain", "key_epochs.owner_id"]),
    )


class EncryptedObject(Base):
    __table__ = sa.Table(
        "encrypted_objects", Base.metadata,
        sa.Column("object_id", sa.Text(), nullable=False),
        sa.Column("object_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("object_version >= 1", name="ck_m4_encrypted_objects_1"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.CheckConstraint("domain IN ('room','user')", name="ck_m4_encrypted_objects_2"),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("record_id", sa.Text(), nullable=True),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.CheckConstraint("artifact_kind IN ('original','record_metadata','room_metadata','room_profile','processing_result', 'room_derivatives','user_profile','auth_identity')", name="ck_m4_encrypted_objects_3"),
        sa.Column("source_object_id", sa.Text(), nullable=True),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("plaintext_length", sa.Integer(), nullable=False),
        sa.CheckConstraint("plaintext_length >= 0", name="ck_m4_encrypted_objects_4"),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("payload_version = 1", name="ck_m4_encrypted_objects_5"),
        sa.Column("aad_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("aad_version = 1", name="ck_m4_encrypted_objects_6"),
        sa.Column("payload_algorithm", sa.Text(), nullable=False),
        sa.CheckConstraint("payload_algorithm = 'AES-256-GCM'", name="ck_m4_encrypted_objects_7"),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        sa.Column("wrapper_algorithm", sa.Text(), nullable=False),
        sa.CheckConstraint("wrapper_algorithm = 'AES-256-KW'", name="ck_m4_encrypted_objects_8"),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("length(wrapped_dek) = 40", name="ck_m4_encrypted_objects_9"),
        sa.Column("envelope_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("envelope_revision >= 1", name="ck_m4_encrypted_objects_10"),
        sa.Column("adapter", sa.Text(), nullable=False),
        sa.CheckConstraint("adapter IN ('local','gcs')", name="ck_m4_encrypted_objects_11"),
        sa.Column("storage_name", sa.Text(), nullable=False),
        sa.Column("exact_version", sa.Text(), nullable=False),
        sa.Column("ciphertext_size", sa.Integer(), nullable=False),
        sa.CheckConstraint("ciphertext_size > 0", name="ck_m4_encrypted_objects_12"),
        sa.Column("ciphertext_sha256", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("length(ciphertext_sha256) = 32", name="ck_m4_encrypted_objects_13"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('STAGED','ACTIVE','RETIRED','CLEANUP_PENDING')", name="ck_m4_encrypted_objects_14"),
        sa.PrimaryKeyConstraint("object_id", "object_version"),
        sa.UniqueConstraint("adapter", "storage_name", "exact_version"),
        sa.UniqueConstraint("object_id", "object_version", "domain", "owner_id"),
        sa.ForeignKeyConstraint(["key_id", "key_epoch", "domain", "owner_id"], ["key_epochs.key_id", "key_epochs.epoch", "key_epochs.domain", "key_epochs.owner_id"]),
        sa.ForeignKeyConstraint(["source_object_id", "source_version", "domain", "owner_id"], ["encrypted_objects.object_id", "encrypted_objects.object_version", "encrypted_objects.domain", "encrypted_objects.owner_id"]),
        sa.CheckConstraint("(source_object_id IS NULL AND source_version IS NULL) OR (source_object_id IS NOT NULL AND source_version IS NOT NULL AND source_version >= 1)", name="ck_m4_encrypted_objects_15"),
        sa.CheckConstraint("domain != 'user' OR record_id IS NULL", name="ck_m4_encrypted_objects_16"),
        sa.CheckConstraint("domain != 'room' OR ((artifact_kind='room_metadata' AND record_id IS NULL) OR (artifact_kind!='room_metadata' AND record_id IS NOT NULL))", name="ck_m4_encrypted_objects_17"),
        sa.CheckConstraint("ciphertext_size <= 33554432", name="ck_m4_encrypted_objects_18"),
        sa.CheckConstraint("(artifact_kind IN ('user_profile','auth_identity') AND domain='user') OR (artifact_kind NOT IN ('user_profile','auth_identity') AND domain='room')", name="ck_m4_encrypted_objects_19"),
    )


class ProtectedPointer(Base):
    __table__ = sa.Table(
        "protected_pointers", Base.metadata,
        sa.Column("owner_type", sa.Text(), nullable=False),
        sa.CheckConstraint("owner_type IN ('user','auth_identity','room','record','file','job','record_index')", name="ck_m4_protected_pointers_1"),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("object_version", sa.Integer(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('LEGACY_PLAINTEXT','MIGRATION_PENDING','ENCRYPTED_ACTIVE','CLEANUP_PENDING','FAILED')", name="ck_m4_protected_pointers_2"),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("source_revision >= 1", name="ck_m4_protected_pointers_3"),
        sa.Column("deletion_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("deletion_epoch >= 0", name="ck_m4_protected_pointers_4"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_protected_pointers_5"),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("authorization_epoch >= 0", name="ck_m4_protected_pointers_6"),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("owner_type", "owner_id"),
        sa.ForeignKeyConstraint(["domain", "scope_id"], ["security_scopes.domain", "security_scopes.owner_id"]),
        sa.ForeignKeyConstraint(["object_id", "object_version", "domain", "scope_id"], ["encrypted_objects.object_id", "encrypted_objects.object_version", "encrypted_objects.domain", "encrypted_objects.owner_id"]),
        sa.CheckConstraint("(object_id IS NULL AND object_version IS NULL) OR (object_id IS NOT NULL AND object_version IS NOT NULL AND object_version >= 1)", name="ck_m4_protected_pointers_7"),
        sa.CheckConstraint("state NOT IN ('ENCRYPTED_ACTIVE','CLEANUP_PENDING') OR object_id IS NOT NULL", name="ck_m4_protected_pointers_8"),
        sa.CheckConstraint("owner_type NOT IN ('user','auth_identity') OR domain='user'", name="ck_m4_protected_pointers_9"),
        sa.CheckConstraint("owner_type IN ('user','auth_identity') OR domain='room'", name="ck_m4_protected_pointers_10"),
    )


class SecureOperation(Base):
    __table__ = sa.Table(
        "secure_operations", Base.metadata,
        sa.Column("operation_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('PAYLOAD','SIGNUP','ROOM_CREATE')", name="ck_m4_secure_operations_1"),
        sa.Column("parent_operation_id", sa.Text(), sa.ForeignKey("secure_operations.operation_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("principal_id", sa.Text(), nullable=True),
        sa.Column("snapshot_json", sa.Text(), nullable=True),
        sa.Column("owner_type", sa.Text(), nullable=False),
        sa.CheckConstraint("owner_type IN ('user','auth_identity','room','record','file','job','record_index')", name="ck_m4_secure_operations_2"),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.CheckConstraint("domain IN ('user','room')", name="ck_m4_secure_operations_3"),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("control_revision >= 1", name="ck_m4_secure_operations_4"),
        sa.Column("pointer_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("pointer_revision >= 1", name="ck_m4_secure_operations_5"),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("source_revision >= 1", name="ck_m4_secure_operations_6"),
        sa.Column("deletion_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("deletion_epoch >= 0", name="ck_m4_secure_operations_7"),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("authorization_epoch >= 0", name="ck_m4_secure_operations_8"),
        sa.Column("runtime_generation", sa.Text(), sa.ForeignKey("credential_generations.generation_id", ondelete="RESTRICT"), nullable=True),
        sa.Column("candidate_object_id", sa.Text(), nullable=True, unique=True),
        sa.Column("activation_key_id", sa.Text(), nullable=True),
        sa.Column("activation_key_epoch", sa.Integer(), nullable=True),
        sa.Column("continuation_digest", sa.LargeBinary(), nullable=True, unique=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("created_at >= 0", name="ck_m4_secure_operations_9"),
        sa.Column("expires_at", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_secure_operations_10"),
        sa.Column("state", sa.Text(), nullable=False),
        sa.CheckConstraint("state IN ('CLAIMED','RECEIPTED','VERIFIED','COMMITTED','ABANDONED','ORPHAN_UNRESOLVED', 'WAITING_CREDENTIAL','READY','EXPIRED','FAILED')", name="ck_m4_secure_operations_11"),
        sa.Column("receipt_json", sa.Text(), nullable=True),
        sa.Column("reserved_ciphertext_bytes", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("reserved_ciphertext_bytes IS NULL OR (reserved_ciphertext_bytes > 0 AND kind='PAYLOAD' AND domain='room')", name="ck_secure_operation_reserved_bytes"),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.CheckConstraint("failure_code IS NULL OR failure_code IN ('source_changed','credential_unavailable','activation_expired','integrity_failed', 'receipt_unknown','cancelled','identity_conflict')", name="ck_m4_secure_operations_12"),
        sa.ForeignKeyConstraint(["domain", "scope_id"], ["security_scopes.domain", "security_scopes.owner_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["activation_key_id", "activation_key_epoch", "domain", "scope_id"], ["key_epochs.key_id", "key_epochs.epoch", "key_epochs.domain", "key_epochs.owner_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("(kind='PAYLOAD' AND principal_id IS NOT NULL AND snapshot_json IS NOT NULL AND runtime_generation IS NOT NULL AND candidate_object_id IS NOT NULL AND activation_key_id IS NULL AND activation_key_epoch IS NULL AND continuation_digest IS NULL AND expires_at IS NULL AND state IN ('CLAIMED','RECEIPTED','VERIFIED','COMMITTED','ABANDONED','ORPHAN_UNRESOLVED')) OR (kind IN ('SIGNUP','ROOM_CREATE') AND candidate_object_id IS NULL AND parent_operation_id IS NULL AND activation_key_id IS NOT NULL AND activation_key_epoch IS NOT NULL AND activation_key_epoch >= 1 AND continuation_digest IS NOT NULL AND length(continuation_digest)=32 AND expires_at IS NOT NULL AND expires_at=created_at+86400 AND receipt_json IS NULL AND state IN ('WAITING_CREDENTIAL','READY','COMMITTED','EXPIRED','FAILED') AND ((kind='SIGNUP' AND principal_id IS NULL AND domain='user' AND owner_type='user') OR (kind='ROOM_CREATE' AND principal_id IS NOT NULL AND domain='room' AND owner_type='room')))", name="ck_m4_secure_operations_13"),
        sa.CheckConstraint("kind='PAYLOAD' OR scope_id=owner_id", name="ck_m4_secure_operations_14"),
        sa.CheckConstraint("kind='PAYLOAD' OR state NOT IN ('READY','COMMITTED') OR runtime_generation IS NOT NULL", name="ck_m4_secure_operations_15"),
        sa.CheckConstraint("kind!='PAYLOAD' OR state NOT IN ('RECEIPTED','VERIFIED','COMMITTED') OR receipt_json IS NOT NULL", name="ck_m4_secure_operations_16"),
        sa.CheckConstraint("state NOT IN ('FAILED','ABANDONED','ORPHAN_UNRESOLVED','EXPIRED') OR failure_code IS NOT NULL", name="ck_m4_secure_operations_17"),
    )


class BlindIndex(Base):
    __table__ = sa.Table(
        "blind_indexes", Base.metadata,
        sa.Column("kind", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('local_username','google_subject','email')", name="ck_m4_blind_indexes_1"),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.CheckConstraint("provider IN ('local','google','email')", name="ck_m4_blind_indexes_2"),
        sa.Column("normalization_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("normalization_version = 1", name="ck_m4_blind_indexes_3"),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("key_epoch", sa.Integer(), nullable=False),
        sa.Column("key_domain", sa.Text(), nullable=False),
        sa.CheckConstraint("key_domain='lookup'", name="ck_m4_blind_indexes_4"),
        sa.Column("key_owner_id", sa.Text(), nullable=False),
        sa.Column("token", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("length(token)=32", name="ck_m4_blind_indexes_5"),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("identity_id", sa.Text(), sa.ForeignKey("auth_identities.id"), nullable=True),
        sa.PrimaryKeyConstraint("kind", "provider", "normalization_version", "key_id", "key_epoch", "token"),
        sa.ForeignKeyConstraint(["key_id", "key_epoch", "key_domain", "key_owner_id"], ["key_epochs.key_id", "key_epochs.epoch", "key_epochs.domain", "key_epochs.owner_id"]),
        sa.CheckConstraint("(kind='email' AND provider='email' AND identity_id IS NULL) OR (kind='local_username' AND provider='local' AND identity_id IS NOT NULL) OR (kind='google_subject' AND provider='google' AND identity_id IS NOT NULL)", name="ck_m4_blind_indexes_6"),
    )


class SecurityTombstone(Base):
    __table__ = sa.Table(
        "security_tombstones", Base.metadata,
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.CheckConstraint("target_kind IN ('room','user','record')", name="ck_m4_security_tombstones_1"),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("deletion_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("deletion_epoch >= 1", name="ck_m4_security_tombstones_2"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_security_tombstones_3"),
        sa.PrimaryKeyConstraint("domain", "owner_id", "target_kind", "target_id"),
        sa.ForeignKeyConstraint(["domain", "owner_id"], ["security_scopes.domain", "security_scopes.owner_id"]),
        sa.CheckConstraint("(target_kind='user' AND domain='user' AND target_id=owner_id) OR (target_kind='room' AND domain='room' AND target_id=owner_id) OR (target_kind='record' AND domain='room')", name="ck_m4_security_tombstones_4"),
    )


class SecureDeletionInventory(Base):
    __table__ = sa.Table(
        "secure_deletion_inventory", Base.metadata,
        sa.Column("id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Text(), nullable=False),
        sa.Column("deletion_epoch", sa.Integer(), nullable=False),
        sa.CheckConstraint("deletion_epoch >= 1", name="ck_m4_secure_deletion_inventory_1"),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_m4_secure_deletion_inventory_2"),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=True),
        sa.Column("object_version", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Text(), nullable=True),
        sa.Column("key_epoch", sa.Integer(), nullable=True),
        sa.Column("adapter", sa.Text(), nullable=True),
        sa.CheckConstraint("adapter IN ('local','gcs')", name="ck_m4_secure_deletion_inventory_3"),
        sa.Column("storage_name", sa.Text(), nullable=True),
        sa.Column("exact_version", sa.Text(), nullable=True),
        sa.Column("ciphertext_size", sa.Integer(), nullable=True),
        sa.Column("ciphertext_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("inventory_revision", sa.Text(), nullable=False),
        sa.Column("logical_state", sa.Text(), nullable=False),
        sa.CheckConstraint("logical_state='LOGICALLY_REVOKED'", name="ck_m4_secure_deletion_inventory_4"),
        sa.Column("key_state", sa.Text(), nullable=False),
        sa.CheckConstraint("key_state IN ('PENDING','KEY_ACCESS_REVOKED','KEY_DESTROYED_LOCAL')", name="ck_m4_secure_deletion_inventory_5"),
        sa.Column("physical_state", sa.Text(), nullable=False),
        sa.CheckConstraint("physical_state IN ('PHYSICAL_CLEANUP_PENDING','DELETED','ABSENT','BLOCKED')", name="ck_m4_secure_deletion_inventory_6"),
        sa.Column("retention_state", sa.Text(), nullable=False),
        sa.CheckConstraint("retention_state IN ('UNKNOWN','NONE_KNOWN','RETENTION_BLOCKED','CLEARED')", name="ck_m4_secure_deletion_inventory_7"),
        sa.Column("backup_state", sa.Text(), nullable=False),
        sa.CheckConstraint("backup_state IN ('UNKNOWN','PRESENT','REMOVED')", name="ck_m4_secure_deletion_inventory_8"),
        sa.Column("crypto_state", sa.Text(), nullable=False),
        sa.CheckConstraint("crypto_state IN ('CRYPTO_ERASURE_NOT_ESTABLISHED','CRYPTO_ERASURE_ESTABLISHED_WITHIN_DECLARED_SCOPE')", name="ck_m4_secure_deletion_inventory_9"),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["domain", "owner_id"], ["security_scopes.domain", "security_scopes.owner_id"]),
        sa.ForeignKeyConstraint(["domain", "owner_id", "target_kind", "target_id"], ["security_tombstones.domain", "security_tombstones.owner_id", "security_tombstones.target_kind", "security_tombstones.target_id"]),
        sa.CheckConstraint("(object_id IS NULL AND object_version IS NULL) OR (object_id IS NOT NULL AND object_version IS NOT NULL AND object_version >= 1)", name="ck_m4_secure_deletion_inventory_10"),
        sa.CheckConstraint("(key_id IS NULL AND key_epoch IS NULL) OR (key_id IS NOT NULL AND key_epoch IS NOT NULL AND key_epoch >= 1)", name="ck_m4_secure_deletion_inventory_11"),
        sa.CheckConstraint("(adapter IS NULL AND storage_name IS NULL AND exact_version IS NULL AND ciphertext_size IS NULL AND ciphertext_sha256 IS NULL) OR (adapter IS NOT NULL AND storage_name IS NOT NULL AND exact_version IS NOT NULL AND ciphertext_size IS NOT NULL AND ciphertext_sha256 IS NOT NULL AND ciphertext_size BETWEEN 1 AND 33554432 AND length(ciphertext_sha256)=32)", name="ck_m4_secure_deletion_inventory_12"),
    )


sa.Index("uq_key_active_owner", Base.metadata.tables["key_epochs"].c.domain, Base.metadata.tables["key_epochs"].c.owner_id, unique=True,
         sqlite_where=sa.text("state = 'ACTIVE'"), postgresql_where=sa.text("state = 'ACTIVE'"))
sa.Index("uq_secure_payload_claim", Base.metadata.tables["secure_operations"].c.owner_type, Base.metadata.tables["secure_operations"].c.owner_id, unique=True,
         sqlite_where=sa.text("kind='PAYLOAD' AND state IN ('CLAIMED','RECEIPTED','VERIFIED')"), postgresql_where=sa.text("kind='PAYLOAD' AND state IN ('CLAIMED','RECEIPTED','VERIFIED')"))
sa.Index("uq_secure_activation_target", Base.metadata.tables["secure_operations"].c.kind, Base.metadata.tables["secure_operations"].c.owner_id, unique=True,
         sqlite_where=sa.text("kind IN ('SIGNUP','ROOM_CREATE')"), postgresql_where=sa.text("kind IN ('SIGNUP','ROOM_CREATE')"))
