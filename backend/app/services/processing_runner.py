"""Two-transaction OCR/STT job runner (FOUNDATION_IDENTITY_MIGRATION_DECISION.md D5).

Transaction A authenticates room membership and copies a `ProcessingInput`,
then closes. The OCR/STT provider call and the storage read both run with no
DB session open. Transaction B persists the result or a safe, typed failure.
"""
from __future__ import annotations

import hashlib
import logging
import asyncio
from dataclasses import asdict
from typing import Callable, Mapping

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.auth_identity import require_persisted_principal
from app.processing.contracts import ProviderError
from app.schemas.processing import ProcessingJobResponse
from app.services import media_loader
from app.services.processing_jobs import (
    IllegalTransition,
    get_job_for_member,
    load_processing_input,
    mark_failed,
    mark_running,
    mark_succeeded,
    serialize_processing_job,
)

logger = logging.getLogger(__name__)


async def run_secure_provider(runtime, guard, processor, content, mime_type):
    """Exact synthetic providers or explicitly admitted trusted DGX OCR only."""
    from app.processing.native_text import extract_native_text
    if processor == "text":
        return extract_native_text(content, mime_type)
    from app.processing.synthetic import SyntheticOCRProvider, SyntheticSTTProvider
    from app.services.secure_runtime import RuntimeFailStop
    provider = getattr(runtime, "processing_providers", {}).get(processor)
    allowed = {"ocr": SyntheticOCRProvider, "stt": SyntheticSTTProvider}
    if provider is None:
        raise ProviderError("processor_not_configured", "Processing is unavailable.")
    from app.services.secure_local_providers import LocalOCR
    local = (processor == "ocr" and getattr(runtime, "local_provider_admission", False) is True
             and type(provider) is LocalOCR)
    if type(provider) is not allowed.get(processor) and not local:
        raise ProviderError("provider_forbidden", "Processing is unavailable.")
    operation = provider.extract(content, mime_type) if processor == "ocr" else provider.transcribe(content, mime_type, language="ko")
    task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait((task,), timeout=30)
        if not done:
            raise ProviderError("provider_timeout", "Processing timed out.")
        if task.cancelled():
            raise ProviderError("cancelled", "Processing was cancelled.")
        result = asdict(task.result())
        result.update(extractor="AUTO_OCR" if processor == "ocr" else "AUTO_STT", extractor_id=provider.extractor_id,
                      requested_language="ko", user_confirmed=False, review_status="unreviewed")
        return result
    finally:
        if not task.done():
            task.cancel()
        cleanup = asyncio.create_task(asyncio.wait((task,), timeout=1))
        cancelled = False
        while True:
            try:
                _, alive = await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                cancelled = True
        if alive:
            guard.poisoned = True
            runtime.fail_stop()
            raise RuntimeFailStop()
        if not task.cancelled():
            task.exception()
        if cancelled:
            raise asyncio.CancelledError()


