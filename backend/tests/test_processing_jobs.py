"""Processing-job state machine tests (Milestone 1 Task 4)."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine, postgres_url_or_skip
from app.core.database import Base
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyMember, FamilyRoom
from app.models.processing_job import ProcessingJob
from app.models.storage_allocation import LegacyStorageAllocation
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.services.processing_jobs import (
    IllegalTransition,
    ProcessingJobNotFound,
    create_job,
    get_job_for_member,
    load_processing_input,
    mark_failed,
    mark_running,
    mark_succeeded,
    recover_abandoned_job,
    retry_failed_job,
    serialize_processing_job,
)

ROOM_ID = "room-pj-1"
OTHER_ROOM_ID = "room-pj-2"
USER_ID = "user-pj-1"
OTHER_USER_ID = "user-pj-2"
RECORD_ID = "record-pj-1"
UPLOAD_FILE_ID = "file-pj-1"


@pytest.fixture()
def session():
    from app.services.record_policy import register_legacy_record
    engine = fk_enforced_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(User(id=USER_ID, google_sub=None, username="pj_user", password_hash="x", email="pj@example.test", name="PJ User"))
        db.add(User(id=OTHER_USER_ID, google_sub=None, username="pj_other", password_hash="x", email="pj-other@example.test", name="PJ Other"))
        db.flush()  # parents before children: FK-enforced engines require users to exist first
        db.add(AuthIdentity(id="ai-pj-1", user_id=USER_ID, provider="local", provider_subject="pj_user", credential_hash="x"))
        db.add(AuthIdentity(id="ai-pj-2", user_id=OTHER_USER_ID, provider="local", provider_subject="pj_other", credential_hash="x"))
        db.add(FamilyRoom(id=ROOM_ID, name="PJ 가족방", invite_code="PJ0001", owner_user_id=USER_ID))
        db.add(FamilyRoom(id=OTHER_ROOM_ID, name="PJ 남의 방", invite_code="PJ0002", owner_user_id=OTHER_USER_ID))
        db.flush()
        db.add(FamilyMember(id="fm-pj-1", room_id=ROOM_ID, user_id=USER_ID, role="owner"))
        db.add(FamilyMember(id="fm-pj-2", room_id=OTHER_ROOM_ID, user_id=OTHER_USER_ID, role="owner"))
        db.add(Upload(id=RECORD_ID, room_id=ROOM_ID, uploader_user_id=USER_ID, type="image", title="영수증 사진", status="uploaded"))
        db.flush()
        register_legacy_record(db, RECORD_ID)
        now = datetime.now(timezone.utc)
        db.add(LegacyStorageAllocation(
            id="allocation-pj-1", room_id=ROOM_ID, upload_id=RECORD_ID,
            storage_bucket="local-media", storage_path=f"{RECORD_ID}.png",
            byte_size=10, state="ACTIVE", reserved_at=now,
            expires_at=now, updated_at=now,
        ))
        db.flush()
        db.add(UploadFile(
            id=UPLOAD_FILE_ID, upload_id=RECORD_ID,
            storage_bucket="local-media", storage_path=f"{RECORD_ID}.png",
            storage_allocation_id="allocation-pj-1",
            mime_type="image/png", file_size=10,
        ))
        db.commit()
        yield db
    engine.dispose()


def test_create_job_is_idempotent(session):
    first = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    second = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    assert first.id == second.id
    assert first.status == "PENDING"
    assert first.attempt == 0


def test_only_legal_transitions_succeed(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")

    with pytest.raises(IllegalTransition):
        mark_succeeded(session, job.id, result={"text": "x"})
    with pytest.raises(IllegalTransition):
        mark_failed(session, job.id, code="ocr_failed", message="OCR failed")

    running = mark_running(session, job.id)
    assert running.status == "RUNNING"

    with pytest.raises(IllegalTransition):
        mark_running(session, job.id)

    succeeded = mark_succeeded(session, job.id, result={"text": "hello"})
    assert succeeded.status == "SUCCEEDED"


def test_retry_increments_attempt_and_succeeded_is_terminal(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="stt")
    mark_running(session, job.id)
    failed = mark_failed(session, job.id, code="stt_timeout", message="Speech-to-text timed out")
    assert failed.attempt == 0

    retried = retry_failed_job(session, job.id)
    assert retried.status == "PENDING"
    assert retried.attempt == 1
    assert retried.failure_code is None

    mark_running(session, job.id)
    succeeded = mark_succeeded(session, job.id, result={"text": "done"})
    assert succeeded.status == "SUCCEEDED"

    with pytest.raises(IllegalTransition):
        retry_failed_job(session, job.id)


def test_cross_room_job_is_indistinguishable_from_missing(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")

    with pytest.raises(ProcessingJobNotFound):
        get_job_for_member(session, job_id=job.id, user_id=OTHER_USER_ID)

    with pytest.raises(ProcessingJobNotFound):
        get_job_for_member(session, job_id="does-not-exist", user_id=OTHER_USER_ID)

    # Owning member still resolves it.
    resolved = get_job_for_member(session, job_id=job.id, user_id=USER_ID)
    assert resolved.id == job.id


def test_load_processing_input_carries_storage_locator(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    processing_input = load_processing_input(session, job_id=job.id, user_id=USER_ID)
    assert processing_input.job_id == job.id
    assert processing_input.storage_bucket == "local-media"
    assert processing_input.storage_path == f"{RECORD_ID}.png"
    assert processing_input.mime_type == "image/png"


def test_load_processing_input_rejects_nonactive_source(session):
    job = create_job(
        session, record_id=RECORD_ID, room_id=ROOM_ID,
        upload_file_id=UPLOAD_FILE_ID, processor="ocr",
    )
    allocation = session.get(LegacyStorageAllocation, "allocation-pj-1")
    allocation.state = "DELETE_PENDING"
    session.commit()
    with pytest.raises(IllegalTransition, match="no longer current"):
        load_processing_input(session, job_id=job.id, user_id=USER_ID)


def test_public_failure_payload_has_only_stable_code_and_safe_message(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    mark_failed(session, job.id, code="ocr_nonzero_exit", message="OCR command exited with an error")

    payload = serialize_processing_job(job)
    assert payload["failure_code"] == "ocr_nonzero_exit"
    assert payload["failure_message"] == "OCR command exited with an error"
    assert payload["result"] is None
    forbidden_markers = ("Traceback", "raise ", "  File \"", "/home/", "C:\\\\", "password", "secret")
    for marker in forbidden_markers:
        assert marker not in (payload["failure_message"] or "")


def _two_sessions(session):
    factory = sessionmaker(bind=session.get_bind())
    return factory(), factory()


def test_two_sessions_only_one_claim_succeeds(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    first, second = _two_sessions(session)
    try:
        assert mark_running(first, job.id).status == "RUNNING"
        with pytest.raises(IllegalTransition):
            mark_running(second, job.id)
        assert second.get(ProcessingJob, job.id).started_at is not None
    finally:
        first.close()
        second.close()


def test_concurrent_create_returns_same_job(session):
    first, second = _two_sessions(session)
    try:
        a = create_job(first, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
        b = create_job(second, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
        assert a.id == b.id
        assert len(session.scalars(select(ProcessingJob)).all()) == 1
    finally:
        first.close()
        second.close()


def test_stale_attempt_cannot_complete_later_work(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    mark_failed(session, job.id, code="ocr_timeout", message="t")
    retry_failed_job(session, job.id)
    mark_running(session, job.id)
    with pytest.raises(IllegalTransition):
        mark_succeeded(session, job.id, result={"text": "stale"}, attempt=0)
    current = session.get(ProcessingJob, job.id)
    assert current.status == "RUNNING" and current.attempt == 1 and current.result_json is None
    assert mark_succeeded(session, job.id, result={"text": "fresh"}, attempt=1).status == "SUCCEEDED"


def test_completion_after_deletion_raises_not_found_and_recreates_nothing(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    session.delete(session.get(ProcessingJob, job.id))
    session.commit()
    with pytest.raises(ProcessingJobNotFound):
        mark_succeeded(session, job.id, result={"text": "late"}, attempt=0)
    with pytest.raises(ProcessingJobNotFound):
        mark_failed(session, job.id, code="x", message="y", attempt=0)
    assert session.scalars(select(ProcessingJob)).all() == []


def test_lifecycle_timestamps(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    assert job.started_at is None and job.completed_at is None
    running = mark_running(session, job.id)
    assert running.started_at is not None and running.completed_at is None
    done = mark_succeeded(session, job.id, result={"text": "ok"})
    assert done.completed_at is not None and done.completed_at >= done.started_at


def test_abandoned_running_job_is_recovered_only_after_threshold(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    assert recover_abandoned_job(session, job.id) is False
    session.get(ProcessingJob, job.id).started_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    session.commit()
    assert recover_abandoned_job(session, job.id) is True
    recovered = session.get(ProcessingJob, job.id)
    assert recovered.status == "FAILED" and recovered.failure_code == "abandoned"
    assert recovered.completed_at is not None
    assert retry_failed_job(session, job.id).status == "PENDING"


def test_running_job_without_started_at_is_recovered(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    session.get(ProcessingJob, job.id).started_at = None
    session.commit()

    assert recover_abandoned_job(session, job.id) is True
    recovered = session.get(ProcessingJob, job.id)
    assert recovered.status == "FAILED"
    assert recovered.failure_code == "abandoned"
    assert retry_failed_job(session, job.id).status == "PENDING"


def test_mark_succeeded_refuses_non_finite_result(session):
    job = create_job(session, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr")
    mark_running(session, job.id)
    with pytest.raises(ValueError):
        mark_succeeded(session, job.id, result={"confidence": float("nan")})
    assert session.get(ProcessingJob, job.id).status == "RUNNING"


def _postgres_engine_with_seed():
    engine = fk_enforced_engine(postgres_url_or_skip())
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(id=USER_ID, google_sub=None, username="pj_user", password_hash="x", email="pj@example.test", name="PJ User"))
        db.flush()
        db.add(FamilyRoom(id=ROOM_ID, name="PJ", invite_code="PJ0001", owner_user_id=USER_ID))
        db.flush()
        db.add(FamilyMember(id="fm-pj-1", room_id=ROOM_ID, user_id=USER_ID, role="owner"))
        db.add(Upload(id=RECORD_ID, room_id=ROOM_ID, uploader_user_id=USER_ID, type="image", title="r", status="uploaded"))
        db.flush()
        db.add(UploadFile(id=UPLOAD_FILE_ID, upload_id=RECORD_ID, storage_bucket="local-media", storage_path=f"{RECORD_ID}.png", mime_type="image/png", file_size=10))
        db.commit()
    return engine, factory


def _run_in_threads(fn, count=2):
    results, errors = [], []

    def worker():
        try:
            results.append(fn())
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results, errors


def test_two_sessions_only_one_claim_succeeds_postgres():
    engine, factory = _postgres_engine_with_seed()
    try:
        with factory() as db:
            job_id = create_job(db, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr").id

        def claim():
            with factory() as db:
                return mark_running(db, job_id).status

        results, errors = _run_in_threads(claim)
        assert results == ["RUNNING"]
        assert len(errors) == 1 and isinstance(errors[0], IllegalTransition)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_concurrent_create_returns_same_job_postgres():
    engine, factory = _postgres_engine_with_seed()
    try:
        def create():
            with factory() as db:
                return create_job(db, record_id=RECORD_ID, room_id=ROOM_ID, upload_file_id=UPLOAD_FILE_ID, processor="ocr").id

        results, errors = _run_in_threads(create)
        assert errors == []
        assert len(set(results)) == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
