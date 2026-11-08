"""Durable pre-create identities and append-only orphan observations."""
from alembic import op
import sqlalchemy as sa
revision = "0013_secure_reconciliation"
down_revision = "0012_secure_room_storage_quota"
branch_labels = depends_on = None
NEW_TABLES = ("secure_create_bindings", "secure_reconciliation_events")


def schema_metadata():
    m = sa.MetaData()
    sa.Table("secure_create_bindings", m,
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("room_id", sa.String(36), nullable=False),
        sa.Column("object_name", sa.String(36), nullable=False, unique=True),
        sa.Column("storage_binding", sa.Text(), nullable=False),
        sa.Column("ciphertext_size", sa.BigInteger(), nullable=False),
        sa.Column("ciphertext_sha256", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("ciphertext_size > 0 AND length(ciphertext_sha256)=32", name="ck_secure_create_binding_size"))
    sa.Table("secure_reconciliation_events", m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("room_id", sa.String(36), nullable=False),
        sa.Column("generation_id", sa.String(36), nullable=False),
        sa.Column("observed_at", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("storage_binding", sa.Text(), nullable=True),
        sa.CheckConstraint("outcome != 'ABSENT' OR storage_binding IS NOT NULL", name="ck_secure_reconciliation_absence_binding"),
        sa.Column("receipt_json", sa.Text(), nullable=True),
        sa.CheckConstraint("outcome IN ('ABSENT','MATCHED','AMBIGUOUS','MISMATCH')", name="ck_secure_reconciliation_outcome"))
    return m


def trigger_statements():
    return [f"CREATE TRIGGER {table}_{event.lower()} BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'immutable reconciliation evidence'); END"
            for table in ("secure_create_bindings", "secure_reconciliation_events") for event in ("UPDATE", "DELETE")]


def upgrade():
    schema_metadata().create_all(op.get_bind())
    if op.get_bind().dialect.name == "sqlite":
        for sql in trigger_statements():
            op.execute(sql)


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
           for table in ("secure_create_bindings", "secure_reconciliation_events")):
        raise RuntimeError("Retained reconciliation evidence cannot be downgraded")
    schema_metadata().drop_all(bind)
