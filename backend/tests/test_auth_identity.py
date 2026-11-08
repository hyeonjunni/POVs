"""Auth identity cutover tests (FOUNDATION_IDENTITY_MIGRATION_DECISION.md D1/D2).

Uses an in-memory SQLite database per test via `app.dependency_overrides`, the
same pattern already used by backend/scripts/e2e_gate_demo.py and
backend/scripts/check_family_answer_ux.py.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from app.core.database import Base, get_db
from app.core.security import decode_access_token
from app.main import app
from app.models.auth_identity import AuthIdentity
from app.models.user import User

EXPECTED_SESSION_KEYS = {"access_token", "token_type", "user"}
EXPECTED_USER_KEYS = {
    "user_id", "username", "email", "name", "profile_image",
    "age", "gender", "phone", "profile_chunk",
}


def test_secure_verified_google_claims_bind_ids_provider_and_exact_subject():
    from app.models.auth_identity import authenticated_identity_matches
    uid, aid = str(uuid.uuid4()), str(uuid.uuid4())
    payload = dict(user_id=uid, identity_id=aid, provider="google", normalization_version=1,
                   normalized_identifier="verified-synthetic-subject", credential_hash=None)
    expected = dict(user_id=uid, identity_id=aid, provider="google", normalized_identifier="verified-synthetic-subject")
    assert authenticated_identity_matches(payload, **expected)
    for change in ({"user_id": str(uuid.uuid4())}, {"identity_id": str(uuid.uuid4())}, {"provider": "local"},
                   {"normalization_version": True}, {"normalized_identifier": "different-subject"},
                   {"normalized_identifier": "synthetic@example.com"}):
        assert not authenticated_identity_matches(payload | change, **expected)


def _client_with_fresh_db():
    engine = fk_enforced_engine("sqlite://")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), engine, TestSession


def _reset_overrides():
    app.dependency_overrides.clear()


def _signup(client: TestClient, *, username="alice_test", email="alice@acme-corp.com", password="correct horse battery"):
    return client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": password, "name": "Alice", "email": email},
    )


def test_signup_creates_one_local_identity():
    client, engine, TestSession = _client_with_fresh_db()
    try:
        response = _signup(client)
        assert response.status_code == 200, response.text
        user_id = response.json()["user"]["user_id"]

        with TestSession() as db:
            identities = db.scalars(select(AuthIdentity).where(AuthIdentity.user_id == user_id)).all()
            assert len(identities) == 1
            identity = identities[0]
            assert identity.provider == "local"
            assert identity.provider_subject == "alice_test"
            assert identity.credential_hash is not None
            assert identity.provider_email == "alice@acme-corp.com"

            user = db.get(User, user_id)
            assert user.google_sub is None
    finally:
        _reset_overrides()
        engine.dispose()


def test_login_reads_hash_from_identity_and_jwt_sub_is_user_id():
    client, engine, _TestSession = _client_with_fresh_db()
    try:
        signup = _signup(client)
        user_id = signup.json()["user"]["user_id"]

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "alice_test", "password": "correct horse battery"},
        )
        assert login.status_code == 200, login.text
        token = login.json()["access_token"]
        payload = decode_access_token(token)
        assert payload["sub"] == user_id

        wrong_password = client.post(
            "/api/v1/auth/login",
            json={"username": "alice_test", "password": "wrong password here"},
        )
        assert wrong_password.status_code == 401
    finally:
        _reset_overrides()
        engine.dispose()


def test_duplicate_username_is_rejected():
    client, engine, _TestSession = _client_with_fresh_db()
    try:
        first = _signup(client, username="dupe_user", email="one@acme-corp.com")
        assert first.status_code == 200

        second = _signup(client, username="dupe_user", email="two@acme-corp.com")
        assert second.status_code == 409
        assert second.json()["detail"] == "Duplicate username"
    finally:
        _reset_overrides()
        engine.dispose()


def test_matching_provider_email_does_not_link_identities():
    """D2 authority limit: a matching provider-asserted email links nothing."""
    client, engine, TestSession = _client_with_fresh_db()
    try:
        signup = _signup(client, username="user_one", email="shared@acme-corp.com")
        assert signup.status_code == 200
        user_one_id = signup.json()["user"]["user_id"]

        with TestSession() as db:
            user_two = User(
                id=str(uuid.uuid4()),
                google_sub="google-sub-distinct",
                username=None,
                password_hash=None,
                email="user-two@acme-corp.com",
                name="User Two",
            )
            db.add(user_two)
            db.flush()
            db.add(
                AuthIdentity(
                    id=str(uuid.uuid4()),
                    user_id=user_two.id,
                    provider="google",
                    provider_subject="google-sub-distinct",
                    credential_hash=None,
                    provider_email="shared@acme-corp.com",
                )
            )
            db.commit()

            matching_email_identities = db.scalars(
                select(AuthIdentity).where(AuthIdentity.provider_email == "shared@acme-corp.com")
            ).all()
            # Two distinct user_id rows may share a provider_email with no
            # constraint violation — provider_email authorizes nothing (D2).
            assert {row.user_id for row in matching_email_identities} == {user_one_id, user_two.id}
    finally:
        _reset_overrides()
        engine.dispose()


def test_legacy_columns_stay_compatible_after_cutover():
    client, engine, _TestSession = _client_with_fresh_db()
    try:
        signup = _signup(client)
        token = signup.json()["access_token"]

        me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        body = me.json()
        assert body["username"] == "alice_test"
        assert body["email"] == "alice@acme-corp.com"
    finally:
        _reset_overrides()
        engine.dispose()


def test_account_cleanup_deletes_identities():
    client, engine, TestSession = _client_with_fresh_db()
    try:
        signup = _signup(client)
        user_id = signup.json()["user"]["user_id"]
        token = signup.json()["access_token"]

        delete_response = client.delete("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert delete_response.status_code == 200

        with TestSession() as db:
            assert db.get(User, user_id).username is None
            remaining_identities = db.scalars(
                select(AuthIdentity).where(AuthIdentity.user_id == user_id)
            ).all()
            assert remaining_identities == []
    finally:
        _reset_overrides()
        engine.dispose()


def test_auth_response_contract_unchanged_by_identity_cutover():
    """Public auth JSON shape is unchanged by the identity cutover (compatibility window)."""
    client, engine, _TestSession = _client_with_fresh_db()
    try:
        signup = _signup(client)
        assert signup.status_code == 200
        signup_body = signup.json()
        assert set(signup_body.keys()) == EXPECTED_SESSION_KEYS
        assert set(signup_body["user"].keys()) == EXPECTED_USER_KEYS
        assert signup_body["token_type"] == "bearer"

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "alice_test", "password": "correct horse battery"},
        )
        assert login.status_code == 200
        login_body = login.json()
        assert set(login_body.keys()) == EXPECTED_SESSION_KEYS
        # Only the (nondeterministic) token differs between signup and login.
        assert login_body["user"] == signup_body["user"]
    finally:
        _reset_overrides()
        engine.dispose()
