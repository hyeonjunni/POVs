"""B1 independent attack regressions; only explicit ephemeral test signing keys."""
import secrets
import time
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, settings, validate_secure_config
from app.core.security import create_access_token, decode_access_token
from app.services.secure_runtime import SecureUnavailable
from app.api import deps
from app.main import app
from test_processing_api import _setup, _teardown, USER_ID


@pytest.mark.parametrize("mode", ["LEGACY", "SECURE"])
@pytest.mark.parametrize("key", ["", "ambient-legacy-dev-secret", "dev-secret", "short", "x" * 64,
    "ambient-legacy-dev-secret" * 2, "dev-secret" * 8, "default-secret" * 4, " " * 64])
def test_all_modes_reject_missing_public_or_weak_secret(mode, key):
    configured = Settings(_env_file=None, security_mode=mode, jwt_secret_key=key,
        database_url="sqlite:///synthetic.sqlite", use_gcs_media_storage=False,
        use_cloud_sql_connector=False, prefer_ollama_for_gemma=False)
    with pytest.raises(SecureUnavailable):
        validate_secure_config(configured)


@pytest.mark.parametrize("mode", ["LEGACY", "SECURE"])
def test_explicit_ephemeral_secret_is_accepted(mode):
    validate_secure_config(Settings(_env_file=None, security_mode=mode,
        jwt_secret_key=secrets.token_urlsafe(48), database_url="sqlite:///synthetic.sqlite",
        use_gcs_media_storage=False, use_cloud_sql_connector=False, prefer_ollama_for_gemma=False))


def test_model_has_no_public_signing_key_default():
    assert Settings.model_fields["jwt_secret_key"].default == ""


@pytest.fixture
def protected_client(monkeypatch):
    engine, tracker, old_session, old_ocr = _setup()
    from sqlalchemy import select
    from app.models.user import User
    with tracker.session_maker() as db:
        for index, user in enumerate(db.scalars(select(User))):
            user.email = f"synthetic{index}@example.com"
        db.commit()
    app.dependency_overrides.pop(deps.get_authenticated_principal)
    monkeypatch.setattr(settings, "jwt_secret_key", secrets.token_urlsafe(48))
    try:
        yield TestClient(app)
    finally:
        _teardown(engine, old_session, old_ocr)


@pytest.mark.parametrize("change", ["no_sub", "no_exp", "no_iat", "no_type", "wrong_type", "expired", "future_iat",
    "int_sub", "empty_sub", "space_sub", "list_sub", "bool_exp", "string_exp", "null_exp", "bool_iat", "string_iat",
    "list_iat", "nan_exp", "wrong_key", "unknown_user", "malformed"])
def test_invalid_access_tokens_are_401(protected_client, change):
    now = int(time.time())
    payload = dict(sub=USER_ID, exp=now + 300, iat=now, type="access")
    if change.startswith("no_"): payload.pop(change[3:])
    else:
        values = {"wrong_type":("type","refresh"), "expired":("exp",now-1), "future_iat":("iat",now+300),
            "int_sub":("sub",123), "empty_sub":("sub",""), "space_sub":("sub"," " + USER_ID),
            "list_sub":("sub",[USER_ID]), "bool_exp":("exp",True), "string_exp":("exp",str(now+300)),
            "null_exp":("exp",None), "bool_iat":("iat",True), "string_iat":("iat",str(now)),
            "list_iat":("iat",[]), "nan_exp":("exp",float("nan")), "unknown_user":("sub","missing-user")}
        if change in values:
            name, value = values[change]
            payload[name] = value
    key = "ambient-legacy-dev-secret" if change == "wrong_key" else settings.jwt_secret_key
    token = "invalid" if change == "malformed" else jwt.encode(payload,key,algorithm="HS256")
    response = protected_client.get("/api/v1/auth/me", headers={"Authorization":"Bearer " + token})
    assert response.status_code == 401, response.text


def test_valid_issued_token_roundtrips(protected_client):
    token = create_access_token(USER_ID)
    decoded = decode_access_token(token)
    assert decoded["sub"] == USER_ID and decoded["type"] == "access"
    assert type(decoded["iat"]) is int and type(decoded["exp"]) is int
    assert protected_client.get("/api/v1/auth/me", headers={"Authorization":"Bearer " + token}).status_code == 200


def test_startup_rechecks_secret_before_catalogue(monkeypatch):
    import app.main as main
    monkeypatch.setattr(settings,"jwt_secret_key","")
    with patch("app.services.secure_runtime.validate_catalogue_mode") as catalogue:
        with pytest.raises(SecureUnavailable): main.on_startup()
        catalogue.assert_not_called()
