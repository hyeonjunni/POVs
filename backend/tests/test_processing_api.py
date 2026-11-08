"""Processing-jobs API tests (Milestone 1 Task 5): MIME/processor validation,
room isolation, idempotent creation, provenance, safe failure, retry, and the
Transaction-A-closes-before-the-provider-call session boundary (D5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from test_processing_providers import FAKE_WAV_BYTES
from app.api import deps
from app.api.routes import processing as processing_routes
from app.api.routes.uploads import LOCAL_MEDIA_ROOT, ONE_PIXEL_PNG
from app.core import database
from app.core.config import settings
from app.core.database import Base
from app.main import app
from app.models.auth_identity import AuthIdentity
from app.models.family import FamilyMember, FamilyRoom
from app.models.processing_job import ProcessingJob
from app.models.storage_allocation import LegacyStorageAllocation
from app.models.upload import Upload, UploadFile
from app.models.user import User
from app.processing.contracts import OCRResult, ProviderError

ROOM_ID = "room-pa-1"
OTHER_ROOM_ID = "room-pa-2"
USER_ID = "user-pa-1"
OTHER_USER_ID = "user-pa-2"
RECORD_ID = "record-pa-1"
AUDIO_RECORD_ID = "record-pa-audio"
IMAGE_FILE_ID = "file-pa-image"
AUDIO_FILE_ID = "file-pa-audio"


class SessionOpenTracker:
    """Doubles as the `session_factory` for `run_processing_job` and as a
    `get_db`-shaped generator for the plain CRUD routes."""

    def __init__(self, engine):
        self.session_maker = sessionmaker(bind=engine)
        self.open_count = 0

    def __call__(self):
        db = self.session_maker()
        self.open_count += 1
        already_closed = False
        original_close = db.close

        def tracked_close():
            nonlocal already_closed
            if not already_closed:
                already_closed = True
                self.open_count -= 1
            original_close()

        db.close = tracked_close
        return db

    def get_db(self):
        db = self()
        try:
            yield db
        finally:
            db.close()


class RecordingOCRProvider:
    def __init__(self, tracker: SessionOpenTracker):
        self.tracker = tracker
        self.calls = 0
        self.open_count_at_call: list[int] = []

    def extract(self, content, mime_type):
        self.calls += 1
        self.open_count_at_call.append(self.tracker.open_count)
        return OCRResult(text="추출된 텍스트", confidence=0.9, language="kor+eng", regions=())


class FailingOCRProvider:
    def extract(self, content, mime_type):
        raise ProviderError("ocr_nonzero_exit", "OCR command exited with an error.")


class ExplodingOCRProvider:
    def extract(self, content, mime_type):
        raise OSError("subprocess vanished")


def _seed(session_maker):
    from app.services.record_policy import register_legacy_record
    with session_maker() as db:
        db.add(User(id=USER_ID, google_sub=None, username="pa_user", password_hash="x", email="pa@example.test", name="PA User"))
        db.add(User(id=OTHER_USER_ID, google_sub=None, username="pa_other", password_hash="x", email="pa-other@example.test", name="PA Other"))
        db.flush()  # parents before children: FK-enforced engines require users to exist first
        db.add(AuthIdentity(id="ai-pa-1", user_id=USER_ID, provider="local", provider_subject="pa_user", credential_hash="x"))
        db.add(AuthIdentity(id="ai-pa-2", user_id=OTHER_USER_ID, provider="local", provider_subject="pa_other", credential_hash="x"))
        db.add(FamilyRoom(id=ROOM_ID, name="PA 가족방", invite_code="PA0001", owner_user_id=USER_ID))
        db.add(FamilyRoom(id=OTHER_ROOM_ID, name="PA 남의 방", invite_code="PA0002", owner_user_id=OTHER_USER_ID))
        db.flush()
        db.add(FamilyMember(id="fm-pa-1", room_id=ROOM_ID, user_id=USER_ID, role="owner"))
        db.add(FamilyMember(id="fm-pa-2", room_id=OTHER_ROOM_ID, user_id=OTHER_USER_ID, role="owner"))
        db.add(Upload(id=RECORD_ID, room_id=ROOM_ID, uploader_user_id=USER_ID, type="image", title="영수증", status="uploaded"))
        db.add(Upload(id=AUDIO_RECORD_ID, room_id=ROOM_ID, uploader_user_id=USER_ID, type="voice", title="음성", status="uploaded"))
        db.flush()
        register_legacy_record(db, RECORD_ID)
        register_legacy_record(db, AUDIO_RECORD_ID)
        now = datetime.now(timezone.utc)
        for allocation_id, record_id, path in (
            ("allocation-pa-image", RECORD_ID, f"{RECORD_ID}.png"),
            ("allocation-pa-audio", AUDIO_RECORD_ID, f"{AUDIO_RECORD_ID}.wav"),
        ):
            db.add(LegacyStorageAllocation(
                id=allocation_id, room_id=ROOM_ID, upload_id=record_id,
                storage_bucket="local-media", storage_path=path, byte_size=10,
                state="ACTIVE", reserved_at=now, expires_at=now, updated_at=now,
            ))
        db.flush()
        db.add(UploadFile(id=IMAGE_FILE_ID, upload_id=RECORD_ID, storage_bucket="local-media", storage_path=f"{RECORD_ID}.png", mime_type="image/png", file_size=10, storage_allocation_id="allocation-pa-image"))
        db.add(UploadFile(id=AUDIO_FILE_ID, upload_id=AUDIO_RECORD_ID, storage_bucket="local-media", storage_path=f"{AUDIO_RECORD_ID}.wav", mime_type="audio/wav", file_size=10, storage_allocation_id="allocation-pa-audio"))
        db.commit()


def _setup():
    engine = fk_enforced_engine("sqlite://")
    engine._previous_audit_engine = database.engine
    database.engine = engine
    Base.metadata.create_all(engine)
    tracker = SessionOpenTracker(engine)
    _seed(tracker.session_maker)

    app.dependency_overrides[database.get_db] = tracker.get_db
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=USER_ID)

    old_session_local = processing_routes.SessionLocal
    processing_routes.SessionLocal = tracker
    old_ocr_provider = processing_routes.PROVIDER_REGISTRY["ocr"]

    return engine, tracker, old_session_local, old_ocr_provider


def _teardown(engine, old_session_local, old_ocr_provider):
    database.engine = engine._previous_audit_engine
    processing_routes.SessionLocal = old_session_local
    processing_routes.PROVIDER_REGISTRY["ocr"] = old_ocr_provider
    app.dependency_overrides.clear()
    engine.dispose()


def _create(client: TestClient, *, upload_file_id: str, processor: str, record_id: str | None = None):
    record_id = record_id or (
        AUDIO_RECORD_ID if upload_file_id == AUDIO_FILE_ID else RECORD_ID
    )
    return client.post(
        f"/api/v1/records/{record_id}/processing-jobs",
        json={"upload_file_id": upload_file_id, "processor": processor},
    )


def _replace_current(tracker):
    with tracker.session_maker() as db:
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        db.add(LegacyStorageAllocation(
            id="allocation-pa-new", room_id=ROOM_ID, upload_id=RECORD_ID,
            storage_bucket="local-media", storage_path=f"{RECORD_ID}__new.png",
            byte_size=10, state="ACTIVE", reserved_at=now,
            expires_at=now, updated_at=now,
        ))
        db.flush()
        db.add(UploadFile(
            id="file-pa-new", upload_id=RECORD_ID,
            storage_bucket="local-media", storage_path=f"{RECORD_ID}__new.png",
            storage_allocation_id="allocation-pa-new", mime_type="image/png",
            file_size=10, created_at=now,
        ))
        db.commit()


def test_create_processing_job_rejects_unsupported_processor():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        response = _create(client, upload_file_id=IMAGE_FILE_ID, processor="translate")
        assert response.status_code == 400
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_create_processing_job_rejects_mime_mismatch():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        response = _create(client, upload_file_id=AUDIO_FILE_ID, processor="ocr")
        assert response.status_code == 400
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_create_processing_job_missing_file_is_404():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        response = _create(client, upload_file_id="no-such-file", processor="ocr")
        assert response.status_code == 404
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_create_processing_job_cross_room_record_is_404():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        with tracker.session_maker() as db:
            db.add(Upload(id="record-foreign", room_id=OTHER_ROOM_ID, uploader_user_id=OTHER_USER_ID, type="image", title="foreign", status="uploaded"))
            db.add(UploadFile(id="file-foreign", upload_id="record-foreign", storage_bucket="local-media", storage_path="record-foreign.png", mime_type="image/png", file_size=1))
            db.commit()
        response = _create(client, upload_file_id="file-foreign", processor="ocr", record_id="record-foreign")
        assert response.status_code == 404
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_create_processing_job_is_idempotent_and_status_is_pending():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        first = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        assert first.status_code == 200, first.text
        second = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        assert second.status_code == 200
        assert first.json()["job_id"] == second.json()["job_id"]
        assert first.json()["status"] == "PENDING"

        status_response = client.get(f"/api/v1/processing-jobs/{first.json()['job_id']}")
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "PENDING"
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_create_processing_job_rejects_noncurrent_source():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        _replace_current(tracker)
        response = _create(
            TestClient(app), upload_file_id=IMAGE_FILE_ID, processor="ocr"
        )
        assert response.status_code == 409
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_run_processing_job_rejects_source_replaced_after_creation():
    engine, tracker, old_sl, old_ocr = _setup()
    provider = RecordingOCRProvider(tracker)
    processing_routes.PROVIDER_REGISTRY["ocr"] = provider
    try:
        client = TestClient(app)
        job_id = _create(
            client, upload_file_id=IMAGE_FILE_ID, processor="ocr"
        ).json()["job_id"]
        _replace_current(tracker)
        response = client.post(f"/api/v1/processing-jobs/{job_id}/run")
        assert response.status_code == 409
        assert provider.calls == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_retry_processing_job_rejects_source_replaced_after_failure():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        job_id = _create(
            client, upload_file_id=IMAGE_FILE_ID, processor="ocr"
        ).json()["job_id"]
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, job_id)
            job.status = "FAILED"
            db.commit()
        _replace_current(tracker)
        response = client.post(f"/api/v1/processing-jobs/{job_id}/retry")
        assert response.status_code == 409
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_get_processing_job_cross_room_is_404():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        created = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        job_id = created.json()["job_id"]

        app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=OTHER_USER_ID)
        response = client.get(f"/api/v1/processing-jobs/{job_id}")
        assert response.status_code == 404
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_run_succeeds_with_auto_ocr_provenance_and_closes_session_before_provider_call():
    engine, tracker, old_sl, old_ocr = _setup()
    provider = RecordingOCRProvider(tracker)
    processing_routes.PROVIDER_REGISTRY["ocr"] = provider
    try:
        client = TestClient(app)
        created = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        job_id = created.json()["job_id"]

        media_path = LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png"
        media_path.write_bytes(ONE_PIXEL_PNG)
        try:
            run_response = client.post(f"/api/v1/processing-jobs/{job_id}/run")
        finally:
            media_path.unlink(missing_ok=True)

        assert run_response.status_code == 200, run_response.text
        body = run_response.json()
        assert body["status"] == "SUCCEEDED"
        assert body["result"]["extractor"] == "AUTO_OCR"
        assert body["result"]["user_confirmed"] is False
        assert body["result"]["review_status"] == "unreviewed"
        assert provider.calls == 1
        # The session was closed (open_count == 0) at the moment extract() ran.
        assert provider.open_count_at_call == [0]
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_run_records_safe_failure_and_allows_retry():
    engine, tracker, old_sl, old_ocr = _setup()
    processing_routes.PROVIDER_REGISTRY["ocr"] = FailingOCRProvider()
    try:
        client = TestClient(app)
        created = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        job_id = created.json()["job_id"]

        media_path = LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png"
        media_path.write_bytes(ONE_PIXEL_PNG)
        try:
            run_response = client.post(f"/api/v1/processing-jobs/{job_id}/run")
        finally:
            media_path.unlink(missing_ok=True)

        assert run_response.status_code == 200, run_response.text
        body = run_response.json()
        assert body["status"] == "FAILED"
        assert body["failure_code"] == "ocr_nonzero_exit"
        assert "Traceback" not in (body["failure_message"] or "")
        assert body["result"] is None
        assert tracker.open_count == 0

        retry_response = client.post(f"/api/v1/processing-jobs/{job_id}/retry")
        assert retry_response.status_code == 200
        retried = retry_response.json()
        assert retried["status"] == "PENDING"
        assert retried["attempt"] == 1
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_retry_of_non_failed_job_is_conflict():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        created = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        job_id = created.json()["job_id"]

        retry_response = client.post(f"/api/v1/processing-jobs/{job_id}/retry")
        assert retry_response.status_code == 409
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_run_rejects_token_whose_identities_were_deleted():
    engine, tracker, old_sl, old_ocr = _setup()
    provider = RecordingOCRProvider(tracker)
    processing_routes.PROVIDER_REGISTRY["ocr"] = provider
    try:
        client = TestClient(app)
        created = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr")
        assert created.status_code == 200
        job_id = created.json()["job_id"]

        with tracker.session_maker() as db:
            db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
            db.commit()

        # Token-only routes: every one must now refuse the still-valid-looking principal.
        assert client.get(f"/api/v1/processing-jobs/{job_id}").status_code == 401
        assert client.post(f"/api/v1/processing-jobs/{job_id}/run").status_code == 401
        assert client.post(f"/api/v1/processing-jobs/{job_id}/retry").status_code == 401
        assert _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr").status_code == 401
        assert provider.calls == 0
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_unexpected_exception_becomes_stable_failed_job():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        processing_routes.PROVIDER_REGISTRY["ocr"] = ExplodingOCRProvider()
        media_path = LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png"
        media_path.write_bytes(ONE_PIXEL_PNG)
        try:
            client = TestClient(app)
            job_id = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr").json()["job_id"]
            response = client.post(f"/api/v1/processing-jobs/{job_id}/run")
        finally:
            media_path.unlink(missing_ok=True)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["failure_code"] == "unexpected_error"
        assert "subprocess" not in body["failure_message"]
        assert body["started_at"] and body["completed_at"]
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_retry_recovers_running_job_without_started_at():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        client = TestClient(app)
        job_id = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr").json()["job_id"]
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, job_id)
            job.status = "RUNNING"
            job.started_at = None
            db.commit()
        response = client.post(f"/api/v1/processing-jobs/{job_id}/retry")
        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "PENDING"
        assert response.json()["attempt"] == 1
        with tracker.session_maker() as db:
            assert db.get(ProcessingJob, job_id).started_at is None

        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, job_id)
            job.status = "RUNNING"
            job.started_at = datetime.now(timezone.utc)
            db.commit()
        assert client.post(f"/api/v1/processing-jobs/{job_id}/retry").status_code == 409
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_deletion_mid_run_returns_404_and_recreates_nothing():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        class DeletingProvider:
            def extract(self, content, mime_type):
                with tracker.session_maker() as db:
                    db.query(ProcessingJob).delete(synchronize_session=False)
                    db.commit()
                return RecordingOCRProvider(tracker).extract(content, mime_type)

        processing_routes.PROVIDER_REGISTRY["ocr"] = DeletingProvider()
        media_path = LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png"
        media_path.write_bytes(ONE_PIXEL_PNG)
        try:
            client = TestClient(app)
            job_id = _create(client, upload_file_id=IMAGE_FILE_ID, processor="ocr").json()["job_id"]
            response = client.post(f"/api/v1/processing-jobs/{job_id}/run")
        finally:
            media_path.unlink(missing_ok=True)
        assert response.status_code == 404
        with tracker.session_maker() as db:
            assert db.scalars(select(ProcessingJob)).all() == []
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


# ------------------- R8: input bound, content sniffing, language, provenance ----


def _run_with_media(client, tracker, media_bytes: bytes, file_id=IMAGE_FILE_ID, processor="ocr", suffix=".png"):
    record_id = AUDIO_RECORD_ID if file_id == AUDIO_FILE_ID else RECORD_ID
    media_path = LOCAL_MEDIA_ROOT / f"{record_id}{suffix}"
    media_path.write_bytes(media_bytes)
    try:
        job_id = _create(client, upload_file_id=file_id, processor=processor).json()["job_id"]
        return client.post(f"/api/v1/processing-jobs/{job_id}/run")
    finally:
        media_path.unlink(missing_ok=True)


def test_oversized_input_fails_before_any_provider_call(monkeypatch):
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        provider = RecordingOCRProvider(tracker)
        processing_routes.PROVIDER_REGISTRY["ocr"] = provider
        monkeypatch.setattr(settings, "ocr_max_input_bytes", len(ONE_PIXEL_PNG) - 1)
        response = _run_with_media(TestClient(app), tracker, ONE_PIXEL_PNG)
        assert response.status_code == 200
        assert response.json()["status"] == "FAILED"
        assert response.json()["failure_code"] == "media_too_large"
        assert provider.calls == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_content_that_contradicts_stored_mime_fails_before_provider():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        provider = RecordingOCRProvider(tracker)
        processing_routes.PROVIDER_REGISTRY["ocr"] = provider
        response = _run_with_media(TestClient(app), tracker, FAKE_WAV_BYTES)   # row says image/png
        assert response.json()["status"] == "FAILED"
        assert response.json()["failure_code"] == "media_content_mismatch"
        assert provider.calls == 0
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_succeeded_payload_records_extractor_and_language():
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        provider = RecordingOCRProvider(tracker)
        provider.extractor_id = "recording_ocr:test"
        provider.language = "kor+eng"
        processing_routes.PROVIDER_REGISTRY["ocr"] = provider
        response = _run_with_media(TestClient(app), tracker, ONE_PIXEL_PNG)
        assert response.json()["status"] == "SUCCEEDED"
        result = response.json()["result"]
        assert result["extractor"] == "AUTO_OCR"
        assert result["extractor_id"] == "recording_ocr:test"
        assert result["requested_language"] == "kor+eng"
        assert result["user_confirmed"] is False and result["review_status"] == "unreviewed"
    finally:
        _teardown(engine, old_sl, old_ocr)


def test_stt_run_passes_configured_language(monkeypatch):
    engine, tracker, old_sl, old_ocr = _setup()
    try:
        class RecordingSTTProvider:
            def __init__(self):
                self.kwargs = []

            def transcribe(self, content, mime_type, language=None):
                self.kwargs.append(language)
                from app.processing.contracts import STTResult
                return STTResult(text="안녕", language=language, segments=())

        stt = RecordingSTTProvider()
        old_stt = processing_routes.PROVIDER_REGISTRY["stt"]
        processing_routes.PROVIDER_REGISTRY["stt"] = stt
        monkeypatch.setattr(settings, "stt_language", "ko")
        try:
            response = _run_with_media(TestClient(app), tracker, FAKE_WAV_BYTES, file_id=AUDIO_FILE_ID, processor="stt", suffix=".wav")
        finally:
            processing_routes.PROVIDER_REGISTRY["stt"] = old_stt
        assert response.json()["status"] == "SUCCEEDED", response.json()
        assert stt.kwargs == ["ko"]
        assert response.json()["result"]["requested_language"] == "ko"
        assert response.json()["result"]["extractor_id"] == "RecordingSTTProvider"
    finally:
        _teardown(engine, old_sl, old_ocr)
