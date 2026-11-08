"""A7: media storage I/O never overlaps an open database session."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import deps
from app.api.routes import ai as ai_routes, uploads as uploads_routes
from app.api.routes.uploads import LOCAL_MEDIA_ROOT, ONE_PIXEL_PNG
from app.core import database
from app.core.config import settings
from app.core.database import Base
from app.main import app
from app.models import AuthIdentity, DeletionIntent, FamilyMember, FamilyRoom, LegacyStorageAllocation, Upload, UploadFile, User
from conftest import fk_enforced_engine
from test_processing_api import SessionOpenTracker

USER_ID = "sio_user"
ROOM_ID = "room-sio"
RECORD_ID = "record-sio-1"


def _setup():
    from app.services.record_policy import register_legacy_record
    engine = fk_enforced_engine()
    engine._previous_audit_engine = database.engine
    database.engine = engine
    Base.metadata.create_all(engine)
    tracker = SessionOpenTracker(engine)
    with tracker.session_maker() as db:
        db.add(User(id=USER_ID, username="sio_user", email="sio@example.test", password_hash="x", name="SIO"))
        db.flush()
        db.add(AuthIdentity(id="ai-sio-1", user_id=USER_ID, provider="local", provider_subject="sio_user", credential_hash="x"))
        db.add(FamilyRoom(id=ROOM_ID, name="SIO", owner_user_id=USER_ID, invite_code="SIO00001"))
        db.flush()
        db.add(FamilyMember(id="fm-sio-1", room_id=ROOM_ID, user_id=USER_ID, role="owner"))
        db.add(Upload(id=RECORD_ID, room_id=ROOM_ID, uploader_user_id=USER_ID, type="image", title="사진", status="uploaded"))
        db.flush()
        register_legacy_record(db, RECORD_ID)
        db.commit()
    app.dependency_overrides[database.get_db] = tracker.get_db
    app.dependency_overrides[deps.get_authenticated_principal] = lambda: deps.AuthenticatedPrincipal(user_id=USER_ID)
    app.dependency_overrides[deps.get_current_user] = lambda: User(id=USER_ID)
    old = (uploads_routes.SessionLocal, ai_routes.SessionLocal)
    uploads_routes.SessionLocal = ai_routes.SessionLocal = tracker
    return engine, tracker, old


def _teardown(engine, old):
    database.engine = engine._previous_audit_engine
    uploads_routes.SessionLocal, ai_routes.SessionLocal = old
    app.dependency_overrides.clear()
    engine.dispose()
    for path in LOCAL_MEDIA_ROOT.glob("record-sio-*"):
        path.unlink(missing_ok=True)


def _delete_record(tracker, record_id=RECORD_ID):
    with tracker.session_maker() as db:
        db.query(Upload).filter(Upload.id == record_id).delete(synchronize_session=False)
        db.commit()


def _delete_membership(tracker):
    with tracker.session_maker() as db:
        db.query(FamilyMember).filter(FamilyMember.room_id == ROOM_ID, FamilyMember.user_id == USER_ID).update({FamilyMember.is_active: False})
        db.commit()


def _delete_identity(tracker):
    with tracker.session_maker() as db:
        db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
        db.commit()


def _factory_with_commit_failures(tracker, *errors):
    # Inject at the publication/cleanup commit, independent of read-only or
    # bounded backlog sessions added before reservation.
    failures = iter(error for error in errors if error is not None)
    def factory():
        db = tracker()
        commit = db.commit
        def fail_publication():
            if any(isinstance(row, (UploadFile, DeletionIntent)) for row in db.new):
                error = next(failures, None)
                if error is not None:
                    raise error
            return commit()
        db.commit = fail_publication
        return db
    return factory


def _post_binary(client):
    return client.post(
        f"/api/v1/uploads/{RECORD_ID}/binary",
        files={"file": ("photo.png", io.BytesIO(ONE_PIXEL_PNG), "image/png")},
    )


def test_upload_binary_writes_media_with_no_session_open(monkeypatch):
    engine, tracker, old = _setup()
    try:
        open_counts = []
        real = uploads_routes.store_media_file

        def observed(*args, **kwargs):
            open_counts.append(tracker.open_count)
            return real(*args, **kwargs)

        monkeypatch.setattr(uploads_routes, "store_media_file", observed)
        response = _post_binary(TestClient(app))
        assert response.status_code == 200, response.json()
        assert response.json()["has_file"] is True
        assert open_counts == [0]
        with tracker.session_maker() as db:
            saved_file = db.get(UploadFile, response.json()["file_id"])
            assert saved_file.upload_id == RECORD_ID
            assert (LOCAL_MEDIA_ROOT / saved_file.storage_path).exists()
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_upload_binary_recheck_failure_discards_media_and_leaves_no_row(monkeypatch):
    engine, tracker, old = _setup()
    try:
        real = uploads_routes.store_media_file

        def store_then_lose_record(*args, **kwargs):
            result = real(*args, **kwargs)
            _delete_record(tracker)
            return result

        monkeypatch.setattr(uploads_routes, "store_media_file", store_then_lose_record)
        response = _post_binary(TestClient(app))
        assert response.status_code == 404
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            intents = db.scalars(select(DeletionIntent)).all()
            assert [(i.scope, i.scope_id, i.status) for i in intents] == [("record", RECORD_ID, "deleted")]
        assert not (LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png").exists()
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_upload_binary_membership_recheck_discards_media(monkeypatch):
    engine, tracker, old = _setup()
    try:
        real = uploads_routes.store_media_file

        def store_then_lose_membership(*args, **kwargs):
            result = real(*args, **kwargs)
            _delete_membership(tracker)
            return result

        monkeypatch.setattr(uploads_routes, "store_media_file", store_then_lose_membership)
        response = _post_binary(TestClient(app))
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "RESOURCE_NOT_AVAILABLE"}}
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert [intent.status for intent in db.scalars(select(DeletionIntent)).all()] == ["deleted"]
        assert not (LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png").exists()
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_upload_binary_principal_recheck_discards_media(monkeypatch):
    engine, tracker, old = _setup()
    try:
        real = uploads_routes.store_media_file

        def store_then_lose_identity(*args, **kwargs):
            result = real(*args, **kwargs)
            _delete_identity(tracker)
            return result

        monkeypatch.setattr(uploads_routes, "store_media_file", store_then_lose_identity)
        response = _post_binary(TestClient(app))
        assert response.status_code == 401
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert [intent.status for intent in db.scalars(select(DeletionIntent)).all()] == ["deleted"]
        assert not (LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png").exists()
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_upload_binary_persistence_failure_discards_media(monkeypatch):
    engine, tracker, old = _setup()
    try:
        uploads_routes.SessionLocal = _factory_with_commit_failures(
            tracker, None, None, RuntimeError("upload persistence failed")
        )
        response = _post_binary(TestClient(app, raise_server_exceptions=False))
        assert response.status_code == 500
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert [intent.status for intent in db.scalars(select(DeletionIntent)).all()] == ["deleted"]
        assert not (LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png").exists()
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_upload_binary_preserves_persistence_error_when_cleanup_commit_fails(monkeypatch):
    class PersistenceFailure(RuntimeError):
        pass

    class CleanupFailure(RuntimeError):
        pass

    engine, tracker, old = _setup()
    try:
        uploads_routes.SessionLocal = _factory_with_commit_failures(
            tracker, None, None, PersistenceFailure("persist"), CleanupFailure("cleanup")
        )
        with pytest.raises(PersistenceFailure, match="persist"):
            _post_binary(TestClient(app))
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert db.scalars(select(DeletionIntent)).all() == []
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def _seed_gcs_file(tracker):
    from app.core.config import settings

    with tracker.session_maker() as db:
        db.add(UploadFile(id="file-sio-gcs", upload_id=RECORD_ID, storage_bucket=settings.gcs_bucket_name,
                          storage_path=f"uploads/{RECORD_ID}.png", mime_type="image/png", file_size=len(ONE_PIXEL_PNG)))
        db.commit()


def test_content_get_historical_gcs_fails_without_remote_io(monkeypatch):
    engine, tracker, old = _setup()
    try:
        _seed_gcs_file(tracker)
        open_counts = []

        def fake_load(bucket, path, **kwargs):
            open_counts.append(tracker.open_count)
            return ONE_PIXEL_PNG

        monkeypatch.setattr(uploads_routes, "load_gcs_file", fake_load)
        response = TestClient(app).get(f"/api/v1/uploads/{RECORD_ID}/content")
        assert response.status_code == 404
        assert ONE_PIXEL_PNG not in response.content
        assert open_counts == []
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_content_get_historical_gcs_never_runs_record_race_callback(monkeypatch):
    engine, tracker, old = _setup()
    try:
        _seed_gcs_file(tracker)

        def load_then_lose_record(bucket, path, **kwargs):
            with tracker.session_maker() as db:
                db.query(UploadFile).filter(UploadFile.upload_id == RECORD_ID).delete(synchronize_session=False)
                db.commit()
            _delete_record(tracker)
            return ONE_PIXEL_PNG

        monkeypatch.setattr(uploads_routes, "load_gcs_file", load_then_lose_record)
        response = TestClient(app).get(f"/api/v1/uploads/{RECORD_ID}/content")
        assert response.status_code == 404
        assert ONE_PIXEL_PNG not in response.content
        with tracker.session_maker() as db:
            assert db.get(Upload, RECORD_ID) is not None
    finally:
        _teardown(engine, old)


def test_content_get_historical_gcs_never_runs_membership_race_callback(monkeypatch):
    engine, tracker, old = _setup()
    try:
        _seed_gcs_file(tracker)

        def load_then_lose_membership(bucket, path, **kwargs):
            _delete_membership(tracker)
            return ONE_PIXEL_PNG

        monkeypatch.setattr(uploads_routes, "load_gcs_file", load_then_lose_membership)
        response = TestClient(app).get(f"/api/v1/uploads/{RECORD_ID}/content")
        assert response.status_code == 404
        assert ONE_PIXEL_PNG not in response.content
        with tracker.session_maker() as db:
            assert db.scalar(select(FamilyMember.id).where(
                FamilyMember.room_id == ROOM_ID,
                FamilyMember.user_id == USER_ID,
            )) is not None
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_content_get_historical_gcs_never_runs_identity_race_callback(monkeypatch):
    engine, tracker, old = _setup()
    try:
        _seed_gcs_file(tracker)

        def load_then_lose_identity(bucket, path, **kwargs):
            _delete_identity(tracker)
            return ONE_PIXEL_PNG

        monkeypatch.setattr(uploads_routes, "load_gcs_file", load_then_lose_identity)
        response = TestClient(app).get(f"/api/v1/uploads/{RECORD_ID}/content")
        assert response.status_code == 404
        assert ONE_PIXEL_PNG not in response.content
        with tracker.session_maker() as db:
            assert db.scalar(select(AuthIdentity.id).where(
                AuthIdentity.user_id == USER_ID
            )) is not None
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)


def test_local_content_get_refuses_untrusted_locator(tmp_path):
    engine, tracker, old = _setup()
    try:
        outside = tmp_path / "secret.png"
        outside.write_bytes(ONE_PIXEL_PNG)
        with tracker.session_maker() as db:
            db.add(UploadFile(id="file-sio-bad", upload_id=RECORD_ID, storage_bucket="local-media",
                              storage_path=str(outside), mime_type="image/png", file_size=1))
            db.commit()
        response = TestClient(app).get(f"/api/v1/uploads/{RECORD_ID}/content")
        assert response.status_code == 404
        assert ONE_PIXEL_PNG not in response.content
    finally:
        _teardown(engine, old)


def test_bootstrap_demo_stores_files_with_no_session_open(monkeypatch):
    engine, tracker, old = _setup()
    stored_paths: list[str] = []
    try:
        open_counts = []
        real = ai_routes.store_media_file

        def observed(*args, **kwargs):
            open_counts.append(tracker.open_count)
            bucket, path = real(*args, **kwargs)
            stored_paths.append(path)
            return bucket, path

        monkeypatch.setattr(ai_routes, "store_media_file", observed)
        response = TestClient(app).post("/api/v1/ai/demo-bootstrap")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["seeded_files"] >= 1
        assert open_counts == [0] * body["seeded_files"]
        with tracker.session_maker() as db:
            assert len(db.scalars(select(UploadFile)).all()) == body["seeded_files"]
        assert tracker.open_count == 0

        assert TestClient(app).post("/api/v1/ai/demo-bootstrap").json()["seeded_files"] == 0
        assert len(open_counts) == body["seeded_files"]
    finally:
        _teardown(engine, old)
        for path in stored_paths:
            (LOCAL_MEDIA_ROOT / path).unlink(missing_ok=True)


def test_bootstrap_demo_honors_room_quota(monkeypatch):
    engine, tracker, old = _setup()
    try:
        monkeypatch.setattr(
            settings, "legacy_room_storage_quota_bytes", len(ONE_PIXEL_PNG) - 1
        )
        response = TestClient(app).post("/api/v1/ai/demo-bootstrap")
        assert response.status_code == 413
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert db.scalars(select(LegacyStorageAllocation)).all() == []
    finally:
        _teardown(engine, old)


def test_bootstrap_demo_principal_recheck_discards_stored_media(monkeypatch):
    engine, tracker, old = _setup()
    stored_paths: list[str] = []
    try:
        real = ai_routes.store_media_file

        def store_then_lose_identity(*args, **kwargs):
            bucket, path = real(*args, **kwargs)
            stored_paths.append(path)
            with tracker.session_maker() as db:
                db.query(AuthIdentity).filter(AuthIdentity.user_id == USER_ID).delete()
                db.commit()
            return bucket, path

        monkeypatch.setattr(ai_routes, "store_media_file", store_then_lose_identity)
        response = TestClient(app).post("/api/v1/ai/demo-bootstrap")
        assert response.status_code == 401
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert [intent.status for intent in db.scalars(select(DeletionIntent)).all()] == ["deleted"]
        assert all(not (LOCAL_MEDIA_ROOT / path).exists() for path in stored_paths)
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)
        for path in stored_paths:
            (LOCAL_MEDIA_ROOT / path).unlink(missing_ok=True)


def test_bootstrap_concurrent_winner_keeps_shared_locator(monkeypatch):
    engine, tracker, old = _setup()
    stored_paths: list[str] = []
    try:
        real = ai_routes.store_media_file

        def store_then_persist_winner(*args, **kwargs):
            bucket, path = real(*args, **kwargs)
            stored_paths.append(path)
            upload_id = Path(args[1]).stem
            with tracker.session_maker() as db:
                db.add(UploadFile(
                    id="concurrent-winner",
                    upload_id=upload_id,
                    storage_bucket=bucket,
                    storage_path=path,
                    mime_type="image/png",
                    file_size=len(ONE_PIXEL_PNG),
                ))
                db.commit()
            return bucket, path

        monkeypatch.setattr(ai_routes, "store_media_file", store_then_persist_winner)
        response = TestClient(app).post("/api/v1/ai/demo-bootstrap")
        assert response.status_code == 200, response.text
        assert response.json()["seeded_files"] == 0
        with tracker.session_maker() as db:
            files = db.scalars(select(UploadFile)).all()
            assert [(item.id, item.storage_path) for item in files] == [("concurrent-winner", stored_paths[0])]
            assert db.scalars(select(DeletionIntent)).all() == []
        assert (LOCAL_MEDIA_ROOT / stored_paths[0]).exists()
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)
        for path in stored_paths:
            (LOCAL_MEDIA_ROOT / path).unlink(missing_ok=True)


def test_bootstrap_persistence_failure_discards_unreferenced_media(monkeypatch):
    engine, tracker, old = _setup()
    stored_paths: list[str] = []
    try:
        real = ai_routes.store_media_file

        def observed(*args, **kwargs):
            bucket, path = real(*args, **kwargs)
            stored_paths.append(path)
            return bucket, path

        monkeypatch.setattr(ai_routes, "store_media_file", observed)
        ai_routes.SessionLocal = _factory_with_commit_failures(
            tracker, None, None, RuntimeError("bootstrap persistence failed")
        )
        response = TestClient(app, raise_server_exceptions=False).post("/api/v1/ai/demo-bootstrap")
        assert response.status_code == 500
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert [intent.status for intent in db.scalars(select(DeletionIntent)).all()] == ["deleted"]
        assert all(not (LOCAL_MEDIA_ROOT / path).exists() for path in stored_paths)
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)
        for path in stored_paths:
            (LOCAL_MEDIA_ROOT / path).unlink(missing_ok=True)


def test_bootstrap_preserves_persistence_error_when_cleanup_commit_fails(monkeypatch):
    class PersistenceFailure(RuntimeError):
        pass

    class CleanupFailure(RuntimeError):
        pass

    engine, tracker, old = _setup()
    stored_paths: list[str] = []
    try:
        real = ai_routes.store_media_file

        def observed(*args, **kwargs):
            bucket, path = real(*args, **kwargs)
            stored_paths.append(path)
            return bucket, path

        monkeypatch.setattr(ai_routes, "store_media_file", observed)
        ai_routes.SessionLocal = _factory_with_commit_failures(
            tracker, None, None, PersistenceFailure("persist"), CleanupFailure("cleanup")
        )
        with pytest.raises(PersistenceFailure, match="persist"):
            TestClient(app).post("/api/v1/ai/demo-bootstrap")
        with tracker.session_maker() as db:
            assert db.scalars(select(UploadFile)).all() == []
            assert db.scalars(select(DeletionIntent)).all() == []
        assert tracker.open_count == 0
    finally:
        _teardown(engine, old)
        for path in stored_paths:
            (LOCAL_MEDIA_ROOT / path).unlink(missing_ok=True)


def test_upload_file_id_flows_into_processing_job():
    engine, tracker, old = _setup()
    client = TestClient(app)
    created = None
    try:
        created = client.post("/api/v1/uploads", json={"room_id": ROOM_ID, "type": "image", "title": "영수증"})
        assert created.status_code == 200, created.json()
        assert created.json()["file_id"] is None
        upload_id = created.json()["upload_id"]

        with_file = client.post(
            f"/api/v1/uploads/{upload_id}/binary",
            files={"file": ("receipt.png", io.BytesIO(ONE_PIXEL_PNG), "image/png")},
        )
        assert with_file.status_code == 200, with_file.json()
        file_id = with_file.json()["file_id"]
        assert file_id

        listed = client.get(f"/api/v1/uploads/{ROOM_ID}").json()
        assert {u["upload_id"]: u["file_id"] for u in listed}[upload_id] == file_id

        # No DB inspection needed: the id from the upload response drives the job.
        job = client.post(f"/api/v1/records/{upload_id}/processing-jobs", json={"upload_file_id": file_id, "processor": "ocr"})
        assert job.status_code == 200, job.json()
        assert job.json()["upload_file_id"] == file_id
        assert job.json()["status"] == "PENDING"
    finally:
        if created is not None and created.status_code == 200:
            (LOCAL_MEDIA_ROOT / f"{created.json()['upload_id']}.png").unlink(missing_ok=True)
        _teardown(engine, old)
