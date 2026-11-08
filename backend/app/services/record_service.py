import hashlib
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.schemas.record import RecordCreateRequest
from app.schemas.upload import UploadCreateRequest, normalize_tags


def authenticated_record_actor(request: Request) -> str:
    from app.api.routes.secure_operations import principal, tx
    uid = principal(request)
    with tx() as db:
        found = db.scalar(text("SELECT u.id FROM users u WHERE u.id=:u AND EXISTS (SELECT 1 FROM auth_identities a WHERE a.user_id=u.id)"), {"u": uid})
        if found is None:
            raise HTTPException(401, "Authenticated user not found")
    return uid


def member_id(db: Connection | Session, room_id: str, user_id: str) -> str:
    result = db.scalar(text("SELECT id FROM family_members WHERE room_id=:r AND user_id=:u AND is_active=true"), {"r": room_id, "u": user_id})
    if result is None:
        raise HTTPException(404, {"code": "RESOURCE_NOT_AVAILABLE"})
    return str(result)


def validate_members(db: Connection | Session, room_id: str, members: list[str]) -> None:
    for mid in set(members):
        if not db.scalar(text("SELECT id FROM family_members WHERE id=:m AND room_id=:r AND is_active=true"), {"m": mid, "r": room_id}):
            raise HTTPException(422, {"code": "INVALID_MEMBER"})


def initialize_record(db: Connection | Session, record_id: str, payload: UploadCreateRequest, user_id: str, visibility: str) -> None:
    mid = member_id(db, payload.room_id, user_id)
    grants = payload.custom_grants if isinstance(payload, RecordCreateRequest) else []
    subjects = payload.subject_member_ids if isinstance(payload, RecordCreateRequest) else []
    participants = payload.participant_member_ids if isinstance(payload, RecordCreateRequest) else []
    perspective = [payload.perspective_member_id] if isinstance(payload, RecordCreateRequest) and payload.perspective_member_id else []
    validate_members(db, payload.room_id, grants + subjects + participants + perspective)
    if visibility != "CUSTOM" and grants:
        raise HTTPException(422, {"code": "CUSTOM_GRANTS_REQUIRE_CUSTOM"})
    db.execute(text("INSERT INTO records (id,room_id,owner_member_id,uploader_member_id,visibility,lifecycle_state,review_status,record_version,metadata_revision,source_revision,authorization_epoch,purge_status) VALUES (:id,:r,:m,:m,:v,'ACTIVE',:review,1,1,1,1,'NOT_REQUESTED')"),
        {"id": record_id, "r": payload.room_id, "m": mid, "v": visibility, "review": "PENDING_REVIEW" if isinstance(payload, RecordCreateRequest) else "APPROVED"})
    for grant in set(grants):
        db.execute(text("INSERT INTO record_grants(record_id,member_id) VALUES (:r,:m)"), {"r": record_id, "m": grant})
    for role, members in (("SUBJECT", subjects), ("PARTICIPANT", participants), ("PERSPECTIVE", perspective)):
        for person in set(members):
            db.execute(text("INSERT INTO record_people(record_id,member_id,role,approved) VALUES (:r,:m,:role,false)"), {"r": record_id, "m": person, "role": role})


def create_legacy_record(db: Session, payload: UploadCreateRequest, user_id: str, *, visibility: str = "FAMILY"):
    from app.api.routes.uploads import pack_description_with_tags
    from app.models.upload import Upload

    member_id(db, payload.room_id, user_id)
    upload = Upload(id=str(uuid4()), room_id=payload.room_id, uploader_user_id=user_id,
        type=payload.type, title=payload.title,
        description=pack_description_with_tags(payload.description, payload.tags), status="uploaded")
    db.add(upload)
    db.flush()
    initialize_record(db, upload.id, payload, user_id, visibility)
    return upload


def create_secure_record(payload: UploadCreateRequest, request: Request, *, visibility: str = "FAMILY"):
    from app.api.routes.secure_media import (
        abandon_staged,
        pointer,
        stage,
        upload_dto,
    )
    from app.api.routes.secure_operations import principal, raw_json, tx
    from app.services import secure_catalogue as cat
    from app.services.media_validation import MAX_OVERHEAD

    uid, runtime = principal(request), request.app.state.secure_runtime
    if payload.type not in ("image", "video", "voice", "audio", "text") or len(raw_json(payload.model_dump())) > MAX_OVERHEAD:
        raise HTTPException(400, "Invalid upload metadata")
    rid, oid = str(cat._uuid(payload.room_id)), str(uuid4())
    metadata = {"record_id": oid, "room_id": rid, "title": payload.title,
        "description": payload.description, "tags": normalize_tags(payload.tags), "file": None}
    with tx() as db:
        cat.lock_rooms(db, (rid,))
        cat.snapshot(db, principal_id=UUID(uid), targets=(("room", UUID(rid)),))
        replay = idempotency(db, request, payload.model_dump_json(), rid) if isinstance(payload, RecordCreateRequest) else None
        if replay:
            from app.services.record_policy import require_record_access
            require_record_access(db, uid, replay)
            published = db.scalar(text("SELECT state FROM protected_pointers WHERE owner_type='record' AND owner_id=:r"), {"r": replay})
            if published == "ENCRYPTED_ACTIVE":
                return replay
            oid = replay
            metadata["record_id"] = replay
        else:
            cat._cas(db, "INSERT INTO uploads (id,room_id,uploader_user_id,type,status) VALUES (:o,:r,:u,:t,'uploaded')", o=oid, r=rid, u=uid, t=payload.type)
            initialize_record(db, oid, payload, uid, visibility)
            pointer(db, "record", oid, rid)
            if isinstance(payload, RecordCreateRequest):
                save_idempotency(db, request, payload.model_dump_json(), rid, oid)
    staged = stage(runtime, uid, ("record", UUID(oid)), raw_json(metadata))
    try:
        with tx() as db:
            cat.publish_many(db, (staged,))
            row = dict(cat._row(db, "SELECT * FROM uploads WHERE id=:o", o=oid))
    except BaseException:
        abandon_staged((staged,))
        raise
    return upload_dto(request, row, metadata)


def idempotency(db: Connection | Session, request: Request, payload: str, scope: str) -> str | None:
    from app.api.routes.secure_operations import principal
    key = request.headers.get("idempotency-key")
    if not key or len(key) > 200:
        raise HTTPException(428, {"code": "IDEMPOTENCY_KEY_REQUIRED"})
    row = db.execute(text("SELECT payload_hash,resource_id FROM record_idempotency WHERE principal_id=:p AND route=:route AND scope=:s AND key=:k"),
        {"p": principal(request), "route": request.url.path, "s": scope, "k": key}).mappings().one_or_none()
    if row is None:
        return None
    if row["payload_hash"] != hashlib.sha256(payload.encode()).hexdigest():
        raise HTTPException(409, {"code": "IDEMPOTENCY_CONFLICT"})
    return row["resource_id"]


def save_idempotency(db: Connection | Session, request: Request, payload: str, scope: str, resource_id: str) -> None:
    from app.api.routes.secure_operations import principal
    db.execute(text("INSERT INTO record_idempotency(id,principal_id,route,scope,key,payload_hash,resource_id) VALUES (:id,:p,:route,:s,:k,:h,:r)"),
        {"id": str(uuid4()), "p": principal(request), "route": request.url.path, "s": scope,
         "k": request.headers["idempotency-key"], "h": hashlib.sha256(payload.encode()).hexdigest(), "r": resource_id})
