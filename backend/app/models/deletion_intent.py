from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class DeletionIntent(Base):
    """Durable storage deletion work; intentionally outlives source rows."""

    __tablename__ = "deletion_intents"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('record', 'room', 'account')",
            name="ck_deletion_intent_scope_allowed",
        ),
        CheckConstraint(
            "status IN ('pending', 'deleted', 'absent', 'failed')",
            name="ck_deletion_intent_status_allowed",
        ),
        UniqueConstraint("storage_allocation_id", name="uq_deletion_intent_allocation"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    scope_id: Mapped[str] = mapped_column(String, nullable=False)
    storage_bucket: Mapped[str] = mapped_column(String, nullable=False)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    storage_allocation_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "legacy_storage_allocations.id",
            name="fk_deletion_intent_allocation",
        ),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", index=True)
    failure_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
