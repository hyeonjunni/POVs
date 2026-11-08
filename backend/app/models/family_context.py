"""M3 reviewed context, separate from legacy graph/API semantics."""
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ContextEntity(Base):
    __tablename__ = "context_entities"
    __table_args__ = (
        UniqueConstraint("room_id", "id", name="uq_context_room_id"),
        CheckConstraint("kind IN ('RELATION','ALIAS','EVENT','PARTICIPATION')", name="ck_context_kind"),
        CheckConstraint("review_status IN ('APPROVED','NEEDS_REVIEW')", name="ck_context_review"),
        CheckConstraint("revision >= 1", name="ck_context_revision"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, ForeignKey("family_rooms.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    review_status: Mapped[str] = mapped_column(String, nullable=False, server_default="APPROVED")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ContextSource(Base):
    __tablename__ = "context_sources"
    entity_id: Mapped[str] = mapped_column(String, ForeignKey("context_entities.id", ondelete="CASCADE"), primary_key=True)
    record_id: Mapped[str] = mapped_column(String, ForeignKey("records.id"), primary_key=True)
    record_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)


def entity_fk():
    return ForeignKeyConstraint(["room_id", "id"], ["context_entities.room_id", "context_entities.id"], ondelete="CASCADE")


def person_fk(column):
    return ForeignKeyConstraint(["room_id", column], ["family_people.room_id", "family_people.id"])


class FamilyRelation(Base):
    __tablename__ = "context_relations"
    __table_args__ = (entity_fk(), person_fk("from_person_id"), person_fk("to_person_id"),
        CheckConstraint("from_person_id != to_person_id", name="ck_context_relation_self"),
        CheckConstraint("relation_type IN ('PARENT_OF','SPOUSE_OF','SIBLING_OF','GUARDIAN_OF','OTHER')", name="ck_context_relation_type"),
        CheckConstraint("relation_type NOT IN ('SPOUSE_OF','SIBLING_OF') OR from_person_id < to_person_id", name="ck_context_relation_order"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)
    from_person_id: Mapped[str] = mapped_column(String, nullable=False)
    to_person_id: Mapped[str] = mapped_column(String, nullable=False)
    relation_type: Mapped[str] = mapped_column(String, nullable=False)
    valid_from: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    valid_to: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class RelationAlias(Base):
    __tablename__ = "relation_aliases"
    __table_args__ = (entity_fk(), person_fk("speaker_person_id"), person_fk("viewer_person_id"),
        UniqueConstraint("room_id", "id", name="uq_alias_room_id"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)
    speaker_person_id: Mapped[str] = mapped_column(String, nullable=False)
    viewer_person_id: Mapped[str | None] = mapped_column(String, nullable=True)
    alias: Mapped[str] = mapped_column(String, nullable=False)
    valid_from: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    valid_to: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class AliasTarget(Base):
    __tablename__ = "relation_alias_targets"
    __table_args__ = (ForeignKeyConstraint(["room_id", "alias_id"], ["relation_aliases.room_id", "relation_aliases.id"], ondelete="CASCADE"), person_fk("person_id"))
    alias_id: Mapped[str] = mapped_column(String, primary_key=True)
    person_id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)


class Event(Base):
    __tablename__ = "context_events"
    __table_args__ = (entity_fk(), UniqueConstraint("room_id", "id", name="uq_context_event_room"),
        CheckConstraint("event_status IN ('PLANNED','ONGOING','COMPLETED','CANCELLED','UNKNOWN')", name="ck_context_event_status"),
        ForeignKeyConstraint(["room_id", "parent_event_id"], ["context_events.room_id", "context_events.id"]))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    event_status: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    location: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parent_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    phase_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    exam_id: Mapped[str | None] = mapped_column(String, nullable=True)


class EventParticipation(Base):
    __tablename__ = "event_participation"
    __table_args__ = (entity_fk(), person_fk("person_id"),
        ForeignKeyConstraint(["room_id", "event_id"], ["context_events.room_id", "context_events.id"]),
        UniqueConstraint("event_id", "person_id", "phase_ref", "mode", name="uq_context_participation"),
        CheckConstraint("mode IN ('ONSITE','REMOTE','SUPPORT','MENTIONED','NOT_PRESENT')", name="ck_context_participation_mode"))
    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False)
    event_id: Mapped[str] = mapped_column(String, nullable=False)
    person_id: Mapped[str] = mapped_column(String, nullable=False)
    phase_ref: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    mode: Mapped[str] = mapped_column(String, nullable=False)
    occurred_at: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    activity: Mapped[str | None] = mapped_column(String, nullable=True)


class ExternalDatasetRef(Base):
    __tablename__ = "external_dataset_refs"
    __table_args__ = (UniqueConstraint("room_id", "package_id", "family_ref", "kind", "external_dataset_id", name="uq_dataset_external_scope"),)
    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, ForeignKey("family_rooms.id"), nullable=False)
    package_id: Mapped[str] = mapped_column(String, nullable=False)
    family_ref: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    external_dataset_id: Mapped[str] = mapped_column(String, nullable=False)
    runtime_id: Mapped[str] = mapped_column(String, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String, nullable=False)
