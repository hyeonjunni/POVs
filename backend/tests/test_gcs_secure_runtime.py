"""GCS runtime integration using only synthetic keys and mocked SDK transport."""
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4
from urllib.parse import urlsplit

import pytest
from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed
from sqlalchemy import text

from app.core.config import Settings, validate_secure_config
from app.services import secure_catalogue as cat, secure_runtime as rt
from app.storage.contracts import StorageReceipt
from app.storage.gcs import GCSCiphertextStore
from test_secure_catalogue import catalogue
from test_secure_identity import identity_client, create_room
from test_secure_media import record, upload, PNG


def configured(**changes):
    return Settings(_env_file=None, **(dict(security_mode="SECURE", database_url="sqlite:///synthetic.sqlite",
        use_gcs_media_storage=False, use_cloud_sql_connector=False, prefer_ollama_for_gemma=False,
        jwt_secret_key="synthetic-test-only-signing-key-00000000000",
        secure_ciphertext_adapter="gcs", secure_gcs_bucket="synthetic-bucket", secure_gcs_project_id="synthetic-project") | changes))


def binding(engine):
    path = Path(engine.url.database)
    path.parent.chmod(0o700)
    with engine.connect() as db:
        cid = db.scalar(text("SELECT catalogue_id FROM security_control"))
    target = path.with_name(path.name + ".gcs-binding.json")
    target.write_text(json.dumps(dict(version=1, catalogue_id=cid, bucket="synthetic-bucket", project_id="synthetic-project")))
    target.chmod(0o600)
    return target


@pytest.mark.parametrize("changes", [dict(secure_ciphertext_adapter="unknown"), dict(secure_gcs_bucket=""),
    dict(secure_gcs_project_id=""), dict(secure_gcs_bucket="gs://bad"),
    dict(secure_ciphertext_adapter="local", secure_gcs_bucket="synthetic-bucket")])
def test_selection_config_fails_closed(changes):
    with pytest.raises(rt.SecureUnavailable):
        validate_secure_config(configured(**changes))


def test_binding_and_restart_use_exact_catalogue_before_sdk(catalogue, monkeypatch):
    engine, _, _ = catalogue
    binding(engine)
    factory = MagicMock()
    factory.return_value.api_endpoint = "https://storage.googleapis.com"
    bucket = factory.return_value.bucket.return_value
    bucket.name = "synthetic-bucket"
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    settings = configured()
    for _ in range(2):
        assert isinstance(rt.ciphertext_store(engine, settings, os.geteuid()), GCSCiphertextStore)
    assert factory.call_count == 2
    factory.assert_called_with(project="synthetic-project", client_options={"api_endpoint":"https://storage.googleapis.com"})
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(engine, configured(secure_gcs_bucket="other-bucket"), os.geteuid())
    assert factory.call_count == 2


@pytest.mark.parametrize("problem", ["missing", "catalogue", "extra", "version", "permissions", "symlink", "hardlink", "invalid", "emulator"])
def test_invalid_binding_never_constructs_sdk(catalogue, monkeypatch, problem):
    engine, _, _ = catalogue
    path = binding(engine)
    if problem == "missing": path.unlink()
    elif problem == "permissions": path.chmod(0o644)
    elif problem == "symlink":
        copy = path.with_suffix(".copy")
        path.rename(copy)
        path.symlink_to(copy)
    elif problem == "hardlink": os.link(path, path.with_suffix(".copy"))
    elif problem == "invalid": path.write_text("[]")
    elif problem == "emulator": monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://synthetic.invalid")
    else:
        data = json.loads(path.read_text())
        data[{"catalogue":"catalogue_id", "extra":"unexpected", "version":"version"}[problem]] = "wrong"
        path.write_text(json.dumps(data))
    factory = MagicMock(side_effect=AssertionError("SDK accessed before binding proof"))
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(engine, configured(), os.geteuid())
    factory.assert_not_called()


def test_gcs_receipt_serialization_retains_exact_adapter():
    receipt = StorageReceipt("gcs", str(uuid4()), "123", 1, hashlib.sha256(b"x").digest())
    assert json.loads(cat.receipt_json(receipt))["adapter"] == "gcs"
    for version in ("0", "01", "-1", "1.0", "１２３", str(uuid4())):
        with pytest.raises(cat.SecureStateChanged):
            cat.receipt_json(replace(receipt, exact_version=version))


class Bucket:
    name = "synthetic-bucket"
    def __init__(self):
        self.objects = {}
        self.deleted = []
        self.failure = None
    def blob(self, name, generation=None):
        bucket = self
        class Blob:
            def __init__(self): self.generation = generation
            def upload_from_string(self, raw, **kwargs):
                assert kwargs == dict(content_type="application/octet-stream", if_generation_match=0, timeout=60)
                if bucket.failure == "upload": raise Forbidden("synthetic denied")
                if name in bucket.objects: raise PreconditionFailed("synthetic collision")
                self.generation = 1
                bucket.objects[name] = bytes(raw)
                if bucket.failure == "uncertain": raise TimeoutError("synthetic uncertain")
            def download_as_bytes(self, **kwargs):
                assert kwargs["if_generation_match"] == self.generation == 1
                if bucket.failure == "read": raise Forbidden("synthetic denied")
                if name not in bucket.objects: raise NotFound("synthetic missing")
                return bucket.objects[name]
            def delete(self, **kwargs):
                assert kwargs == dict(if_generation_match=1, timeout=60)
                if name not in bucket.objects: raise NotFound("synthetic missing")
                del bucket.objects[name]
                bucket.deleted.append(name)
        return Blob()


