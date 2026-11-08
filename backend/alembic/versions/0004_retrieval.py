"""room-isolated retrieval chunks and embeddings

Revision ID: 0004_retrieval
Revises: 0003_processing_jobs
Create Date: 2026-09-09
"""
from __future__ import annotations

from collections.abc import Sequence
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator
from pgvector.sqlalchemy import Vector

revision = "0004_retrieval"
down_revision = "0003_processing_jobs"
branch_labels = None
depends_on = None

M2_EMBEDDING_DIMENSIONS = 1024


class M2EmbeddingVector(TypeDecorator):
    """Frozen M2 storage type; later model changes need later migrations."""

    impl = sa.Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(M2_EMBEDDING_DIMENSIONS))
        return dialect.type_descriptor(sa.Text())

    def process_bind_param(self, value: Sequence[float] | None, dialect: Dialect):
        if value is None or dialect.name == "postgresql":
            return value
        return json.dumps([float(item) for item in value], separators=(",", ":"), allow_nan=False)

    def process_result_value(self, value, dialect: Dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        return json.loads(value)

PARENT_UNIQUES = (
    ("uploads", "uq_uploads_id_room_id", ("id", "room_id")),
    ("upload_files", "uq_upload_files_id_upload_id", ("id", "upload_id")),
    (
        "processing_jobs",
        "uq_processing_jobs_id_record_file_room",
        ("id", "record_id", "upload_file_id", "room_id"),
    ),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        try:
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        except SQLAlchemyError as error:
            raise RuntimeError(
                "0004 retrieval migration requires PostgreSQL extension 'vector'; "
                "grant CREATE privilege or install it before retrying"
            ) from error

    for table_name, constraint_name, columns in PARENT_UNIQUES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.create_unique_constraint(constraint_name, list(columns))

    op.create_table(
        "record_chunks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("room_id", sa.String(), nullable=False),
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("processing_job_id", sa.String(), nullable=False),
        sa.Column("source_attempt", sa.Integer(), nullable=False),
        sa.Column("source_result_sha256", sa.CHAR(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.CHAR(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("source_locator_json", sa.Text(), nullable=False),
        sa.Column("extractor", sa.String(), nullable=False),
        sa.Column("extractor_id", sa.String(), nullable=False),
        sa.Column("review_status", sa.String(), nullable=False),
        sa.Column("user_confirmed", sa.Boolean(), nullable=False),
        sa.Column("chunker_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "processing_job_id",
            "source_attempt",
            "chunker_version",
            "ordinal",
            name="uq_record_chunk_source_version_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["record_id", "room_id"],
            ["uploads.id", "uploads.room_id"],
            name="fk_record_chunks_record_room",
        ),
        sa.ForeignKeyConstraint(
            ["file_id", "record_id"],
            ["upload_files.id", "upload_files.upload_id"],
            name="fk_record_chunks_file_record",
        ),
        sa.ForeignKeyConstraint(
            ["processing_job_id", "record_id", "file_id", "room_id"],
            [
                "processing_jobs.id",
                "processing_jobs.record_id",
                "processing_jobs.upload_file_id",
                "processing_jobs.room_id",
            ],
            name="fk_record_chunks_job_source",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_record_chunk_ordinal_nonnegative"),
        sa.CheckConstraint("token_count >= 0", name="ck_record_chunk_token_count_nonnegative"),
        sa.CheckConstraint("char_start >= 0", name="ck_record_chunk_char_start_nonnegative"),
        sa.CheckConstraint("char_end >= char_start", name="ck_record_chunk_char_end_order"),
        sa.CheckConstraint(
            "review_status IN ('unreviewed', 'confirmed', 'rejected')",
            name="ck_record_chunk_review_status_allowed",
        ),
    )
    op.create_index("ix_record_chunks_room_id_id", "record_chunks", ["room_id", "id"])
    op.create_index("ix_record_chunks_record_id", "record_chunks", ["record_id"])
    op.create_index("ix_record_chunks_file_id", "record_chunks", ["file_id"])

    op.create_table(
        "record_embeddings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "chunk_id",
            sa.String(),
            sa.ForeignKey("record_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_key", sa.String(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", M2EmbeddingVector(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("chunk_id", "model_key", name="uq_record_embedding_chunk_model"),
        sa.CheckConstraint(
            f"dimensions = {M2_EMBEDDING_DIMENSIONS}",
            name="ck_record_embedding_dimensions_m2",
        ),
    )
    op.create_index(
        "ix_record_embeddings_model_key_chunk_id",
        "record_embeddings",
        ["model_key", "chunk_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    counts = {
        table_name: bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar_one()
        for table_name in ("record_chunks", "record_embeddings")
    }
    populated = {table_name: count for table_name, count in counts.items() if count}
    if populated:
        raise RuntimeError(
            "0004 downgrade refused: "
            + ", ".join(f"{table_name}={count}" for table_name, count in counts.items())
        )

    op.drop_index("ix_record_embeddings_model_key_chunk_id", table_name="record_embeddings")
    op.drop_table("record_embeddings")
    op.drop_index("ix_record_chunks_file_id", table_name="record_chunks")
    op.drop_index("ix_record_chunks_record_id", table_name="record_chunks")
    op.drop_index("ix_record_chunks_room_id_id", table_name="record_chunks")
    op.drop_table("record_chunks")

    for table_name, constraint_name, _columns in reversed(PARENT_UNIQUES):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(constraint_name, type_="unique")
