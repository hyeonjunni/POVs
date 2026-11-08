"""Forward secure catalogue expansion; no encryption or startup migration.

Revision ID: 0005_secure_catalogue
Revises: 0004_retrieval
"""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "0005_secure_catalogue"
down_revision = "0004_retrieval"
branch_labels = None
depends_on = None

NULLABLE_COLUMNS = {
    "users": ("email", "name"),
    "auth_identities": ("provider_subject",),
    "family_rooms": ("name", "invite_code"),
    "uploads": ("title",),
    "upload_files": ("storage_bucket", "storage_path", "mime_type"),
}
AUTH_PAYLOAD_CHECK = "((payload_state='LEGACY' AND ((provider='local')=(credential_hash IS NOT NULL))) OR (payload_state='ENCRYPTED' AND provider_subject IS NULL AND provider_email IS NULL AND credential_hash IS NULL))"
SECURE_TABLES = ["security_control","security_scopes","key_epochs","credential_generations","credential_generation_keys","encrypted_objects","protected_pointers","secure_operations","blind_indexes","security_tombstones","secure_deletion_inventory"]

PLAIN_FIELDS = {
    "users": "NEW.email IS NOT NULL OR NEW.name IS NOT NULL OR NEW.username IS NOT NULL OR NEW.password_hash IS NOT NULL OR NEW.google_sub IS NOT NULL OR NEW.profile_image IS NOT NULL OR NEW.age IS NOT NULL OR NEW.gender IS NOT NULL OR NEW.phone IS NOT NULL OR NEW.profile_chunk IS NOT NULL",
    "auth_identities": "NEW.payload_state != 'ENCRYPTED' OR NEW.provider_subject IS NOT NULL OR NEW.provider_email IS NOT NULL OR NEW.credential_hash IS NOT NULL",
    "family_rooms": "NEW.name IS NOT NULL OR NEW.invite_code IS NOT NULL",
    "family_members": "NEW.relation_to_related_user IS NOT NULL",
    "uploads": "NEW.title IS NOT NULL OR NEW.description IS NOT NULL",
    "upload_files": "NEW.encrypted != 1 OR NEW.storage_bucket IS NOT NULL OR NEW.storage_path IS NOT NULL OR NEW.mime_type IS NOT NULL",
    "processing_jobs": "NEW.result_json IS NOT NULL OR NEW.failure_message IS NOT NULL",
    "record_chunks": "1", "record_embeddings": "1",
}
NONLEGACY = "COALESCE((SELECT mode FROM security_control WHERE id=1),'QUARANTINED') IN ('CUTOVER','SECURE','QUARANTINED')"


