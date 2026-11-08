import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.services.record_policy import record_capabilities, require_record_access
from app.services.record_service import idempotency, save_idempotency


def begin_mutation(db: Connection, request: Request, record_id: str, payload: str):
    from app.api.routes.secure_operations import principal
    uid = principal(request)
    row = require_record_access(db, uid, record_id, purpose="manage")
    db.execute(text("UPDATE security_scopes SET revision=revision WHERE domain='room' AND owner_id=:r"), {"r": row["room_id"]})
    row = require_record_access(db, uid, record_id, purpose="manage")
    replay = idempotency(db, request, payload, record_id) if request.method == "POST" else None
    if replay:
        return row, True
    expected = request.headers.get("if-match")
    if expected is None:
        raise HTTPException(428, {"code": "PRECONDITION_REQUIRED"})
    if expected != f'"{row["record_version"]}"':
        raise HTTPException(412, {"code": "REVISION_CONFLICT"})
    result = db.execute(text("UPDATE records SET record_version=record_version+1 WHERE id=:r AND record_version=:v"), {"r": record_id, "v": row["record_version"]})
    if result.rowcount != 1:
        raise HTTPException(412, {"code": "REVISION_CONFLICT"})
    return row, False


def finish_mutation(db: Connection, request: Request, row, *, action: str, reason: str, payload: str, invalidate: bool = True) -> None:
    from app.api.routes.secure_operations import principal
    if invalidate:
        db.execute(text("UPDATE records SET authorization_epoch=authorization_epoch+1 WHERE id=:r"), {"r": row["id"]})
        db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room' AND owner_id=:r"), {"r": row["room_id"]})
    db.execute(text("INSERT INTO record_audits(id,record_id,actor_user_id,action,before_version,after_version,reason_digest,request_id) VALUES (:id,:r,:u,:a,:b,:v,:reason,:q)"),
        {"id": str(uuid4()), "r": row["id"], "u": principal(request), "a": action,
         "b": row["record_version"], "v": row["record_version"] + 1,
         "reason": hashlib.sha256(reason.encode()).hexdigest(), "q": str(uuid4())})
    if request.method == "POST":
        save_idempotency(db, request, payload, row["id"], row["id"])


def require_capability(db: Connection | Session, uid: str, record_id: str, capability: str) -> None:
    caps = record_capabilities(db, uid, record_id)
    if not getattr(caps, capability):
        raise HTTPException(403, {"code": "ACTION_NOT_ALLOWED"})


def utc_datetime(value: datetime | str) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def transition(db: Connection | Session, row, action: str, *, now: datetime | None = None) -> None:
    instant = now or datetime.now(timezone.utc)
    state = row["lifecycle_state"]
    active_restrictions = db.scalar(text("SELECT count(*) FROM record_restrictions WHERE record_id=:r AND active=true"), {"r": row["id"]})
    match action:
        case "HIDE":
            allowed, target = state in ("ACTIVE", "RESTRICTED"), "HIDDEN"
        case "ARCHIVE":
            allowed, target = state in ("ACTIVE", "RESTRICTED", "HIDDEN"), "ARCHIVED"
        case "SOFT_DELETE":
            allowed, target = state not in ("SOFT_DELETED", "PURGED"), "SOFT_DELETED"
        case "RESTORE":
            allowed = state in ("HIDDEN", "ARCHIVED", "SOFT_DELETED")
            if state == "SOFT_DELETED":
                allowed = bool(row["soft_deleted_at"]) and instant < utc_datetime(row["soft_deleted_at"]) + timedelta(days=30)
            target = "ARCHIVED"
        case "ACTIVATE":
            allowed = state in ("ARCHIVED", "HIDDEN", "RESTRICTED") and row["review_status"] == "APPROVED"
            target = "RESTRICTED" if active_restrictions else "ACTIVE"
        case _:
            raise HTTPException(422, {"code": "INVALID_TRANSITION"})
    if not allowed:
        raise HTTPException(409, {"code": "INVALID_TRANSITION"})
    db.execute(text("UPDATE records SET lifecycle_state=:s WHERE id=:r"), {"s": target, "r": row["id"]})
    if target == "SOFT_DELETED":
        db.execute(text("UPDATE records SET soft_deleted_at=:t,purge_approved_at=NULL,purge_status='NOT_REQUESTED' WHERE id=:r"), {"t": instant, "r": row["id"]})
    if action == "RESTORE":
        db.execute(text("UPDATE records SET review_status='PENDING_REVIEW',purge_approved_at=NULL,purge_status='NOT_REQUESTED',soft_deleted_at=NULL WHERE id=:r"), {"r": row["id"]})
        invalidate_evidence(db, row["id"])


def invalidate_evidence(db: Connection | Session, record_id: str) -> None:
    from app.api.routes.secure_media import invalidate_results
    invalidate_results(db, record_id)


def legacy_soft_delete(db: Connection | Session, user_id: str, record_id: str) -> None:
    row = require_record_access(db, user_id, record_id, purpose="manage")
    db.execute(text("UPDATE security_scopes SET revision=revision WHERE domain='room' AND owner_id=:r"), {"r": row["room_id"]})
    row = require_record_access(db, user_id, record_id, purpose="manage")
    if row["lifecycle_state"] == "SOFT_DELETED" and row["owner_member_id"] == row["actor_member_id"]:
        return
    require_capability(db, user_id, record_id, "can_soft_delete")
    connection = db.connection() if isinstance(db, Session) else db
    result = connection.execute(text("UPDATE records SET record_version=record_version+1,authorization_epoch=authorization_epoch+1 WHERE id=:r AND record_version=:v"), {"r": record_id, "v": row["record_version"]})
    if result.rowcount != 1:
        raise HTTPException(409, {"code": "REVISION_CONFLICT"})
    transition(db, row, "SOFT_DELETE")
    db.execute(text("UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room' AND owner_id=:r"), {"r": row["room_id"]})
    db.execute(text("INSERT INTO record_audits(id,record_id,actor_user_id,action,before_version,after_version) VALUES (:id,:r,:u,'SOFT_DELETED',:b,:a)"),
        {"id": str(uuid4()), "r": record_id, "u": user_id, "b": row["record_version"], "a": row["record_version"] + 1})
