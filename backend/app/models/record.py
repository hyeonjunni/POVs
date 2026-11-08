"""Record policy metadata; plaintext content stays in encrypted record objects."""
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Record(Base):
    __tablename__ = "records"
    __table_args__ = (
        CheckConstraint("visibility IN ('PRIVATE','FAMILY','CUSTOM')", name="ck_record_visibility"),
        CheckConstraint("lifecycle_state IN ('ACTIVE','RESTRICTED','HIDDEN','ARCHIVED','DELETION_REQUESTED','SOFT_DELETED','PURGED')", name="ck_record_lifecycle"),
        CheckConstraint("review_status IN ('PREDICTED','PENDING_REVIEW','APPROVED')", name="ck_record_review"),
        CheckConstraint("record_version >= 1 AND metadata_revision >= 0 AND source_revision >= 1 AND authorization_epoch >= 0", name="ck_record_versions"),
    )
    id: Mapped[str] = mapped_column(ForeignKey("uploads.id", ondelete="CASCADE"), primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("family_rooms.id"), index=True)
    owner_member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    uploader_member_id: Mapped[str | None] = mapped_column(ForeignKey("family_members.id"))
    visibility: Mapped[str] = mapped_column(String, default="PRIVATE", server_default="PRIVATE")
    lifecycle_state: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    previous_state: Mapped[str | None] = mapped_column(String)
    review_status: Mapped[str] = mapped_column(String, default="PENDING_REVIEW", server_default="PENDING_REVIEW")
    record_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    metadata_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    source_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    authorization_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    soft_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_status: Mapped[str | None] = mapped_column(String)
    metadata_json: Mapped[dict[str, JsonValue] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecordGrant(Base):
    __tablename__ = "record_grants"
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"), primary_key=True)


class RecordPerson(Base):
    __tablename__ = "record_people"
    __table_args__ = (CheckConstraint("role IN ('SUBJECT','PARTICIPANT','PERSPECTIVE')", name="ck_record_person_role"),)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class RecordRestriction(Base):
    __tablename__ = "record_restrictions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")


class RecordRestrictionGrant(Base):
    __tablename__ = "record_restriction_grants"
    restriction_id: Mapped[str] = mapped_column(ForeignKey("record_restrictions.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"), primary_key=True)


class RecordAudit(Base):
    __tablename__ = "record_audits"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(String, index=True)
    actor_user_id: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    before_version: Mapped[int] = mapped_column(Integer)
    after_version: Mapped[int] = mapped_column(Integer)
    reason_digest: Mapped[str | None] = mapped_column(String)
    request_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecordMetadataRevision(Base):
    __tablename__ = "record_metadata_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    actor_member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"))
    metadata_revision: Mapped[int] = mapped_column(Integer)
    resulting_record_version: Mapped[int] = mapped_column(Integer)
    changed_fields: Mapped[list[str]] = mapped_column(JSON)
    reason_digest: Mapped[str] = mapped_column(String)
    source_affecting: Mapped[bool] = mapped_column(Boolean)
    object_id: Mapped[str | None] = mapped_column(String)
    object_version: Mapped[int | None] = mapped_column(Integer)
    legacy_detail: Mapped[dict[str, JsonValue] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecordDeletionRequest(Base):
    __tablename__ = "record_deletion_requests"
    __table_args__ = (CheckConstraint("status IN ('OPEN','ACCEPTED','DECLINED','WITHDRAWN','CLOSED')", name="ck_record_deletion_status"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("records.id"), index=True)
    requester_member_id: Mapped[str] = mapped_column(ForeignKey("family_members.id"))
    status: Mapped[str] = mapped_column(String, default="OPEN", server_default="OPEN")
    reason_digest: Mapped[str] = mapped_column(String)
    resolution: Mapped[str | None] = mapped_column(String)
    previous_state: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecordIdempotency(Base):
    __tablename__ = "record_idempotency"
    __table_args__ = (UniqueConstraint("principal_id", "route", "scope", "key", name="uq_record_idempotency"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    principal_id: Mapped[str] = mapped_column(String)
    route: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String)
    payload_hash: Mapped[str] = mapped_column(String)
    resource_id: Mapped[str] = mapped_column(String)
