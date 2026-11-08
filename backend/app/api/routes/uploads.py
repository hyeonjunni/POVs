import hashlib
from datetime import datetime, timezone
import json
import logging
import urllib.parse
import uuid
from base64 import b64decode
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.models.auth_identity import PrincipalNotPersisted, require_persisted_principal
from app.models.family import FamilyMember, FamilyRoom
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.schemas.upload import UploadCreateRequest, UploadResponse, normalize_tags


router = APIRouter()
logger = logging.getLogger(__name__)
LOCAL_MEDIA_ROOT = Path(__file__).resolve().parents[2] / "storage" / "uploads"
LOCAL_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
TAG_PREFIX = "[[tags:"
TAG_SUFFIX = "]]"
ONE_PIXEL_PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOoCBABAAIgAN3LGpnNAAAAAElFTkSuQmCC"
)


def build_file_url(request: Request | None, upload: Upload) -> str | None:
    if request is None:
        return None
    return f"{str(request.base_url).rstrip('/')}/api/v1/uploads/{upload.id}/content"


def pack_description_with_tags(description: str | None, tags: list[str] | None) -> str | None:
    normalized_tags = normalize_tags(tags)
    normalized_description = (description or "").strip()
    if not normalized_tags:
        return normalized_description or None

    tag_payload = ",".join(normalized_tags)
    if normalized_description:
        return f"{TAG_PREFIX}{tag_payload}{TAG_SUFFIX} {normalized_description}"
    return f"{TAG_PREFIX}{tag_payload}{TAG_SUFFIX}"


def unpack_description_and_tags(raw_description: str | None) -> tuple[str | None, list[str]]:
    if not raw_description:
        return None, []

    description = raw_description.strip()
    if not description.startswith(TAG_PREFIX):
        return description, []

    tag_end = description.find(TAG_SUFFIX)
    if tag_end == -1:
        return description, []

    raw_tags = description[len(TAG_PREFIX):tag_end]
    tags = normalize_tags(raw_tags.split(","))
    remainder = description[tag_end + len(TAG_SUFFIX):].strip()
    return remainder or None, tags


def build_content_disposition(filename: str, *, attachment: bool = False) -> str:
    filename = "".join(
        char if char.isprintable() and char not in '\"\\/;' else "_"
        for char in (filename or "download")
    )[:150]
    if attachment:
        filename += ".bin"
    encoded_filename = urllib.parse.quote(filename, safe="")
    safe_ascii_filename = "".join(char if ord(char) < 128 else "_" for char in filename) or "download"
    disposition = "attachment" if attachment else "inline"
    return f"{disposition}; filename=\"{safe_ascii_filename}\"; filename*=UTF-8''{encoded_filename}"


def media_response_metadata(content: bytes, title: str | None, declared_mime: str | None = None) -> tuple[str, dict[str, str]]:
    from app.services.media_loader import passive_media_type
    media_type = passive_media_type(content)
    if declared_mime == "text/plain":
        from app.processing.contracts import ProviderError
        from app.processing.native_text import decode_native_text
        try:
            decode_native_text(content)
        except ProviderError:
            raise HTTPException(409, "Stored text source is invalid") from None
        media_type = "text/plain"
    return media_type or "application/octet-stream", {
        "Content-Disposition": build_content_disposition(title, attachment=media_type in (None, "text/plain")),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'",
    }


def get_gcs_client():
    """Compatibility seam: LEGACY never constructs a remote storage client."""
    return None


def delete_gcs_blob(*_args, **_kwargs):
    raise HTTPException(status_code=409, detail="Historical LEGACY remote storage is unsupported")


def load_gcs_file(*_args, **_kwargs):
    raise HTTPException(status_code=409, detail="Historical LEGACY remote storage is unsupported")


def store_media_file_locally(file_bytes: bytes, stored_filename: str) -> tuple[str, str]:
    stored_path = LOCAL_MEDIA_ROOT / stored_filename
    stream = stored_path.open("xb")
    try:
        with stream:
            stream.write(file_bytes)
    except BaseException:
        stored_path.unlink(missing_ok=True)
        raise
    return "local-media", stored_filename


