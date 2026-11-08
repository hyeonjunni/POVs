import pytest
from app.api import deps
from app.api.routes import uploads
from app.main import app
from app.models.family import FamilyMember
from app.models.user import User
from app.services import retrieval
from sqlalchemy import text
from test_native_text_legacy import approve_and_index
from test_native_text_legacy import native_legacy as _native_legacy
from test_processing_api import OTHER_USER_ID, RECORD_ID, ROOM_ID
from test_upload_immutable_publication import case as _case

case = _case
native_legacy = _native_legacy


def join_viewer(tracker, monkeypatch):
    with tracker.session_maker() as db:
        db.add(FamilyMember(id="m2-viewer", room_id=ROOM_ID,
            user_id=OTHER_USER_ID, role="admin"))
        db.commit()
    monkeypatch.setitem(app.dependency_overrides, deps.get_current_user,
        lambda: User(id=OTHER_USER_ID))
    monkeypatch.setitem(app.dependency_overrides, deps.get_authenticated_principal,
        lambda: deps.AuthenticatedPrincipal(user_id=OTHER_USER_ID))


@pytest.mark.parametrize("visibility,status", [("FAMILY", 403), ("PRIVATE", 404)])
def test_room_admin_cannot_replace_another_owners_binary(case, monkeypatch, visibility, status):
    client, tracker, _ = case
    with tracker.session_maker() as db:
        db.execute(text("UPDATE records SET visibility=:v WHERE id=:id"),
            {"v": visibility, "id": RECORD_ID})
        db.commit()
    join_viewer(tracker, monkeypatch)
    monkeypatch.setattr(uploads, "store_media_file",
        lambda *a, **k: pytest.fail("Unauthorized replacement reached storage"))
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary",
        files={"file": ("changed.png", uploads.ONE_PIXEL_PNG, "image/png")})
    assert response.status_code == status, response.text
    if status == 403:
        assert response.json() == {"detail": {"code": "ACTION_NOT_ALLOWED"}}


def test_binary_publication_rechecks_lifecycle_after_storage(case, monkeypatch):
    client, tracker, root = case
    with tracker.session_maker() as db:
        before = db.scalar(text("SELECT count(*) FROM upload_files WHERE upload_id=:id"), {"id": RECORD_ID})
    original = uploads.store_media_file
    stored = []

    def delete_during_storage(*args, **kwargs):
        location = original(*args, **kwargs)
        stored.append(location)
        with tracker.session_maker() as db:
            db.execute(text("UPDATE records SET lifecycle_state='SOFT_DELETED' WHERE id=:id"), {"id": RECORD_ID})
            db.commit()
        return location

    monkeypatch.setattr(uploads, "store_media_file", delete_during_storage)
    response = client.post(f"/api/v1/uploads/{RECORD_ID}/binary",
        files={"file": ("changed.png", uploads.ONE_PIXEL_PNG, "image/png")})
    assert stored
    assert response.status_code == 404, response.text
    assert not (root / stored[0][1]).exists()
    with tracker.session_maker() as db:
        after = db.scalar(text("SELECT count(*) FROM upload_files WHERE upload_id=:id"), {"id": RECORD_ID})
    assert after == before


def test_selected_private_record_and_absent_record_are_indistinguishable(native_legacy, monkeypatch):
    client, tracker, rid, jid = native_legacy
    approve_and_index(client, rid, jid)
    with tracker.session_maker() as db:
        db.execute(text("UPDATE records SET visibility='PRIVATE' WHERE id=:id"), {"id": rid})
        db.commit()
    join_viewer(tracker, monkeypatch)
    replies = [client.post("/api/v1/ai/chat-demo", json={
        "room_id": ROOM_ID, "model_id": "exaone-45-33b-dgx", "persona_id": "father-calm",
        "query": "아버지 검진 몇 시?", "selected_record_id": selected,
    }) for selected in (rid, "m2-absent-record")]
    assert [response.status_code for response in replies] == [404, 404]
    assert replies[0].json() == replies[1].json()


def test_search_rechecks_visibility_after_ranking(native_legacy, monkeypatch):
    client, tracker, rid, jid = native_legacy
    approve_and_index(client, rid, jid)
    join_viewer(tracker, monkeypatch)
    original = retrieval.rank_bm25

    def revoke_after_ranking(*args, **kwargs):
        hits = original(*args, **kwargs)
        assert hits
        with tracker.session_maker() as db:
            db.execute(text("UPDATE records SET visibility='PRIVATE', authorization_epoch=authorization_epoch+1 WHERE id=:id"), {"id": rid})
            db.commit()
        return hits

    monkeypatch.setattr(retrieval, "rank_bm25", revoke_after_ranking)
    response = client.post(f"/api/v1/families/{ROOM_ID}/retrieval/search",
        json={"query": "아버지 검진", "arm": "bm25", "k": 5})
    assert response.status_code == 409, response.text
    assert rid not in response.text and "오후 2시" not in response.text
