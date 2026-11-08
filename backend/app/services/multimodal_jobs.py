"""M4 provider selection and source-bound persistence using existing job envelopes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select, text, update

from app.processing.multimodal import (
    SyntheticImageOCR,
    SyntheticSpeechProvider,
    SyntheticVideoDecoder,
    SyntheticVisualProvider,
    extract,
    review,
)
from app.processing.multimodal_local import LocalImageOCR, LocalMediaDecoder
from app.retrieval.chunking import canonical_json, sha256_text
from app.schemas.multimodal import MediaReviewRequest


def extract_configured(content: bytes, mime: str, *, runtime=None, attempt_id: str = "") -> dict[str, Any]:
    """Operator-selected local adapters, after caller authorization/materialization."""
    from app.core.config import settings
    if settings.secure_provider_profile == "synthetic":
        return extract(content, mime, ocr=SyntheticImageOCR(), visual=SyntheticVisualProvider(),
                       speech=SyntheticSpeechProvider(),
                       decoder=SyntheticVideoDecoder() if mime == "video/mp4" else None)
    if runtime is not None and not getattr(runtime, "local_provider_admission", False):
        from app.processing.contracts import ProviderError
        raise ProviderError("UNSUPPORTED_MEDIA", "Local media processing unavailable.")
    root = Path(settings.secure_provider_temp_directory)
    from app.processing.contracts import ProviderError
    from app.processing.multimodal_providers import (
        OllamaVisualProvider,
        PCMDecoder,
        TransformersWhisperProvider,
    )
    if settings.secure_provider_profile != "trusted-dgx-local":
        raise ProviderError("UNSUPPORTED_MEDIA", "Local media processing unavailable.")
    if mime.startswith("image/"):
        if settings.media_visual_provider not in ("none", "ollama"):
            raise ProviderError("VLM_ERROR", "Unsupported visual provider.")
        visual = (OllamaVisualProvider(settings, root, attempt_id)
                  if settings.media_visual_provider == "ollama" else None)
        return extract(content, mime, ocr=LocalImageOCR(root), visual=visual)
    if mime.startswith("audio/"):
        if settings.media_speech_provider not in ("none", "transformers_whisper"):
            raise ProviderError("STT_ERROR", "Unsupported speech provider.")
        speech = (TransformersWhisperProvider(settings, root, attempt_id)
                  if settings.media_speech_provider == "transformers_whisper" else None)
        return extract(content, mime, speech=speech, decoder=PCMDecoder())
    # M4 video foundation remains unchanged; codec selection is a separate gate.
    return extract(content, mime, ocr=LocalImageOCR(root), decoder=LocalMediaDecoder(root))


def review_digest(value: dict[str, Any], attempt: int, action: str) -> str:
    return canonical_json({"status": "rejected" if action == "reject" else "confirmed",
        "source_attempt": attempt, "source_result_sha256": sha256_text(canonical_json(value))})


def _authority(db, uid: str, record_id: str, file_id: str):
    from app.services.record_policy import require_record_access
    require_record_access(db, uid, record_id)
    row = db.execute(text("SELECT r.owner_member_id, m.id AS viewer_member_id, f.file_uploader_user_id "
        "FROM records r JOIN family_members m ON m.room_id=r.room_id AND m.user_id=:u AND m.is_active=1 "
        "JOIN upload_files f ON f.upload_id=r.id AND f.id=:f WHERE r.id=:r"),
        {"u": uid, "f": file_id, "r": record_id}).mappings().first()
    if row is None or (row["owner_member_id"] != row["viewer_member_id"] and row["file_uploader_user_id"] != uid):
        raise HTTPException(404, "Resource not available")


def _validate_members(db, room_id: str, request: MediaReviewRequest):
    ids = set(request.metadata.visible_member_ids)
    if request.metadata.speaker_member_id:
        ids.add(request.metadata.speaker_member_id)
    for mid in ids:
        if db.execute(text("SELECT id FROM family_members WHERE id=:m AND room_id=:r AND is_active=1"),
                      {"m": str(mid), "r": room_id}).first() is None:
            raise HTTPException(404, "Resource not available")


def _transform(result, payload, attempt):
    if not isinstance(result, dict) or "m4" not in result:
        raise HTTPException(409, "Multimodal result is not available")
    try:
        return review(result, payload, attempt)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None



def review_precondition(db, request, uid, record_id, payload):
    from app.services.record_policy import require_record_access
    from app.services.record_service import idempotency
    row = require_record_access(db, uid, record_id, purpose="manage")
    if idempotency(db, request, payload.model_dump_json(), record_id):
        return True
    expected = request.headers.get("if-match")
    if expected is None:
        raise HTTPException(428, {"code": "PRECONDITION_REQUIRED"})
    if expected != f'"{row["record_version"]}"':
        raise HTTPException(412, {"code": "REVISION_CONFLICT"})
    return False

def legacy_review(db, uid: str, job_id: str, payload: MediaReviewRequest, request):
    from app.models.processing_job import ProcessingJob
    from app.models.retrieval import RecordChunk, RecordEmbedding
    from app.models.upload import UploadFile
    from app.services.processing_jobs import (
        _lock_job_room,
        get_job_for_member,
    )
    from app.services.storage_quota import source_is_current_active
    job = get_job_for_member(db, job_id=job_id, user_id=uid)
    _authority(db, uid, job.record_id, job.upload_file_id)
    _lock_job_room(db, job_id)
    db.expire_all()
    job = get_job_for_member(db, job_id=job_id, user_id=uid)
    _authority(db, uid, job.record_id, job.upload_file_id)
    file = db.get(UploadFile, job.upload_file_id)
    if file is None or not source_is_current_active(db, file) or job.status != "SUCCEEDED":
        raise HTTPException(409, "STALE_SOURCE")
    _validate_members(db, job.room_id, payload)
    if payload.metadata.event_id is not None:
        from app.services.family_context import entity_ids
        if str(payload.metadata.event_id) not in db.scalars(entity_ids(db, uid, job.room_id, "EVENT")):
            raise HTTPException(404, "Resource not available")
    if review_precondition(db, request, uid, job.record_id, payload):
        return legacy_visible_job(db, uid, job)
    from app.services.record_lifecycle import begin_mutation, finish_mutation
    mutation, _ = begin_mutation(db, request, job.record_id, payload.model_dump_json())
    old = job.result_json
    value = _transform(json.loads(old or "null"), payload, job.attempt)
    changed = db.execute(update(ProcessingJob).where(ProcessingJob.id == job_id,
        ProcessingJob.result_json == old, ProcessingJob.attempt == job.attempt,
        ProcessingJob.status == "SUCCEEDED").values(result_json=canonical_json(value),
        review_json=review_digest(value, job.attempt, payload.action))).rowcount
    if changed != 1:
        db.rollback()
        raise HTTPException(409, "STALE_SOURCE")
    chunks = select(RecordChunk.id).where(RecordChunk.processing_job_id == job_id)
    db.execute(delete(RecordEmbedding).where(RecordEmbedding.chunk_id.in_(chunks)))
    db.execute(delete(RecordChunk).where(RecordChunk.processing_job_id == job_id))
    finish_mutation(db, request, mutation, action="M4_REVIEW_" + payload.action.upper(),
        reason="multimodal review", payload=payload.model_dump_json())
    db.commit()
    db.expire_all()
    return legacy_visible_job(db, uid, get_job_for_member(db, job_id=job_id, user_id=uid))


def secure_review(runtime, uid: str, job_id: str, payload: MediaReviewRequest, request):
    from app.api.routes import secure_media as media
    from app.api.routes.secure_operations import raw_json, tx
    from app.services import secure_catalogue as cat
    with tx() as db:
        saved, row, source, values = media.job_source(db, uid, job_id)
        _authority(db, uid, row["record_id"], row["upload_file_id"])
        _validate_members(db, row["room_id"], payload)
        replay = review_precondition(db, request, uid, row["record_id"], payload)
    if replay:
        return media.read_job(runtime, uid, job_id)
    if payload.metadata.event_id is not None:
        from app.services.secure_family_context import query
        if str(payload.metadata.event_id) not in query(runtime, uid, row["room_id"], "accessible_event_ids"):
            raise HTTPException(404, "Resource not available")
    if row["status"] != "SUCCEEDED" or values is None:
        raise HTTPException(409, "STALE_SOURCE")
    if source[3] is None:
        raise HTTPException(409, "STALE_SOURCE")
    media.record_payload(runtime, source)
    envelope = json.loads(media.open_bytes(runtime, values))
    media.validate_result_binding(runtime, row, source, envelope)
    value = _transform(envelope["result"], payload, row["attempt"])
    with tx() as db:
        cat.revalidate(db, saved)
        if dict(cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=job_id)) != row:
            raise cat.SecureStateChanged()
        obj = uuid4()
        saved = cat.claim(db, saved, operation_id=uuid4(), target=("job", UUID(job_id)), candidate_object_id=obj)
    staged = media.stage(runtime, uid, ("job", UUID(job_id)),
        raw_json(envelope | {"result": value}), dependencies=tuple(p[:2] for p in source[0].pointers),
        source=(source[3][0].object_id, source[3][0].object_version), claimed=(saved, obj))
    try:
        with tx() as db:
            cat.lock_rooms(db, (row["room_id"],))
            _authority(db, uid, row["record_id"], row["upload_file_id"])
            if dict(cat._row(db, "SELECT * FROM processing_jobs WHERE id=:j", j=job_id)) != row:
                raise cat.SecureStateChanged()
            from app.services.record_lifecycle import begin_mutation, finish_mutation
            mutation, replay = begin_mutation(db, request, row["record_id"], payload.model_dump_json())
            if replay:
                raise cat.SecureStateChanged()
            cat._cas(db, "UPDATE processing_jobs SET review_json=:v WHERE id=:j AND attempt=:a AND status='SUCCEEDED'",
                v=review_digest(value, row["attempt"], payload.action), j=job_id, a=row["attempt"])
            media.invalidate_pointers(db, [("record_index", row["record_id"])])
            cat.publish_many(db, (staged,))
            finish_mutation(db, request, mutation, action="M4_REVIEW_" + payload.action.upper(),
                reason="multimodal review", payload=payload.model_dump_json())
    except BaseException:
        media.abandon_staged([staged])
        raise
    return media.read_job(runtime, uid, job_id)


def visible_result(result, *, uid, room_id, db=None, runtime=None):
    """A record grant does not grant access to a separately sourced M3 event."""
    import copy
    if not isinstance(result, dict) or "m4" not in result:
        return result
    metadata = result["m4"].get("trusted_metadata", {})
    event_id = metadata.get("event_id")
    if event_id is None:
        return result
    if runtime is not None:
        from app.services.secure_family_context import query
        allowed = query(runtime, uid, room_id, "accessible_event_ids")
    else:
        if db is None:
            raise ValueError("Authorization session is required")
        from app.services.family_context import accessible_event_ids
        allowed = accessible_event_ids(db, uid, room_id)
    if event_id in allowed:
        return result
    filtered = copy.deepcopy(result)
    filtered["m4"]["trusted_metadata"]["event_id"] = None
    # Preserve an opaque review CAS for authorized reviewers of a redacted projection.
    filtered["m4"]["review_source_sha256"] = sha256_text(canonical_json(result))
    return filtered


def legacy_visible_job(db, uid, job):
    from app.services.processing_jobs import serialize_processing_job
    value = serialize_processing_job(job)
    value["result"] = visible_result(value["result"], uid=uid, room_id=job.room_id, db=db)
    return value
