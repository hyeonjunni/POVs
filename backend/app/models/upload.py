from sqlalchemy import Boolean, DateTime, FetchedValue, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (UniqueConstraint("id", "room_id", name="uq_uploads_id_room_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("family_rooms.id"))
    uploader_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    type: Mapped[str] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="uploaded")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UploadFile(Base):
    __tablename__ = "upload_files"
    __table_args__ = (
        UniqueConstraint("id", "upload_id", name="uq_upload_files_id_upload_id"),
        UniqueConstraint(
            "storage_allocation_id", name="uq_upload_file_storage_allocation"
        ),
        {"implicit_returning": False},
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("uploads.id"))
    file_uploader_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        deferred=True,
        server_default=FetchedValue(),
    )
    storage_bucket: Mapped[str | None] = mapped_column(String)
    storage_path: Mapped[str | None] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_allocation_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "legacy_storage_allocations.id",
            name="fk_upload_file_storage_allocation",
        ),
        nullable=True,
        deferred=True,
        server_default=FetchedValue(),
    )
    encrypted: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
