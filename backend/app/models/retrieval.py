from __future__ import annotations

import json
from collections.abc import Sequence

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.core.database import Base

M2_EMBEDDING_DIMENSIONS = 1024


class EmbeddingVector(TypeDecorator):
    """pgvector on PostgreSQL, canonical JSON text on SQLite."""

    impl = Text
    cache_ok = True

    def __init__(self, dimensions: int = M2_EMBEDDING_DIMENSIONS) -> None:
        super().__init__()
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dimensions))
        return dialect.type_descriptor(Text())

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


class RecordChunk(Base):
    __tablename__ = "record_chunks"
    __table_args__ = (
        UniqueConstraint(
            "processing_job_id",
            "source_attempt",
            "chunker_version",
            "ordinal",
            name="uq_record_chunk_source_version_ordinal",
        ),
        ForeignKeyConstraint(
            ["record_id", "room_id"],
            ["uploads.id", "uploads.room_id"],
            name="fk_record_chunks_record_room",
        ),
        ForeignKeyConstraint(
            ["file_id", "record_id"],
            ["upload_files.id", "upload_files.upload_id"],
            name="fk_record_chunks_file_record",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint("ordinal >= 0", name="ck_record_chunk_ordinal_nonnegative"),
        CheckConstraint("token_count >= 0", name="ck_record_chunk_token_count_nonnegative"),
        CheckConstraint("char_start >= 0", name="ck_record_chunk_char_start_nonnegative"),
        CheckConstraint("char_end >= char_start", name="ck_record_chunk_char_end_order"),
        CheckConstraint(
            "review_status IN ('unreviewed', 'confirmed', 'rejected')",
            name="ck_record_chunk_review_status_allowed",
        ),
        Index("ix_record_chunks_room_id_id", "room_id", "id"),
        Index("ix_record_chunks_record_id", "record_id"),
        Index("ix_record_chunks_file_id", "file_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)
    record_id: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    processing_job_id: Mapped[str] = mapped_column(String, nullable=False)
    source_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    source_result_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    source_locator_json: Mapped[str] = mapped_column(Text, nullable=False)
    extractor: Mapped[str] = mapped_column(String, nullable=False)
    extractor_id: Mapped[str] = mapped_column(String, nullable=False)
    review_status: Mapped[str] = mapped_column(String, nullable=False)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    chunker_version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RecordEmbedding(Base):
    __tablename__ = "record_embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "model_key", name="uq_record_embedding_chunk_model"),
        CheckConstraint(
            f"dimensions = {M2_EMBEDDING_DIMENSIONS}",
            name="ck_record_embedding_dimensions_m2",
        ),
        Index("ix_record_embeddings_model_key_chunk_id", "model_key", "chunk_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("record_chunks.id", ondelete="CASCADE"), nullable=False
    )
    model_key: Mapped[str] = mapped_column(String, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        EmbeddingVector(M2_EMBEDDING_DIMENSIONS), nullable=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
