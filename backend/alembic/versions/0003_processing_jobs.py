"""processing_jobs table (room-scoped OCR/STT jobs)

Revision ID: 0003_processing_jobs
Revises: 0002_auth_identities
Create Date: 2026-09-03

Purely additive: one persisted job per (record, upload file, processor), plus
durable deletion intents added by the A1 in-place audit amendment. The JSON
result payload remains text for SQLite/PostgreSQL portability in Milestone 1.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_processing_jobs"
down_revision = "0002_auth_identities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("record_id", sa.String(), sa.ForeignKey("uploads.id"), nullable=False),
        sa.Column("upload_file_id", sa.String(), sa.ForeignKey("upload_files.id"), nullable=False),
        sa.Column("room_id", sa.String(), sa.ForeignKey("family_rooms.id"), nullable=False),
        sa.Column("processor", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("failure_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "record_id", "upload_file_id", "processor", name="uq_processing_job_record_file_processor"
        ),
        sa.CheckConstraint("processor IN ('ocr', 'stt')", name="ck_processing_job_processor_allowed"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_processing_job_status_allowed",
        ),
    )
    op.create_index("ix_processing_jobs_record_id", "processing_jobs", ["record_id"])
    op.create_index("ix_processing_jobs_room_id", "processing_jobs", ["room_id"])

    op.create_table(
        "deletion_intents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("storage_bucket", sa.String(), nullable=False),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scope IN ('record', 'room', 'account')",
            name="ck_deletion_intent_scope_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'deleted', 'absent', 'failed')",
            name="ck_deletion_intent_status_allowed",
        ),
    )
    op.create_index("ix_deletion_intents_status", "deletion_intents", ["status"])


def downgrade() -> None:
    op.drop_index("ix_deletion_intents_status", table_name="deletion_intents")
    op.drop_table("deletion_intents")
    op.drop_index("ix_processing_jobs_room_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_record_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")
