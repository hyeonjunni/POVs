"""B2 immutable attempt publication: no live cloud or production data."""
import hashlib
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api import deps
from app.api.routes import uploads
from app.core.config import settings
from app.main import app
from app.models.family import FamilyMember
from app.models.upload import UploadFile
from app.models.user import User
from app.services import account_cleanup, media_loader
from app.processing.contracts import ProviderError
from test_processing_api import _setup, _teardown, USER_ID, ROOM_ID, RECORD_ID


@pytest.fixture
def case(tmp_path, monkeypatch):
    engine, tracker, old_sl, old_ocr = _setup()
    monkeypatch.setattr(uploads,"SessionLocal",tracker)
    monkeypatch.setattr(uploads,"LOCAL_MEDIA_ROOT",tmp_path)
    monkeypatch.setattr(account_cleanup,"LOCAL_MEDIA_ROOT",tmp_path)
    monkeypatch.setattr(uploads,"get_gcs_client",lambda: None)
    app.dependency_overrides[deps.get_current_user] = lambda: User(id=USER_ID)
    try:
        yield TestClient(app),tracker,tmp_path
    finally:
        _teardown(engine,old_sl,old_ocr)


def post(client, data):
    return client.post(f"/api/v1/uploads/{RECORD_ID}/binary",files={"file":("original.png",data,"image/png")})


def test_rejected_replacement_never_changes_previous_original(case, monkeypatch):
    client,tracker,root = case
    first = post(client,uploads.ONE_PIXEL_PNG+b"FIRST")
    assert first.status_code == 200
    with tracker.session_maker() as db:
        old = db.get(UploadFile,first.json()["file_id"])
        old_id,old_path,old_uploader = old.id,old.storage_path,old.file_uploader_user_id
    original = (root/old_path).read_bytes()
    original_hash = hashlib.sha256(original).digest()
    real_store = uploads.store_media_file
    attempts=[]
    def revoked_store(*args,**kwargs):
        assert tracker.open_count == 0
        location = real_store(*args,**kwargs)
        attempts.append(location)
        with tracker.session_maker() as db:
            db.execute(update(FamilyMember).where(FamilyMember.room_id==ROOM_ID, FamilyMember.user_id==USER_ID).values(is_active=False))
            db.commit()
        return location
    monkeypatch.setattr(uploads,"store_media_file",revoked_store)
    response = post(client,uploads.ONE_PIXEL_PNG+b"UNAUTHORIZED")
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "RESOURCE_NOT_AVAILABLE"}}
    assert (root/old_path).read_bytes() == original
    assert hashlib.sha256((root/old_path).read_bytes()).digest() == original_hash
    assert attempts[0][1] != old_path and not (root/attempts[0][1]).exists()
    with tracker.session_maker() as db:
        old=db.get(UploadFile,old_id)
        assert old.storage_path==old_path and old.file_uploader_user_id==old_uploader
        assert db.scalar(select(UploadFile.id).where(UploadFile.storage_path==attempts[0][1])) is None


def test_successful_replacements_have_unique_readable_paths(case):
    client,tracker,root=case
    paths=[]
    for content in (uploads.ONE_PIXEL_PNG+b"first",uploads.ONE_PIXEL_PNG+b"second"):
        response=post(client,content)
        assert response.status_code==200
        with tracker.session_maker() as db:
            file=db.get(UploadFile,response.json()["file_id"])
            paths.append(file.storage_path)
            attempt=Path(file.storage_path).stem.removeprefix(RECORD_ID+"__")
            assert str(UUID(attempt))==attempt
            assert media_loader.load_media_bytes(file.storage_bucket,file.storage_path,record_id=RECORD_ID,max_bytes=100)==content
    assert paths[0]!=paths[1]
    assert not (root/paths[0]).exists()
    assert (root/paths[1]).read_bytes()==uploads.ONE_PIXEL_PNG+b"second"


def test_local_collision_is_exclusive_and_preserves_existing_bytes(case):
    _,_,root=case
    name=f"{RECORD_ID}__{uuid4()}.png"
    (root/name).write_bytes(b"trusted")
    with pytest.raises(FileExistsError): uploads.store_media_file_locally(b"evil",name)
    assert (root/name).read_bytes()==b"trusted"


