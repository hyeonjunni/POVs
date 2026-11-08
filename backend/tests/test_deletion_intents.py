"""A5: deletion = DB phase then no-session object phase with durable intents."""
from __future__ import annotations

import uuid
import json
from unittest.mock import MagicMock

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select, text

from app.api.routes import uploads as uploads_routes
from app.api.routes.uploads import LOCAL_MEDIA_ROOT, ONE_PIXEL_PNG
from app.core import database
from app.core.config import settings
from app.core.database import Base
from app.core.security import create_access_token
from app.main import app
from app.models import (
    AuthIdentity,
    DeletionIntent,
    FamilyMember,
    FamilyRoom,
    ProcessingJob,
    RecordChunk,
    RecordEmbedding,
    Upload,
    UploadFile,
    User,
)
from app.services import account_cleanup
from conftest import fk_enforced_engine
from test_processing_api import SessionOpenTracker
from test_retrieval_indexing import FakeEmbeddingProvider, result_payload
from app.services.retrieval_indexing import index_processing_result
from scripts.adopt_alembic import _build_alembic_config

USER_ID = "di_user"
ROOM_ID = "room-di"


def _setup():
    engine = fk_enforced_engine()
    Base.metadata.create_all(engine)
    tracker = SessionOpenTracker(engine)
    app.dependency_overrides[database.get_db] = tracker.get_db
    old = uploads_routes.SessionLocal
    uploads_routes.SessionLocal = tracker
    return engine, tracker, old


def _client():
    return TestClient(
        app,
        headers={"Authorization": f"Bearer {create_access_token(USER_ID)}"},
    )


def _teardown(engine, old):
    uploads_routes.SessionLocal = old
    app.dependency_overrides.clear()
    engine.dispose()
    for path in LOCAL_MEDIA_ROOT.glob("record-di-*"):
        path.unlink(missing_ok=True)
    (LOCAL_MEDIA_ROOT / "r3-victim.png").unlink(missing_ok=True)


def _seed(tracker, *, records=3):
    record_ids = [f"record-di-{uuid.uuid4().hex[:8]}" for _ in range(records)]
    with tracker.session_maker() as db:
        db.add(User(id=USER_ID, username="di_user", email="di@example.com", password_hash="x", name="DI"))
        db.flush()
        # This shared seed also targets explicit0004: avoid current ORM
        # RETURNING payload_state, a column introduced only in0005.
        db.execute(text("INSERT INTO auth_identities (id,user_id,provider,provider_subject,credential_hash) "
                        "VALUES (:id,:user_id,:provider,:subject,:hash)"),
                   {"id": "ai-di-1", "user_id": USER_ID, "provider": "local", "subject": "di_user", "hash": "x"})
        db.execute(text("INSERT INTO family_rooms(id,name,owner_user_id,invite_code) VALUES (:r,'DI',:u,'DI123456')"), {"r": ROOM_ID, "u": USER_ID})
        db.execute(text("INSERT INTO family_members(id,room_id,user_id,role) VALUES ('fm-di-1',:r,:u,'owner')"), {"r": ROOM_ID, "u": USER_ID})
        for index, record_id in enumerate(record_ids):
            (LOCAL_MEDIA_ROOT / f"{record_id}.png").write_bytes(ONE_PIXEL_PNG)
            db.add(
                Upload(
                    id=record_id,
                    room_id=ROOM_ID,
                    uploader_user_id=USER_ID,
                    type="photo",
                    title=record_id,
                )
            )
            db.flush()
            if inspect(db.connection()).has_table("records"):
                from app.services.record_policy import register_legacy_record
                register_legacy_record(db, record_id)
            db.add(
                UploadFile(
                    id=f"file-{record_id}",
                    upload_id=record_id,
                    storage_bucket="local-media",
                    storage_path=f"{record_id}.png",
                    mime_type="image/png",
                    file_size=len(ONE_PIXEL_PNG),
                )
            )
            db.flush()
            db.add(
                ProcessingJob(
                    id=f"job-{record_id}",
                    record_id=record_id,
                    upload_file_id=f"file-{record_id}",
                    room_id=ROOM_ID,
                    processor="ocr",
                    status=("SUCCEEDED", "FAILED", "RUNNING")[index % 3],
                    attempt=1,
                )
            )
        db.commit()
    return record_ids