def store_media_file(
    file_bytes: bytes,
    stored_filename: str,
    content_type: str,
    metadata: dict[str, str] | None = None,
    *,
    attempt_id: str | None = None,
    session_factory=None,
) -> tuple[str, str]:
    # One immutable physical object per attempt, shared by uploads and demo seeding.
    name = Path(stored_filename)
    if name.name != stored_filename or "\\" in stored_filename:
        raise ValueError("Invalid media filename")
    attempt = uuid.UUID(attempt_id) if attempt_id is not None else uuid.uuid4()
    if str(attempt) != str(attempt_id or attempt):
        raise ValueError("Invalid storage attempt")
    stored_filename = f"{name.stem}__{attempt}{name.suffix}"
    if settings.use_gcs_media_storage:
        logger.warning("Deprecated LEGACY GCS upload setting ignored; media storage is local only.")
    if attempt_id is not None:
        from app.services.storage_quota import publish_local
        return publish_local(
            session_factory or SessionLocal, allocation_id=attempt_id,
            storage_path=stored_filename, byte_size=len(file_bytes),
            writer=lambda: store_media_file_locally(file_bytes, stored_filename),
        )
    return store_media_file_locally(file_bytes, stored_filename)


def serialize_upload(upload: Upload, request: Request | None = None, upload_file: UploadFile | None = None) -> UploadResponse:
    clean_description, tags = unpack_description_and_tags(upload.description)
    return UploadResponse(
        upload_id=upload.id,
        room_id=upload.room_id,
        uploader_user_id=upload.uploader_user_id,
        type=upload.type,
        title=upload.title,
        description=clean_description,
        tags=tags,
        status=upload.status,
        created_at=upload.created_at.isoformat() if upload.created_at else None,
        has_file=upload_file is not None,
        file_url=build_file_url(request, upload) if upload_file else None,
        mime_type=upload_file.mime_type if upload_file else None,
        file_size=upload_file.file_size if upload_file else None,
        file_id=upload_file.id if upload_file else None,
    )


