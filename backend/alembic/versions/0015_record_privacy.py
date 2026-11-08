import sqlalchemy as sa
from alembic import op

revision = "0015_record_privacy"
down_revision = "0014_native_text"
branch_labels = depends_on = None

TABLES = (
    "records", "record_grants", "record_people", "record_restrictions",
    "record_restriction_grants", "record_audits", "record_metadata_revisions",
    "record_deletion_requests", "record_idempotency",
)


class RecordPrivacyDowngradeError(RuntimeError):
    def __str__(self) -> str:
        return "0015 downgrade refused: record policy or history would be lost"


def schema_metadata() -> sa.MetaData:
    m = sa.MetaData()
    for name in ("uploads", "family_rooms", "family_members"):
        sa.Table(name, m, sa.Column("id", sa.String(), primary_key=True))
    sa.Table("records", m,
        sa.Column("id", sa.String(), sa.ForeignKey("uploads.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("room_id", sa.String(), sa.ForeignKey("family_rooms.id"), nullable=False, index=True),
        sa.Column("owner_member_id", sa.String(), sa.ForeignKey("family_members.id")),
        sa.Column("uploader_member_id", sa.String(), sa.ForeignKey("family_members.id")),
        sa.Column("visibility", sa.String(), nullable=False, server_default="PRIVATE"),
        sa.Column("lifecycle_state", sa.String(), nullable=False, server_default="ACTIVE"),
        sa.Column("previous_state", sa.String()),
        sa.Column("review_status", sa.String(), nullable=False, server_default="PENDING_REVIEW"),
        sa.Column("record_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metadata_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("soft_deleted_at", sa.DateTime(timezone=True)),
        sa.Column("purge_approved_at", sa.DateTime(timezone=True)),
        sa.Column("purge_status", sa.String()),
        sa.Column("metadata_json", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("visibility IN ('PRIVATE','FAMILY','CUSTOM')", name="ck_record_visibility"),
        sa.CheckConstraint("lifecycle_state IN ('ACTIVE','RESTRICTED','HIDDEN','ARCHIVED','DELETION_REQUESTED','SOFT_DELETED','PURGED')", name="ck_record_lifecycle"),
        sa.CheckConstraint("review_status IN ('PREDICTED','PENDING_REVIEW','APPROVED')", name="ck_record_review"),
        sa.CheckConstraint("record_version >= 1 AND metadata_revision >= 0 AND source_revision >= 1 AND authorization_epoch >= 0", name="ck_record_versions"))
    sa.Table("record_grants", m,
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("member_id", sa.String(), sa.ForeignKey("family_members.id"), primary_key=True))
    sa.Table("record_people", m,
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("member_id", sa.String(), sa.ForeignKey("family_members.id"), primary_key=True),
        sa.Column("role", sa.String(), primary_key=True),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("role IN ('SUBJECT','PARTICIPANT','PERSPECTIVE')", name="ck_record_person_role"))
    sa.Table("record_restrictions", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("member_id", sa.String(), sa.ForeignKey("family_members.id"), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()))
    sa.Table("record_restriction_grants", m,
        sa.Column("restriction_id", sa.String(), sa.ForeignKey("record_restrictions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("member_id", sa.String(), sa.ForeignKey("family_members.id"), primary_key=True))
    sa.Table("record_audits", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), nullable=False, index=True),
        sa.Column("actor_user_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("before_version", sa.Integer(), nullable=False),
        sa.Column("after_version", sa.Integer(), nullable=False),
        sa.Column("reason_digest", sa.String()),
        sa.Column("request_id", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    sa.Table("record_metadata_revisions", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id"), nullable=False, index=True),
        sa.Column("actor_member_id", sa.String(), sa.ForeignKey("family_members.id"), nullable=False),
        sa.Column("metadata_revision", sa.Integer(), nullable=False),
        sa.Column("resulting_record_version", sa.Integer(), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("reason_digest", sa.String(), nullable=False),
        sa.Column("source_affecting", sa.Boolean(), nullable=False),
        sa.Column("object_id", sa.String()),
        sa.Column("object_version", sa.Integer()),
        sa.Column("legacy_detail", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    sa.Table("record_deletion_requests", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), sa.ForeignKey("records.id"), nullable=False, index=True),
        sa.Column("requester_member_id", sa.String(), sa.ForeignKey("family_members.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="OPEN"),
        sa.Column("reason_digest", sa.String(), nullable=False),
        sa.Column("resolution", sa.String()),
        sa.Column("previous_state", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('OPEN','ACCEPTED','DECLINED','WITHDRAWN','CLOSED')", name="ck_record_deletion_status"))
    sa.Table("record_idempotency", m,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("principal_id", sa.String(), nullable=False),
        sa.Column("route", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("payload_hash", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.UniqueConstraint("principal_id", "route", "scope", "key", name="uq_record_idempotency"))
    return m


def trigger_statements() -> list[str]:
    statements = [f"CREATE TRIGGER record_audits_no_{action.lower()} BEFORE {action} ON record_audits BEGIN SELECT RAISE(ABORT,'immutable record audit'); END" for action in ("UPDATE", "DELETE")]
    for table, column in (("records", "metadata_json"), ("record_metadata_revisions", "legacy_detail")):
        for action in ("INSERT", "UPDATE"):
            statements.append(f"CREATE TRIGGER m2_{table}_{action.lower()} BEFORE {action} ON {table} WHEN NEW.{column} IS NOT NULL AND COALESCE((SELECT mode FROM security_control WHERE id=1),'UNKNOWN') <> 'LEGACY' BEGIN SELECT RAISE(ABORT,'record policy rejects plaintext writer'); END")
    statements.append("CREATE TRIGGER m2_plaintext_cutover BEFORE UPDATE ON security_control WHEN NEW.mode <> 'LEGACY' AND (EXISTS (SELECT 1 FROM records WHERE metadata_json IS NOT NULL) OR EXISTS (SELECT 1 FROM record_metadata_revisions WHERE legacy_detail IS NOT NULL)) BEGIN SELECT RAISE(ABORT,'record policy plaintext requires migration'); END")
    return statements


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("family_rooms", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("family_members", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    metadata = schema_metadata()
    for name in TABLES:
        metadata.tables[name].create(bind)
    bind.execute(sa.text(
        "INSERT INTO records (id,room_id,owner_member_id,uploader_member_id,visibility,review_status) "
        "SELECT u.id,u.room_id,m.id,m.id,'FAMILY','APPROVED' FROM uploads u "
        "LEFT JOIN family_members m ON m.room_id=u.room_id AND m.user_id=u.uploader_user_id"
    ))
    if bind.dialect.name == "sqlite":
        for statement in trigger_statements():
            op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if any(bind.execute(sa.text(f"SELECT 1 FROM {name} LIMIT 1")).first() for name in TABLES):
        raise RecordPrivacyDowngradeError()
    if bind.dialect.name == "sqlite":
        for statement in trigger_statements():
            op.execute("DROP TRIGGER " + statement.split()[2])
    for name in reversed(TABLES):
        op.drop_table(name)
    op.drop_column("family_members", "is_active")
    op.drop_column("family_rooms", "is_active")
