from dataclasses import asdict
from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.demo_service import list_persona_options
from app.ai.model_registry import MODEL_REGISTRY, get_model_profile
from app.api.deps import AuthenticatedPrincipal, get_authenticated_principal, get_current_user
from app.api.routes.uploads import (
    ONE_PIXEL_PNG,
    get_latest_upload_file,
    store_media_file,
)
from app.core.database import SessionLocal, get_db
from app.models.auth_identity import PrincipalNotPersisted, require_persisted_principal
from app.models.family import FamilyMember, FamilyRoom
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.schemas.ai import (
    AskRequest,
    AIDemoBootstrapResponse,
    AIDemoChatRequest,
    AIDemoChatResponse,
    AIModelOption,
    AIPersonaOption,
)


router = APIRouter()
DEMO_ROOM_NAME = "목요일 데모 가족방"
DEMO_UPLOADS = [
    {
        "type": "text",
        "title": "아버지 인터뷰 메모",
        "description": "준비를 길게 하기보다, 작은 시도라도 계속 이어가는 태도가 중요하다고 남긴 기록입니다.",
        "tags": ["인터뷰", "아버지", "조언", "가족기록"],
    },
    {
        "type": "image",
        "title": "2024 가족 송년회 사진",
        "description": "OCR 예시: 2024 가족 송년회 / 가족이 함께 모여 건배하는 장면",
        "tags": ["송년회", "사진", "OCR", "가족행사"],
        "binary_filename": "demo-year-end-photo.png",
        "binary_mime_type": "image/png",
        "binary_bytes": ONE_PIXEL_PNG,
    },
    {
        "type": "video",
        "title": "송년회 건배 영상",
        "description": "영상 OCR/STT 예시: 건강하게 오래 함께 지내자 / 다음 가족 모임 건배사",
        "tags": ["송년회", "영상", "OCR", "STT", "건배사"],
    },
    {
        "type": "text",
        "title": "부산 여행 회상 메모",
        "description": "광안리 바다 앞에서 가족사진을 찍고 바람이 시원했다고 남긴 기록입니다.",
        "tags": ["부산", "여행", "회상", "가족사진"],
    },
]


@router.get("/models", response_model=list[AIModelOption])
def list_models():
    return [
        AIModelOption(
            id=model.id,
            display_name=model.display_name,
            provider=model.provider,
            placement=model.placement,
            supports_image=model.supports_image,
            supports_audio=model.supports_audio,
            max_context_tokens=model.max_context_tokens,
            notes=model.notes,
        )
        for model in MODEL_REGISTRY
    ]


@router.get("/personas", response_model=list[AIPersonaOption])
def list_personas():
    return [AIPersonaOption(**item) for item in list_persona_options()]


@router.get("/runtime-status")
def get_runtime_status():
    return {
        "status": "diagnostics-disabled",
        "gcs_media_storage_enabled": False,
    }


