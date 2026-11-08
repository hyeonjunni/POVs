"""Room-scoped OCR/STT processing-job state machine."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.family import FamilyMember
from app.models.processing_job import STATUSES, ProcessingJob
from app.models.upload import UploadFile

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"RUNNING"},
    "RUNNING": {"SUCCEEDED", "FAILED"},
    "FAILED": {"PENDING"},
    "SUCCEEDED": set(),
}
assert set(LEGAL_TRANSITIONS) == set(STATUSES)


class ProcessingJobNotFound(LookupError):
    """Missing and out-of-room jobs are deliberately indistinguishable."""


class IllegalTransition(ValueError):
    """Requested state transition lost its compare-and-set guard."""


@dataclass(frozen=True)
class ProcessingInput:
    """Detached inputs for one attempt's slow phase."""

    job_id: str
    record_id: str
    room_id: str
    upload_file_id: str
    storage_bucket: str
    storage_path: str
    mime_type: str
    processor: str
    attempt: int


ABANDONED_AFTER = timedelta(minutes=10)


def _is_room_member(db: Session, room_id: str, user_id: str) -> bool:
    return (
        db.scalar(
            select(FamilyMember.id)
            .where(FamilyMember.room_id == room_id, FamilyMember.user_id == user_id,
                   FamilyMember.is_active.is_(True))
            .limit(1)
        )
        is not None
    )


def _find_job(
    db: Session, *, record_id: str, upload_file_id: str, processor: str
) -> ProcessingJob | None:
    return db.scalar(
        select(ProcessingJob).where(
            ProcessingJob.record_id == record_id,
            ProcessingJob.upload_file_id == upload_file_id,
            ProcessingJob.processor == processor,
        )
    )


def create_job(
    db: Session, *, record_id: str, room_id: str, upload_file_id: str, processor: str
) -> ProcessingJob:
    from app.services.storage_quota import _lock_room
    _lock_room(db, room_id)
    job = ProcessingJob(
        id=str(uuid.uuid4()),
        record_id=record_id,
        upload_file_id=upload_file_id,
        room_id=room_id,
        processor=processor,
        status="PENDING",
        attempt=0,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _find_job(
            db,
            record_id=record_id,
            upload_file_id=upload_file_id,
            processor=processor,
        )
        if existing is None:
            raise
        return existing
    db.refresh(job)
    return job


def get_job_for_member(db: Session, *, job_id: str, user_id: str) -> ProcessingJob:
    job = db.get(ProcessingJob, job_id)
    if job is None or not _is_room_member(db, job.room_id, user_id):
        raise ProcessingJobNotFound("processing job not found")
    from app.services.record_policy import RecordAccessDenied, require_record_access
    try:
        require_record_access(db, user_id, job.record_id)
    except RecordAccessDenied as error:
        raise ProcessingJobNotFound("processing job not found") from error
    return job


def load_processing_input(db: Session, *, job_id: str, user_id: str) -> ProcessingInput:
    job = get_job_for_member(db, job_id=job_id, user_id=user_id)
    upload_file = db.get(UploadFile, job.upload_file_id)
    if upload_file is None:
        raise ProcessingJobNotFound("processing job not found")
    from app.services.storage_quota import source_is_current_active
    if not source_is_current_active(db, upload_file):
        raise IllegalTransition("Processing source is no longer current")
    return ProcessingInput(
        job_id=job.id,
        record_id=job.record_id,
        room_id=job.room_id,
        upload_file_id=job.upload_file_id,
        storage_bucket=upload_file.storage_bucket,
        storage_path=upload_file.storage_path,
        mime_type=upload_file.mime_type,
        processor=job.processor,
        attempt=job.attempt,
    )


def _lock_job_room(db: Session, job_id: str) -> None:
    from app.services.storage_quota import _lock_room, StorageAllocationStateChanged
    room_id = db.scalar(select(ProcessingJob.room_id).where(ProcessingJob.id == job_id))
    if room_id is None:
        raise ProcessingJobNotFound("processing job not found")
    try:
        _lock_room(db, room_id)
    except StorageAllocationStateChanged as error:
        raise ProcessingJobNotFound("processing job not found") from error


def _cas(
    db: Session,
    job_id: str,
    *,
    expected_status: str,
    expected_attempt: int | None,
    target: str,
    **values,
) -> ProcessingJob:
    if target not in LEGAL_TRANSITIONS[expected_status]:
        raise IllegalTransition(
            f"cannot move processing job from {expected_status} to {target}"
        )
    _lock_job_room(db, job_id)
    statement = (
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.status == expected_status)
        .values(status=target, updated_at=datetime.now(timezone.utc), **values)
    )
    if expected_attempt is not None:
        statement = statement.where(ProcessingJob.attempt == expected_attempt)
    rowcount = db.execute(statement).rowcount
    db.commit()
    db.expire_all()
    job = db.get(ProcessingJob, job_id)
    if job is None:
        raise ProcessingJobNotFound("processing job not found")
    if rowcount != 1:
        raise IllegalTransition(f"cannot move processing job from {job.status} to {target}")
    return job


