"""AL-RT-01/03 regression tests; synthetic local files and fake GCS only."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.routes import uploads
from app.core.config import settings
from app.processing.contracts import ProviderError
from app.services import account_cleanup, media_loader
from test_upload_immutable_publication import case, post


def test_historical_legacy_gcs_read_is_explicitly_unsupported(case, monkeypatch):
    remote = MagicMock(return_value=b"historical plaintext")
    monkeypatch.setattr(settings, "gcs_bucket_name", "synthetic-bucket")
    monkeypatch.setattr(media_loader, "load_gcs_file", remote)

    with pytest.raises(ProviderError) as exc_info:
        media_loader.load_media_bytes(
            "synthetic-bucket",
            "uploads/record-pa-1.png",
            record_id="record-pa-1",
            max_bytes=1024,
        )

    assert exc_info.value.code == "historical_storage_unsupported"
    remote.assert_not_called()


def test_historical_legacy_gcs_delete_is_explicitly_unsupported(case, monkeypatch):
    remote = MagicMock(return_value=True)
    monkeypatch.setattr(settings, "gcs_bucket_name", "synthetic-bucket")
    monkeypatch.setattr(account_cleanup, "delete_gcs_blob", remote)

    with pytest.raises(RuntimeError, match="historical_storage_unsupported"):
        account_cleanup._delete_storage_object(
            "synthetic-bucket", "uploads/record-pa-1.png"
        )

    remote.assert_not_called()


def test_repeated_uploads_cannot_exceed_room_quota(case, monkeypatch):
    client, _, _ = case
    payload = uploads.ONE_PIXEL_PNG + b"quota"
    monkeypatch.setattr(
        settings,
        "legacy_room_storage_quota_bytes",
        20 + len(payload),
        raising=False,
    )

    assert post(client, payload).status_code == 200
    rejected = post(client, payload)

    assert rejected.status_code == 413
    assert rejected.json() == {"detail": "Family room storage quota exceeded"}
