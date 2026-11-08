"""baseline schema (users, family_rooms, family_members, uploads, upload_files)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-03

Describes exactly the schema that ``Base.metadata.create_all`` plus the three
retired startup helpers (``ensure_user_profile_columns``, ``ensure_user_auth_columns``,
``ensure_family_member_relation_columns``) used to produce. This is the
downgrade boundary (see docs/backend/decisions/FOUNDATION_IDENTITY_MIGRATION_DECISION.md D4):
``downgrade()`` intentionally raises.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("google_sub", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("profile_image", sa.String(), nullable=True),
        sa.Column("age", sa.Integer(), nullable=True),
        sa.Column("gender", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("profile_chunk", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_users_google_sub", "users", ["google_sub"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "family_rooms",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("invite_code", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_family_rooms_invite_code", "family_rooms", ["invite_code"], unique=True)

    op.create_table(
        "family_members",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), sa.ForeignKey("family_rooms.id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column("related_to_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("relation_to_related_user", sa.String(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("room_id", "user_id", name="uq_room_user"),
    )

    op.create_table(
        "uploads",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), sa.ForeignKey("family_rooms.id"), nullable=False),
        sa.Column("uploader_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="uploaded"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "upload_files",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("upload_id", sa.String(), sa.ForeignKey("uploads.id"), nullable=False),
        sa.Column("storage_bucket", sa.String(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "downgrade below 0001_baseline is HUMAN_AUTHORITY_REQUIRED and is never "
        "executed by automation (FOUNDATION_IDENTITY_MIGRATION_DECISION.md D4)."
    )