@pytest.fixture
def gcs_client(identity_client, monkeypatch):
    c = identity_client
    c.bucket = Bucket()
    binding(c.engine)
    factory = MagicMock()
    factory.return_value.api_endpoint = "https://storage.googleapis.com"
    factory.return_value.bucket.return_value = c.bucket
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    c.app.state.secure_runtime.store = rt.ciphertext_store(c.engine, configured(), os.geteuid())
    c.session, _, _ = c.signup()
    c.room, _ = create_room(c, c.session)
    c.auth = {"Authorization": "Bearer " + c.session["access_token"]}
    return c


def test_secure_gcs_activation_upload_read_and_exact_delete(gcs_client):
    c = gcs_client
    row = upload(c)
    original = urlsplit(row["file_url"])
    path = original.path.removeprefix("/api/v1") + "?" + original.query
    assert c.request("GET", "/uploads/" + row["upload_id"] + "/content", headers=c.auth).status_code == 409
    assert c.request("GET", path, headers=c.auth).content == PNG
    assert all(PNG not in value for value in c.bucket.objects.values())
    with c.engine.connect() as db:
        rows = db.execute(text("SELECT * FROM encrypted_objects")).mappings().all()
        assert rows and {r["adapter"] for r in rows} == {"gcs"}
        assert {"user_profile", "auth_identity", "room_metadata", "original"} <= {r["artifact_kind"] for r in rows}
        original = next(r for r in rows if r["artifact_kind"] == "original")
        receipt = cat.envelope_values(original)[3]
    assert all(key not in blob for key in c.material.values() for blob in c.bucket.objects.values())
    assert all(bytes(r["wrapped_dek"]) not in blob for r in rows for blob in c.bucket.objects.values())
    c.app.state.secure_runtime.store = GCSCiphertextStore(c.bucket)
    assert hashlib.sha256(c.request("GET", path, headers=c.auth).content).digest() == hashlib.sha256(PNG).digest()
    assert c.app.state.secure_runtime.store.delete(receipt) == "deleted"
    assert c.app.state.secure_runtime.store.delete(receipt) == "absent"
    response = c.request("GET", path, headers=c.auth)
    assert response.status_code == 503 and PNG not in response.content


@pytest.mark.parametrize("failure", ["upload", "uncertain", "read"])
def test_uncertain_media_creation_does_not_publish_or_delete(gcs_client, failure):
    c = gcs_client
    row = record(c)
    c.bucket.failure = failure
    response = c.request("POST", "/uploads/" + row["upload_id"] + "/binary", headers=c.auth,
        files={"file": ("synthetic.png", PNG, "image/png")})
    assert response.status_code == 503
    assert not c.bucket.deleted
    with c.engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM protected_pointers WHERE owner_type='file' AND state='ENCRYPTED_ACTIVE'")) == 0
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects WHERE artifact_kind='original' AND state='ACTIVE'")) == 0


def test_local_default_never_resolves_adc(catalogue, monkeypatch):
    engine, _, path = catalogue
    private = path.parent / "ciphertext"
    private.mkdir(mode=0o700)
    factory = MagicMock(side_effect=AssertionError("local runtime resolved ADC"))
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    settings = configured(secure_ciphertext_adapter="local", secure_gcs_bucket="", secure_gcs_project_id="",
                          secure_ciphertext_directory=str(private))
    validate_secure_config(settings)
    store = rt.ciphertext_store(engine, settings, os.geteuid())
    receipt = store.create(uuid4(), b"synthetic ciphertext")
    assert store.read(receipt) == b"synthetic ciphertext"
    factory.assert_not_called()


def test_local_history_cannot_be_reinterpreted_as_gcs(identity_client, monkeypatch):
    c = identity_client
    c.signup()
    binding(c.engine)
    factory = MagicMock(side_effect=AssertionError("SDK created with local history"))
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(c.engine, configured(), os.geteuid())
    factory.assert_not_called()


def test_gcs_history_cannot_be_reinterpreted_as_local(gcs_client, monkeypatch):
    c = gcs_client
    constructor = MagicMock(side_effect=AssertionError("fallback constructed"))
    monkeypatch.setattr("app.storage.local.LocalCiphertextStore", constructor)
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(c.engine, configured(secure_ciphertext_adapter="local", secure_gcs_bucket="", secure_gcs_project_id=""), os.geteuid())
    constructor.assert_not_called()