def test_gcs_client_is_unused_by_local_only_storage(case,monkeypatch):
    _,_,root=case
    client=MagicMock()
    factory=MagicMock(return_value=client)
    monkeypatch.setattr(uploads,"get_gcs_client",factory)
    bucket,path=uploads.store_media_file(b"first",RECORD_ID+".png","image/png")
    assert bucket=="local-media"
    assert (root/path).read_bytes()==b"first"
    factory.assert_not_called()
    client.bucket.assert_not_called()


@pytest.mark.parametrize("suffix", ["not-a-uuid",str(uuid4()).upper(),str(uuid4())+"__extra"])
def test_new_locator_parser_rejects_malformed_attempt(case,suffix):
    with pytest.raises(ProviderError):
        media_loader.resolve_trusted_locator("local-media",f"{RECORD_ID}__{suffix}.png",record_id=RECORD_ID)


def test_old_and_new_locators_share_read_and_cleanup_validation(case):
    _,tracker,root=case
    record_id = "unreferenced-b2"
    for name in (record_id+".png",f"{record_id}__{uuid4()}.png"):
        (root/name).write_bytes(b"original")
        assert media_loader.load_media_bytes("local-media",name,record_id=record_id,max_bytes=100)==b"original"
        with tracker.session_maker() as db:
            intents=account_cleanup.write_unreferenced_deletion_intents(db,[(record_id,"local-media",name)])
            db.commit()
        assert account_cleanup.drive_deletion_intents(tracker,own_intent_ids=intents)==0
        assert not (root/name).exists()


def test_commit_failure_cleans_only_new_attempt(case,monkeypatch):
    client,tracker,root=case
    first=post(client,uploads.ONE_PIXEL_PNG+b"trusted")
    assert first.status_code==200
    with tracker.session_maker() as db:
        old=db.get(UploadFile,first.json()["file_id"])
        old_path=old.storage_path
    calls=0
    def factory():
        nonlocal calls
        db=tracker()
        calls+=1
        if calls==1:
            def fail(): raise RuntimeError("synthetic publication failed")
            db.commit=fail
        return db
    monkeypatch.setattr(uploads,"SessionLocal",factory)
    response=post(TestClient(app,raise_server_exceptions=False),uploads.ONE_PIXEL_PNG+b"unpublished")
    assert response.status_code==500
    assert (root/old_path).read_bytes()==uploads.ONE_PIXEL_PNG+b"trusted"
    assert sorted(p.name for p in root.iterdir())==[old_path]
    with tracker.session_maker() as db:
        assert db.get(UploadFile,first.json()["file_id"]).storage_path==old_path


def test_gcs_locator_accepts_only_bound_record_attempt(case,monkeypatch):
    monkeypatch.setattr(settings,"gcs_bucket_name","synthetic-bucket")
    name=f"{RECORD_ID}__{uuid4()}.png"
    with pytest.raises(ProviderError) as historical:
        media_loader.resolve_trusted_locator(
            "synthetic-bucket", "uploads/" + name, record_id=RECORD_ID
        )
    assert historical.value.code == "historical_storage_unsupported"
    for bucket,path in (("other-bucket","uploads/"+name),("synthetic-bucket","uploads/../"+name),
        ("synthetic-bucket","uploads/other__"+str(uuid4())+".png")):
        with pytest.raises(ProviderError):
            media_loader.resolve_trusted_locator(bucket,path,record_id=RECORD_ID)


def test_local_flush_failure_removes_only_created_attempt(case,monkeypatch):
    _,_,root=case
    trusted=root/"trusted.png"
    trusted.write_bytes(b"trusted")
    target=root/"new-attempt.png"
    real_open=Path.open
    class BrokenClose:
        def __init__(self,stream): self.stream=stream
        def __enter__(self): return self.stream
        def __exit__(self,*args):
            self.stream.close()
            raise OSError("synthetic flush failed")
    def open_file(path,*args,**kwargs):
        stream=real_open(path,*args,**kwargs)
        return BrokenClose(stream) if path==target else stream
    monkeypatch.setattr(Path,"open",open_file)
    with pytest.raises(OSError): uploads.store_media_file_locally(b"partial",target.name)
    assert trusted.read_bytes()==b"trusted"
    assert not target.exists()
