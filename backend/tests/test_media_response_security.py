"""B4 passive responses and header-only auth; no external services."""
from ocr_approval_helpers import approve_job_fixture
import hashlib
import json
import pytest

from app.api import deps
from app.api.routes import uploads
from app.core.security import create_access_token
from app.main import app
from app.models.upload import Upload, UploadFile
from app.models.processing_job import ProcessingJob
from test_citation_original_binding import original_case, rig, URL, ORIGINAL
from test_retrieval_indexing import USER_ID, RECORD_ID, FILE_ID, JOB_ID, ROOM_ID

@pytest.mark.parametrize("route", ["ordinary", "original", "ask"])
def test_query_token_cannot_authenticate_any_media_or_principal_route(original_case, route):
    _, _, _, client, _ = original_case
    app.dependency_overrides.pop(deps.get_current_user)
    app.dependency_overrides.pop(deps.get_authenticated_principal)
    token = create_access_token(USER_ID)
    path = URL if route == "original" else f"/api/v1/uploads/{RECORD_ID}/content"
    if route == "ask":
        path = f"/api/v1/families/{ROOM_ID}/ask"
    def call(headers):
        if route == "ask":
            return client.post(path, params={"access_token": token}, headers=headers,
                json={"query": "아버지 검진은 몇 시였어?", "model_id": "exaone-45-33b-dgx", "arm": "vector", "k": 1})
        return client.get(path, params={"access_token": token, **({"expected_sha256": hashlib.sha256(ORIGINAL).hexdigest()} if route == "original" else {})}, headers=headers)
    assert call({}).status_code == 401
    assert call({"Authorization": "Bearer " + token}).status_code == 200

@pytest.mark.parametrize("endpoint", ["ordinary", "original"])
@pytest.mark.parametrize("payload,claimed,expected,disposition", [
    (b"<!doctype html><script>window.test=1</script>", "text/html", "application/octet-stream", "attachment"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "image/svg+xml", "application/octet-stream", "attachment"),
    (b"<html>mislabeled image</html>", "image/png", "application/octet-stream", "attachment"),
    (b"unknown bytes", "video/mp4", "application/octet-stream", "attachment"),
    (b"\xff\xd8\xff\xe0" + bytes(20), "image/jpeg", "image/jpeg", "inline"),
    (b"ID3" + bytes(20), "audio/mpeg", "audio/mpeg", "inline"),
    (b"RIFF" + bytes(4) + b"WAVE" + bytes(20), "audio/wav", "audio/wav", "inline"),
    (bytes([0, 0, 0, 20]) + b"ftypisom" + bytes(20), "video/mp4", "video/mp4", "inline"),
    (uploads.ONE_PIXEL_PNG, "text/html", "image/png", "inline"),
    (uploads.ONE_PIXEL_PNG, "image/png", "image/png", "inline"),
    (b"GIF89a" + bytes(20), "image/gif", "image/gif", "inline"),
    (b"RIFF" + bytes(4) + b"WEBP" + bytes(20), "image/webp", "image/webp", "inline"),
])
def test_response_uses_actual_passive_signature_and_restrictive_headers(original_case, endpoint, payload, claimed, expected, disposition):
    tracker, _, _, client, root = original_case
    digest = hashlib.sha256(payload).hexdigest()
    with tracker.session_maker() as db:
        file = db.get(UploadFile, FILE_ID)
        file.mime_type, file.file_size = claimed, len(payload)
        db.get(Upload, RECORD_ID).title = 'dangerous"\r\nX-Injection: yes.html'
        job = db.get(ProcessingJob, JOB_ID)
        result = json.loads(job.result_json)
        result["source_sha256"] = digest
        job.result_json = json.dumps(result)
        approve_job_fixture(job)
        db.commit()
    (root / (RECORD_ID + ".png")).write_bytes(payload)
    path = (f"/api/v1/uploads/{RECORD_ID}/files/{FILE_ID}/content?expected_sha256={digest}"
        if endpoint == "original" else f"/api/v1/uploads/{RECORD_ID}/content")
    response = client.get(path)
    assert response.status_code == 200 and response.content == payload
    assert response.headers["content-type"] == expected
    assert response.headers["content-disposition"].startswith(disposition + ";")
    assert "\r" not in response.headers["content-disposition"] and "\n" not in response.headers["content-disposition"]
    assert "x-injection" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "sandbox" in response.headers["content-security-policy"]
    if disposition == "attachment":
        assert ".bin" in response.headers["content-disposition"]