def trigger_statements():
    """Frozen, enumerated SQLite fences; no caller-controlled SQL identifiers."""
    statements = []

    def reject(name, event, table, condition, message="secure catalogue rejects invalid mutation"):
        statements.append(f"CREATE TRIGGER {name} BEFORE {event} ON {table} WHEN ({condition}) BEGIN SELECT RAISE(ABORT,'{message}'); END")

    def changed(columns):
        return " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns.split())

    for table, condition in PLAIN_FIELDS.items():
        for event in ("INSERT", "UPDATE"):
            reject(f"m4_{table}_plain_{event.lower()}", event, table,
                   f"{NONLEGACY} AND ({condition})", "secure catalogue rejects plaintext writer")

    reject("m4_control_insert", "INSERT", "security_control",
           "NEW.id != 1 OR NEW.mode != 'LEGACY' OR NEW.admission != 'CLOSED' OR NEW.revision != 1 OR NEW.runtime_generation IS NOT NULL")
    reject("m4_control_delete", "DELETE", "security_control", "1")
    reject("m4_control_transition", "UPDATE", "security_control",
           changed("id catalogue_id schema_version") + " OR NEW.revision != OLD.revision+1 OR NOT ("
           "(NEW.mode=OLD.mode AND (OLD.admission=NEW.admission OR NEW.admission='CLOSED' OR (NEW.mode='SECURE' AND OLD.admission='CLOSED')))"
           " OR (OLD.admission='CLOSED' AND NEW.admission='CLOSED' AND ((OLD.mode='LEGACY' AND NEW.mode='CUTOVER')"
           " OR (OLD.mode='CUTOVER' AND NEW.mode='SECURE') OR (OLD.mode='QUARANTINED' AND NEW.mode='CUTOVER')))"
           " OR (NEW.mode='QUARANTINED' AND NEW.admission='CLOSED'))"
           " OR (NEW.runtime_generation IS NOT OLD.runtime_generation AND (OLD.admission!='CLOSED' OR NEW.admission!='CLOSED'))")
    for event in ("INSERT", "UPDATE"):
        # An unchanged pointer may name a drained generation while CLOSED.
        runtime_changed = "1" if event == "INSERT" else "NEW.runtime_generation IS NOT OLD.runtime_generation"
        reject("m4_control_runtime_binding" + ("_insert" if event == "INSERT" else ""), event, "security_control",
               f"(NEW.admission='OPEN' OR ({runtime_changed})) AND NEW.runtime_generation IS NOT NULL AND NOT EXISTS ("
               "SELECT 1 FROM credential_generations g WHERE g.generation_id=NEW.runtime_generation "
               "AND g.catalogue_id=NEW.catalogue_id AND g.state='VERIFIED')")

    reject("m4_generation_insert", "INSERT", "credential_generations",
           "NEW.state!='REQUESTED' OR NEW.revision!=1 OR NEW.invocation_id IS NOT NULL OR NEW.boot_id IS NOT NULL OR NOT EXISTS ("
           "SELECT 1 FROM security_control c WHERE c.id=1 AND c.catalogue_id=NEW.catalogue_id AND c.admission='CLOSED' AND c.revision=NEW.control_revision)")
    reject("m4_generation_transition", "UPDATE", "credential_generations",
           changed("generation_id catalogue_id inventory_revision unit_name approved_uid control_revision") +
           " OR NEW.revision!=OLD.revision+1 OR NOT ((OLD.state='REQUESTED' AND NEW.state IN ('VERIFIED','FAILED'))"
           " OR (OLD.state='VERIFIED' AND NEW.state IN ('DRAINING','FAILED')) OR (OLD.state='DRAINING' AND NEW.state IN ('DRAINED','FAILED')))"
           " OR (NOT (OLD.state='REQUESTED' AND NEW.state='VERIFIED') AND (" + changed("invocation_id boot_id") + "))"
           " OR (NEW.state='VERIFIED' AND (NEW.invocation_id IS NULL OR NEW.boot_id IS NULL OR NOT EXISTS ("
           "SELECT 1 FROM security_control c WHERE c.id=1 AND c.catalogue_id=NEW.catalogue_id AND c.admission='CLOSED' AND c.revision=NEW.control_revision)"
           " OR EXISTS (SELECT 1 FROM credential_generation_keys g JOIN key_epochs k ON k.key_id=g.key_id AND k.epoch=g.key_epoch"
           " JOIN security_scopes s ON s.domain=k.domain AND s.owner_id=k.owner_id WHERE g.generation_id=NEW.generation_id"
           " AND (k.state IN ('REVOCATION_PENDING','DESTROYED_LOCAL') OR k.provisioning_state!='PERSISTED' OR s.state!='ACTIVE'))))")
    reject("m4_generation_current_fence", "UPDATE", "credential_generations",
           "NEW.state!='VERIFIED' AND EXISTS (SELECT 1 FROM security_control WHERE id=1 AND admission='OPEN' AND runtime_generation=OLD.generation_id)")
    reject("m4_generation_history", "DELETE", "credential_generations", "1")
    reject("m4_generation_keys_frozen_insert", "INSERT", "credential_generation_keys",
           "NOT EXISTS (SELECT 1 FROM credential_generations WHERE generation_id=NEW.generation_id AND state='REQUESTED')")
    for event in ("UPDATE", "DELETE"):
        reject("m4_generation_keys_frozen" + ("_delete" if event == "DELETE" else ""), event, "credential_generation_keys", "1")

    reject("m4_operation_insert", "INSERT", "secure_operations",
           "NEW.revision!=1 OR NOT ((NEW.kind='PAYLOAD' AND NEW.state='CLAIMED' AND NEW.receipt_json IS NULL)"
           " OR (NEW.kind IN ('SIGNUP','ROOM_CREATE') AND NEW.state='WAITING_CREDENTIAL' AND NEW.runtime_generation IS NULL)) OR NEW.failure_code IS NOT NULL")
    operation_identity = "operation_id kind parent_operation_id principal_id owner_type owner_id domain scope_id control_revision pointer_revision source_revision deletion_epoch authorization_epoch candidate_object_id activation_key_id activation_key_epoch continuation_digest created_at expires_at snapshot_json"
    reject("m4_operation_transition", "UPDATE", "secure_operations",
           changed(operation_identity) + " OR NEW.revision!=OLD.revision+1 OR NOT ("
           "(OLD.kind='PAYLOAD' AND ((OLD.state='CLAIMED' AND NEW.state IN ('RECEIPTED','ABANDONED','ORPHAN_UNRESOLVED'))"
           " OR (OLD.state='RECEIPTED' AND NEW.state IN ('VERIFIED','ABANDONED')) OR (OLD.state='VERIFIED' AND NEW.state IN ('COMMITTED','ABANDONED'))))"
           " OR (OLD.kind IN ('SIGNUP','ROOM_CREATE') AND ((OLD.state='WAITING_CREDENTIAL' AND NEW.state IN ('READY','EXPIRED','FAILED'))"
           " OR (OLD.state='READY' AND NEW.state IN ('WAITING_CREDENTIAL','COMMITTED','EXPIRED','FAILED')))))"
           " OR (NEW.kind='PAYLOAD' AND NEW.runtime_generation IS NOT OLD.runtime_generation)"
           " OR (NEW.receipt_json IS NOT OLD.receipt_json AND NOT (OLD.kind='PAYLOAD' AND OLD.state='CLAIMED' AND NEW.state='RECEIPTED'))"
           " OR (NEW.failure_code IS NOT OLD.failure_code AND NEW.state NOT IN ('ABANDONED','ORPHAN_UNRESOLVED','EXPIRED','FAILED'))"
           " OR (NEW.kind!='PAYLOAD' AND NEW.state='WAITING_CREDENTIAL' AND NEW.runtime_generation IS NOT NULL)"
           " OR (NEW.kind!='PAYLOAD' AND NEW.state IN ('COMMITTED','EXPIRED','FAILED') AND NEW.runtime_generation IS NOT OLD.runtime_generation)")
    reject("m4_operation_history", "DELETE", "secure_operations", "1")
    for event in ("INSERT", "UPDATE"):
        suffix = "_insert" if event == "INSERT" else ""
        reject("m4_operation_activation_binding" + suffix, event, "secure_operations",
               "(NEW.kind IN ('SIGNUP','ROOM_CREATE') AND NEW.state='READY' AND NOT EXISTS ("
               "SELECT 1 FROM credential_generation_keys m JOIN credential_generations g ON g.generation_id=m.generation_id"
               " JOIN security_control c ON c.runtime_generation=g.generation_id AND c.catalogue_id=g.catalogue_id"
               " JOIN key_epochs k ON k.key_id=m.key_id AND k.epoch=m.key_epoch AND k.domain=m.domain AND k.owner_id=m.owner_id"
               " WHERE c.id=1 AND g.state='VERIFIED' AND g.generation_id=NEW.runtime_generation AND m.key_id=NEW.activation_key_id"
               " AND m.key_epoch=NEW.activation_key_epoch AND m.domain=NEW.domain AND m.owner_id=NEW.scope_id"
               " AND k.state='PREPARED' AND k.provisioning_state='PERSISTED'))"
               " OR (NEW.kind='PAYLOAD' AND NEW.state IN ('CLAIMED','RECEIPTED','VERIFIED','COMMITTED') AND NEW.parent_operation_id IS NOT NULL AND NOT EXISTS ("
               "SELECT 1 FROM secure_operations p JOIN credential_generation_keys m ON m.generation_id=p.runtime_generation"
               " WHERE p.operation_id=NEW.parent_operation_id AND p.kind IN ('SIGNUP','ROOM_CREATE') AND p.state='READY'"
               " AND p.domain=NEW.domain AND p.scope_id=NEW.scope_id AND p.runtime_generation=NEW.runtime_generation"
               " AND m.key_id=p.activation_key_id AND m.key_epoch=p.activation_key_epoch AND m.domain=p.domain AND m.owner_id=p.scope_id"
               " AND ((p.kind='SIGNUP' AND NEW.principal_id=p.owner_id) OR (p.kind='ROOM_CREATE' AND NEW.principal_id=p.principal_id))))"
               " OR (NEW.kind='PAYLOAD' AND NEW.state IN ('CLAIMED','RECEIPTED','VERIFIED','COMMITTED') AND NEW.parent_operation_id IS NULL AND NOT EXISTS ("
               "SELECT 1 FROM users u JOIN auth_identities a ON a.user_id=u.id WHERE u.id=NEW.principal_id))"
               " OR (NEW.kind='ROOM_CREATE' AND NEW.state IN ('WAITING_CREDENTIAL','READY','COMMITTED') AND NOT EXISTS (SELECT 1 FROM users u JOIN auth_identities a ON a.user_id=u.id WHERE u.id=NEW.principal_id))"
               " OR (NEW.kind IN ('SIGNUP','ROOM_CREATE') AND NEW.state='COMMITTED' AND ("
               "NOT EXISTS (SELECT 1 FROM key_epochs k JOIN credential_generation_keys m ON m.key_id=k.key_id AND m.key_epoch=k.epoch"
               " JOIN credential_generations g ON g.generation_id=m.generation_id JOIN security_control c ON c.runtime_generation=g.generation_id"
               " WHERE k.key_id=NEW.activation_key_id AND k.epoch=NEW.activation_key_epoch AND k.domain=NEW.domain AND k.owner_id=NEW.scope_id"
               " AND k.state='ACTIVE' AND k.provisioning_state='PERSISTED' AND m.generation_id=NEW.runtime_generation AND g.state='VERIFIED' AND c.id=1)"
               " OR NOT EXISTS (SELECT 1 FROM protected_pointers p JOIN secure_operations child ON child.operation_id=p.operation_id"
               " WHERE p.owner_type=NEW.owner_type AND p.owner_id=NEW.owner_id AND p.domain=NEW.domain AND p.scope_id=NEW.scope_id"
               " AND p.state='ENCRYPTED_ACTIVE' AND child.parent_operation_id=NEW.operation_id AND child.state='COMMITTED')"
               " OR EXISTS (SELECT 1 FROM secure_operations child WHERE child.parent_operation_id=NEW.operation_id AND child.state NOT IN ('COMMITTED','ABANDONED'))"
               " OR (NEW.kind='SIGNUP' AND NOT EXISTS (SELECT 1 FROM users u JOIN auth_identities a ON a.user_id=u.id WHERE u.id=NEW.owner_id))"
               " OR (NEW.kind='ROOM_CREATE' AND NOT EXISTS (SELECT 1 FROM family_rooms r WHERE r.id=NEW.owner_id AND r.owner_user_id=NEW.principal_id))))")
        reject("m4_operation_receipt_binding" + suffix, event, "secure_operations",
               "NEW.kind='PAYLOAD' AND NEW.state IN ('RECEIPTED','VERIFIED','COMMITTED') AND NOT EXISTS ("
               "SELECT 1 FROM encrypted_objects e WHERE e.object_id=NEW.candidate_object_id AND e.object_version=1"
               " AND e.domain=NEW.domain AND e.owner_id=NEW.scope_id"
               " AND ((NEW.state IN ('RECEIPTED','VERIFIED') AND e.state='STAGED') OR (NEW.state='COMMITTED' AND e.state='ACTIVE'))"
               " AND (NEW.parent_operation_id IS NULL OR EXISTS (SELECT 1 FROM secure_operations p WHERE p.operation_id=NEW.parent_operation_id"
               " AND p.activation_key_id=e.key_id AND p.activation_key_epoch=e.key_epoch)))"
               " OR (NEW.kind='PAYLOAD' AND NEW.state='COMMITTED' AND NOT EXISTS (SELECT 1 FROM protected_pointers p"
               " WHERE p.owner_type=NEW.owner_type AND p.owner_id=NEW.owner_id AND p.domain=NEW.domain AND p.scope_id=NEW.scope_id"
               " AND p.object_id=NEW.candidate_object_id AND p.object_version=1 AND p.state='ENCRYPTED_ACTIVE'"
               " AND p.operation_id=NEW.operation_id AND p.revision=NEW.pointer_revision+1 AND p.source_revision=NEW.source_revision))")

    # Source parent IDs remain available in BEFORE DELETE, including cascades.
    owners = {
        "users": ("user", "OLD.id", "user", "OLD.id", "user", "OLD.id"),
        "auth_identities": ("user", "OLD.user_id", "user", "OLD.user_id", "auth_identity", "OLD.id"),
        "family_rooms": ("room", "OLD.id", "room", "OLD.id", "room", "OLD.id"),
        "uploads": ("room", "OLD.room_id", "record", "OLD.id", "record", "OLD.id"),
        "upload_files": ("room", "(SELECT room_id FROM uploads WHERE id=OLD.upload_id)", "record", "OLD.upload_id", "file", "OLD.id"),
        "processing_jobs": ("room", "OLD.room_id", "record", "OLD.record_id", "job", "OLD.id"),
        "record_chunks": ("room", "OLD.room_id", "record", "OLD.record_id", "record_index", "OLD.record_id"),
        "record_embeddings": ("room", "(SELECT room_id FROM record_chunks WHERE id=OLD.chunk_id)", "record", "(SELECT record_id FROM record_chunks WHERE id=OLD.chunk_id)", "record_index", "(SELECT record_id FROM record_chunks WHERE id=OLD.chunk_id)"),
    }
    for table, (domain, owner, target_kind, target, pointer_type, pointer_owner) in owners.items():
        tombstone = (f"t.domain='{domain}' AND t.owner_id={owner} AND ((t.target_kind='{target_kind}' AND t.target_id={target})"
                     + (f" OR (t.target_kind='room' AND t.target_id={owner})" if domain == "room" else "") + ")")
        proof = ("EXISTS (SELECT 1 FROM security_tombstones t JOIN security_scopes s ON s.domain=t.domain AND s.owner_id=t.owner_id"
                 " JOIN secure_deletion_inventory i ON i.domain=t.domain AND i.owner_id=t.owner_id AND i.target_kind=t.target_kind"
                 " AND i.target_id=t.target_id AND i.deletion_epoch=t.deletion_epoch"
                 f" WHERE {tombstone} AND s.deletion_epoch>=t.deletion_epoch)"
                 " AND NOT EXISTS (SELECT 1 FROM protected_pointers p JOIN encrypted_objects e ON e.object_id=p.object_id AND e.object_version=p.object_version"
                 f" WHERE p.owner_type='{pointer_type}' AND p.owner_id={pointer_owner} AND NOT EXISTS ("
                 "SELECT 1 FROM secure_deletion_inventory i JOIN security_tombstones t ON t.domain=i.domain AND t.owner_id=i.owner_id"
                 " AND t.target_kind=i.target_kind AND t.target_id=i.target_id AND t.deletion_epoch=i.deletion_epoch"
                 f" WHERE {tombstone} AND i.object_id=e.object_id AND i.object_version=e.object_version AND i.key_id=e.key_id AND i.key_epoch=e.key_epoch"
                 " AND i.adapter=e.adapter AND i.storage_name=e.storage_name AND i.exact_version=e.exact_version"
                 " AND i.ciphertext_size=e.ciphertext_size AND i.ciphertext_sha256=e.ciphertext_sha256))")
        exception = ""
        if table in ("record_chunks", "record_embeddings"):
            exception = f" OR ((SELECT mode FROM security_control WHERE id=1)='CUTOVER' AND m4_mutation_authorized('cutover_cleanup','{table}',OLD.id)=1)"
        reject(f"m4_{table}_delete", "DELETE", table, f"{NONLEGACY} AND NOT (({proof}){exception})")
    reject("m4_family_members_delete", "DELETE", "family_members",
           f"{NONLEGACY} AND NOT (EXISTS (SELECT 1 FROM security_tombstones t JOIN secure_deletion_inventory i ON "
           "i.domain=t.domain AND i.owner_id=t.owner_id AND i.target_kind=t.target_kind AND i.target_id=t.target_id AND i.deletion_epoch=t.deletion_epoch "
           "WHERE (t.domain='room' AND t.owner_id=OLD.room_id AND t.target_kind='room' AND t.target_id=OLD.room_id)"
           " OR (t.domain='user' AND t.owner_id=OLD.user_id AND t.target_kind='user' AND t.target_id=OLD.user_id))"
           " OR m4_mutation_authorized('membership','family_members',OLD.id)=1)")
    return statements