def mark_running(db: Session, job_id: str) -> ProcessingJob:
    return _cas(
        db,
        job_id,
        expected_status="PENDING",
        expected_attempt=None,
        target="RUNNING",
        started_at=datetime.now(timezone.utc),
        completed_at=None,
    )


def mark_succeeded(
    db: Session, job_id: str, *, result: dict, attempt: int | None = None
) -> ProcessingJob:
    result_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
    return _cas(
        db,
        job_id,
        expected_status="RUNNING",
        expected_attempt=attempt,
        target="SUCCEEDED",
        result_json=result_json,
        failure_code=None,
        failure_message=None,
        completed_at=datetime.now(timezone.utc),
    )


def mark_failed(
    db: Session,
    job_id: str,
    *,
    code: str,
    message: str,
    attempt: int | None = None,
) -> ProcessingJob:
    return _cas(
        db,
        job_id,
        expected_status="RUNNING",
        expected_attempt=attempt,
        target="FAILED",
        failure_code=code,
        failure_message=message,
        completed_at=datetime.now(timezone.utc),
    )


def retry_failed_job(db: Session, job_id: str) -> ProcessingJob:
    return _cas(
        db,
        job_id,
        expected_status="FAILED",
        expected_attempt=None,
        target="PENDING",
        attempt=ProcessingJob.attempt + 1,
        failure_code=None,
        failure_message=None,
        started_at=None,
        completed_at=None,
    )


