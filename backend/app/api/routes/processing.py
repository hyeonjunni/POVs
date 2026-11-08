from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import AuthenticatedPrincipal, get_authenticated_principal
from app.core.database import SessionLocal, get_db
from app.models.auth_identity import PrincipalNotPersisted, require_persisted_principal
from app.models.family import FamilyMember
from app.models.processing_job import PROCESSORS
from app.models.upload import Upload, UploadFile
from app.processing.local_ocr import LocalTesseractOCRProvider
from app.processing.local_stt import LocalWhisperSTTProvider
from app.schemas.processing import (
    OCRReviewRequest, OCRReviewResponse, ProcessingJobCreateRequest, ProcessingJobResponse,
)
from app.services.processing_jobs import (
    IllegalTransition,
    OCRReviewUnsupported,
    read_ocr_review,
    review_ocr_result,
    ProcessingJobNotFound,
    create_job,
    get_job_for_member,
    recover_abandoned_job,
    retry_failed_job,
    serialize_processing_job,
)
from app.services.processing_runner import run_processing_job

router = APIRouter()

ALLOWED_PROCESSORS = set(PROCESSORS)
# Server-decided MIME allow-list per processor — never derived from client input.
PROCESSOR_MIME_PREFIXES = {"ocr": ("image/",), "stt": ("audio/", "video/"), "text": ("text/plain",)}

# Providers are server configuration, never client-supplied (Milestone 1 Task 5).
PROVIDER_REGISTRY = {
    "ocr": LocalTesseractOCRProvider(),
    "stt": LocalWhisperSTTProvider(),
}


def _is_room_member(db: Session, room_id: str, user_id: str) -> bool:
    return (
        db.scalar(
            select(FamilyMember.id).where(
                FamilyMember.room_id == room_id, FamilyMember.user_id == user_id
            ).limit(1)
        )
        is not None
    )


@router.post("/records/{record_id}/processing-jobs", response_model=ProcessingJobResponse)
def create_processing_job(
    record_id: str,
    payload: ProcessingJobCreateRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        require_persisted_principal(db, principal.user_id)
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error

    if payload.processor not in ALLOWED_PROCESSORS:
        raise HTTPException(status_code=400, detail="Unsupported processor")

    record = db.get(Upload, record_id)
    from app.services.record_policy import require_record_access
    require_record_access(db, principal.user_id, record_id)
    if record is None or not _is_room_member(db, record.room_id, principal.user_id):
        # Cross-room / missing records are indistinguishable (no existence leak).
        raise HTTPException(status_code=404, detail="Record not found")

    upload_file = db.get(UploadFile, payload.upload_file_id)
    if upload_file is None or upload_file.upload_id != record_id:
        raise HTTPException(status_code=404, detail="Upload file not found")
    from app.services.storage_quota import source_is_current_active
    if not source_is_current_active(db, upload_file):
        raise HTTPException(status_code=409, detail="Upload file is no longer current")

    allowed_prefixes = PROCESSOR_MIME_PREFIXES[payload.processor]
    if payload.processor == "text" and upload_file.mime_type != "text/plain":
        raise HTTPException(status_code=400, detail="Unsupported text MIME type")
    if not (upload_file.mime_type or "").startswith(allowed_prefixes):
        raise HTTPException(
            status_code=400, detail="Upload file MIME type is not supported by this processor"
        )

    job = create_job(
        db,
        record_id=record_id,
        room_id=record.room_id,
        upload_file_id=payload.upload_file_id,
        processor=payload.processor,
    )
    return ProcessingJobResponse(**serialize_processing_job(job))


@router.get("/processing-jobs/{job_id}", response_model=ProcessingJobResponse)
def get_processing_job(
    job_id: str,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        require_persisted_principal(db, principal.user_id)
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error

    try:
        job = get_job_for_member(db, job_id=job_id, user_id=principal.user_id)
    except ProcessingJobNotFound:
        raise HTTPException(status_code=404, detail="Processing job not found")
    from app.services.multimodal_jobs import visible_result
    value = serialize_processing_job(job)
    value["result"] = visible_result(value["result"], uid=principal.user_id, room_id=job.room_id, db=db)
    return ProcessingJobResponse(**value)


@router.post("/processing-jobs/{job_id}/run", response_model=ProcessingJobResponse)
def run_processing_job_route(
    job_id: str,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
):
    # `SessionLocal` is called fresh for Transaction A and Transaction B — no
    # session stays open across the OCR/STT provider call in between (D5).
    try:
        return run_processing_job(job_id, principal.user_id, PROVIDER_REGISTRY, SessionLocal)
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error
    except ProcessingJobNotFound:
        raise HTTPException(status_code=404, detail="Processing job not found")
    except IllegalTransition as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.post("/processing-jobs/{job_id}/retry", response_model=ProcessingJobResponse)
def retry_processing_job_route(
    job_id: str,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: Session = Depends(get_db),
):
    try:
        require_persisted_principal(db, principal.user_id)
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error

    try:
        job = get_job_for_member(db, job_id=job_id, user_id=principal.user_id)
        from app.services.storage_quota import source_is_current_active
        upload_file = db.get(UploadFile, job.upload_file_id)
        if upload_file is None or not source_is_current_active(db, upload_file):
            raise IllegalTransition("Processing source is no longer current")
        recover_abandoned_job(db, job_id)
        job = retry_failed_job(db, job_id)
    except ProcessingJobNotFound:
        raise HTTPException(status_code=404, detail="Processing job not found")
    except IllegalTransition as error:
        raise HTTPException(status_code=409, detail=str(error))
    return ProcessingJobResponse(**serialize_processing_job(job))


def _ocr_review(db, principal, job_id, action=None):
    try:
        require_persisted_principal(db, principal.user_id)
        if action is None:
            return read_ocr_review(db, job_id, principal.user_id)
        return review_ocr_result(db, job_id, principal.user_id, action)
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error
    except ProcessingJobNotFound:
        raise HTTPException(status_code=404, detail="Processing job not found")
    except OCRReviewUnsupported as error:
        raise HTTPException(status_code=400, detail=str(error))
    except IllegalTransition as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.post("/processing-jobs/{job_id}/review", response_model=OCRReviewResponse)
def review_processing_job(job_id: str, payload: OCRReviewRequest,
                          principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
                          db: Session = Depends(get_db)):
    return _ocr_review(db, principal, job_id, payload.action)


@router.get("/processing-jobs/{job_id}/review", response_model=OCRReviewResponse)
def get_processing_job_review(job_id: str,
                             principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
                             db: Session = Depends(get_db)):
    return _ocr_review(db, principal, job_id)
