import hashlib
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from app.api.routes.secure_operations import principal, tx
from app.schemas.record import (
    DeletionDecision,
    RecordAction,
    VisibilityChange,
    VisibilityReduction,
)
from app.services.record_lifecycle import (
    begin_mutation,
    finish_mutation,
    require_capability,
    transition,
)
from app.services.record_policy import require_record_access
from app.services.record_service import validate_members

router = APIRouter()


@router.post("/records/{record_id}/visibility-changes")
async def visibility_change(record_id: str, payload: VisibilityChange, request: Request):
    from app.services.record_service_response import record_response
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            require_capability(db, principal(request), record_id, "can_change_visibility")
            validate_members(db, row["room_id"], payload.custom_grants)
            if payload.visibility != "CUSTOM" and payload.custom_grants:
                raise HTTPException(422, {"code": "CUSTOM_GRANTS_REQUIRE_CUSTOM"})
            db.execute(text("DELETE FROM record_grants WHERE record_id=:r"), {"r": record_id})
            for member in set(payload.custom_grants):
                db.execute(text("INSERT INTO record_grants(record_id,member_id) VALUES (:r,:m)"), {"r": record_id, "m": member})
            db.execute(text("UPDATE records SET visibility=:v WHERE id=:r"), {"v": payload.visibility, "r": record_id})
            finish_mutation(db, request, row, action="VISIBILITY_CHANGED", reason=payload.reason, payload=payload.model_dump_json())
    return await record_response(record_id, request)


@router.post("/records/{record_id}/visibility-reductions")
async def visibility_reduction(record_id: str, payload: VisibilityReduction, request: Request):
    from app.services.record_service_response import record_response
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            uid = principal(request)
            require_capability(db, uid, record_id, "can_restrict")
            validate_members(db, row["room_id"], payload.allowed_member_ids)
            audience = set(payload.allowed_member_ids) | {row["owner_member_id"], row["actor_member_id"]}
            for member in audience:
                user = db.scalar(text("SELECT user_id FROM family_members WHERE id=:m"), {"m": member})
                require_record_access(db, user, record_id, purpose="manage")
            restriction = str(uuid4())
            db.execute(text("INSERT INTO record_restrictions(id,record_id,member_id,active) VALUES (:id,:r,:m,true)"), {"id": restriction, "r": record_id, "m": row["actor_member_id"]})
            for member in audience:
                db.execute(text("INSERT INTO record_restriction_grants(restriction_id,member_id) VALUES (:id,:m)"), {"id": restriction, "m": member})
            db.execute(text("UPDATE records SET lifecycle_state=CASE WHEN lifecycle_state='ACTIVE' THEN 'RESTRICTED' ELSE lifecycle_state END WHERE id=:r"), {"r": record_id})
            finish_mutation(db, request, row, action="SUBJECT_RESTRICTION_ADDED", reason=payload.reason, payload=payload.model_dump_json())
    return await record_response(record_id, request)


@router.post("/records/{record_id}/visibility-reductions/{restriction_id}/withdrawals")
async def withdraw_restriction(record_id: str, restriction_id: str, payload: RecordAction, request: Request):
    from app.services.record_service_response import record_response
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            result = db.execute(text("UPDATE record_restrictions SET active=false WHERE id=:id AND record_id=:r AND member_id=:m AND active=true"), {"id": restriction_id, "r": record_id, "m": row["actor_member_id"]})
            if result.rowcount != 1:
                raise HTTPException(404, {"code": "RESOURCE_NOT_AVAILABLE"})
            finish_mutation(db, request, row, action="SUBJECT_RESTRICTION_WITHDRAWN", reason=payload.reason, payload=payload.model_dump_json())
    return await record_response(record_id, request)


@router.post("/records/{record_id}/deletion-requests")
async def request_deletion(record_id: str, payload: RecordAction, request: Request):
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            require_capability(db, principal(request), record_id, "can_request_delete")
            request_id = str(uuid4())
            db.execute(text("INSERT INTO record_deletion_requests(id,record_id,requester_member_id,status,reason_digest,previous_state) VALUES (:id,:r,:m,'OPEN',:h,:s)"),
                {"id": request_id, "r": record_id, "m": row["actor_member_id"], "h": hashlib.sha256(payload.reason.encode()).hexdigest(), "s": row["previous_state"] or row["lifecycle_state"]})
            db.execute(text("UPDATE records SET previous_state=CASE WHEN lifecycle_state='DELETION_REQUESTED' THEN previous_state ELSE lifecycle_state END,lifecycle_state='DELETION_REQUESTED' WHERE id=:r"), {"r": record_id})
            finish_mutation(db, request, row, action="DELETE_REQUESTED", reason=payload.reason, payload=payload.model_dump_json())
        requests = [dict(item) for item in db.execute(text("SELECT id,status FROM record_deletion_requests WHERE record_id=:r AND requester_member_id=:m ORDER BY created_at,id"), {"r": record_id, "m": row["actor_member_id"]}).mappings()]
    return {"record_id": record_id, "requests": requests, "record_version": row["record_version"] if replay else row["record_version"] + 1}


@router.post("/deletion-requests/{request_id}/decisions")
async def deletion_decision(request_id: str, payload: DeletionDecision, request: Request):
    from app.services.record_service_response import record_response
    with tx() as db:
        deletion = db.execute(text("SELECT * FROM record_deletion_requests WHERE id=:id"), {"id": request_id}).mappings().one_or_none()
        if deletion is None:
            raise HTTPException(404, {"code": "RESOURCE_NOT_AVAILABLE"})
        row, replay = begin_mutation(db, request, deletion["record_id"], payload.model_dump_json())
        if not replay:
            actor = row["actor_member_id"]
            permitted = actor == deletion["requester_member_id"] if payload.decision == "WITHDRAW" else actor == row["owner_member_id"]
            if not permitted:
                raise HTTPException(403, {"code": "ACTION_NOT_ALLOWED"})
            if deletion["status"] != "OPEN":
                raise HTTPException(409, {"code": "REQUEST_ALREADY_RESOLVED"})
            status = {"ACCEPT": "ACCEPTED", "DECLINE": "DECLINED", "WITHDRAW": "WITHDRAWN"}[payload.decision]
            db.execute(text("UPDATE record_deletion_requests SET status=:s,resolution=:v WHERE id=:id"), {"s": status, "v": status, "id": request_id})
            if payload.decision == "ACCEPT":
                transition(db, row, "SOFT_DELETE")
                db.execute(text("UPDATE record_deletion_requests SET status='CLOSED',resolution='ACCEPTED' WHERE record_id=:r AND status='OPEN'"), {"r": row["id"]})
            else:
                remaining = db.scalar(text("SELECT count(*) FROM record_deletion_requests WHERE record_id=:r AND status='OPEN'"), {"r": row["id"]})
                if not remaining:
                    restricted = db.scalar(text("SELECT count(*) FROM record_restrictions WHERE record_id=:r AND active=true"), {"r": row["id"]})
                    previous = row["previous_state"] or "ARCHIVED"
                    restored = "RESTRICTED" if previous == "ACTIVE" and restricted else previous
                    db.execute(text("UPDATE records SET lifecycle_state=:s,previous_state=NULL,review_status='PENDING_REVIEW' WHERE id=:r"), {"s": restored, "r": row["id"]})
            finish_mutation(db, request, row, action="DELETE_" + status, reason=payload.reason, payload=payload.model_dump_json())
    if payload.decision == "WITHDRAW":
        return {"request_id": request_id, "status": "WITHDRAWN"}
    return await record_response(row["id"], request)