def recover_abandoned_job(
    db: Session, job_id: str, *, older_than: timedelta = ABANDONED_AFTER
) -> bool:
    try:
        _lock_job_room(db, job_id)
    except ProcessingJobNotFound:
        return False
    cutoff = datetime.now(timezone.utc) - older_than
    now = datetime.now(timezone.utc)
    rowcount = db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status == "RUNNING",
            or_(ProcessingJob.started_at.is_(None), ProcessingJob.started_at < cutoff),
        )
        .values(
            status="FAILED",
            failure_code="abandoned",
            failure_message="Processing run was abandoned.",
            completed_at=now,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    db.expire_all()
    return rowcount == 1


def processing_result_is_evidence(job: ProcessingJob) -> bool:
    """Require current uploader approval for OCR; review writers enforce identity."""
    return _processing_result_is_valid(job, require_ocr_review=True)


def _processing_result_is_valid(job: ProcessingJob, *, require_ocr_review: bool) -> bool:
    from app.retrieval.chunking import canonical_json, sha256_text

    if job.status != "SUCCEEDED":
        return False
    try:
        result = json.loads(job.result_json or "null")
        if not isinstance(result, dict):
            return False
        json.dumps(result, allow_nan=False)
        if "m4" in result:
            from app.processing.multimodal import eligible
            if not require_ocr_review:
                return True
            if not eligible(result):
                return False
        if "m4" not in result and job.processor not in ("ocr", "stt", "text"):
            return True
        if job.review_json is None:
            return not require_ocr_review
        review = json.loads(job.review_json)
        return (isinstance(review, dict)
                and set(review) == {"status", "source_attempt", "source_result_sha256"}
                and review["status"] == "confirmed"
                and type(review["source_attempt"]) is int
                and review["source_attempt"] == job.attempt
                and review["source_result_sha256"] == sha256_text(canonical_json(result)))
    except (ValueError, TypeError):
        return False


def serialize_processing_job(job: ProcessingJob) -> dict:
    # Unreviewed extraction remains visible for review, but is not evidence.
    return {
        "job_id": job.id,
        "record_id": job.record_id,
        "room_id": job.room_id,
        "upload_file_id": job.upload_file_id,
        "processor": job.processor,
        "status": job.status,
        "attempt": job.attempt,
        "result": json.loads(job.result_json) if _processing_result_is_valid(job, require_ocr_review=False) else None,
        "failure_code": job.failure_code,
        "failure_message": job.failure_message,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


class OCRReviewUnsupported(ValueError):
    pass


def _review_snapshot(db: Session, job_id: str, user_id: str, *, mutation: bool):
    from app.models.upload import Upload
    from app.retrieval.chunking import canonical_json, sha256_text

    job = get_job_for_member(db, job_id=job_id, user_id=user_id)
    record = db.get(Upload, job.record_id)
    file = db.get(UploadFile, job.upload_file_id)
    if record is None or file is None or record.room_id != job.room_id or file.upload_id != record.id:
        raise ProcessingJobNotFound("Processing job not found")
    if mutation:
        if file.file_uploader_user_id is None:
            raise IllegalTransition("Actual file uploader is unknown")
        if file.file_uploader_user_id != user_id:
            raise ProcessingJobNotFound("Processing job not found")
    if job.processor not in ("ocr", "stt", "text"):
        raise OCRReviewUnsupported("Only extraction results can be reviewed")
    latest = db.scalar(select(UploadFile.id).where(UploadFile.upload_id == record.id)
                       .order_by(UploadFile.created_at.desc(), UploadFile.id.desc()).limit(1))
    if latest != file.id or job.status != "SUCCEEDED":
        raise IllegalTransition("OCR result is incomplete or superseded")
    try:
        result = json.loads(job.result_json or "null")
        if (not isinstance(result, dict) or not isinstance(result.get("text"), str)
                or result.get("extractor") != {"ocr": "AUTO_OCR", "stt": "AUTO_STT", "text": "NATIVE_TEXT"}[job.processor]
                or not isinstance(result.get("extractor_id"), str) or not result["extractor_id"].strip()):
            raise ValueError()
        json.dumps(result, allow_nan=False)
        digest = sha256_text(canonical_json(result))
        review = json.loads(job.review_json) if job.review_json is not None else None
        if review is not None and (not isinstance(review, dict)
                or set(review) != {"status", "source_attempt", "source_result_sha256"}
                or type(review.get("source_attempt")) is not int
                or review.get("status") not in ("confirmed", "rejected")
                or review.get("source_attempt") != job.attempt
                or review.get("source_result_sha256") != digest):
            raise ValueError()
    except (ValueError, TypeError):
        raise IllegalTransition("OCR result or review source is invalid") from None
    return job, review, digest


def read_ocr_review(db: Session, job_id: str, user_id: str) -> dict:
    job, review, digest = _review_snapshot(db, job_id, user_id, mutation=False)
    status = review["status"] if review else "unreviewed"
    return dict(job_id=job.id, review_status=status, user_confirmed=status == "confirmed",
                source_attempt=job.attempt, source_result_sha256=digest)


def review_ocr_result(db: Session, job_id: str, user_id: str, action: str) -> dict:
    from app.models.upload import Upload
    from app.retrieval.chunking import canonical_json

    before, _, before_digest = _review_snapshot(db, job_id, user_id, mutation=True)
    before_attempt = before.attempt
    _lock_job_room(db, job_id)
    db.expire_all()
    job, review, digest = _review_snapshot(db, job_id, user_id, mutation=True)
    if (job.attempt, digest) != (before_attempt, before_digest):
        raise IllegalTransition("OCR review source changed")
    status = {"approve": "confirmed", "reject": "rejected"}[action]
    if review is not None and review["status"] != status:
        raise IllegalTransition("OCR review is already final")
    source_attempt = job.attempt
    old_review = job.review_json
    new_review = canonical_json(dict(status=status, source_attempt=job.attempt,
                                    source_result_sha256=digest))
    latest = select(UploadFile.id).where(UploadFile.upload_id == job.record_id).order_by(
        UploadFile.created_at.desc(), UploadFile.id.desc()).limit(1).scalar_subquery()
    member = select(FamilyMember.id).where(
        FamilyMember.room_id == job.room_id, FamilyMember.user_id == user_id).exists()
    file = select(UploadFile.id).where(
        UploadFile.id == job.upload_file_id, UploadFile.upload_id == job.record_id,
        UploadFile.file_uploader_user_id == user_id).exists()
    record = select(Upload.id).where(Upload.id == job.record_id, Upload.room_id == job.room_id).exists()
    written = db.execute(update(ProcessingJob).where(
        ProcessingJob.id == job.id, ProcessingJob.record_id == job.record_id,
        ProcessingJob.room_id == job.room_id, ProcessingJob.upload_file_id == job.upload_file_id,
        ProcessingJob.status == "SUCCEEDED", ProcessingJob.processor == job.processor,
        ProcessingJob.attempt == job.attempt, ProcessingJob.result_json == job.result_json,
        ProcessingJob.review_json == old_review if old_review is not None else ProcessingJob.review_json.is_(None),
        member, file, record, ProcessingJob.upload_file_id == latest,
    ).values(review_json=new_review).execution_options(synchronize_session=False)).rowcount
    if written == 1 and status == "rejected":
        from app.models.retrieval import RecordChunk, RecordEmbedding

        chunk_ids = select(RecordChunk.id).where(RecordChunk.processing_job_id == job.id)
        db.execute(delete(RecordEmbedding).where(RecordEmbedding.chunk_id.in_(chunk_ids)))
        db.execute(delete(RecordChunk).where(RecordChunk.processing_job_id == job.id))
    db.commit()
    db.expire_all()
    fresh, current_review, current_digest = _review_snapshot(db, job_id, user_id, mutation=True)
    if written != 1 and (current_digest != digest or fresh.attempt != source_attempt
                         or current_review is None or current_review["status"] != status):
        raise IllegalTransition("OCR review source changed")
    return read_ocr_review(db, job_id, user_id)