def test_adc_failure_has_no_local_fallback(catalogue, monkeypatch):
    engine, _, _ = catalogue
    binding(engine)
    factory = MagicMock(side_effect=RuntimeError("synthetic credential failure"))
    local = MagicMock(side_effect=AssertionError("fallback constructed"))
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    monkeypatch.setattr("app.storage.local.LocalCiphertextStore", local)
    with pytest.raises(rt.SecureUnavailable, match="credential_unavailable"):
        rt.ciphertext_store(engine, configured(), os.geteuid())
    local.assert_not_called()


def test_secure_gcs_synthetic_processing_search_citation_and_revocation(gcs_client, monkeypatch):
    from test_secure_retrieval import processed, index_secure, search_secure, ask_secure
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    from app.ai.grounded_answering import SyntheticPlanProvider
    c = gcs_client
    runtime = c.app.state.secure_runtime
    runtime.embedding_provider = SyntheticTokenHashProvider()
    runtime.inference_provider = SyntheticPlanProvider()
    row, done = processed(c, monkeypatch)
    assert index_secure(c, row, done).status_code == 200
    assert search_secure(c).status_code == 200
    answer = ask_secure(c)
    assert answer.status_code == 200 and answer.json()["provider_mode"] == "selected", answer.text
    assert answer.json()["citations"]
    assert row["upload_id"] in json.dumps(answer.json()["citations"])
    original = urlsplit(row["file_url"])
    path = original.path.removeprefix("/api/v1") + "?" + original.query
    assert c.request("GET", "/uploads/" + row["upload_id"] + "/content", headers=c.auth).status_code == 409
    assert hashlib.sha256(c.request("GET", path, headers=c.auth).content).digest() == hashlib.sha256(PNG).digest()
    assert c.request("GET", path).status_code == 401
    other, _, _ = c.signup("other-user", "other@example.com")
    denied = c.request("GET", path, headers={"Authorization":"Bearer " + other["access_token"]})
    assert denied.status_code in (404, 503) and PNG not in denied.content
    assert c.request("DELETE", "/uploads/" + row["upload_id"], headers=c.auth).status_code == 200
    assert c.request("GET", path, headers=c.auth).status_code in (404, 503)
    assert not c.bucket.deleted  # Logical revocation never silently enables physical cleanup.


@pytest.mark.parametrize("variable", ["API_ENDPOINT_OVERRIDE", "STORAGE_EMULATOR_HOST"])
def test_endpoint_override_rejected_before_adc(catalogue, monkeypatch, variable):
    engine, _, _ = catalogue
    binding(engine)
    factory = MagicMock()
    factory.return_value.bucket.return_value.name = "synthetic-bucket"
    factory.return_value.api_endpoint = "https://storage.googleapis.com"
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    monkeypatch.setenv(variable, "https://synthetic-foreign.invalid")
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(engine, configured(), os.geteuid())
    factory.assert_not_called()


def test_actual_sdk_endpoint_must_match_before_bucket_is_bound(catalogue, monkeypatch):
    engine, _, _ = catalogue
    binding(engine)
    factory = MagicMock()
    factory.return_value.api_endpoint = "https://synthetic-foreign.invalid"
    factory.return_value.bucket.return_value.name = "synthetic-bucket"
    monkeypatch.setattr("google.cloud.storage.Client", factory)
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(engine, configured(), os.geteuid())
    factory.return_value.bucket.assert_not_called()


@pytest.mark.parametrize("state", ["valid", "malformed", "dangling_symlink"])
def test_empty_catalogue_with_gcs_binding_never_selects_local(catalogue, monkeypatch, state):
    engine, _, _ = catalogue
    path = binding(engine)
    if state == "malformed": path.write_text("invalid")
    elif state == "dangling_symlink":
        path.unlink()
        path.symlink_to(path.with_suffix(".missing"))
    with engine.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM encrypted_objects")) == 0
    local = MagicMock()
    sdk = MagicMock()
    monkeypatch.setattr("app.storage.local.LocalCiphertextStore", local)
    monkeypatch.setattr("google.cloud.storage.Client", sdk)
    with pytest.raises(rt.SecureUnavailable):
        rt.ciphertext_store(engine, configured(secure_ciphertext_adapter="local", secure_gcs_bucket="", secure_gcs_project_id=""), os.geteuid())
    local.assert_not_called()
    sdk.assert_not_called()


def test_secure_rejected_review_never_becomes_gcs_evidence(gcs_client, monkeypatch):
    from test_secure_retrieval import processed, index_secure, search_secure, ask_secure
    from app.retrieval.evaluation import SyntheticTokenHashProvider
    from app.ai.grounded_answering import SyntheticPlanProvider
    c=gcs_client
    runtime=c.app.state.secure_runtime
    runtime.embedding_provider=SyntheticTokenHashProvider()
    runtime.inference_provider=SyntheticPlanProvider()
    row,done=processed(c,monkeypatch)
    assert index_secure(c,row,done).status_code==200
    with c.engine.begin() as db:
        # The identity fixture uses the existing 0009 review schema.
        db.execute(text("UPDATE processing_jobs SET review_json=:review WHERE id=:j"),
            {"j":done["job_id"],"review":json.dumps({"status":"rejected"})})
    answer=ask_secure(c)
    assert answer.status_code in (409,503), "rejected OCR admitted to evidence"
    assert search_secure(c).status_code in (409,503)
