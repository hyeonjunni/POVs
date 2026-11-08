from sqlalchemy import CheckConstraint, DateTime, FetchedValue, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

PROCESSORS = ("ocr", "stt", "text")
STATUSES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED")


class ProcessingJob(Base):
    """One extraction job per (record, upload file, processor). Room-scoped for isolation."""

    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint(
            "record_id", "upload_file_id", "processor", name="uq_processing_job_record_file_processor"
        ),
        UniqueConstraint(
            "id",
            "record_id",
            "upload_file_id",
            "room_id",
            name="uq_processing_jobs_id_record_file_room",
        ),
        CheckConstraint("processor IN ('ocr', 'stt', 'text')", name="ck_processing_job_processor_allowed"),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_processing_job_status_allowed",
        ),
        {"implicit_returning": False},
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("uploads.id"), nullable=False, index=True)
    upload_file_id: Mapped[str] = mapped_column(ForeignKey("upload_files.id"), nullable=False)
    room_id: Mapped[str] = mapped_column(ForeignKey("family_rooms.id"), nullable=False, index=True)
    processor: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, deferred=True, server_default=FetchedValue(),
    )
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
