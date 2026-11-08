from typing import Annotated

"""Additive M4 routes; shared schemas, separately authorized storage paths."""
from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import AuthenticatedPrincipal, get_authenticated_principal
from app.core.config import settings
from app.schemas.multimodal import MediaReviewRequest
from app.schemas.processing import ProcessingJobResponse

router = APIRouter()


@router.post("/processing-jobs/{job_id}/multimodal/run", response_model=ProcessingJobResponse)
async def run_multimodal(job_id: str, request: Request,
                         principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]):
    if settings.security_mode == "SECURE":
        from app.api.routes import secure_media as media
        from app.api.routes.secure_operations import tx
        from app.services.multimodal_jobs import _authority
        uid = media.principal(request)
        with tx() as db:
            _, row, _, _ = media.job_source(db, uid, job_id)
            _authority(db, uid, row["record_id"], row["upload_file_id"])
            from app.services.record_service import idempotency
            replay = idempotency(db, request, "{}", job_id)
            if row["processor"] not in ("ocr", "stt"):
                raise HTTPException(422, "Multimodal processing requires media")
        if replay:
            return media.read_job(request.app.state.secure_runtime, uid, job_id)
        request.scope["m4_processing"] = True
        return await media.run_job(job_id, request)
    from app.core.database import SessionLocal
    from app.models.auth_identity import require_persisted_principal
    from app.services.multimodal_jobs import _authority
    from app.services.processing_jobs import (
        IllegalTransition,
        ProcessingJobNotFound,
        get_job_for_member,
    )
    from app.services.processing_runner import run_processing_job
    try:
        with SessionLocal() as db:
            require_persisted_principal(db, principal.user_id)
            job = get_job_for_member(db, job_id=job_id, user_id=principal.user_id)
            _authority(db, principal.user_id, job.record_id, job.upload_file_id)
            from app.services.record_service import idempotency
            replay = idempotency(db, request, "{}", job_id)
            if replay:
                from app.services.multimodal_jobs import legacy_visible_job
                return legacy_visible_job(db, principal.user_id, job)
            if job.processor not in ("ocr", "stt"):
                raise HTTPException(422, "Multimodal processing requires media")
        result = run_processing_job(job_id, principal.user_id, {}, SessionLocal, multimodal=True)
        from app.services.record_service import save_idempotency
        with SessionLocal() as db:
            get_job_for_member(db, job_id=job_id, user_id=principal.user_id)
            if not idempotency(db, request, "{}", job_id):
                save_idempotency(db, request, "{}", job_id, job_id)
                db.commit()
        return result
    except ProcessingJobNotFound:
        raise HTTPException(404, "Resource not available") from None
    except IllegalTransition:
        raise HTTPException(409, "Processing state changed") from None


@router.post("/processing-jobs/{job_id}/multimodal/review", response_model=ProcessingJobResponse)
async def review_multimodal(job_id: str, payload: MediaReviewRequest, request: Request,
                            principal: Annotated[AuthenticatedPrincipal, Depends(get_authenticated_principal)]):
    from app.services.multimodal_jobs import legacy_review, secure_review
    if settings.security_mode == "SECURE":
        from app.api.routes.secure_operations import principal as secure_principal
        return secure_review(request.app.state.secure_runtime, secure_principal(request), job_id, payload, request)
    from app.core.database import SessionLocal
    from app.models.auth_identity import require_persisted_principal
    from app.services.processing_jobs import ProcessingJobNotFound
    try:
        with SessionLocal() as db:
            require_persisted_principal(db, principal.user_id)
            return legacy_review(db, principal.user_id, job_id, payload, request)
    except ProcessingJobNotFound:
        raise HTTPException(404, "Resource not available") from None