def upgrade():
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError("0005 secure catalogue requires local SQLite; PostgreSQL execution is not approved")
    for table_name, columns in NULLABLE_COLUMNS.items():
        with op.batch_alter_table(table_name) as batch:
            for column in columns:
                batch.alter_column(column, existing_type=sa.String(), nullable=True)
            if table_name == "auth_identities":
                batch.add_column(sa.Column("payload_state", sa.Text(), nullable=False, server_default="LEGACY"))
                batch.drop_constraint("ck_auth_identity_local_requires_hash", type_="check")
                batch.create_check_constraint("ck_auth_identity_local_requires_hash", AUTH_PAYLOAD_CHECK)
                batch.create_check_constraint("ck_auth_identity_payload_state", "payload_state IN ('LEGACY','ENCRYPTED')")
    op.create_table(
        "security_control",
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
    op.create_table(
        "security_scopes",
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
    op.create_table(
        "key_epochs",
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
    op.create_table(
        "credential_generations",
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
    op.create_table(
        "credential_generation_keys",
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
    op.create_table(
        "encrypted_objects",
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
    op.create_table(
        "protected_pointers",
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
    op.create_table(
        "secure_operations",
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
    op.create_table(
        "blind_indexes",
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
    op.create_table(
        "security_tombstones",
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
    op.create_table(
        "secure_deletion_inventory",
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
    op.create_index("uq_key_active_owner", "key_epochs", ["domain","owner_id"], unique=True,
                    sqlite_where=sa.text("state = 'ACTIVE'"))
    op.create_index("uq_secure_payload_claim", "secure_operations", ["owner_type","owner_id"], unique=True,
                    sqlite_where=sa.text("kind='PAYLOAD' AND state IN ('CLAIMED','RECEIPTED','VERIFIED')"))
    op.create_index("uq_secure_activation_target", "secure_operations", ["kind","owner_id"], unique=True,
                    sqlite_where=sa.text("kind IN ('SIGNUP','ROOM_CREATE')"))
    op.get_bind().execute(sa.text("INSERT INTO security_control (id,mode,schema_version,revision,catalogue_id,admission) VALUES (1,'LEGACY',1,1,:catalogue,'CLOSED')"), {"catalogue": str(uuid4())})
    for statement in trigger_statements():
        op.execute(statement)


def downgrade():
    bind = op.get_bind()
    control = bind.execute(sa.text("SELECT mode FROM security_control WHERE id=1")).scalar_one_or_none()
    if control != "LEGACY" or any(bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first() for name in SECURE_TABLES if name != "security_control"):
        raise RuntimeError("0005 downgrade refused: nonempty or non-LEGACY secure catalogue")
    for table_name, columns in NULLABLE_COLUMNS.items():
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} WHERE " + " OR ".join(f"{column} IS NULL" for column in columns) + " LIMIT 1")).first():
            raise RuntimeError("0005 downgrade refused: legacy nonnull requirements are not satisfied")
    if bind.execute(sa.text("SELECT 1 FROM auth_identities WHERE payload_state != 'LEGACY' LIMIT 1")).first():
        raise RuntimeError("0005 downgrade refused: encrypted identity")
    for statement in trigger_statements():
        op.execute("DROP TRIGGER " + statement.split()[2])
    for name in reversed(SECURE_TABLES):
        op.drop_table(name)
    for table_name, columns in NULLABLE_COLUMNS.items():
        with op.batch_alter_table(table_name) as batch:
            for column in columns:
                batch.alter_column(column, existing_type=sa.String(), nullable=False)
            if table_name == "auth_identities":
                batch.drop_constraint("ck_auth_identity_local_requires_hash", type_="check")
                batch.drop_constraint("ck_auth_identity_payload_state", type_="check")
                batch.drop_column("payload_state")
                batch.create_check_constraint("ck_auth_identity_local_requires_hash", "(provider = 'local') = (credential_hash IS NOT NULL)")