def run_processing_job(
    job_id: str,
    user_id: str,
    provider_registry: Mapping[str, object],
    session_factory: Callable[[], Session],
    *, multimodal: bool = False,
) -> ProcessingJobResponse:
    # Transaction A: authenticate + copy inputs, then close.
    db = session_factory()
    try:
        require_persisted_principal(db, user_id)   # A6: before any ACL read
        processing_input = load_processing_input(db, job_id=job_id, user_id=user_id)
        mark_running(db, job_id)
    finally:
        db.close()

    # Slow phase — no DB session open here.
    failure_code: str | None = None
    failure_message: str | None = None
    result_payload: dict | None = None
    try:
        max_bytes = {"ocr": settings.ocr_max_input_bytes, "stt": settings.stt_max_input_bytes,
                     "text": settings.text_max_input_bytes}[processing_input.processor]
        if multimodal:
            max_bytes = {"image": settings.media_image_max_bytes, "audio": settings.media_audio_max_bytes,
                         "video": settings.media_video_max_bytes}[processing_input.mime_type.split("/")[0]]
        content = media_loader.load_media_bytes(
            processing_input.storage_bucket,
            processing_input.storage_path,
            record_id=processing_input.record_id,
            max_bytes=max_bytes,
        )
        # Magic-number family must agree with the recorded MIME before any provider runs.
        family = media_loader.media_family(content)
        top_level = (processing_input.mime_type or "").split("/", 1)[0]
        if processing_input.processor != "text" and (family is None or top_level not in family):
            raise ProviderError(
                "media_content_mismatch",
                "Stored media content does not match its recorded type.",
            )

        if multimodal:
            from app.services.multimodal_jobs import extract_configured
            result_payload = extract_configured(content, processing_input.mime_type,
                attempt_id=f"{job_id}:{processing_input.attempt}")
            extractor_id = result_payload["extractor_id"]
        elif processing_input.processor == "text":
            from app.processing.native_text import EXTRACTOR_ID, extract_native_text
            result_payload = dict(extract_native_text(content, processing_input.mime_type))
            extractor_id = EXTRACTOR_ID
        else:
            provider = provider_registry.get(processing_input.processor)
            if provider is None:
                raise ProviderError(
                    "processor_not_configured",
                    f"No provider is configured for '{processing_input.processor}'.",
                )
            extractor_id = getattr(provider, "extractor_id", type(provider).__name__)

            if processing_input.processor == "ocr":
                ocr_result = provider.extract(content, processing_input.mime_type)
                result_payload = {
                    "text": ocr_result.text,
                    "confidence": ocr_result.confidence,
                    "language": ocr_result.language,
                    "regions": [
                        {
                            "text": region.text,
                            "confidence": region.confidence,
                            "left": region.left,
                            "top": region.top,
                            "width": region.width,
                            "height": region.height,
                        }
                        for region in ocr_result.regions
                    ],
                    "extractor": "AUTO_OCR",
                    "requested_language": getattr(provider, "language", None),
                }
            else:
                stt_result = provider.transcribe(
                    content, processing_input.mime_type, language=settings.stt_language
                )
                result_payload = {
                    "text": stt_result.text,
                    "language": stt_result.language,
                    "segments": [
                        {
                            "text": segment.text,
                            "start_seconds": segment.start_seconds,
                            "end_seconds": segment.end_seconds,
                        }
                        for segment in stt_result.segments
                    ],
                    "extractor": "AUTO_STT",
                    "requested_language": settings.stt_language,
                }
        result_payload["source_sha256"] = hashlib.sha256(content).hexdigest()
        result_payload["extractor_id"] = extractor_id
        # Provenance: automated extraction is never presented as already-reviewed.
        result_payload["user_confirmed"] = False
        result_payload["review_status"] = "unreviewed"
    except ProviderError as error:
        failure_code, failure_message = error.code, error.message
    except Exception as error:
        logger.warning(
            "processing job %s failed unexpectedly: %s", job_id, type(error).__name__
        )
        failure_code, failure_message = (
            "unexpected_error",
            "Processing failed unexpectedly.",
        )

    # Transaction B: attempt-bound persist; stale work cannot overwrite a retry.
    db = session_factory()
    try:
        try:
            if multimodal:
                current_input = load_processing_input(db, job_id=job_id, user_id=user_id)
                if current_input != processing_input:
                    raise IllegalTransition("Processing source changed")
            if failure_code is not None:
                job = mark_failed(
                    db,
                    job_id,
                    code=failure_code,
                    message=failure_message or "Processing failed.",
                    attempt=processing_input.attempt,
                )
            else:
                job = mark_succeeded(
                    db,
                    job_id,
                    result=result_payload or {},
                    attempt=processing_input.attempt,
                )
        except IllegalTransition:
            job = get_job_for_member(db, job_id=job_id, user_id=user_id)
        return ProcessingJobResponse(**serialize_processing_job(job))
    finally:
        db.close()
