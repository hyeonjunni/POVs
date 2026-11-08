"""LEGACY video classification and private text receipts. Frozen additive schema."""
from alembic import op
import sqlalchemy as sa

revision = "0007_video_albums_reads"
down_revision = "0006_photo_albums"
branch_labels = None
depends_on = None
NEW_TABLES = ("album_videos", "record_read_receipts")
ALBUM_TABLES = ("albums", "album_photos", "photo_upload_batches", "photo_upload_items") + NEW_TABLES


def schema_metadata():
    from scripts.adopt_alembic import _album_migration
    metadata = _album_migration().schema_metadata()
    albums = metadata.tables["albums"]
    albums.append_column(sa.Column("media_kind", sa.String(), nullable=False, server_default="image"))
    albums.append_constraint(sa.CheckConstraint("media_kind IN ('image','video')", name="ck_albums_media_kind"))
    sa.Table("album_videos", metadata,
        sa.Column("album_id", sa.String(), primary_key=True),
        sa.Column("upload_id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("added_by", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["album_id", "room_id"], ["albums.id", "albums.room_id"], name="fk_album_videos_album_room"),
        sa.ForeignKeyConstraint(["upload_id", "room_id"], ["uploads.id", "uploads.room_id"], name="fk_album_videos_upload_room"),
        sa.CheckConstraint("sort_order >= 0", name="ck_album_videos_sort_order"),
        sa.Index("ix_album_videos_album_order", "album_id", "sort_order", "upload_id"),
        sa.Index("ix_album_videos_upload", "upload_id"))
    sa.Table("record_read_receipts", metadata,
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("upload_id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("first_opened_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["upload_id", "room_id"], ["uploads.id", "uploads.room_id"], name="fk_record_read_upload_room"),
        sa.Index("ix_record_read_room_user", "room_id", "user_id"),
        sa.Index("ix_record_read_upload", "upload_id"))
    return metadata


def trigger_statements():
    from scripts.adopt_alembic import _album_migration
    statements = _album_migration().trigger_statements()
    for table in NEW_TABLES:
        for action in ("INSERT", "UPDATE", "DELETE"):
            statements.append(f"CREATE TRIGGER album_{table}_{action.lower()} BEFORE {action} ON {table} "
                "WHEN COALESCE((SELECT mode FROM security_control WHERE id=1),'UNKNOWN') <> 'LEGACY' "
                "BEGIN SELECT RAISE(ABORT,'album catalogue requires LEGACY'); END")
    return statements


def upgrade():
    from scripts.adopt_alembic import verify_migration_boundary
    bind = op.get_bind()
    if verify_migration_boundary(bind, down_revision):
        raise RuntimeError("0007 refused: unknown, drifted or non-LEGACY 0006 catalogue")
    # Lock before DDL; no table rebuild or changes to existing IDs/FKs/files.
    query = "SELECT mode,admission,runtime_generation FROM security_control"
    if bind.dialect.name == "postgresql":
        query += " FOR UPDATE"
    if bind.execute(sa.text(query)).all() != [("LEGACY", "CLOSED", None)]:
        raise RuntimeError("0007 refused: catalogue is not closed LEGACY")
    if bind.dialect.name == "sqlite":
        op.execute("ALTER TABLE albums ADD COLUMN media_kind VARCHAR NOT NULL DEFAULT 'image' "
                   "CONSTRAINT ck_albums_media_kind CHECK (media_kind IN ('image','video'))")
    else:
        op.add_column("albums", sa.Column("media_kind", sa.String(), nullable=False, server_default="image"))
        op.create_check_constraint("ck_albums_media_kind", "albums", "media_kind IN ('image','video')")
    metadata = schema_metadata()
    for table in NEW_TABLES:
        metadata.tables[table].create(bind)
    if bind.dialect.name == "sqlite":
        for statement in trigger_statements():
            if any(f" ON {table} " in statement for table in NEW_TABLES):
                op.execute(statement)


def downgrade():
    bind = op.get_bind()
    from scripts.adopt_alembic import verify_migration_boundary
    if verify_migration_boundary(bind, revision):
        raise RuntimeError("0007 downgrade refused: unknown or non-LEGACY catalogue")
    if (any(bind.execute(sa.text(f"SELECT 1 FROM {t} LIMIT 1")).first() for t in NEW_TABLES)
            or bind.execute(sa.text("SELECT 1 FROM albums WHERE media_kind <> 'image' LIMIT 1")).first()):
        raise RuntimeError("0007 downgrade refused: video albums or read receipts are not empty")
    for table in reversed(NEW_TABLES):
        op.drop_table(table)
    if bind.dialect.name != "sqlite":
        op.drop_constraint("ck_albums_media_kind", "albums", type_="check")
    op.drop_column("albums", "media_kind")
