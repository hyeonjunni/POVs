"""Public system status never exposes or probes private provider diagnostics."""
from urllib import error, request
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from google.cloud import storage

from app.api.routes import system
from app.core.config import settings
from app.core.security import create_access_token
from app.main import app


@pytest.mark.parametrize("authenticated", [False, True], ids=["no-jwt", "normal-user-jwt"])
@pytest.mark.parametrize("method,path,status,payload", [
    ("GET", "/system/health/gcp", 200, {
        "gcs_media_storage_enabled": False,
        "legacy_media_storage": "local-only",
    }),
    ("GET", "/system/health/ollama", 200, {"ollama": "diagnostics-disabled"}),
    ("POST", "/system/health/gcp/storage-smoke-test", 409, {
        "detail": "LEGACY plaintext GCS uploads are disabled; media storage is local only",
    }),
    ("GET", "/ai/runtime-status", 200, {
        "status": "diagnostics-disabled",
        "gcs_media_storage_enabled": False,
    }),
])
def test_public_system_routes_are_redacted_and_inert(
    monkeypatch, authenticated, method, path, status, payload,
):
    # A normal user token must not unlock operator diagnostics or mutations.
    headers = {"Authorization": "Bearer " + create_access_token("ordinary-user")} if authenticated else {}
    for field, value in {
        "gcp_project_id": "private-project",
        "instance_connection_name": "private-project:region:private-db",
        "gcs_bucket_name": "private-bucket",
        "gcp_credentials_path": "/private/credentials/provider-key.json",
        "ollama_base_url": "http://private-provider:11434/token-secret",
        "use_gcs_media_storage": True,
        "use_cloud_sql_connector": True,
    }.items():
        monkeypatch.setattr(settings, field, value)
    network = MagicMock(side_effect=error.URLError("private-provider token-secret connection failure"))
    provider = MagicMock(side_effect=AssertionError("provider SDK must not run"))
    credential_path = MagicMock()
    monkeypatch.setattr(request, "urlopen", network)
    monkeypatch.setattr(storage, "Client", provider)
    monkeypatch.setattr(system, "Path", credential_path, raising=False)

    response = TestClient(app).request(method, f"/api/v1{path}", headers=headers)

    assert response.status_code == status
    assert response.json() == payload
    network.assert_not_called()
    provider.assert_not_called()
    credential_path.assert_not_called()
