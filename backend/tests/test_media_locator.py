"""A6 locator provenance: bucket must be server-controlled, local paths confined,
and the object name bound to the record — checked before any byte fetch."""
from __future__ import annotations

import pytest

from app.api.routes.uploads import LOCAL_MEDIA_ROOT, ONE_PIXEL_PNG
from app.core.config import settings
from app.processing.contracts import ProviderError
from app.services import media_loader

RECORD_ID = "record-loc-1"


@pytest.fixture()
def no_gcs_fetch(monkeypatch):
    def _forbidden(bucket, path, **kwargs):
        raise AssertionError(f"byte fetch attempted for {bucket}/{path}")

    monkeypatch.setattr(media_loader, "load_gcs_file", _forbidden)


@pytest.mark.parametrize(
    "bucket, path",
    [
        ("someone-elses-bucket", f"uploads/{RECORD_ID}.png"),
        (settings.gcs_bucket_name, "uploads/record-other.png"),
        (settings.gcs_bucket_name, f"{RECORD_ID}.png"),
        (settings.gcs_bucket_name, f"uploads/../{RECORD_ID}.png"),
        ("local-media", f"../{RECORD_ID}.png"),
        ("local-media", f"sub/{RECORD_ID}.png"),
        ("local-media", "record-other.png"),
        ("local-media", "legacy-client-attached.png"),
    ],
)
def test_untrusted_locators_fail_closed_without_fetch(no_gcs_fetch, bucket, path):
    with pytest.raises(ProviderError) as exc_info:
        media_loader.load_media_bytes(bucket, path, record_id=RECORD_ID, max_bytes=1024)
    assert exc_info.value.code == "locator_untrusted"


def test_absolute_local_path_fails_closed(no_gcs_fetch, tmp_path):
    outside = tmp_path / f"{RECORD_ID}.png"
    outside.write_bytes(ONE_PIXEL_PNG)
    with pytest.raises(ProviderError) as exc_info:
        media_loader.load_media_bytes("local-media", str(outside), record_id=RECORD_ID, max_bytes=1024)
    assert exc_info.value.code == "locator_untrusted"


def test_each_untrusted_locator_failure_has_a_fresh_exception(no_gcs_fetch):
    errors = []
    for _ in range(2):
        with pytest.raises(ProviderError) as exc_info:
            media_loader.load_media_bytes(
                "someone-elses-bucket",
                f"uploads/{RECORD_ID}.png",
                record_id=RECORD_ID,
                max_bytes=1024,
            )
        errors.append(exc_info.value)

    assert errors[0] is not errors[1]


def test_server_named_local_locator_is_readable():
    media_path = LOCAL_MEDIA_ROOT / f"{RECORD_ID}.png"
    media_path.write_bytes(ONE_PIXEL_PNG)
    try:
        assert media_loader.load_media_bytes(
            "local-media", f"{RECORD_ID}.png", record_id=RECORD_ID, max_bytes=1024
        ) == ONE_PIXEL_PNG
    finally:
        media_path.unlink(missing_ok=True)


def test_client_locator_attach_route_is_gone():
    from fastapi.testclient import TestClient

    from app.main import app

    response = TestClient(app).post(
        "/api/v1/uploads/record-x/file",
        json={"storage_bucket": "b", "storage_path": "p", "mime_type": "image/png"},
    )
    assert response.status_code in (404, 405)
