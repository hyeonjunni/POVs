from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LegacyStorageAllocation(Base):
    """Durable accounting for one immutable LEGACY local-storage attempt."""

    __tablename__ = "legacy_storage_allocations"
    __table_args__ = (
        CheckConstraint("storage_bucket = 'local-media'", name="ck_legacy_allocation_local_only"),
        CheckConstraint("byte_size > 0", name="ck_legacy_allocation_positive_size"),
        CheckConstraint(
            "state IN ('RESERVED', 'ACTIVE', 'DELETE_PENDING', 'RELEASED')",
            name="ck_legacy_allocation_state",
        ),
        UniqueConstraint("storage_bucket", "storage_path", name="uq_legacy_allocation_locator"),
        Index("ix_legacy_allocation_room_state", "room_id", "state"),
        Index("ix_legacy_allocation_upload_state", "upload_id", "state"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)
    upload_id: Mapped[str] = mapped_column(String, nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String, nullable=False)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    reserved_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
