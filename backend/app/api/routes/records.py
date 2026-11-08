
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text

from app.api.routes.secure_operations import principal, tx
from app.schemas.record import LifecycleTransition, PurgeRequest, RecordCreateRequest
from app.services.record_lifecycle import (
    begin_mutation,
    finish_mutation,
    require_capability,
    transition,
    utc_datetime,
)
from app.services.record_policy import (
    allowed_record_ids,
    require_record_access,
)
from app.services.record_service import authenticated_record_actor, create_secure_record

router = APIRouter(dependencies=[Depends(authenticated_record_actor)])


from app.services.record_service_response import record_response


@router.post("/records")
async def create_record(payload: RecordCreateRequest, request: Request):
    if getattr(request.app.state, "secure_runtime", None) is not None:
        result = create_secure_record(payload, request, visibility=payload.visibility)
        record_id = result if isinstance(result, str) else result.upload_id
    else:
        from app.core.database import SessionLocal
        from app.services.record_service import (
            create_legacy_record,
            idempotency,
            save_idempotency,
        )
        with SessionLocal() as db:
            db.execute(text("UPDATE family_rooms SET is_active=is_active WHERE id=:r"), {"r": payload.room_id})
            replay = idempotency(db, request, payload.model_dump_json(), payload.room_id)
            if replay:
                require_record_access(db, principal(request), replay)
                record_id = replay
            else:
                upload = create_legacy_record(db, payload, principal(request), visibility=payload.visibility)
                record_id = upload.id
                save_idempotency(db, request, payload.model_dump_json(), payload.room_id, record_id)
                db.commit()
    return await record_response(record_id, request)


@router.get("/records/{record_id}")
async def get_record(record_id: str, request: Request):
    with tx() as db:
        require_record_access(db, principal(request), record_id)
    return await record_response(record_id, request)


@router.get("/records/{record_id}/management")
async def manage_record(record_id: str, request: Request):
    return await record_response(record_id, request)


@router.get("/families/{room_id}/records")
async def list_records(room_id: str, request: Request):
    scope = request.query_params.get("scope", "COMBINED")
    if scope not in ("PERSONAL", "FAMILY", "COMBINED"):
        raise HTTPException(422, {"code": "INVALID_SCOPE"})
    with tx() as db:
        ids = allowed_record_ids(db, principal(request), room_id, purpose="browse", scope=scope)
    import json
    items = [json.loads(bytes((await record_response(rid, request)).body)) for rid in ids]
    return {"items": items, "next_cursor": None}


@router.post("/records/{record_id}/lifecycle-transitions")
async def lifecycle(record_id: str, payload: LifecycleTransition, request: Request):
    capability = {"HIDE": "can_archive", "ARCHIVE": "can_archive", "RESTORE": "can_restore", "SOFT_DELETE": "can_soft_delete", "ACTIVATE": "can_archive"}[payload.action]
    if payload.action == "RESTORE" and getattr(request.app.state, "secure_runtime", None) is not None:
        from app.api.routes.secure_media import open_bytes
        from app.services import secure_catalogue as cat
        with tx() as db:
            require_capability(db, principal(request), record_id, "can_restore")
            objects = db.execute(text("SELECT e.* FROM protected_pointers p JOIN encrypted_objects e ON e.object_id=p.object_id AND e.object_version=p.object_version WHERE (p.owner_type='record' AND p.owner_id=:r OR p.owner_type='file' AND p.owner_id IN (SELECT id FROM upload_files WHERE upload_id=:r)) AND p.state='ENCRYPTED_ACTIVE' AND e.state='ACTIVE'"), {"r": record_id}).mappings().all()
            envelopes = [cat.envelope_values(item) for item in objects]
        if not envelopes:
            raise HTTPException(409, {"code": "RESTORE_SOURCE_UNAVAILABLE"})
        for envelope in envelopes:
            open_bytes(request.app.state.secure_runtime, envelope)
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            require_capability(db, principal(request), record_id, capability)
            if payload.action == "RESTORE" and getattr(request.app.state, "secure_runtime", None) is not None:
                count = db.scalar(text("SELECT count(*) FROM protected_pointers p JOIN encrypted_objects e ON e.object_id=p.object_id AND e.object_version=p.object_version WHERE p.owner_type='record' AND p.owner_id=:r AND p.state='ENCRYPTED_ACTIVE' AND e.state='ACTIVE'"), {"r": record_id})
                if count != 1:
                    raise HTTPException(409, {"code": "RESTORE_SOURCE_UNAVAILABLE"})
            transition(db, row, payload.action)
            finish_mutation(db, request, row, action=payload.action, reason=payload.reason, payload=payload.model_dump_json())
    return await record_response(record_id, request)


@router.post("/records/{record_id}/purge-requests", status_code=202)
async def purge_request(record_id: str, payload: PurgeRequest, request: Request):
    from datetime import datetime, timedelta, timezone
    with tx() as db:
        row, replay = begin_mutation(db, request, record_id, payload.model_dump_json())
        if not replay:
            require_capability(db, principal(request), record_id, "can_approve_purge")
            now = datetime.now(timezone.utc)
            if not row["soft_deleted_at"] or now < utc_datetime(row["soft_deleted_at"]) + timedelta(days=30):
                raise HTTPException(409, {"code": "RETENTION_PERIOD_ACTIVE"})
            db.execute(text("UPDATE records SET purge_approved_at=:t,purge_status='PENDING' WHERE id=:r"), {"t": now, "r": record_id})
            finish_mutation(db, request, row, action="PURGE_APPROVED", reason=payload.reason, payload=payload.model_dump_json())
    return {"record_id": record_id, "purge_status": "PENDING", "physical_deletion_confirmed": False, "blocked_reason": "INVENTORY_ADAPTERS_NOT_CONFIRMED"}


@router.get("/records/{record_id}/purge-status")
async def purge_status(record_id: str, request: Request):
    with tx() as db:
        row = require_record_access(db, principal(request), record_id, purpose="manage")
        if row["actor_member_id"] != row["owner_member_id"]:
            raise HTTPException(403, {"code": "ACTION_NOT_ALLOWED"})
    return {"record_id": record_id, "lifecycle_state": row["lifecycle_state"], "purge_status": row["purge_status"], "physical_deletion_confirmed": False, "blocked_reason": "INVENTORY_ADAPTERS_NOT_CONFIRMED"}


from app.api.routes.records_metadata import router as metadata_router
from app.api.routes.records_privacy import router as privacy_router

router.include_router(privacy_router)
router.include_router(metadata_router)