def _intents(tracker):
    with tracker.session_maker() as db:
        return {
            (intent.storage_path, intent.status, intent.failure_code)
            for intent in db.scalars(select(DeletionIntent)).all()
        }


def _count(tracker, model):
    with tracker.session_maker() as db:
        return len(db.scalars(select(model)).all())


def _index_record(tracker, record_id):
    with tracker.session_maker() as db:
        job = db.get(ProcessingJob, f"job-{record_id}")
        job.status = "SUCCEEDED"
        job.result_json = json.dumps(result_payload())
        from ocr_approval_helpers import approve_job_fixture
        approve_job_fixture(job)
        db.commit()
    index_processing_result(
        record_id=record_id, processing_job_id=f"job-{record_id}", user_id=USER_ID,
        provider=FakeEmbeddingProvider(tracker), session_factory=tracker,
    )


@pytest.mark.parametrize("scope", ["record", "room", "account"])
def test_retrieval_deletion_is_ordered_and_committed_before_storage(scope, monkeypatch):
    engine, tracker, old = _setup()
    try:
        record_id = _seed(tracker, records=1)[0]
        _index_record(tracker, record_id)
        deletes = []

        @event.listens_for(engine, "before_cursor_execute")
        def capture_delete(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("DELETE FROM "):
                deletes.append(statement.split()[2])

        real = account_cleanup._delete_storage_object
        observed = []

        def inspect_committed_state(bucket, path):
            assert tracker.open_count == 0
            assert {status for _, status, _ in _intents(tracker)} == {"pending"}
            for model in (RecordEmbedding, RecordChunk, ProcessingJob, UploadFile, Upload):
                assert _count(tracker, model) == 0
            observed.append(path)
            return real(bucket, path)

        monkeypatch.setattr(account_cleanup, "_delete_storage_object", inspect_committed_state)
        endpoint = {
            "record": f"/api/v1/uploads/{record_id}",
            "room": f"/api/v1/families/{ROOM_ID}",
            "account": "/api/v1/auth/me",
        }[scope]
        if scope == "record":
            with tracker.session_maker() as db:
                intent_ids = account_cleanup.delete_upload_record(db, db.get(Upload, record_id))
                db.commit()
            assert account_cleanup.drive_deletion_intents(tracker, own_intent_ids=intent_ids) == 0
        else:
            response = _client().delete(endpoint)
            assert response.status_code == 200, response.json()
            assert response.json()["media_cleanup_failed_count"] == 0
            assert observed == []
            assert _count(tracker, Upload) == 1
            assert _intents(tracker) == set()
            with tracker.session_maker() as db:
                assert db.get(FamilyRoom, ROOM_ID).is_active is False
                assert db.get(FamilyMember, "fm-di-1").is_active is False
            return
        assert observed == [f"{record_id}.png"]
        assert deletes[:5] == [
            "record_embeddings", "record_chunks", "processing_jobs", "upload_files", "uploads",
        ]
        assert deletes.count("processing_jobs") == 1
    finally:
        _teardown(engine, old)


def test_m1_delete_order_cascades_on_populated_migrated_0004(tmp_path):
    url = f"sqlite:///{tmp_path / 'm1-rollback-0004.db'}"
    command.upgrade(_build_alembic_config(url), "0004_retrieval")
    engine = fk_enforced_engine(url)
    tracker = SessionOpenTracker(engine)
    record_ids = []
    try:
        record_ids = _seed(tracker, records=1)
        # Exercise the historical 0004 FK contract with historical rows; the
        # current indexer requires the later OCR review eligibility schema.
        from app.retrieval.chunking import ChunkSource, chunk_processing_result
        with tracker.session_maker() as db:
            job = db.get(ProcessingJob, f"job-{record_ids[0]}")
            chunks = chunk_processing_result(ChunkSource(
                room_id=ROOM_ID, record_id=record_ids[0], file_id=job.upload_file_id,
                processing_job_id=job.id, source_attempt=job.attempt, result=result_payload()))
            db.add_all(RecordChunk(**vars(chunk)) for chunk in chunks)
            db.flush()
            db.add_all(RecordEmbedding(id=f"historical-{chunk.id}", chunk_id=chunk.id,
                model_key="historical", dimensions=1024, embedding=[1.0]*1024) for chunk in chunks)
            db.commit()
        assert _count(tracker, RecordEmbedding) == _count(tracker, RecordChunk) == 1
        with tracker.session_maker() as db:
            # The unmodified M1 sequence knows nothing about retrieval tables.
            db.query(ProcessingJob).filter(ProcessingJob.record_id.in_(record_ids)).delete(synchronize_session=False)
            db.query(UploadFile).filter(UploadFile.upload_id.in_(record_ids)).delete(synchronize_session=False)
            db.query(Upload).filter(Upload.id.in_(record_ids)).delete(synchronize_session=False)
            db.commit()
        assert _count(tracker, RecordEmbedding) == _count(tracker, RecordChunk) == 0
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        engine.dispose()
        for record_id in record_ids:
            (LOCAL_MEDIA_ROOT / f"{record_id}.png").unlink(missing_ok=True)


def test_record_room_account_deletion_under_fk():
    engine, tracker, old = _setup()
    try:
        record_ids = _seed(tracker)
        client = _client()

        response = client.delete(f"/api/v1/uploads/{record_ids[0]}")
        assert response.status_code == 200, response.json()
        assert response.json()["media_cleanup_failed_count"] == 0
        assert (LOCAL_MEDIA_ROOT / f"{record_ids[0]}.png").exists()
        assert _count(tracker, ProcessingJob) == 3
        assert _intents(tracker) == set()

        response = client.delete(f"/api/v1/families/{ROOM_ID}")
        assert response.status_code == 200, response.json()
        assert _count(tracker, ProcessingJob) == 3
        assert _count(tracker, Upload) == 3
        assert _count(tracker, FamilyRoom) == 1
        for record_id in record_ids:
            assert (LOCAL_MEDIA_ROOT / f"{record_id}.png").exists()

        response = client.delete("/api/v1/auth/me")
        assert response.status_code == 200, response.json()
        assert _count(tracker, User) == 1
        assert _count(tracker, AuthIdentity) == 0
        assert tracker.open_count == 0
        assert _intents(tracker) == set()
    finally:
        _teardown(engine, old)


def test_account_deletion_preserves_provenance_in_rooms_the_user_does_not_own():
    engine, tracker, old = _setup()
    try:
        record_ids = _seed(tracker, records=1)
        with tracker.session_maker() as db:
            db.add(User(id="di_owner", username="di_owner", email="owner@example.com", password_hash="x", name="O"))
            db.flush()
            db.add(
                AuthIdentity(
                    id="ai-di-2",
                    user_id="di_owner",
                    provider="local",
                    provider_subject="di_owner",
                    credential_hash="x",
                )
            )
            db.add(
                FamilyRoom(
                    id="room-di-other",
                    name="Other",
                    owner_user_id="di_owner",
                    invite_code="DI654321",
                )
            )
            db.flush()
            db.add(
                FamilyMember(
                    id="fm-di-2",
                    room_id="room-di-other",
                    user_id=USER_ID,
                    role="member",
                )
            )
            (LOCAL_MEDIA_ROOT / "record-di-guest.png").write_bytes(ONE_PIXEL_PNG)
            db.add(
                Upload(
                    id="record-di-guest",
                    room_id="room-di-other",
                    uploader_user_id=USER_ID,
                    type="photo",
                    title="guest",
                )
            )
            db.flush()
            db.add(
                UploadFile(
                    id="file-record-di-guest",
                    upload_id="record-di-guest",
                    storage_bucket="local-media",
                    storage_path="record-di-guest.png",
                    mime_type="image/png",
                    file_size=1,
                )
            )
            db.flush()
            db.add(
                ProcessingJob(
                    id="job-record-di-guest",
                    record_id="record-di-guest",
                    upload_file_id="file-record-di-guest",
                    room_id="room-di-other",
                    processor="ocr",
                    status="RUNNING",
                    attempt=1,
                )
            )
            db.commit()

        response = _client().delete("/api/v1/auth/me")
        assert response.status_code == 200, response.json()
        assert _count(tracker, ProcessingJob) == 2
        assert _count(tracker, FamilyRoom) == 2
        assert (LOCAL_MEDIA_ROOT / "record-di-guest.png").exists()
        assert (LOCAL_MEDIA_ROOT / f"{record_ids[0]}.png").exists()
        with tracker.session_maker() as db:
            assert db.get(FamilyMember, "fm-di-2").is_active is False
            assert db.get(FamilyRoom, "room-di-other").is_active is True
    finally:
        _teardown(engine, old)


def test_account_deletion_preserves_relation_provenance_without_login_identity():
    engine, tracker, old = _setup()
    try:
        _seed(tracker, records=1)
        with tracker.session_maker() as db:
            db.add(
                User(
                    id="di_owner",
                    username="di_owner",
                    email="owner@example.com",
                    password_hash="x",
                    name="O",
                )
            )
            db.flush()
            db.add(
                FamilyRoom(
                    id="room-di-other",
                    name="Other",
                    owner_user_id="di_owner",
                    invite_code="DI654321",
                )
            )
            db.flush()
            db.add(
                FamilyMember(
                    id="fm-di-3",
                    room_id="room-di-other",
                    user_id="di_owner",
                    role="owner",
                    related_to_user_id=USER_ID,
                    relation_to_related_user="parent",
                )
            )
            db.commit()

        response = _client().delete("/api/v1/auth/me")
        assert response.status_code == 200, response.json()
        assert _count(tracker, User) == 2
        with tracker.session_maker() as db:
            user = db.get(User, USER_ID)
            assert user is not None and user.username is None and user.email is None
            assert _count(tracker, AuthIdentity) == 0
            assert db.get(FamilyRoom, "room-di-other") is not None
            member = db.get(FamilyMember, "fm-di-3")
            assert member is not None
            assert member.user_id == "di_owner"
            assert member.related_to_user_id == USER_ID
    finally:
        _teardown(engine, old)


def test_object_phase_runs_with_no_session_open(monkeypatch):
    engine, tracker, old = _setup()
    try:
        record_ids = _seed(tracker, records=1)
        open_counts = []
        real = account_cleanup._delete_storage_object

        def observed(bucket, path):
            open_counts.append(tracker.open_count)
            return real(bucket, path)

        monkeypatch.setattr(account_cleanup, "_delete_storage_object", observed)
        with tracker.session_maker() as db:
            intent_ids = account_cleanup.delete_upload_record(db, db.get(Upload, record_ids[0]))
            db.commit()
        assert account_cleanup.drive_deletion_intents(tracker, own_intent_ids=intent_ids) == 0
        assert open_counts == [0]
    finally:
        _teardown(engine, old)


def test_crash_after_commit_leaves_pending_intents_that_recovery_drives():
    engine, tracker, old = _setup()
    try:
        record_ids = _seed(tracker, records=2)
        with tracker.session_maker() as db:
            intent_ids = []
            for record_id in record_ids:
                intent_ids += account_cleanup.delete_upload_record(db, db.get(Upload, record_id))
            db.commit()
        assert len(intent_ids) == 2
        for record_id in record_ids:
            assert (LOCAL_MEDIA_ROOT / f"{record_id}.png").exists()
        assert {status for _, status, _ in _intents(tracker)} == {"pending"}

        assert account_cleanup.drive_deletion_intents(tracker) == 0
        for record_id in record_ids:
            assert not (LOCAL_MEDIA_ROOT / f"{record_id}.png").exists()
        assert {status for _, status, _ in _intents(tracker)} == {"deleted"}
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_absent_object_is_acknowledged_and_failure_is_stable_and_retried(monkeypatch):
    engine, tracker, old = _setup()
    try:
        record_ids = _seed(tracker, records=2)
        (LOCAL_MEDIA_ROOT / f"{record_ids[0]}.png").unlink()
        real = account_cleanup._delete_storage_object

        def failing(bucket, path):
            if path == f"{record_ids[1]}.png":
                raise OSError("disk unplugged")
            return real(bucket, path)

        monkeypatch.setattr(account_cleanup, "_delete_storage_object", failing)
        with tracker.session_maker() as db:
            intent_ids = []
            for record_id in record_ids:
                intent_ids += account_cleanup.delete_upload_record(db, db.get(Upload, record_id))
            db.commit()
        assert account_cleanup.drive_deletion_intents(tracker, own_intent_ids=intent_ids) == 1
        assert _intents(tracker) == {
            (f"{record_ids[0]}.png", "absent", None),
            (f"{record_ids[1]}.png", "failed", "storage_delete_error"),
        }

        monkeypatch.setattr(account_cleanup, "_delete_storage_object", real)
        assert account_cleanup.drive_deletion_intents(tracker) == 0
        assert _intents(tracker) == {
            (f"{record_ids[0]}.png", "absent", None),
            (f"{record_ids[1]}.png", "deleted", None),
        }
    finally:
        _teardown(engine, old)


def test_untrusted_intents_never_delete_local_or_foreign_gcs(monkeypatch, tmp_path):
    engine, tracker, old = _setup()
    try:
        outside = tmp_path / "victim.bin"
        outside.write_bytes(b"keep me")

        def forbidden(*_args, **_kwargs):
            raise AssertionError("foreign GCS deletion attempted")

        monkeypatch.setattr(account_cleanup, "delete_gcs_blob", forbidden)
        with tracker.session_maker() as db:
            db.add_all(
                [
                    DeletionIntent(
                        id="intent-evil-local",
                        scope="record",
                        scope_id="r",
                        storage_bucket="local-media",
                        storage_path=str(outside),
                        status="pending",
                    ),
                    DeletionIntent(
                        id="intent-evil-gcs",
                        scope="record",
                        scope_id="r",
                        storage_bucket="foreign-bucket",
                        storage_path="uploads/r.png",
                        status="pending",
                    ),
                ]
            )
            db.commit()

        account_cleanup.drive_deletion_intents(tracker)
        assert outside.exists()
        assert _intents(tracker) == {
            (str(outside), "failed", "locator_untrusted"),
            ("uploads/r.png", "failed", "locator_untrusted"),
        }
    finally:
        _teardown(engine, old)


def test_record_deletion_refuses_legacy_untrusted_file_rows(monkeypatch):
    engine, tracker, old = _setup()
    try:
        record_id = _seed(tracker, records=1)[0]
        victim = LOCAL_MEDIA_ROOT / "r3-victim.png"
        victim.write_bytes(b"keep me")
        with tracker.session_maker() as db:
            db.add_all(
                [
                    UploadFile(
                        id="file-legacy-local",
                        upload_id=record_id,
                        storage_bucket="local-media",
                        storage_path=victim.name,
                        mime_type="image/png",
                    ),
                    UploadFile(
                        id="file-legacy-gcs",
                        upload_id=record_id,
                        storage_bucket="foreign-bucket",
                        storage_path=f"uploads/{record_id}.png",
                        mime_type="image/png",
                    ),
                ]
            )
            db.commit()

        monkeypatch.setattr(
            account_cleanup,
            "delete_gcs_blob",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("foreign GCS deletion attempted")
            ),
        )
        with tracker.session_maker() as db:
            intent_ids = account_cleanup.delete_upload_record(db, db.get(Upload, record_id))
            db.commit()
        assert account_cleanup.drive_deletion_intents(tracker, own_intent_ids=intent_ids) == 2
        assert victim.exists()
        assert sum(
            status == "failed" and code == "locator_untrusted"
            for _, status, code in _intents(tracker)
        ) == 2
    finally:
        _teardown(engine, old)


def test_historical_gcs_intent_is_stable_and_never_retried(monkeypatch):
    engine, tracker, old = _setup()
    try:
        with tracker.session_maker() as db:
            db.add(
                DeletionIntent(
                    id="intent-gcs-runtime",
                    scope="record",
                    scope_id="record-gcs-runtime",
                    storage_bucket=settings.gcs_bucket_name,
                    storage_path="uploads/record-gcs-runtime.png",
                    status="pending",
                )
            )
            db.commit()

        client = MagicMock()
        deletion = MagicMock()
        monkeypatch.setattr(account_cleanup, "get_gcs_client", client)
        monkeypatch.setattr(account_cleanup, "delete_gcs_blob", deletion)
        assert account_cleanup.drive_deletion_intents(
            tracker, own_intent_ids=["intent-gcs-runtime"]
        ) == 1
        assert _intents(tracker) == {
            ("uploads/record-gcs-runtime.png", "failed", "historical_storage_unsupported")
        }

        assert account_cleanup.drive_deletion_intents(tracker) == 0
        assert _intents(tracker) == {
            ("uploads/record-gcs-runtime.png", "failed", "historical_storage_unsupported")
        }
        client.assert_not_called()
        deletion.assert_not_called()
    finally:
        _teardown(engine, old)
