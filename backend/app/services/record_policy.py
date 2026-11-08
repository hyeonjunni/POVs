from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.orm import Session

from app.core.config import settings

Purpose = Literal["read", "manage", "browse", "rag"]
Scope = Literal["PERSONAL", "FAMILY", "COMBINED"]


class RecordAccessDenied(HTTPException):
    def __init__(self, actor_user_id: str, record_id: str) -> None:
        self.actor_user_id = actor_user_id
        self.record_id = record_id
        super().__init__(status_code=404, detail={"code": "RESOURCE_NOT_AVAILABLE"})


@dataclass(frozen=True, slots=True)
class RecordCapabilities:
    can_read: bool
    can_edit_content: bool
    can_edit_metadata: bool
    can_change_visibility: bool
    can_restrict: bool
    can_request_delete: bool
    can_archive: bool
    can_soft_delete: bool
    can_restore: bool
    can_approve_purge: bool
    can_reduce_visibility: bool
    can_approve_delete: bool
    can_purge: bool


def _predicate(purpose: Purpose, scope: Scope) -> str:
    clauses = [
        "r.id NOT IN :quarantined_ids",
        "r.owner_member_id IS NOT NULL",
        "EXISTS (SELECT 1 FROM uploads u WHERE u.id=r.id AND u.room_id=r.room_id)",
        "EXISTS (SELECT 1 FROM family_members om WHERE om.id=r.owner_member_id AND om.room_id=r.room_id)",
        "(m.is_active=true OR (:purpose='manage' AND r.visibility='PRIVATE' AND r.owner_member_id=m.id))",
        "(r.owner_member_id=m.id OR r.visibility='FAMILY' OR (r.visibility='CUSTOM' AND EXISTS (SELECT 1 FROM record_grants g WHERE g.record_id=r.id AND g.member_id=m.id)))",
        "(r.owner_member_id=m.id OR NOT EXISTS (SELECT 1 FROM record_restrictions x WHERE x.record_id=r.id AND x.active=true AND NOT EXISTS (SELECT 1 FROM record_restriction_grants xg WHERE xg.restriction_id=x.id AND xg.member_id=m.id)))",
        "(:scope='COMBINED' OR (:scope='PERSONAL' AND r.visibility='PRIVATE' AND r.owner_member_id=m.id) OR (:scope='FAMILY' AND r.visibility IN ('FAMILY','CUSTOM')))",
        "(r.lifecycle_state NOT IN ('HIDDEN','ARCHIVED') OR r.owner_member_id=m.id)",
        "(r.lifecycle_state <> 'DELETION_REQUESTED' OR r.owner_member_id=m.id OR EXISTS (SELECT 1 FROM record_deletion_requests d WHERE d.record_id=r.id AND d.requester_member_id=m.id AND d.status='OPEN'))",
        "(EXISTS (SELECT 1 FROM family_rooms room WHERE room.id=r.room_id AND room.is_active=true) OR (:purpose='manage' AND r.visibility='PRIVATE' AND r.owner_member_id=m.id))",
    ]
    if purpose in ("browse", "rag"):
        clauses.append("r.lifecycle_state='ACTIVE'")
    elif purpose == "manage":
        clauses.append("(r.lifecycle_state NOT IN ('SOFT_DELETED','PURGED') OR r.owner_member_id=m.id)")
    else:
        clauses.append("r.lifecycle_state NOT IN ('SOFT_DELETED','PURGED')")
    if purpose != "manage":
        clauses.append("NOT EXISTS (SELECT 1 FROM security_tombstones t WHERE (t.domain='room' AND t.owner_id=r.room_id AND (t.target_kind='room' OR (t.target_kind='record' AND t.target_id=r.id))) OR (t.domain='user' AND t.owner_id=m.user_id))")
    if purpose == "rag":
        clauses.append("r.review_status='APPROVED'")
    return " AND ".join(clauses)


def allowed_record_ids(
    db: Session | Connection, user_id: str, room_id: str, *,
    purpose: Purpose = "read", scope: Scope = "COMBINED",
) -> list[str]:
    return list(db.execute(text(
        "SELECT r.id FROM records r JOIN family_members m ON m.room_id=r.room_id "
        "AND m.user_id=:user WHERE r.room_id=:room AND " + _predicate(purpose, scope)
    ).bindparams(bindparam("quarantined_ids", expanding=True)),
        {"user": user_id, "room": room_id, "purpose": purpose, "scope": scope,
         "quarantined_ids": [str(value) for value in settings.quarantined_record_ids]}).scalars())


def require_record_access(
    db: Session | Connection, user_id: str, record_id: str, *,
    purpose: Purpose = "read", room_id: str | None = None,
) -> RowMapping:
    row = db.execute(text(
        "SELECT r.*,m.id AS actor_member_id,m.role AS actor_role,m.is_active AS actor_is_active "
        "FROM records r JOIN family_members m ON m.room_id=r.room_id AND m.user_id=:user "
        "WHERE r.id=:record AND (:room IS NULL OR r.room_id=:room) AND "
        + _predicate(purpose, "COMBINED")
    ).bindparams(bindparam("quarantined_ids", expanding=True)),
        {"user": user_id, "record": record_id, "room": room_id,
         "purpose": purpose, "scope": "COMBINED",
         "quarantined_ids": [str(value) for value in settings.quarantined_record_ids]}).mappings().first()
    if row is None:
        raise RecordAccessDenied(user_id, record_id)
    return row


def record_capabilities(db: Session | Connection, user_id: str, record_id: str) -> RecordCapabilities:
    row = require_record_access(db, user_id, record_id, purpose="manage")
    owner = row["owner_member_id"] == row["actor_member_id"]
    active = bool(row["actor_is_active"])
    present = row["lifecycle_state"] not in ("SOFT_DELETED", "PURGED")
    subject = active and present and db.execute(text(
        "SELECT 1 FROM record_people WHERE record_id=:record AND member_id=:member "
        "AND approved=true AND role IN ('SUBJECT','PARTICIPANT') LIMIT 1"
    ), {"record": record_id, "member": row["actor_member_id"]}).first() is not None
    admin = active and row["actor_role"].lower() in ("admin", "owner") and row["visibility"] != "PRIVATE"
    return RecordCapabilities(
        can_read=active and present,
        can_edit_content=active and owner and present,
        can_edit_metadata=present and (active and owner or admin),
        can_change_visibility=active and owner and present,
        can_restrict=active and present and (owner or subject),
        can_request_delete=subject,
        can_archive=active and owner and present,
        can_soft_delete=owner and present,
        can_restore=active and owner and row["lifecycle_state"] in ("SOFT_DELETED", "ARCHIVED", "HIDDEN"),
        can_approve_purge=owner and row["lifecycle_state"] == "SOFT_DELETED",
        can_reduce_visibility=active and present and (owner or subject),
        can_approve_delete=owner and row["lifecycle_state"] == "DELETION_REQUESTED",
        can_purge=False,
    )


def register_legacy_record(db: Session | Connection, record_id: str) -> None:
    db.execute(text(
        "INSERT INTO records (id,room_id,owner_member_id,uploader_member_id,visibility,review_status) "
        "SELECT u.id,u.room_id,m.id,m.id,'FAMILY','APPROVED' FROM uploads u "
        "LEFT JOIN family_members m ON m.room_id=u.room_id AND m.user_id=u.uploader_user_id "
        "WHERE u.id=:id AND NOT EXISTS (SELECT 1 FROM records r WHERE r.id=u.id)"
    ), {"id": record_id})