def ensure_room_member(db: Session, room_id: str, user_id: str) -> FamilyMember:
    membership = db.scalar(
        select(FamilyMember).where(
            FamilyMember.room_id == room_id,
            FamilyMember.user_id == user_id,
            FamilyMember.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="User is not a member of this family room")
    return membership


def get_latest_upload_file(db: Session, upload_id: str) -> UploadFile | None:
    return db.scalar(
        select(UploadFile)
        .where(UploadFile.upload_id == upload_id)
        .order_by(UploadFile.created_at.desc(), UploadFile.id.desc())
    )


@router.post("", response_model=UploadResponse, deprecated=True)
def create_upload(payload: UploadCreateRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room = db.get(FamilyRoom, payload.room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Family room not found")

    ensure_room_member(db, payload.room_id, current_user.id)

    from app.services.record_service import create_legacy_record
    upload = create_legacy_record(db, payload, current_user.id)
    db.commit()
    db.refresh(upload)

    return serialize_upload(upload)


@router.post("/{upload_id}/binary", response_model=UploadResponse)
async def upload_binary_file(
    upload_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.services.record_policy import require_record_access
    require_record_access(db, current_user.id, upload_id)
    from app.services.record_lifecycle import require_capability
    require_capability(db, current_user.id, upload_id, "can_edit_content")
    upload = db.get(Upload, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")

    ensure_room_member(db, upload.room_id, current_user.id)
    _, upload_tags = unpack_description_and_tags(upload.description)
    metadata = {
        "upload_type": upload.type,
        "upload_tags": json.dumps(upload_tags, ensure_ascii=False),
        "room_id": upload.room_id,
        "uploader_user_id": upload.uploader_user_id,
    }
    user_id = current_user.id
    db.close()

    from app.services.media_validation import read_media_upload

    limit = {"image": min(settings.ocr_max_input_bytes, settings.media_image_max_bytes),
             "voice": min(settings.stt_max_input_bytes, settings.media_audio_max_bytes),
             "video": min(settings.stt_max_input_bytes, settings.media_video_max_bytes),
             "text": settings.text_max_input_bytes}[metadata["upload_type"]]
    file_bytes, content_type = await read_media_upload(request, max_bytes=limit)
    if content_type.split("/")[0] != {"voice": "audio"}.get(metadata["upload_type"], metadata["upload_type"]):
        raise HTTPException(400, "Upload type mismatch")
    return _publish_upload_bytes(upload_id, user_id, metadata, file_bytes, content_type, request)


@router.post("/{upload_id}/publish-text", response_model=UploadResponse)
def publish_text(upload_id: str, request: Request, db: Annotated[Session, Depends(get_db)],
                 current_user: Annotated[User, Depends(get_current_user)]) -> UploadResponse:
    from app.services.record_policy import require_record_access
    require_record_access(db, current_user.id, upload_id)
    upload = db.get(Upload, upload_id)
    if upload is None:
        raise HTTPException(404, "Upload not found")
    ensure_room_member(db, upload.room_id, current_user.id)
    if upload.uploader_user_id != current_user.id:
        raise HTTPException(404, "Upload not found")
    if upload.type != "text":
        raise HTTPException(400, "Upload type mismatch")
    description, tags = unpack_description_and_tags(upload.description)
    from app.services.media_validation import validate_media
    content = (description or "").encode("utf-8")
    validate_media(content, "text/plain", "source.txt")
    metadata = {"upload_type": upload.type, "upload_tags": json.dumps(tags, ensure_ascii=False),
                "room_id": upload.room_id, "uploader_user_id": upload.uploader_user_id}
    expected_description, user_id = upload.description, current_user.id
    db.close()
    return _publish_upload_bytes(upload_id, user_id, metadata, content, "text/plain", request,
                                 expected_description=expected_description)


def _publish_upload_bytes(upload_id, user_id, metadata, file_bytes, content_type, request,
                          *, expected_description=None) -> UploadResponse:
    from app.services.account_cleanup import drive_deletion_intents
    from app.services.media_validation import EXTENSIONS
    from app.services.storage_quota import (
        RoomStorageQuotaExceeded,
        StorageAllocationStateChanged,
        abandon,
        activate,
        recover_expired,
        reserve,
    )
    suffix = EXTENSIONS[content_type][0]
    recovered = recover_expired(SessionLocal)
    drive_deletion_intents(SessionLocal, own_intent_ids=recovered)

    reservation_session = SessionLocal()
    try:
        require_persisted_principal(reservation_session, user_id)
        from app.services.record_policy import require_record_access
        require_record_access(reservation_session, user_id, upload_id)
        from app.services.record_lifecycle import require_capability
        require_capability(reservation_session, user_id, upload_id, "can_edit_content")
        current = reservation_session.get(Upload, upload_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Upload not found")
        ensure_room_member(reservation_session, current.room_id, user_id)
        if expected_description is not None and (current.description != expected_description
                or current.uploader_user_id != user_id or current.type != "text"):
            raise HTTPException(409, "Text source changed")
        try:
            reservation = reserve(
                reservation_session,
                room_id=current.room_id,
                upload_id=upload_id,
                byte_size=len(file_bytes),
                suffix=suffix,
            )
        except RoomStorageQuotaExceeded:
            reservation_session.rollback()
            raise HTTPException(413, "Family room storage quota exceeded") from None
    except PrincipalNotPersisted:
        reservation_session.rollback()
        raise HTTPException(401, "Authenticated user not found") from None
    finally:
        reservation_session.close()

    try:
        storage_bucket, storage_path = store_media_file(
            file_bytes,
            f"{upload_id}{suffix}",
            content_type,
            metadata=metadata,
            attempt_id=reservation.id,
        )
        if (storage_bucket, storage_path) != (
            reservation.storage_bucket,
            reservation.storage_path,
        ):
            raise StorageAllocationStateChanged("Storage publication locator changed")
    except BaseException:
        cleanup_ids = abandon(SessionLocal, reservation)
        if cleanup_ids:
            drive_deletion_intents(SessionLocal, own_intent_ids=cleanup_ids)
        raise

    # Transaction B rejects by deferral, never by raising: one commit, one
    # `drive_deletion_intents`, and the `HTTPException` only after the session
    # is closed — so no rejection path can double-record or double-drive an
    # intent, and `except Exception` below only ever sees a real failure.
    session = SessionLocal()
    rejection: HTTPException | None = None
    intent_ids: list[str] = []
    response: UploadResponse | None = None
    upload: Upload | None = None
    try:
        try:
            require_persisted_principal(session, user_id)
            require_record_access(session, user_id, upload_id)
            require_capability(session, user_id, upload_id, "can_edit_content")
        except PrincipalNotPersisted:
            rejection = HTTPException(status_code=401, detail="Authenticated user not found")
        else:
            from app.services.storage_quota import _lock_room
            try:
                _lock_room(session, reservation.room_id)
            except StorageAllocationStateChanged:
                rejection = HTTPException(status_code=404, detail="Upload not found")
            from app.services.retrieval_indexing import serialize_record
            if session.get(Upload, upload_id) is not None:
                serialize_record(session, upload_id)
                session.expire_all()
            upload = session.get(Upload, upload_id)
            if expected_description is not None and upload is not None and (
                    upload.description != expected_description or upload.uploader_user_id != user_id
                    or upload.type != "text"):
                rejection = HTTPException(409, "Text source changed")
            member = upload is not None and session.scalar(
                select(FamilyMember.id).where(
                    FamilyMember.room_id == upload.room_id,
                    FamilyMember.user_id == user_id,
                )
            ) is not None
            if not member:
                rejection = HTTPException(
                    status_code=404 if upload is None else 403,
                    detail="Upload not found",
                )

        if rejection is not None:
            session.rollback()
        else:
            assert upload is not None
            upload_file = UploadFile(
                id=str(uuid.uuid4()),
                upload_id=upload_id,
                storage_bucket=storage_bucket,
                storage_path=storage_path,
                mime_type=content_type,
                file_size=len(file_bytes),
                encrypted=False,
                created_at=datetime.now(timezone.utc),
            )
            if settings.security_mode == "LEGACY":
                upload_file.file_uploader_user_id = user_id
            from app.models.retrieval import RecordChunk, RecordEmbedding
            old_chunks = select(RecordChunk.id).where(RecordChunk.record_id == upload_id)
            session.execute(delete(RecordEmbedding).where(RecordEmbedding.chunk_id.in_(old_chunks)))
            session.execute(delete(RecordChunk).where(RecordChunk.record_id == upload_id))
            intent_ids = activate(session, reservation, upload_file=upload_file)
            from sqlalchemy import text
            session.execute(text("UPDATE records SET record_version=record_version+1,source_revision=source_revision+1,authorization_epoch=authorization_epoch+1 WHERE id=:r"), {"r": upload_id})
            session.commit()
            session.refresh(upload)
            response = serialize_upload(upload, request=request, upload_file=upload_file)
    except Exception:
        session.rollback()
        session.close()
        cleanup_ids = abandon(SessionLocal, reservation)
        if cleanup_ids:
            drive_deletion_intents(SessionLocal, own_intent_ids=cleanup_ids)
        raise
    finally:
        session.close()

    if rejection is not None:
        intent_ids = abandon(SessionLocal, reservation)
    if intent_ids:
        drive_deletion_intents(SessionLocal, own_intent_ids=intent_ids)
    if rejection is not None:
        raise rejection
    assert response is not None
    return response


@router.get("/{room_id}", response_model=list[UploadResponse])
def list_uploads(room_id: str, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.services.record_policy import allowed_record_ids
    room = db.get(FamilyRoom, room_id)
    if room is None:
        raise HTTPException(status_code=404, detail="Family room not found")

    ensure_room_member(db, room_id, current_user.id)

    uploads = db.scalars(
        select(Upload)
        .where(Upload.room_id == room_id,
               Upload.id.in_(allowed_record_ids(db, current_user.id, room_id, purpose="browse")))
        .order_by(Upload.created_at.desc(), Upload.id.desc())
    ).all()

    return [
        serialize_upload(upload, request=request, upload_file=get_latest_upload_file(db, upload.id))
        for upload in uploads
    ]


def _original_snapshot(db: Session, upload_id: str, file_id: str, digest: str, user_id: str):
    from app.models.processing_job import ProcessingJob
    from app.services.processing_jobs import processing_result_is_evidence

    require_persisted_principal(db, user_id)
    from app.services.record_policy import require_record_access
    require_record_access(db, user_id, upload_id, purpose="rag")
    upload = db.get(Upload, upload_id)
    if upload is None:
        raise HTTPException(404, "Upload not found")
    ensure_room_member(db, upload.room_id, user_id)
    file = get_latest_upload_file(db, upload_id)
    if file is None or file.id != file_id:
        raise HTTPException(409, "Original source changed; reprocess the current file")
    jobs = db.scalars(select(ProcessingJob).where(
        ProcessingJob.upload_file_id == file_id, ProcessingJob.record_id == upload_id,
        ProcessingJob.room_id == upload.room_id, ProcessingJob.status == "SUCCEEDED",
    ).order_by(ProcessingJob.id))
    for job in jobs:
        if not processing_result_is_evidence(job):
            continue
        try:
            result = json.loads(job.result_json or "null")
            if isinstance(result, dict) and result.get("source_sha256") == digest:
                return (file.storage_bucket, file.storage_path, file.file_size,
                        file.mime_type, upload.title, upload.room_id,
                        job.id, job.attempt, job.result_json,
                        job.review_json if job.processor in ("ocr", "text") else None)
        except (TypeError, ValueError):
            continue
    raise HTTPException(409, "Original digest unavailable; reprocess the current file")


@router.get("/{upload_id}/files/{file_id}/content")
def get_citation_original(
    upload_id: str, file_id: str,
    expected_sha256: str = Query(..., pattern="^[0-9a-f]{64}$"),
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user),
):
    from app.processing.contracts import ProviderError
    from app.services import media_loader

    user_id = current_user.id
    try:
        snapshot = _original_snapshot(db, upload_id, file_id, expected_sha256, user_id)
    except PrincipalNotPersisted:
        raise HTTPException(401, "Authenticated user not found") from None
    finally:
        db.close()
    bucket, path, size, media_type, title, *_ = snapshot
    if (type(size) is not int or size < 0 or not isinstance(bucket, str)
            or not bucket or not isinstance(path, str) or not path):
        raise HTTPException(409, "Original size unavailable; reprocess the current file")
    try:
        content = media_loader.load_media_bytes(
            bucket, path, record_id=upload_id,
            max_bytes=min(size, max(settings.ocr_max_input_bytes, settings.stt_max_input_bytes, settings.media_video_max_bytes)),
        )
    except (ProviderError, OSError):
        raise HTTPException(404, "Stored original unavailable") from None
    if len(content) != size or hashlib.sha256(content).hexdigest() != expected_sha256:
        raise HTTPException(409, "Original bytes changed; reprocess the current file")
    session = SessionLocal()
    try:
        if _original_snapshot(session, upload_id, file_id, expected_sha256, user_id) != snapshot:
            raise HTTPException(409, "Original source changed")
    except PrincipalNotPersisted:
        raise HTTPException(401, "Authenticated user not found") from None
    finally:
        session.close()
    # Return these verified bytes; never reopen a mutable path during response streaming.
    media_type, headers = media_response_metadata(content, title, media_type)
    return Response(content, media_type=media_type, headers=headers)


def _media_snapshot(db, upload_id, user_id):
    require_persisted_principal(db, user_id)
    from app.services.record_policy import require_record_access
    require_record_access(db, user_id, upload_id)
    upload = db.get(Upload, upload_id)
    if upload is None:
        raise HTTPException(404, "Upload not found")
    ensure_room_member(db, upload.room_id, user_id)
    file = get_latest_upload_file(db, upload_id)
    if file is None:
        raise HTTPException(404, "Upload file not found")
    return (file.id, file.storage_bucket, file.storage_path, file.file_size,
            file.mime_type, upload.title, upload.room_id)


@router.get("/{upload_id}/content")
def get_upload_content(upload_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.processing.contracts import ProviderError
    from app.services.media_loader import load_media_bytes

    user_id = current_user.id
    try:
        snapshot = _media_snapshot(db, upload_id, user_id)
    except PrincipalNotPersisted:
        raise HTTPException(401, "Authenticated user not found") from None
    finally:
        db.close()
    _, bucket, path, size, declared_mime, title, _ = snapshot
    if type(size) is not int or size <= 0:
        raise HTTPException(409, "Original size unavailable")
    try:
        content = load_media_bytes(
            bucket, path, record_id=upload_id,
            max_bytes=min(size, max(settings.ocr_max_input_bytes, settings.stt_max_input_bytes, settings.media_video_max_bytes)),
        )
    except (ProviderError, OSError):
        raise HTTPException(404, "Stored original unavailable") from None
    if len(content) != size:
        raise HTTPException(409, "Original size changed")
    session = SessionLocal()
    try:
        if _media_snapshot(session, upload_id, user_id) != snapshot:
            raise HTTPException(409, "Original source changed")
    except HTTPException as error:
        if error.status_code == 403:
            raise HTTPException(404, "Upload not found") from None
        raise
    except PrincipalNotPersisted:
        raise HTTPException(401, "Authenticated user not found") from None
    finally:
        session.close()
    # Materialize under a bound, then reauthorize before publication; no lazy reopen.
    media_type, headers = media_response_metadata(content, title, declared_mime)
    return Response(content, media_type=media_type, headers=headers)


@router.delete("/{upload_id}")
def delete_upload(upload_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    from app.services.record_lifecycle import legacy_soft_delete
    legacy_soft_delete(db, current_user.id, upload_id)
    db.commit()
    return {
        "deleted": True,
        "upload_id": upload_id,
        "media_cleanup_failed_count": 0,
    }
