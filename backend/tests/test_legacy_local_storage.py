"""B2 Option A: legacy plaintext never crosses the local storage boundary."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.routes import uploads
from app.core.config import settings
from app.main import app
from app.models.upload import UploadFile
from test_upload_immutable_publication import case, post
from test_processing_api import RECORD_ID

REAL_GCS_CLIENT = uploads.get_gcs_client


def test_ambiguous_gcs_success_is_unreachable(case, monkeypatch):
    client, tracker, root = case
    remote = MagicMock()
    committed = {}
    def commit_then_timeout(content, **kwargs):
        committed["orphan"] = content
        raise TimeoutError("synthetic response lost after remote commit")
    blob = remote.bucket.return_value.blob.return_value
    blob.upload_from_string.side_effect = commit_then_timeout
    monkeypatch.setattr(uploads, "get_gcs_client", lambda: remote)
    monkeypatch.setattr(settings, "use_gcs_media_storage", True)
    monkeypatch.setattr(settings, "gcs_bucket_name", "synthetic-bucket")
    response = post(client, uploads.ONE_PIXEL_PNG + b"synthetic-private-original")
    assert response.status_code == 200
    # Pre-fix: API reports local success while the fake cloud retains plaintext.
    assert blob.upload_from_string.call_count == 0, committed
    assert committed == {}
    with tracker.session_maker() as db:
        stored = db.get(UploadFile, response.json()["file_id"])
        assert stored.storage_bucket == "local-media"
        assert (root / stored.storage_path).read_bytes() == uploads.ONE_PIXEL_PNG + b"synthetic-private-original"


@pytest.mark.parametrize("enabled,bucket", [(False, ""), (False, "synthetic-bucket"), (True, ""), (True, "synthetic-bucket")])
def test_legacy_config_cannot_enable_remote_upload(case, monkeypatch, caplog, enabled, bucket):
    # Earlier migration tests use fileConfig, which disables existing app loggers.
    monkeypatch.setattr(uploads.logger, "disabled", False)
    client, _, _ = case
    factory = MagicMock()
    monkeypatch.setattr(uploads, "get_gcs_client", factory)
    monkeypatch.setattr(settings, "use_gcs_media_storage", enabled)
    monkeypatch.setattr(settings, "gcs_bucket_name", bucket)
    assert post(client, uploads.ONE_PIXEL_PNG + b"local-only").status_code == 200
    factory.assert_not_called()
    if enabled:
        assert "local only" in caplog.text


def test_valid_mocked_credentials_do_not_activate_gcs(case, monkeypatch):
    from google.cloud import storage
    from google.oauth2 import service_account
    client, _, root = case
    credential_path = root / "synthetic-credential.json"
    credential_path.write_text("{}")
    credentials = MagicMock(valid=True)
    credential_loader = MagicMock(return_value=credentials)
    remote_factory = MagicMock()
    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", credential_loader)
    monkeypatch.setattr(storage, "Client", remote_factory)
    monkeypatch.setattr(settings, "gcp_credentials_path", str(credential_path))
    monkeypatch.setattr(settings, "gcp_project_id", "synthetic-project")
    monkeypatch.setattr(settings, "gcs_bucket_name", "synthetic-bucket")
    monkeypatch.setattr(settings, "use_gcs_media_storage", True)
    # Restore the real factory from the fixture override to exercise config selection.
    monkeypatch.setattr(uploads, "get_gcs_client", REAL_GCS_CLIENT)
    assert post(client, uploads.ONE_PIXEL_PNG + b"private-original").status_code == 200
    credential_loader.assert_not_called()
    remote_factory.assert_not_called()


def test_local_write_failure_never_falls_back_to_gcs(case, monkeypatch):
    remote = MagicMock()
    monkeypatch.setattr(uploads, "get_gcs_client", remote)
    monkeypatch.setattr(settings, "use_gcs_media_storage", True)
    def fail(*args, **kwargs):
        raise OSError("synthetic local disk failure")
    monkeypatch.setattr(uploads, "store_media_file_locally", fail)
    response = post(TestClient(app, raise_server_exceptions=False), uploads.ONE_PIXEL_PNG + b"private-original")
    assert response.status_code == 500
    remote.assert_not_called()


def test_local_original_replacement_and_delete(case, monkeypatch):
    client, tracker, root = case
    monkeypatch.setattr(settings, "use_gcs_media_storage", True)
    remote = MagicMock()
    monkeypatch.setattr(uploads, "get_gcs_client", remote)
    for data in (uploads.ONE_PIXEL_PNG + b"first", uploads.ONE_PIXEL_PNG + b"second"):
        response = post(client, data)
        assert response.status_code == 200
        content = client.get(f"/api/v1/uploads/{RECORD_ID}/content")
        assert content.status_code == 200
        assert content.content == data
        assert content.headers["cache-control"] == "no-store"
    with tracker.session_maker() as db:
        current = db.get(UploadFile, response.json()["file_id"])
        current_path = root / current.storage_path
    assert client.delete(f"/api/v1/uploads/{RECORD_ID}").status_code == 200
    assert current_path.exists()
    from app.models.record import Record
    with tracker.session_maker() as db:
        record = db.get(Record, RECORD_ID)
        assert record.lifecycle_state == "SOFT_DELETED"
        assert record.soft_deleted_at is not None
    assert client.get(f"/api/v1/uploads/{RECORD_ID}/content").status_code == 404
    remote.assert_not_called()


def test_status_and_smoke_test_do_not_enable_plaintext_gcs(case, monkeypatch):
    client, _, _ = case
    remote = MagicMock()
    monkeypatch.setattr(uploads, "get_gcs_client", remote)
    monkeypatch.setattr(settings, "use_gcs_media_storage", True)
    health = client.get("/api/v1/system/health/gcp")
    assert health.status_code == 200
    assert health.json()["gcs_media_storage_enabled"] is False
    assert health.json()["legacy_media_storage"] == "local-only"
    status = client.get("/api/v1/ai/runtime-status")
    assert status.status_code == 200
    assert status.json()["gcs_media_storage_enabled"] is False
    response = client.post("/api/v1/system/health/gcp/storage-smoke-test")
    assert response.status_code == 409
    assert "local only" in response.json()["detail"]
    remote.assert_not_called()