@router.post("/demo-bootstrap", response_model=AIDemoBootstrapResponse)
def bootstrap_demo(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.account_cleanup import drive_deletion_intents
    from app.services.storage_quota import (
        RoomStorageQuotaExceeded,
        abandon,
        activate,
        recover_expired,
        reserve,
    )

    user_id = current_user.id
    room = db.scalar(
        select(FamilyRoom).where(
            FamilyRoom.owner_user_id == user_id,
            FamilyRoom.name == DEMO_ROOM_NAME,
        )
    )

    if room is None:
        room = FamilyRoom(
            id=str(uuid.uuid4()),
            name=DEMO_ROOM_NAME,
            invite_code=f"DEMO{str(uuid.uuid4())[:2].upper()}",
            owner_user_id=user_id,
        )
        db.add(room)
        db.flush()
        db.add(
            FamilyMember(
                id=str(uuid.uuid4()),
                room_id=room.id,
                user_id=user_id,
                role="owner",
            )
        )

    seeded_count = 0
    pending: list[tuple[str, dict]] = []
    for item in DEMO_UPLOADS:
        upload = db.scalar(
            select(Upload).where(
                Upload.room_id == room.id,
                Upload.title == item["title"],
            )
        )
        if upload is None:
            db.flush()
            from app.schemas.upload import UploadCreateRequest
            from app.services.record_service import create_legacy_record
            upload = create_legacy_record(db, UploadCreateRequest.model_validate({
                "room_id": room.id, "type": item["type"], "title": item["title"],
                "description": item["description"], "tags": item.get("tags", [])}), user_id)
            seeded_count += 1

        if item.get("binary_bytes") and get_latest_upload_file(db, upload.id) is None:
            pending.append((upload.id, item))

    db.commit()
    room_id, room_name = room.id, room.name
    db.close()

    seeded_files = 0
    principal_missing = False
    recovered = recover_expired(SessionLocal)
    drive_deletion_intents(SessionLocal, own_intent_ids=recovered)
    for upload_id, item in pending:
        suffix = Path(item["binary_filename"]).suffix or ".bin"
        session = SessionLocal()
        try:
            require_persisted_principal(session, user_id)
        except PrincipalNotPersisted:
            principal_missing = True
            session.rollback()
            session.close()
            break
        else:
            room_exists = session.get(FamilyRoom, room_id) is not None
            member = room_exists and session.scalar(
                select(FamilyMember.id).where(
                    FamilyMember.room_id == room_id,
                    FamilyMember.user_id == user_id,
                )
            ) is not None
            if (
                not member
                or session.get(Upload, upload_id) is None
                or get_latest_upload_file(session, upload_id) is not None
            ):
                session.rollback()
                session.close()
                continue
            try:
                reservation = reserve(
                    session,
                    room_id=room_id,
                    upload_id=upload_id,
                    byte_size=len(item["binary_bytes"]),
                    suffix=suffix,
                )
            except RoomStorageQuotaExceeded:
                session.rollback()
                session.close()
                raise HTTPException(413, "Family room storage quota exceeded") from None
        session.close()

        try:
            bucket, path = store_media_file(
                item["binary_bytes"],
                f"{upload_id}{suffix}",
                item["binary_mime_type"],
                metadata={
                    "upload_type": item["type"],
                    "upload_tags": ",".join(item.get("tags", [])),
                    "room_id": room_id,
                    "uploader_user_id": user_id,
                    "demo_seed": "true",
                },
                attempt_id=reservation.id,
                session_factory=SessionLocal,
            )
            if (bucket, path) != (
                reservation.storage_bucket,
                reservation.storage_path,
            ):
                raise RuntimeError("Storage publication locator changed")
        except BaseException:
            cleanup_ids = abandon(SessionLocal, reservation)
            if cleanup_ids:
                drive_deletion_intents(SessionLocal, own_intent_ids=cleanup_ids)
            raise

        intent_ids: list[str] = []
        session = SessionLocal()
        try:
            require_persisted_principal(session, user_id)
            member = session.scalar(
                select(FamilyMember.id).where(
                    FamilyMember.room_id == room_id,
                    FamilyMember.user_id == user_id,
                )
            ) is not None
            if (
                not member
                or session.get(Upload, upload_id) is None
                or get_latest_upload_file(session, upload_id) is not None
            ):
                session.rollback()
                cleanup_ids = abandon(SessionLocal, reservation)
                if cleanup_ids:
                    drive_deletion_intents(SessionLocal, own_intent_ids=cleanup_ids)
                continue
            upload_file = UploadFile(
                id=str(uuid.uuid4()),
                upload_id=upload_id,
                storage_bucket=bucket,
                storage_path=path,
                mime_type=item["binary_mime_type"],
                file_size=len(item["binary_bytes"]),
                encrypted=False,
                file_uploader_user_id=user_id,
            )
            intent_ids = activate(session, reservation, upload_file=upload_file)
            session.commit()
            seeded_files += 1
        except PrincipalNotPersisted:
            session.rollback()
            principal_missing = True
            cleanup_ids = abandon(SessionLocal, reservation)
            if cleanup_ids:
                drive_deletion_intents(SessionLocal, own_intent_ids=cleanup_ids)
            break
        except BaseException:
            session.rollback()
            cleanup_ids = abandon(SessionLocal, reservation)
            if cleanup_ids:
                drive_deletion_intents(SessionLocal, own_intent_ids=cleanup_ids)
            raise
        finally:
            session.close()
        if intent_ids:
            drive_deletion_intents(SessionLocal, own_intent_ids=intent_ids)
    if principal_missing:
        raise HTTPException(status_code=401, detail="Authenticated user not found")

    return AIDemoBootstrapResponse(
        room_id=room_id,
        room_name=room_name,
        seeded_uploads=seeded_count,
        seeded_files=seeded_files,
    )


@router.post("/chat-demo", response_model=AIDemoChatResponse)
def chat_demo(
    payload: AIDemoChatRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
):
    # The shipped demo UI is a compatibility client of the canonical pipeline.
    # A selected record may never narrow the room-wide safety evidence pool.
    from app.api.routes import grounded_answering

    if payload.selected_record_id:
        with grounded_answering.SessionLocal() as db:
            try:
                require_persisted_principal(db, principal.user_id)
            except PrincipalNotPersisted:
                raise HTTPException(401, "Authenticated user not found") from None
            from app.services.record_policy import require_record_access
            require_record_access(db, principal.user_id, payload.selected_record_id,
                                  purpose="rag", room_id=payload.room_id)

    result = grounded_answering.ask_family_room(
        payload.room_id,
        AskRequest(query=payload.query, model_id=payload.model_id, arm="bm25", k=3),
        principal,
    )
    model = get_model_profile(payload.model_id)
    return AIDemoChatResponse(
        answer=result.answer or "",
        answer_source="canonical_grounded",
        gate_route=result.gate_route,
        gate_action=result.gate_action,
        inference_source=model.placement,
        provider_name=model.provider if result.provider_mode != "not_called" else "rule_gate",
        provider_mode=result.provider_mode,
        selected_model=asdict(model),
        retrieved_evidence=[],
        citations=result.citations,
    )
