from app.api.routes import uploads
from app.models import FamilyRoom
from app.services import account_cleanup
from test_upload_immutable_publication import case, post
from test_processing_api import ROOM_ID


def test_room_deleted_before_publication_returns_not_found(case, monkeypatch):
    client, tracker, root = case
    real_store = uploads.store_media_file
    def deleted_room(*args, **kwargs):
        locator = real_store(*args, **kwargs)
        with tracker.session_maker() as db:
            account_cleanup.delete_family_room_with_related_data(db, db.get(FamilyRoom, ROOM_ID))
            db.commit()
        return locator
    monkeypatch.setattr(uploads, "store_media_file", deleted_room)
    from fastapi.testclient import TestClient
    from app.main import app
    response = post(TestClient(app, raise_server_exceptions=False), uploads.ONE_PIXEL_PNG)
    assert response.status_code == 404
    assert not list(root.glob("*.png"))
