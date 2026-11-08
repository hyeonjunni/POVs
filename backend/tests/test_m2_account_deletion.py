from collections.abc import Iterator
from uuid import uuid4

import pytest
from app.api.routes import uploads
from app.core import database
from app.core.database import Base
from app.main import app
from app.services.record_policy import require_record_access
from conftest import fk_enforced_engine
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def legacy_client(monkeypatch) -> Iterator[tuple[TestClient, sessionmaker]]:
    engine = fk_enforced_engine()
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setattr(uploads, "SessionLocal", sessions)

    def get_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[database.get_db] = get_db
    try:
        with TestClient(app) as client:
            yield client, sessions
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def signup(client: TestClient, username: str) -> tuple[str, dict[str, str]]:
    response = client.post("/api/v1/auth/signup", json={
        "username": username, "name": username, "email": f"{username}@example.com",
        "password": "Correct horse battery 42!",
    })
    assert response.status_code == 200, response.text
    return response.json()["user"]["user_id"], {"Authorization": "Bearer " + response.json()["access_token"]}


def test_room_delete_revokes_access_without_purging_member_records(legacy_client) -> None:
    client, sessions = legacy_client
    owner, owner_auth = signup(client, "room_owner")
    member, member_auth = signup(client, "room_member")
    room_response = client.post("/api/v1/families", headers=owner_auth, json={"name": "Family"})
    assert room_response.status_code == 200, room_response.text
    room = room_response.json()
    joined = client.post("/api/v1/families/join", headers=member_auth, json={
        "invite_code": room["invite_code"], "related_to_user_id": owner,
        "relation_to_related_user": "child",
    })
    assert joined.status_code == 200, joined.text
    created = client.post("/api/v1/records", headers={**member_auth, "Idempotency-Key": str(uuid4())}, json={
        "room_id": room["room_id"], "type": "text", "title": "Private source",
    })
    assert created.status_code == 200, created.text
    record_id = created.json()["record_id"]
    response = client.delete(f"/api/v1/families/{room['room_id']}", headers=owner_auth)
    assert response.json() == {"deleted": True, "room_id": room["room_id"], "media_cleanup_failed_count": 0}
    assert client.get("/api/v1/families", headers=member_auth).json() == []
    assert client.get(f"/api/v1/families/{room['room_id']}", headers=member_auth).status_code == 404
    assert client.get(f"/api/v1/families/invite/{room['invite_code']}", headers=member_auth).status_code == 404
    assert client.get(f"/api/v1/records/{record_id}", headers=member_auth).status_code == 404
    with sessions() as db:
        record = require_record_access(db, member, record_id, purpose="manage")
        assert record["lifecycle_state"] == "ACTIVE"
        assert record["purge_approved_at"] is None
        assert db.scalar(text("SELECT COUNT(*) FROM deletion_intents")) == 0
        assert db.scalar(text("SELECT COUNT(*) FROM uploads WHERE id=:id"), {"id": record_id}) == 1


def test_account_delete_revokes_tokens_but_retains_record_provenance(legacy_client) -> None:
    client, sessions = legacy_client
    user, auth = signup(client, "account_owner")
    room = client.post("/api/v1/families", headers=auth, json={"name": "Family"}).json()
    created = client.post("/api/v1/records", headers={**auth, "Idempotency-Key": str(uuid4())}, json={
        "room_id": room["room_id"], "type": "text", "title": "Retained source",
    })
    assert created.status_code == 200, created.text
    record_id = created.json()["record_id"]
    response = client.delete("/api/v1/auth/me", headers=auth)
    assert response.json() == {"deleted": True, "user_id": user, "media_cleanup_failed_count": 0}
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401
    with sessions() as db:
        assert db.scalar(text("SELECT COUNT(*) FROM auth_identities WHERE user_id=:user"), {"user": user}) == 0
        assert db.scalar(text("SELECT COUNT(*) FROM uploads WHERE id=:id AND uploader_user_id=:user"), {"id": record_id, "user": user}) == 1
        assert db.scalar(text("SELECT COUNT(*) FROM records WHERE id=:id"), {"id": record_id}) == 1
        assert db.scalar(text("SELECT COUNT(*) FROM deletion_intents")) == 0
    replacement = client.post("/api/v1/auth/signup", json={
        "username": "account_owner", "name": "Replacement", "email": "account_owner@example.com",
        "password": "Correct horse battery 42!",
    })
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["user"]["user_id"] != user
