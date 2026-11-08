from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class FamilyRoom(Base):
    __tablename__ = "family_rooms"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String)
    invite_code: Mapped[str | None] = mapped_column(String, unique=True, index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FamilyMember(Base):
    __tablename__ = "family_members"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_room_user"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("family_rooms.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String, default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    related_to_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    relation_to_related_user: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FamilyPerson(Base):
    __tablename__ = "family_people"
    __table_args__ = (
        UniqueConstraint("room_id", "id", name="uq_family_people_room_id"),
        UniqueConstraint("room_id", "user_id", name="uq_family_people_room_user"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("family_rooms.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    gender: Mapped[str | None] = mapped_column(String, nullable=True)
    birth_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    death_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FamilyRelationship(Base):
    __tablename__ = "family_relationships"
    __table_args__ = (
        UniqueConstraint(
            "room_id", "from_person_id", "to_person_id", "relation_type",
            name="uq_family_relationship_edge",
        ),
        ForeignKeyConstraint(
            ["room_id", "from_person_id"],
            ["family_people.room_id", "family_people.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["room_id", "to_person_id"],
            ["family_people.room_id", "family_people.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("from_person_id != to_person_id", name="ck_family_relationship_not_self"),
        CheckConstraint(
            "relation_type IN ('PARENT_OF', 'SPOUSE_OF')",
            name="ck_family_relationship_type",
        ),
        CheckConstraint(
            "relation_type != 'SPOUSE_OF' OR from_person_id < to_person_id",
            name="ck_family_relationship_spouse_order",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("family_rooms.id", ondelete="CASCADE"), nullable=False
    )
    from_person_id: Mapped[str] = mapped_column(String, nullable=False)
    to_person_id: Mapped[str] = mapped_column(String, nullable=False)
    relation_type: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
