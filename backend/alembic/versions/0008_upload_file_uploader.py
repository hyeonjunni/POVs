"""Persist the authenticated byte uploader for LEGACY uploads.

Revision ID: 0008_upload_file_uploader
Revises: 0006_family_graph
"""
from alembic import op
import sqlalchemy as sa


revision = "0008_upload_file_uploader"
down_revision = "0006_family_graph"
branch_labels = None
depends_on = None


def _sqlite_triggers():
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return []
    return connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='upload_files' ORDER BY name"
    ).scalars().all()


def upgrade():
    triggers = _sqlite_triggers()
    with op.batch_alter_table("upload_files") as batch_op:
        batch_op.add_column(sa.Column("file_uploader_user_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_upload_files_file_uploader_user_id_users",
            "users",
            ["file_uploader_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    for sql in triggers:
        op.get_bind().exec_driver_sql(sql)


def downgrade():
    files = sa.table("upload_files", sa.column("file_uploader_user_id", sa.String()))
    if op.get_bind().execute(sa.select(files.c.file_uploader_user_id).where(
        files.c.file_uploader_user_id.is_not(None)).limit(1)).first() is not None:
        raise RuntimeError("refusing downgrade: actual file uploader attribution would be lost")
    triggers = _sqlite_triggers()
    with op.batch_alter_table("upload_files") as batch_op:
        batch_op.drop_constraint(
            "fk_upload_files_file_uploader_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("file_uploader_user_id")

    for sql in triggers:
        op.get_bind().exec_driver_sql(sql)
