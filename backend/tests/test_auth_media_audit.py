"""Synthetic regressions for room isolation and authenticated private media."""
import hashlib
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from test_retrieval_api import setup_api, teardown_api
from test_retrieval_indexing import (
    FILE_ID, USER_ID, OTHER_USER_ID, ROOM_ID, OTHER_ROOM_ID,
    RECORD_ID, OTHER_RECORD_ID, JOB_ID, OTHER_JOB_ID,
)
from app.api import deps
from app.api.routes import uploads
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.storage_allocation import LegacyStorageAllocation
from app.models.upload import UploadFile


@pytest.mark.parametrize("arm", ["substring", "bm25", "vector", "hybrid_rrf"])
def test_real_jwt_search_isolated_both_directions(arm):
    engine, tracker, old_sl, old_provider = setup_api()
    app.dependency_overrides.pop(deps.get_authenticated_principal)
    users = (
        (USER_ID, ROOM_ID, RECORD_ID, JOB_ID),
        (OTHER_USER_ID, OTHER_ROOM_ID, OTHER_RECORD_ID, OTHER_JOB_ID),
    )
    try:
        with TestClient(app) as client:
            for user_id, room_id, record_id, job_id in users:
                response = client.post(
                    f"/api/v1/records/{record_id}/retrieval-index",
                    headers={"Authorization": "Bearer " + create_access_token(user_id)},
                    json={"processing_job_id": job_id},
                )
                assert response.status_code == 200, response.text
            for user_id, own_room, own_record, _ in users:
                for room_id in (ROOM_ID, OTHER_ROOM_ID):
                    response = client.post(
                        f"/api/v1/families/{room_id}/retrieval/search",
                        headers={"Authorization": "Bearer " + create_access_token(user_id)},
                        json={"query": "여름", "arm": arm, "k": 5},
                    )
                    if room_id == own_room:
                        assert response.status_code == 200, response.text
                        assert {hit["record_id"] for hit in response.json()["hits"]} == {own_record}
                    else:
                        assert response.status_code == 404, response.text
                        assert response.json() == {"detail": "Retrieval room not found"}
                    print(f"{arm} {user_id} -> {room_id}: {response.status_code}")
    finally:
        teardown_api(engine, old_sl, old_provider)


def test_legacy_static_media_requires_authentication(monkeypatch):
    engine, tracker, old_sl, old_provider = setup_api()
    app.dependency_overrides.pop(deps.get_authenticated_principal)
    app.dependency_overrides[get_db] = tracker.get_db
    monkeypatch.setattr(uploads, "SessionLocal", tracker)
    monkeypatch.setattr(settings, "use_gcs_media_storage", False)
    payload = uploads.ONE_PIXEL_PNG + b"ambient-auth-media-audit-synthetic-only"
    auth = {"Authorization": "Bearer " + create_access_token(USER_ID)}
    foreign_auth = {"Authorization": "Bearer " + create_access_token(OTHER_USER_ID)}
    created_file = None
    try:
        with tracker.session_maker() as db:
            existing_file = db.get(UploadFile, FILE_ID)
            existing_file.file_size = 1
            allocation = LegacyStorageAllocation(
                id="auth-media-audit-allocation",
                room_id=ROOM_ID,
                upload_id=RECORD_ID,
                storage_bucket="local-media",
                storage_path=existing_file.storage_path,
                byte_size=1,
                state="ACTIVE",
                expires_at=datetime(2100, 1, 1),
            )
            db.add(allocation)
            existing_file.storage_allocation_id = allocation.id
            db.commit()
        with TestClient(app) as client:
            response = client.post("/api/v1/uploads", headers=auth, json={
                "room_id": ROOM_ID, "type": "image", "title": "Synthetic audit image",
            })
            assert response.status_code == 200, response.text
            record_id = response.json()["upload_id"]
            existing_paths = {path.name for path in uploads.LOCAL_MEDIA_ROOT.iterdir()}
            response = client.post(
                f"/api/v1/uploads/{record_id}/binary", headers=auth,
                files={"file": ("synthetic.png", payload, "image/png")},
            )
            assert response.status_code == 200, response.text
            with tracker.session_maker() as db:
                saved_file = db.get(UploadFile, response.json()["file_id"])
                assert saved_file.upload_id == record_id
                assert saved_file.storage_path not in existing_paths, "Never overwrite an existing file"
                created_file = uploads.LOCAL_MEDIA_ROOT / saved_file.storage_path
            assert created_file.read_bytes() == payload
            content_url = f"/api/v1/uploads/{record_id}/content"
            assert response.json()["file_url"] == str(client.base_url).rstrip("/") + content_url
            protected = client.get(content_url)
            owner = client.get(content_url, headers=auth)
            foreign = client.get(content_url, headers=foreign_auth)
            assert protected.status_code == 401, protected.text
            assert owner.status_code == 200 and owner.content == payload
            missing = client.get("/api/v1/uploads/absent-record/content", headers=foreign_auth)
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json() and payload not in foreign.content
            direct_url = f"/media/uploads/{created_file.name}"
            direct = client.get(direct_url)
            assert "authorization" not in direct.request.headers
            assert "cookie" not in direct.request.headers
            assert not direct.request.url.query
            exact_bytes = direct.content == payload
            print(f"protected_no_auth={protected.status_code}; protected_owner={owner.status_code}; foreign={foreign.status_code}")
            print(f"direct_url={direct_url}; no_jwt_status={direct.status_code}; exact_bytes={exact_bytes}")
            print(f"synthetic_bytes={len(payload)}; sha256={hashlib.sha256(payload).hexdigest()}")
            if direct.status_code == 200 and exact_bytes:
                pytest.fail("AUTH_BYPASS_CONFIRMED: unauthenticated /media returned the uploaded bytes")
            assert direct.status_code in (401, 403, 404), direct.status_code
            assert payload not in direct.content
            deleted = client.delete(f"/api/v1/uploads/{record_id}", headers=auth)
            assert deleted.status_code == 200 and deleted.json()["deleted"] is True
            after_delete = client.get(content_url, headers=auth)
            assert after_delete.status_code == 404 and payload not in after_delete.content
            direct_after_delete = client.get(direct_url)
            assert direct_after_delete.status_code in (401, 403, 404)
            assert payload not in direct_after_delete.content
            print(f"deleted_content={after_delete.status_code}; deleted_direct={direct_after_delete.status_code}")
    finally:
        if created_file is not None:
            created_file.unlink(missing_ok=True)
        teardown_api(engine, old_sl, old_provider)
