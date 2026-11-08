"""Durable room quota ledger for LEGACY local media.

Revision ID: 0011_legacy_room_storage_quota
Revises: 0010_merge_album_family
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from alembic import op
import sqlalchemy as sa

revision = "0011_legacy_room_storage_quota"
down_revision = "0010_merge_album_family"
branch_labels = None
depends_on = None

ALLOCATION_STATES = "'RESERVED','ACTIVE','DELETE_PENDING','RELEASED'"


def _sqlite_upload_triggers(bind) -> list[str]:
    if bind.dialect.name != "sqlite":
        return []
    return [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='upload_files' AND name LIKE 'm4_upload_files_%' "
                "ORDER BY name"
            )
        )
        if row[0]
    ]


def _drop_sqlite_upload_triggers(bind) -> list[str]:
    statements = _sqlite_upload_triggers(bind)
    names = bind.execute(
        sa.text(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='upload_files' AND name LIKE 'm4_upload_files_%'"
        )
    ).scalars().all()
    for name in names:
        op.execute(f'DROP TRIGGER "{name}"')
    return statements


def _restore_sqlite_upload_triggers(statements: list[str]) -> None:
    for statement in statements:
        op.execute(statement)


def _record_filename(name: str, record_id: str) -> bool:
    if Path(name).name != name or "\\" in name:
        return False
    stem = Path(name).stem
    if stem == record_id:
        return True
    prefix = record_id + "__"
    if not stem.startswith(prefix):
        return False
    try:
        attempt = stem[len(prefix):]
        return str(UUID(attempt)) == attempt
    except ValueError:
        return False


def _backfill(bind) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT f.id,f.upload_id,u.room_id,f.storage_bucket,f.storage_path,f.file_size "
            "FROM upload_files f JOIN uploads u ON u.id=f.upload_id "
            "WHERE f.storage_bucket='local-media' ORDER BY f.id"
        )
    ).mappings().all()
    configured = os.environ.get("AMBIENT_LEGACY_STORAGE_ROOT", "")
    default_root = (
        Path(__file__).resolve().parents[2] / "app" / "storage" / "uploads"
    ).resolve()
    inspection_root = Path(configured).resolve() if configured else default_root
    disk_entries = list(inspection_root.iterdir()) if inspection_root.is_dir() else []
    if not rows and not disk_entries:
        return
    if os.environ.get("AMBIENT_LEGACY_STORAGE_BACKFILL_APPROVED") != "1":
        raise RuntimeError(
            "0011 backfill refused: set AMBIENT_LEGACY_STORAGE_BACKFILL_APPROVED=1 "
            "only after stopping LEGACY writers and inventorying local media"
        )
    if not configured or not Path(configured).is_absolute():
        raise RuntimeError("0011 backfill refused: absolute AMBIENT_LEGACY_STORAGE_ROOT required")
    root = Path(configured).resolve()
    if not root.is_dir():
        raise RuntimeError("0011 backfill refused: storage root missing")
    if bind.execute(
        sa.text(
            "SELECT 1 FROM deletion_intents WHERE storage_bucket='local-media' "
            "AND status IN ('pending','failed') LIMIT 1"
        )
    ).first():
        raise RuntimeError("0011 backfill refused: nonterminal local deletion intent")
    duplicates = bind.execute(
        sa.text(
            "SELECT storage_path FROM upload_files WHERE storage_bucket='local-media' "
            "GROUP BY storage_path HAVING COUNT(*)>1 LIMIT 1"
        )
    ).first()
    if duplicates:
        raise RuntimeError("0011 backfill refused: duplicate local locator")

    tracked: set[str] = set()
    for row in rows:
        name = row["storage_path"]
        size = row["file_size"]
        if not isinstance(name, str) or not _record_filename(name, row["upload_id"]):
            raise RuntimeError("0011 backfill refused: invalid local locator")
        path = root / name
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise RuntimeError("0011 backfill refused: tracked local file missing") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("0011 backfill refused: local locator is not a regular file")
        if path.resolve().parent != root or type(size) is not int or size <= 0:
            raise RuntimeError("0011 backfill refused: invalid local size")
        if metadata.st_size != size:
            raise RuntimeError("0011 backfill refused: exact local size mismatch")
        tracked.add(name)

    physical = {entry.name for entry in root.iterdir()}
    if physical != tracked:
        raise RuntimeError("0011 backfill refused: untracked physical local file")

    for row in rows:
        allocation_id = str(uuid5(NAMESPACE_URL, "ambient-legacy-allocation:" + row["id"]))
        bind.execute(
            sa.text(
                "INSERT INTO legacy_storage_allocations "
                "(id,room_id,upload_id,storage_bucket,storage_path,byte_size,state,"
                "reserved_at,expires_at,updated_at) "
                "VALUES (:id,:room,:upload,'local-media',:path,:size,'ACTIVE',"
                "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ),
            dict(
                id=allocation_id,
                room=row["room_id"],
                upload=row["upload_id"],
                path=row["storage_path"],
                size=row["file_size"],
            ),
        )
        bind.execute(
            sa.text(
                "UPDATE upload_files SET storage_allocation_id=:allocation WHERE id=:id"
            ),
            dict(allocation=allocation_id, id=row["id"]),
        )


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "legacy_storage_allocations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("upload_id", sa.String(), nullable=False),
        sa.Column("storage_bucket", sa.String(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("storage_bucket='local-media'", name="ck_legacy_allocation_local_only"),
        sa.CheckConstraint("byte_size>0", name="ck_legacy_allocation_positive_size"),
        sa.CheckConstraint(f"state IN ({ALLOCATION_STATES})", name="ck_legacy_allocation_state"),
        sa.UniqueConstraint("storage_bucket", "storage_path", name="uq_legacy_allocation_locator"),
    )
    op.create_index(
        "ix_legacy_allocation_room_state",
        "legacy_storage_allocations",
        ["room_id", "state"],
    )
    op.create_index(
        "ix_legacy_allocation_upload_state",
        "legacy_storage_allocations",
        ["upload_id", "state"],
    )

    if bind.dialect.name == "sqlite":
        triggers = _drop_sqlite_upload_triggers(bind)
        with op.batch_alter_table("upload_files") as batch:
            batch.add_column(sa.Column("storage_allocation_id", sa.String(), nullable=True))
            batch.create_unique_constraint("uq_upload_file_storage_allocation", ["storage_allocation_id"])
            batch.create_foreign_key(
                "fk_upload_file_storage_allocation",
                "legacy_storage_allocations",
                ["storage_allocation_id"],
                ["id"],
            )
        _restore_sqlite_upload_triggers(triggers)
        with op.batch_alter_table("deletion_intents") as batch:
            batch.add_column(sa.Column("storage_allocation_id", sa.String(), nullable=True))
            batch.create_unique_constraint("uq_deletion_intent_allocation", ["storage_allocation_id"])
            batch.create_foreign_key(
                "fk_deletion_intent_allocation",
                "legacy_storage_allocations",
                ["storage_allocation_id"],
                ["id"],
            )
    else:
        op.add_column("upload_files", sa.Column("storage_allocation_id", sa.String(), nullable=True))
        op.create_unique_constraint("uq_upload_file_storage_allocation", "upload_files", ["storage_allocation_id"])
        op.create_foreign_key("fk_upload_file_storage_allocation", "upload_files", "legacy_storage_allocations", ["storage_allocation_id"], ["id"])
        op.add_column("deletion_intents", sa.Column("storage_allocation_id", sa.String(), nullable=True))
        op.create_unique_constraint("uq_deletion_intent_allocation", "deletion_intents", ["storage_allocation_id"])
        op.create_foreign_key("fk_deletion_intent_allocation", "deletion_intents", "legacy_storage_allocations", ["storage_allocation_id"], ["id"])
    _backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM legacy_storage_allocations LIMIT 1")).first():
        raise RuntimeError("0011 downgrade refused: storage allocation ledger is not empty")
    if bind.execute(sa.text("SELECT 1 FROM upload_files WHERE storage_allocation_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("0011 downgrade refused: upload allocation link remains")
    if bind.execute(sa.text("SELECT 1 FROM deletion_intents WHERE storage_allocation_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("0011 downgrade refused: deletion allocation link remains")
    if bind.dialect.name == "sqlite":
        triggers = _drop_sqlite_upload_triggers(bind)
        with op.batch_alter_table("deletion_intents") as batch:
            batch.drop_constraint("fk_deletion_intent_allocation", type_="foreignkey")
            batch.drop_constraint("uq_deletion_intent_allocation", type_="unique")
            batch.drop_column("storage_allocation_id")
        with op.batch_alter_table("upload_files") as batch:
            batch.drop_constraint("fk_upload_file_storage_allocation", type_="foreignkey")
            batch.drop_constraint("uq_upload_file_storage_allocation", type_="unique")
            batch.drop_column("storage_allocation_id")
        _restore_sqlite_upload_triggers(triggers)
    else:
        op.drop_constraint("fk_deletion_intent_allocation", "deletion_intents", type_="foreignkey")
        op.drop_constraint("uq_deletion_intent_allocation", "deletion_intents", type_="unique")
        op.drop_column("deletion_intents", "storage_allocation_id")
        op.drop_constraint("fk_upload_file_storage_allocation", "upload_files", type_="foreignkey")
        op.drop_constraint("uq_upload_file_storage_allocation", "upload_files", type_="unique")
        op.drop_column("upload_files", "storage_allocation_id")
    op.drop_index("ix_legacy_allocation_upload_state", table_name="legacy_storage_allocations")
    op.drop_index("ix_legacy_allocation_room_state", table_name="legacy_storage_allocations")
    op.drop_table("legacy_storage_allocations")
