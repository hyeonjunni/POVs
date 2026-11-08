"""Measured uploads and final authorization: synthetic local tests only."""
import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select, update

from app.api.routes import uploads
from app.core.config import settings
from app.models.family import FamilyMember
from app.models.upload import UploadFile
from test_upload_immutable_publication import case
from test_processing_api import RECORD_ID, ROOM_ID, USER_ID
from test_secure_media import media_client, record, identity_client, catalogue


def image_bytes(fmt="PNG"):
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), (30, 90, 160)).save(stream, format=fmt)
    return stream.getvalue()


PNG = image_bytes()
BAD = [
    (b"", "image/png", "x.png"),
    (b"<html>private</html>", "image/png", "x.png"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/png", "x.png"),
    (PNG[:24], "image/png", "x.png"),
    (PNG[:45] + b"broken" + PNG[51:], "image/png", "x.png"),
    (PNG, "image/jpeg", "x.jpg"),
    (PNG, "image/png", "x.jpg"),
    (PNG, "image/png", "x.svg"),
]


@pytest.mark.parametrize("data,mime,name", BAD)
def test_legacy_rejects_invalid_before_publication(case, data, mime, name):
    client, tracker, root = case
    with tracker.session_maker() as db:
        before = set(db.scalars(select(UploadFile.id)))
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary", files={"file": (name, data, mime)})
    assert response.status_code == 400
    assert not list(root.iterdir())
    with tracker.session_maker() as db:
        assert set(db.scalars(select(UploadFile.id))) == before


@pytest.mark.parametrize("data,mime,name", BAD)
def test_secure_rejects_invalid_before_publication(media_client, data, mime, name):
    c = media_client
    row = record(c)
    before = len(c.objects)
    response = c.request("POST", f"/uploads/{row['upload_id']}/binary",
                         headers=c.auth, files={"file": (name, data, mime)})
    assert response.status_code == 400
    assert len(c.objects) == before


def test_legacy_limit_is_measured_without_spooling(case, monkeypatch):
    client, _, root = case
    monkeypatch.setattr(settings, "ocr_max_input_bytes", len(PNG) - 1)
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 413
    assert not list(root.iterdir())


def test_local_membership_revoked_before_open_releases_no_bytes(case, monkeypatch):
    client, tracker, root = case
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 200
    with tracker.session_maker() as db:
        target = root / db.get(UploadFile, response.json()["file_id"]).storage_path
    real_open = Path.open
    revoked = []
    def revoke(path, *args, **kwargs):
        if path == target and not revoked:
            with tracker.session_maker() as db:
                db.execute(update(FamilyMember).where(FamilyMember.room_id == ROOM_ID, FamilyMember.user_id == USER_ID).values(is_active=False))
                db.commit()
            revoked.append(True)
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", revoke)
    response = client.get(f"/api/v1/uploads/{RECORD_ID}/content")
    assert revoked
    assert response.status_code in (403, 404)
    assert PNG not in response.content


@pytest.mark.parametrize("fmt,mime,suffix", [("PNG", "image/png", ".png"), ("JPEG", "image/jpeg", ".jpg"), ("WEBP", "image/webp", ".webp"), ("GIF", "image/gif", ".gif")])
def test_supported_images_validate_and_remain_readable(case, fmt, mime, suffix, monkeypatch):
    client, _, _ = case
    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", lambda *a, **k: pytest.fail("unbounded multipart spool"))
    content = image_bytes(fmt)
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary", files={"file": ("x" + suffix, content, mime)})
    assert response.status_code == 200
    response = client.get(f"/api/v1/uploads/{RECORD_ID}/content")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == mime


def test_decoded_image_pixel_budget_is_enforced(case, monkeypatch):
    from app.services import media_validation
    monkeypatch.setattr(media_validation, "MAX_IMAGE_PIXELS", 3)
    client, _, root = case
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 400
    assert not list(root.iterdir())


def test_stream_stops_at_measured_limit_without_consuming_tail(case, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.services.media_validation import read_media_upload

    chunks = iter([
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="x.png"\r\nContent-Type: image/png\r\n\r\n',
        PNG,
    ])
    calls = []
    async def receive():
        calls.append(True)
        chunk = next(chunks)  # No terminating body is needed: parser must stop now.
        return {"type": "http.request", "body": chunk, "more_body": True}
    request = Request({"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=b")]}, receive)
    with pytest.raises(HTTPException) as error:
        asyncio.run(read_media_upload(request, max_bytes=len(PNG) - 1))
    assert error.value.status_code == 413
    assert len(calls) == 2


def test_current_media_never_reopens_path_after_final_authorization(case, monkeypatch):
    client, tracker, root = case
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary", files={"file": ("x.png", PNG, "image/png")})
    assert response.status_code == 200
    with tracker.session_maker() as db:
        target = root / db.get(UploadFile, response.json()["file_id"]).storage_path
    original = uploads._media_snapshot
    calls = []
    def snapshot(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 2:
            target.write_bytes(b"later-mutation")
        return result
    monkeypatch.setattr(uploads, "_media_snapshot", snapshot)
    response = client.get(f"/api/v1/uploads/{RECORD_ID}/content")
    assert response.status_code == 200 and response.content == PNG
    assert len(calls) == 2


@pytest.mark.parametrize("fmt,mime,suffix", [("PNG", "image/png", ".png"), ("GIF", "image/gif", ".gif"), ("JPEG", "image/jpeg", ".jpg"), ("WEBP", "image/webp", ".webp")])
def test_missing_last_image_container_byte_is_rejected(case, fmt, mime, suffix):
    client, _, root = case
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary",
                           files={"file": ("x" + suffix, image_bytes(fmt)[:-1], mime)})
    assert response.status_code == 400
    assert not list(root.iterdir())
