"""LEGACY-only photo albums and resumable photo operations.

Revision ID: 0006_photo_albums
Revises: 0005_secure_catalogue

The frozen schema intentionally does not import live ORM definitions. Existing
SECURE catalogues stay at 0005; a later encrypted album design needs its own
reviewed cutover path. No original records or files are changed here.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_photo_albums"
down_revision = "0005_secure_catalogue"
branch_labels = None
depends_on = None

ALBUM_TABLES = ("albums", "album_photos", "photo_upload_batches", "photo_upload_items")


def schema_metadata():
    metadata = sa.MetaData()
    # Referenced parent columns are placeholders, never created by this revision.
    sa.Table("users", metadata, sa.Column("id", sa.String(), primary_key=True))
    sa.Table("family_rooms", metadata, sa.Column("id", sa.String(), primary_key=True))
    sa.Table("uploads", metadata, sa.Column("id", sa.String(), primary_key=True),
             sa.Column("room_id", sa.String(), nullable=False))
    sa.Table("upload_files", metadata, sa.Column("id", sa.String(), primary_key=True),
             sa.Column("upload_id", sa.String(), nullable=False))
    sa.Table(
        "albums", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), sa.ForeignKey("family_rooms.id"), nullable=False),
        sa.Column("parent_album_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("cover_upload_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("request_hash", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("id", "room_id", name="uq_albums_id_room_id"),
        sa.UniqueConstraint("room_id", "created_by", "request_id", name="uq_albums_request"),
        sa.ForeignKeyConstraint(["parent_album_id", "room_id"], ["albums.id", "albums.room_id"], name="fk_albums_parent_room"),
        sa.ForeignKeyConstraint(["cover_upload_id", "room_id"], ["uploads.id", "uploads.room_id"], name="fk_albums_cover_room"),
        sa.CheckConstraint("version > 0", name="ck_albums_version"),
        sa.CheckConstraint("sort_order >= 0", name="ck_albums_sort_order"),
        sa.CheckConstraint("parent_album_id IS NULL OR parent_album_id <> id", name="ck_albums_not_own_parent"),
        sa.CheckConstraint("(request_id IS NULL AND request_hash IS NULL) OR (request_id IS NOT NULL AND request_hash IS NOT NULL)", name="ck_albums_request_pair"),
        sa.Index("ix_albums_room_parent_order", "room_id", "parent_album_id", "sort_order", "id"),
    )
    sa.Table(
        "album_photos", metadata,
        sa.Column("album_id", sa.String(), primary_key=True),
        sa.Column("upload_id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["album_id", "room_id"], ["albums.id", "albums.room_id"], name="fk_album_photos_album_room"),
        sa.ForeignKeyConstraint(["upload_id", "room_id"], ["uploads.id", "uploads.room_id"], name="fk_album_photos_upload_room"),
        sa.CheckConstraint("sort_order >= 0", name="ck_album_photos_sort_order"),
        sa.Index("ix_album_photos_album_order", "album_id", "sort_order", "upload_id"),
        sa.Index("ix_album_photos_upload", "upload_id"),
    )
    sa.Table(
        "photo_upload_batches", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), sa.ForeignKey("family_rooms.id"), nullable=False),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("album_id", sa.String(), nullable=True),
        sa.Column("target_removed", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("room_id", "created_by", "request_id", name="uq_photo_batch_request"),
        sa.ForeignKeyConstraint(["album_id", "room_id"], ["albums.id", "albums.room_id"], name="fk_photo_batch_album_room"),
        sa.Index("ix_photo_upload_batches_room_id", "room_id"),
    )
    sa.Table(
        "photo_upload_items", metadata,
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("photo_upload_batches.id"), nullable=False),
        sa.Column("client_item_id", sa.String(64), nullable=False),
        sa.Column("upload_id", sa.String(), sa.ForeignKey("uploads.id"), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("actual_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_id", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("batch_id", "client_item_id", name="uq_photo_batch_item"),
        sa.CheckConstraint("state IN ('pending','uploading','uploaded','complete','failed','deleted')", name="ck_photo_item_state"),
        sa.CheckConstraint("actual_size >= 0", name="ck_photo_item_size"),
        sa.Index("ix_photo_upload_items_batch_id", "batch_id"),
    )
    return metadata


def trigger_statements():
    statements = [
        "CREATE TRIGGER album_legacy_cutover_fence BEFORE UPDATE ON security_control "
        "WHEN NEW.mode <> 'LEGACY' BEGIN SELECT RAISE(ABORT,'album catalogue requires LEGACY'); END"
    ]
    for table in ALBUM_TABLES:
        for event in ("INSERT", "UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER album_{table}_{event.lower()}_fence BEFORE {event} ON {table} "
                "WHEN COALESCE((SELECT mode FROM security_control WHERE id=1),'UNKNOWN') <> 'LEGACY' "
                "BEGIN SELECT RAISE(ABORT,'album catalogue requires LEGACY'); END"
            )
    return statements


def _legacy_preflight(bind):
    from scripts.adopt_alembic import verify_migration_boundary, _secure_migration

    if bind.dialect.name not in ("sqlite", "postgresql"):
        raise RuntimeError("0006 refused: unsupported database dialect")
    if verify_migration_boundary(bind, "0005_secure_catalogue"):
        raise RuntimeError("0006 refused: unknown or drifted 0005 catalogue")
    control_query = "SELECT mode,admission,runtime_generation FROM security_control"
    if bind.dialect.name == "postgresql":
        control_query += " FOR UPDATE"
    rows = bind.execute(sa.text(control_query)).all()
    if rows != [("LEGACY", "CLOSED", None)]:
        raise RuntimeError("0006 refused: catalogue is not closed LEGACY")
    for table in _secure_migration().SECURE_TABLES:
        if table != "security_control" and bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError("0006 refused: secure catalogue contains lifecycle data")
    if bind.execute(sa.text("SELECT 1 FROM auth_identities WHERE payload_state <> 'LEGACY' LIMIT 1")).first():
        raise RuntimeError("0006 refused: encrypted identities present")


def upgrade():
    bind = op.get_bind()
    _legacy_preflight(bind)
    metadata = schema_metadata()
    for name in ALBUM_TABLES:
        metadata.tables[name].create(bind)
    if bind.dialect.name == "sqlite":
        for statement in trigger_statements():
            op.execute(statement)


def downgrade():
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT mode,admission,runtime_generation FROM security_control")).all()
    if rows != [("LEGACY", "CLOSED", None)]:
        raise RuntimeError("0006 downgrade refused: non-LEGACY catalogue")
    # Dropping a populated classification or resume operation loses user state.
    if any(bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() for table in ALBUM_TABLES):
        raise RuntimeError("0006 downgrade refused: album tables are not empty")
    if bind.dialect.name == "sqlite":
        for statement in trigger_statements():
            op.execute("DROP TRIGGER " + statement.split()[2])
    for name in reversed(ALBUM_TABLES):
        op.drop_table(name)
